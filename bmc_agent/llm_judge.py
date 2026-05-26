"""
LLM-as-judge: simple, single-loop verdict on CBMC counterexamples.

Replaces the classifier → realism → refinement → feedback-loop multi-stage
pipeline with one tool-using LLM call. The LLM gets the function source,
the CBMC failure, callers, callees, struct definitions, and a set of tools
to fetch more context on demand (read_function, read_struct, read_file_region,
grep_corpus, list_callers, read_cbmc_trace_verbose, rerun_cbmc).

The LLM decides REALISTIC / UNREALISTIC / UNCERTAIN by calling ``final_verdict``.

No pre-LLM pattern filters, no learned-constraint feedback loops. The
heavy pipeline can quietly kill real bugs at each stage; this design
keeps the judgment in one place where a senior auditor (the LLM) can
weigh all the evidence.

Public entry point: ``JudgeAgent(config, parsed_files, corpus_root, harness_source).judge(...)``.
"""

from __future__ import annotations

import json
import logging

from bmc_agent.logger import get_logger
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from bmc_agent.config import Config
from bmc_agent.parser import FunctionInfo, ParsedCFile

# Use the project logger so judge-turn INFO + warnings appear in sweep.log.
# Bare logging.getLogger() bypasses the file/console handlers configured by
# bmc_agent.logger.get_logger and silently drops everything.
logger = get_logger("llm_judge")


MAX_TURNS = 8                  # cap on tool-use rounds. Empirically GPT-5
                               # finishes the primary judgment in 6-10 turns,
                               # gpt-5-mini converges faster. Final-turn
                               # forcing ensures we always get a structured
                               # verdict even if the cap hits.
MAX_TOOL_RESULT_CHARS = 12000  # truncate individual tool results
MAX_TRACE_STEPS_INITIAL = 80   # how many trace steps land in initial context

# After the LLM votes UNREALISTIC, we re-prompt it to hunt for OTHER bugs in
# the same function / nearby code (the "adjacent bugs" search). That second
# loop has its own turn cap and is allowed to do its own tool investigation.
MAX_ADJACENT_SEARCH_TURNS = 5


SYSTEM_PROMPT = """\
You are a senior security auditor reviewing a CBMC verification finding for a C library.

You will be given:
- The function under verification, with its full source
- The CBMC harness (which inputs were left nondeterministic; what was assumed)
- The CBMC failing property + variable-assignment witness + abbreviated trace
- The function's callers and callees in this corpus

Your job: decide whether the CBMC counterexample is a REAL EXPLOITABLE BUG
(REALISTIC) or a harness/verification artifact (UNREALISTIC), or UNCERTAIN.

You have tools to fetch ANY additional context you need before deciding:
- read_function(name), read_struct(name), read_file_region(path, start, end)
- grep_corpus(pattern), list_callers(name), read_cbmc_trace_verbose()
- rerun_cbmc(unwind, extra_flags) — costly; use sparingly

Use the tools. Do not guess about what callers do or what a callee returns;
look. When confident, call ``final_verdict``.

Bias toward verifying reachability through real public-API callers. A bug
that requires a witness no caller can produce is an artifact, even if CBMC
flagged it. A bug that can be triggered by malformed attacker input flowing
through a public API is realistic, even if the witness uses extreme values.

Be specific. In your reasoning, name the caller chain, the attacker-controlled
input, and the exact line where the violation occurs. If you can name an
adjacent bug nearby (same function, same file), include it in ``adjacent_bugs``.

**For each entry in ``adjacent_bugs``, the ``location`` field MUST include
the function name** in one of these forms (downstream BMC re-verification
extracts the function from this string):
  - ``foo_bar (archive_acl.c:123)``
  - ``archive_acl.c:123-145 (function foo_bar)``
  - ``archive_acl.c:123-145 (foo_bar)``
A bare line range like ``archive_acl.c:123-145`` is not actionable for the
re-verifier — always name the function.

CRITICAL: Your final answer MUST be a call to the ``final_verdict`` function
with structured arguments (verdict, confidence, reasoning, attacker_scenario,
adjacent_bugs). Do NOT respond with prose like "Verdict: unrealistic" — call
the function. Prose responses are discarded.
"""


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_function",
            "description": (
                "Return the full source body of a function in the project. "
                "Use to inspect callers, callees, or any related function."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_struct",
            "description": (
                "Return the definition of a struct or typedef (fields + types). "
                "Use to understand what an opaque pointer points to."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_region",
            "description": (
                "Return a line range from a file (corpus .c/.h, README, etc.). "
                "Inclusive line numbers. End-start capped at 400 lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_corpus",
            "description": (
                "grep -nE across all .c and .h in the corpus root. "
                "Returns up to max_matches lines as `path:line: text`. "
                "Use to locate macros, callback registrations, magic constants."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "max_matches": {"type": "integer", "default": 40},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_callers",
            "description": (
                "Return the list of in-corpus functions that call the named "
                "function, plus the list of in-corpus functions that take its "
                "address (vtable / callback registration). Use to assess "
                "whether a CBMC state is reachable from any real caller."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_cbmc_trace_verbose",
            "description": (
                "Return the full step-by-step CBMC trace. The initial context "
                "showed an abbreviated version (first ~80 steps). Use only when "
                "the abbreviated trace is insufficient."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rerun_cbmc",
            "description": (
                "Re-run CBMC on the current harness with a different unwind "
                "bound and/or extra flags (e.g. --pointer-overflow-check). "
                "Returns the new verdict + first 3 counterexamples. Costs a "
                "full CBMC invocation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "unwind": {"type": "integer"},
                    "extra_flags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["unwind"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "final_verdict",
            "description": (
                "Emit your final verdict for this CBMC counterexample and "
                "terminate. Call this when you have enough information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["realistic", "unrealistic", "uncertain"],
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "reasoning": {
                        "type": "string",
                        "description": (
                            "Why this verdict. Be specific: name the caller "
                            "chain, the attacker-controlled input, the line of "
                            "the violation. 2-6 sentences."
                        ),
                    },
                    "attacker_scenario": {
                        "type": "string",
                        "description": (
                            "If REALISTIC: how an attacker would trigger the "
                            "bug via the public API. Empty otherwise."
                        ),
                    },
                    "adjacent_bugs": {
                        "type": "array",
                        "description": (
                            "Other bugs you noticed in the same function or "
                            "nearby. Each: {location, bug_type, "
                            "attacker_scenario, confidence}."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": ["verdict", "confidence", "reasoning"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass
class JudgeResult:
    verdict: str          # realistic | unrealistic | uncertain
    confidence: str       # high | medium | low
    reasoning: str
    attacker_scenario: str = ""
    adjacent_bugs: list = None
    turns_used: int = 0
    tools_invoked: list = None  # [(name, args_compact), ...]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "attacker_scenario": self.attacker_scenario or "",
            "adjacent_bugs": self.adjacent_bugs or [],
            "turns_used": self.turns_used,
            "tools_invoked": self.tools_invoked or [],
        }


class JudgeAgent:
    """Single-LLM-call (with tool use) verdict on a CBMC counterexample.

    Drop-in replacement for the classifier → realism → refinement →
    feedback-loop chain.
    """

    def __init__(
        self,
        config: Config,
        parsed_files: dict[str, ParsedCFile],
        corpus_root: Path,
        harness_source: str = "",
        cbmc_rerun_callback=None,  # signature: (unwind, extra_flags) -> dict
    ) -> None:
        self.config = config
        self.parsed_files = dict(parsed_files)
        self.corpus_root = Path(corpus_root)
        self.harness_source = harness_source
        self._cbmc_rerun = cbmc_rerun_callback

        # Aggregated indices across all parsed files in the corpus
        self._fn_to_file: dict[str, str] = {}
        self._struct_to_file: dict[str, str] = {}
        for path, p in self.parsed_files.items():
            for name in p.functions:
                self._fn_to_file.setdefault(name, path)
            for name in p.struct_definitions:
                self._struct_to_file.setdefault(name, path)

        # Per-judgment scratch state
        self._full_trace: list[str] = []
        self._tools_invoked: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def judge(self, func: FunctionInfo, counterexample, cbmc_result=None) -> JudgeResult:
        self._full_trace = list(getattr(counterexample, "trace", None) or [])
        self._tools_invoked = []

        initial_ctx = self._build_initial_context(func, counterexample, cbmc_result)
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": initial_ctx},
        ]

        result = self._run_tool_loop(
            messages,
            max_turns=MAX_TURNS,
            label="primary",
        )

        # After UNREALISTIC, hunt for OTHER bugs in this function or nearby
        # — the LLM inspects all the information it has plus anything it
        # fetches via tools, and emits structured bug hypotheses. Each one
        # is then a candidate for downstream validation (another CBMC run
        # against a tightened harness, or a focused judge call per hypothesis).
        if result.verdict == "unrealistic":
            adjacent = self._search_adjacent_bugs(messages, primary_result=result)
            if adjacent:
                primary = result.adjacent_bugs if isinstance(result.adjacent_bugs, list) else []
                extra = adjacent if isinstance(adjacent, list) else []
                result.adjacent_bugs = primary + extra
        return result

    # ------------------------------------------------------------------
    # Inner tool-use loop (reused for primary verdict + adjacent search)
    # ------------------------------------------------------------------

    def _run_tool_loop(
        self,
        messages: list[dict],
        max_turns: int,
        label: str,
    ) -> JudgeResult:
        for turn in range(max_turns):
            remaining = max_turns - turn
            # On the LAST turn, force the LLM to emit final_verdict — without
            # this, large reasoning models tend to keep investigating until
            # they run out of turns and we get "uncertain by timeout".
            if remaining <= 1:
                tool_choice = {
                    "type": "function",
                    "function": {"name": "final_verdict"},
                }
            else:
                tool_choice = "auto"

            try:
                resp = self._call_llm_with_tools(messages, tool_choice=tool_choice)
            except Exception as exc:
                logger.warning("[%s] judge LLM call failed at turn %d: %s",
                               label, turn + 1, exc)
                return JudgeResult(
                    verdict="uncertain",
                    confidence="low",
                    reasoning=f"LLM call failed at turn {turn + 1}: {exc}",
                    turns_used=turn + 1,
                    tools_invoked=list(self._tools_invoked),
                )

            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            tool_calls = msg.get("tool_calls") or []

            messages.append({
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": tool_calls or None,
            })

            if not tool_calls:
                # LLM returned prose instead of calling final_verdict (a
                # common reasoning-model failure mode). Try to recover by
                # parsing the prose for an obvious verdict line; on the
                # next iteration we'll force tool_choice=final_verdict if
                # we haven't already.
                content_text = (msg.get("content") or "").strip()
                parsed = _parse_verdict_from_prose(content_text)
                if parsed is not None:
                    parsed["turns_used"] = turn + 1
                    parsed["tools_invoked"] = list(self._tools_invoked)
                    return JudgeResult(**parsed)
                # No structured verdict, no prose verdict — nudge it to
                # call the function and try once more.
                messages.append({
                    "role": "user",
                    "content": (
                        "You returned prose, not a tool call. Call "
                        "`final_verdict` now with verdict, confidence, "
                        "reasoning, attacker_scenario, adjacent_bugs."
                    ),
                })
                continue

            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                args_compact = json.dumps(args, separators=(",", ":"))[:120]
                self._tools_invoked.append((f"{label}:{name}", args_compact))

                if name == "final_verdict":
                    # Tool calls MUST be followed by a role=tool message
                    # before any subsequent assistant turn (OpenAI protocol
                    # — without this, a follow-up loop like the adjacent
                    # search rejects with "No tool output found"). Append
                    # a synthetic confirmation so messages[] stays valid.
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": "verdict_recorded",
                    })
                    raw_adj = args.get("adjacent_bugs")
                    adj_list = raw_adj if isinstance(raw_adj, list) else []
                    return JudgeResult(
                        verdict=str(args.get("verdict", "uncertain")).lower(),
                        confidence=str(args.get("confidence", "low")).lower(),
                        reasoning=str(args.get("reasoning", "")),
                        attacker_scenario=str(args.get("attacker_scenario", "")),
                        adjacent_bugs=adj_list,
                        turns_used=turn + 1,
                        tools_invoked=list(self._tools_invoked),
                    )

                result_text = self._dispatch_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": _truncate(result_text, MAX_TOOL_RESULT_CHARS),
                })

            # Soft hint when we're running low — the LLM sees this as a
            # tool-result-shaped message and tends to wrap up.
            if remaining <= 3:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[reminder] You have {remaining-1} tool turn(s) left "
                        f"before final_verdict is forced. If you're satisfied, "
                        f"call final_verdict now."
                    ),
                })

        return JudgeResult(
            verdict="uncertain",
            confidence="low",
            reasoning=f"max tool-use turns ({max_turns}) exceeded in {label} loop",
            turns_used=max_turns,
            tools_invoked=list(self._tools_invoked),
        )

    # ------------------------------------------------------------------
    # Adjacent-bug search (after UNREALISTIC verdict)
    # ------------------------------------------------------------------

    def _search_adjacent_bugs(
        self,
        messages: list[dict],
        primary_result: JudgeResult,
    ) -> list[dict]:
        """After a primary UNREALISTIC verdict, ask the LLM to hunt for other
        bugs in the same function or nearby. Returns a list of structured
        bug hypotheses (each: {location, bug_type, attacker_scenario,
        confidence, evidence}). Empty list on failure.
        """
        messages.append({
            "role": "user",
            "content": (
                "You judged the CBMC counterexample UNREALISTIC. Now use the "
                "tools to look harder for OTHER real bugs in this function "
                "and nearby code. The CBMC counterexample being an artifact "
                "does NOT mean the function is bug-free. Investigate:\n"
                "  - Edge cases the harness over-constrained away\n"
                "  - Bugs in callees that wouldn't surface in THIS CBMC run\n"
                "  - Caller misuses that you can spot from caller bodies\n"
                "  - Documented seed bugs / CVE-like patterns\n"
                "When you find concrete hypotheses, emit them via "
                "final_verdict with verdict='unrealistic' (carrying the "
                "primary verdict over) and the adjacent_bugs[] array "
                "populated. Each entry must include: location "
                "(file:line or function:line), bug_type, attacker_scenario "
                "(concrete trigger via public API), confidence "
                "(high|medium|low), and evidence (what tool output convinced you). "
                "If after investigation you find NO additional bugs, emit "
                "final_verdict with empty adjacent_bugs."
            ),
        })

        adj_result = self._run_tool_loop(
            messages,
            max_turns=MAX_ADJACENT_SEARCH_TURNS,
            label="adjacent",
        )
        return adj_result.adjacent_bugs if isinstance(adj_result.adjacent_bugs, list) else []

    # ------------------------------------------------------------------
    # Initial-context builder
    # ------------------------------------------------------------------

    def _build_initial_context(self, func: FunctionInfo, cex, cbmc_result) -> str:
        # Callers across the whole parsed corpus
        callers: list[str] = []
        for path, p in self.parsed_files.items():
            for caller, callees in p.call_graph.items():
                if func.name in callees:
                    callers.append(caller)
        addr_takers: set[str] = set()
        for path, p in self.parsed_files.items():
            addr_takers |= (p.address_taken_in or {}).get(func.name, set())

        callees = sorted(set(func.callees or set()))
        # Resolve which file each caller / callee lives in for navigation
        def _loc(n: str) -> str:
            f = self._fn_to_file.get(n)
            return f"{n}  ({Path(f).name})" if f else n

        # Witness pretty-print
        var_lines = []
        va = getattr(cex, "variable_assignments", None) or {}
        if isinstance(va, dict):
            for k, v in va.items():
                var_lines.append(f"  {k} = {v}")
        witness_block = "\n".join(var_lines) if var_lines else "(no variable assignments)"

        trace = self._full_trace[:MAX_TRACE_STEPS_INITIAL]
        trace_block = "\n".join(f"  {i+1:3d}. {s}" for i, s in enumerate(trace))
        if len(self._full_trace) > MAX_TRACE_STEPS_INITIAL:
            trace_block += f"\n  ... ({len(self._full_trace) - MAX_TRACE_STEPS_INITIAL} more steps; use read_cbmc_trace_verbose to see all)"

        body = func.body or "(body unavailable)"
        sig = self._format_sig(func)
        file_path = func.source_file or self._fn_to_file.get(func.name, "?")

        callers_block = (
            "\n".join("  " + _loc(c) for c in sorted(set(callers))) or "  (none in this corpus)"
        )
        addr_takers_block = (
            "\n".join("  " + _loc(c) for c in sorted(addr_takers))
            or "  (none — function address is not taken in this corpus)"
        )
        callees_block = (
            "\n".join("  " + _loc(c) for c in callees) or "  (none in this corpus)"
        )

        failing_property = getattr(cex, "failing_property", "?") or "?"
        description = getattr(cex, "description", "") or ""

        sections = [
            "=== Function under verification ===",
            f"File: {file_path}",
            f"Signature: {sig}",
            "",
            "```c",
            body,
            "```",
            "",
            "=== CBMC harness ===",
            "```c",
            self.harness_source or "(harness source unavailable)",
            "```",
            "",
            "=== CBMC verdict ===",
            f"verified: False",
            f"failing_property: {failing_property}",
            f"description: {description or '(none)'}",
            "",
            "=== Witness (variable assignments at failure) ===",
            witness_block,
            "",
            f"=== CBMC trace (first {MAX_TRACE_STEPS_INITIAL} of {len(self._full_trace)} steps) ===",
            trace_block,
            "",
            "=== Direct in-corpus callers ===",
            callers_block,
            "",
            "=== Functions that take the address of this function (vtable / callback) ===",
            addr_takers_block,
            "",
            "=== Direct in-corpus callees ===",
            callees_block,
            "",
            "=== Task ===",
            "Decide if this CBMC counterexample is a REAL exploitable bug or an "
            "artifact of the harness. Use tools to fetch any context you need "
            "(callers, callee bodies, struct defs, file regions, grep, rerun CBMC). "
            "Then call final_verdict.",
        ]
        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _dispatch_tool(self, name: str, args: dict) -> str:
        try:
            if name == "read_function":
                return self._tool_read_function(args.get("name", ""))
            if name == "read_struct":
                return self._tool_read_struct(args.get("name", ""))
            if name == "read_file_region":
                return self._tool_read_file_region(
                    args.get("path", ""),
                    int(args.get("start_line", 1)),
                    int(args.get("end_line", 1)),
                )
            if name == "grep_corpus":
                return self._tool_grep_corpus(
                    args.get("pattern", ""),
                    int(args.get("max_matches", 40)),
                )
            if name == "list_callers":
                return self._tool_list_callers(args.get("name", ""))
            if name == "read_cbmc_trace_verbose":
                return self._tool_full_trace()
            if name == "rerun_cbmc":
                return self._tool_rerun_cbmc(
                    int(args.get("unwind", self.config.cbmc_unwind)),
                    list(args.get("extra_flags") or []),
                )
            return f"ERROR: unknown tool '{name}'"
        except Exception as exc:
            logger.warning("tool '%s' raised: %s", name, exc)
            return f"ERROR: tool '{name}' raised: {exc}"

    def _tool_read_function(self, fname: str) -> str:
        if not fname:
            return "ERROR: name is required"
        path = self._fn_to_file.get(fname)
        if not path:
            return f"NOT_FOUND: no function named '{fname}' in the parsed corpus. " \
                   f"It may be a libc/system function or in a header not parsed."
        p = self.parsed_files[path]
        body = p.function_bodies.get(fname) or p.function_definitions.get(fname) or ""
        sig = p.functions.get(fname)
        sig_str = ""
        if sig is not None:
            params = ", ".join(f"{t} {n}" for t, n in (sig.parameters or [])) or "void"
            sig_str = f"{sig.return_type} {fname}({params})"
        return (
            f"// from {Path(path).name}\n"
            f"// signature: {sig_str}\n"
            f"{body}"
        )

    def _tool_read_struct(self, sname: str) -> str:
        if not sname:
            return "ERROR: name is required"
        path = self._struct_to_file.get(sname)
        if not path:
            return f"NOT_FOUND: no struct/typedef '{sname}' captured during parsing"
        fields = self.parsed_files[path].struct_definitions.get(sname) or []
        lines = [f"struct {sname} {{  // from {Path(path).name}"]
        for ftype, fname in fields:
            lines.append(f"    {ftype} {fname};")
        lines.append("};")
        return "\n".join(lines)

    def _tool_read_file_region(self, path: str, start_line: int, end_line: int) -> str:
        if not path:
            return "ERROR: path is required"
        end_line = min(end_line, start_line + 400)
        if start_line < 1:
            start_line = 1
        # Resolve relative paths within the corpus root
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.corpus_root / path
        if not candidate.is_file():
            return f"NOT_FOUND: file '{candidate}' does not exist"
        try:
            lines = candidate.read_text(errors="replace").splitlines()
        except Exception as exc:
            return f"ERROR: read failed: {exc}"
        slice_ = lines[start_line - 1:end_line]
        out = [f"// {candidate} lines {start_line}-{start_line + len(slice_) - 1}"]
        for i, line in enumerate(slice_, start=start_line):
            out.append(f"{i:5d}: {line}")
        return "\n".join(out)

    def _tool_grep_corpus(self, pattern: str, max_matches: int) -> str:
        if not pattern:
            return "ERROR: pattern is required"
        max_matches = min(max(max_matches, 1), 200)
        cmd = ["grep", "-nE", "-r",
               "--include=*.c", "--include=*.h",
               "-m", str(max_matches),
               pattern, str(self.corpus_root)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "ERROR: grep timed out (>30s) — pattern is probably too broad"
        if r.returncode > 1:
            return f"ERROR: grep returned {r.returncode}: {r.stderr[:300]}"
        out_lines = (r.stdout or "").splitlines()[:max_matches]
        if not out_lines:
            return f"NO_MATCH for pattern: {pattern}"
        # Strip the corpus_root prefix to make output more compact
        root_prefix = str(self.corpus_root).rstrip("/") + "/"
        out = [line.replace(root_prefix, "") for line in out_lines]
        return f"// grep -nE '{pattern}' in {self.corpus_root}\n" + "\n".join(out)

    def _tool_list_callers(self, fname: str) -> str:
        if not fname:
            return "ERROR: name is required"
        callers = []
        for path, p in self.parsed_files.items():
            for caller, callees in p.call_graph.items():
                if fname in callees:
                    callers.append((caller, Path(path).name))
        addr_takers = []
        for path, p in self.parsed_files.items():
            for n in (p.address_taken_in or {}).get(fname, set()):
                addr_takers.append((n, Path(path).name))
        lines = [f"Direct callers of '{fname}' in corpus ({len(callers)}):"]
        for caller, file_ in sorted(set(callers)):
            lines.append(f"  {caller}  ({file_})")
        if not callers:
            lines.append("  (none)")
        lines.append("")
        lines.append(f"Address takers (vtable / callback registrants) ({len(addr_takers)}):")
        for n, file_ in sorted(set(addr_takers)):
            lines.append(f"  {n}  ({file_})")
        if not addr_takers:
            lines.append("  (none)")
        return "\n".join(lines)

    def _tool_full_trace(self) -> str:
        if not self._full_trace:
            return "(no trace was supplied with this counterexample)"
        lines = [f"CBMC trace — {len(self._full_trace)} steps:"]
        for i, s in enumerate(self._full_trace):
            lines.append(f"  {i+1:4d}. {s}")
        return "\n".join(lines)

    def _tool_rerun_cbmc(self, unwind: int, extra_flags: list) -> str:
        if self._cbmc_rerun is None:
            return (
                "DISABLED: rerun_cbmc is not wired up in this run. "
                "The judge is operating without the ability to re-invoke CBMC."
            )
        try:
            res = self._cbmc_rerun(unwind, extra_flags)
        except Exception as exc:
            return f"ERROR: rerun_cbmc raised: {exc}"
        return json.dumps(res, indent=2)[:MAX_TOOL_RESULT_CHARS]

    # ------------------------------------------------------------------
    # OpenAI-compatible function-calling HTTP path
    # ------------------------------------------------------------------

    def _call_llm_with_tools(self, messages: list[dict], tool_choice="auto") -> dict:
        """Dispatch a tool-use turn. Picks Anthropic native Messages API
        when the configured provider is "anthropic" (sk-ant key against
        api.anthropic.com), otherwise an OpenAI-compatible
        /chat/completions endpoint (OpenRouter, K2 Think, etc.).

        Returns the response in OpenAI shape so the rest of the tool-use
        loop is provider-agnostic.
        """
        rs = self.config.role_settings("realism")
        # Resolve provider: explicit role > config > auto-detect on key.
        provider = (
            rs.get("provider")
            or getattr(self.config, "llm_provider", "")
            or self.config.resolved_provider()
        )
        if (provider or "").lower() == "anthropic":
            return self._call_anthropic_with_tools(
                messages, tool_choice=tool_choice, rs=rs,
            )

        # ---- OpenAI-compatible path (OpenRouter / K2 / OpenAI) ----
        api_key = rs.get("api_key") or self.config.resolved_api_key()
        base_url = rs.get("base_url") or self.config.llm_base_url or "https://openrouter.ai/api/v1"
        model = rs.get("model") or self.config.llm_model
        base = base_url.rstrip("/")
        if not base.endswith("/v1") and not base.endswith("/v1/"):
            if "/v1" not in base:
                base = base + "/v1"
        url = base.rstrip("/") + "/chat/completions"

        payload = {
            "model": model,
            "messages": messages,
            "tools": _TOOLS,
            "tool_choice": tool_choice,
            # GPT-5 burns reasoning tokens; give comfortable headroom.
            "max_tokens": 16384,
            "temperature": 0.2,
        }

        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise RuntimeError("httpx required for llm_judge") from exc

        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 600.0))
        timeout = httpx.Timeout(timeout_s, connect=15.0)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"judge LLM HTTP {resp.status_code}: {resp.text[:600]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"judge LLM non-JSON response: {resp.text[:600]}") from exc
        usage = data.get("usage") or {}
        logger.info(
            "judge LLM turn: prompt_tokens=%s completion_tokens=%s",
            usage.get("prompt_tokens"), usage.get("completion_tokens"),
        )
        return data

    # ------------------------------------------------------------------
    # Anthropic native tool-use (Messages API)
    # ------------------------------------------------------------------

    def _call_anthropic_with_tools(
        self, messages: list[dict], tool_choice, rs: dict,
    ) -> dict:
        """Translate OpenAI-shape messages/tools/tool_choice to Anthropic
        Messages API format, call client.messages.create, and translate
        the response back to OpenAI shape. Keeps the OpenAI-format
        messages history intact so the calling loop is unchanged.
        """
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "anthropic package required for the anthropic provider path"
            ) from exc

        api_key = rs.get("api_key") or self.config.resolved_api_key()
        base_url = rs.get("base_url") or self.config.llm_base_url or ""
        model = rs.get("model") or self.config.llm_model

        client_kwargs: dict = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = anthropic.Anthropic(**client_kwargs)

        # 1. Translate the OpenAI tools schema → Anthropic tools schema.
        #    Add cache_control on the LAST tool so the whole tools array is
        #    in the cached prefix (Anthropic caches everything up to and
        #    including the last cache_control marker).
        a_tools = []
        for t in _TOOLS:
            fn = (t or {}).get("function") or {}
            a_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {
                    "type": "object", "properties": {},
                },
            })
        if a_tools:
            a_tools[-1] = dict(a_tools[-1])  # don't mutate _TOOLS-derived dict
            a_tools[-1]["cache_control"] = {"type": "ephemeral"}

        # 2. Translate tool_choice.
        if tool_choice == "auto":
            a_tool_choice = {"type": "auto"}
        elif isinstance(tool_choice, dict):
            forced_name = ((tool_choice.get("function") or {}).get("name")
                           or tool_choice.get("name"))
            if forced_name:
                a_tool_choice = {"type": "tool", "name": forced_name}
            else:
                a_tool_choice = {"type": "auto"}
        else:
            a_tool_choice = {"type": "auto"}

        # 3. Translate messages.
        #    OpenAI: [{role: system|user|assistant|tool, content, tool_calls?, tool_call_id?}, ...]
        #    Anthropic: system is a top-level param; messages alternate user/assistant
        #    with content blocks. Tool calls → tool_use blocks. Tool results → user
        #    role with tool_result content blocks.
        system_text = ""
        a_messages: list[dict] = []
        # We need to fold consecutive tool-result messages into a single
        # user message with multiple tool_result blocks.
        pending_tool_results: list[dict] = []

        def flush_pending():
            if pending_tool_results:
                a_messages.append({
                    "role": "user",
                    "content": list(pending_tool_results),
                })
                pending_tool_results.clear()

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                # Anthropic uses system as a top-level param. If the
                # transcript has multiple system messages, concatenate.
                txt = msg.get("content") or ""
                if isinstance(txt, list):
                    txt = "".join(b.get("text", "") for b in txt if isinstance(b, dict))
                system_text = (system_text + ("\n\n" if system_text else "") + str(txt))
                continue

            if role == "tool":
                # Accumulate into the next user-role tool_result batch.
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id") or "",
                    "content": str(msg.get("content") or ""),
                })
                continue

            # Any non-tool message ends the pending tool_result batch.
            flush_pending()

            if role == "user":
                content_str = str(msg.get("content") or "")
                # Cache the FIRST user message (the initial context — function
                # source, witness, callers, etc. — ~3k tokens, stable across
                # all turns of the same judge call). Use list-of-blocks form
                # so we can attach cache_control. Later user messages stay as
                # plain strings since they're tool-result / nudges that
                # change each turn.
                first_user_so_far = not any(
                    m.get("role") == "user" and isinstance(m.get("content"), list)
                    and any(b.get("cache_control") for b in m["content"])
                    for m in a_messages
                )
                if first_user_so_far:
                    a_messages.append({
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": content_str,
                            "cache_control": {"type": "ephemeral"},
                        }],
                    })
                else:
                    a_messages.append({"role": "user", "content": content_str})
                continue

            if role == "assistant":
                blocks: list[dict] = []
                content = msg.get("content")
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in (msg.get("tool_calls") or []) or []:
                    fn = (tc.get("function") or {})
                    try:
                        tc_input = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        tc_input = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name", ""),
                        "input": tc_input,
                    })
                if not blocks:
                    # Empty assistant message would be rejected. Inject
                    # a stub so the protocol stays valid.
                    blocks.append({"type": "text", "text": "(continuing)"})
                a_messages.append({"role": "assistant", "content": blocks})
                continue

            # Unknown role — skip rather than crash.
        flush_pending()

        # Defensive: if first message after system is not user, prepend a
        # placeholder. Anthropic requires the first message be from user.
        if not a_messages or a_messages[0]["role"] != "user":
            a_messages.insert(0, {"role": "user", "content": "(begin)"})

        # 4. Call Anthropic Messages API.
        #    System prompt is cached too (~3k tokens of SYSTEM_PROMPT is
        #    re-used across every turn). Use the list-of-blocks form so we
        #    can attach cache_control.
        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 600.0))
        system_payload = [{
            "type": "text",
            "text": system_text or "You are a helpful assistant.",
            "cache_control": {"type": "ephemeral"},
        }]
        try:
            response = client.with_options(timeout=timeout_s).messages.create(
                model=model,
                system=system_payload,
                messages=a_messages,
                tools=a_tools,
                tool_choice=a_tool_choice,
                max_tokens=16384,
                temperature=0.2,
            )
        except anthropic.APIError as exc:
            raise RuntimeError(f"anthropic API error: {exc}") from exc

        # 5. Translate the response back to OpenAI shape.
        #    Anthropic response.content is a list of blocks: text|tool_use.
        out_text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in response.content or []:
            btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if btype == "text":
                txt = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
                out_text_parts.append(str(txt))
            elif btype == "tool_use":
                tu_id = getattr(block, "id", None) or (block.get("id") if isinstance(block, dict) else "")
                tu_name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else "")
                tu_input = getattr(block, "input", None)
                if tu_input is None and isinstance(block, dict):
                    tu_input = block.get("input", {})
                try:
                    args_json = json.dumps(tu_input or {})
                except (TypeError, ValueError):
                    args_json = "{}"
                tool_calls.append({
                    "id": tu_id,
                    "type": "function",
                    "function": {"name": tu_name, "arguments": args_json},
                })

        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            logger.info(
                "judge LLM turn (anthropic): input=%s output=%s "
                "cache_write=%s cache_hit=%s tool_calls=%d",
                in_tok, out_tok, cache_create, cache_read, len(tool_calls),
            )

        return {
            "choices": [{
                "message": {
                    "content": "".join(out_text_parts) or None,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": getattr(response, "stop_reason", None),
            }],
            "usage": {
                "prompt_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            },
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_sig(self, func: FunctionInfo) -> str:
        sig = func.signature
        if sig is None:
            return f"{func.name}(...)"
        params = ", ".join(f"{t} {n}" for t, n in (sig.parameters or [])) or "void"
        return f"{sig.return_type} {func.name}({params})"


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n... [truncated; original {len(text)} chars]"


_VERDICT_RE = re.compile(r"verdict\s*[:=]\s*[\"']?(realistic|unrealistic|uncertain)\b",
                         re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"confidence\s*[:=]\s*[\"']?(high|medium|low)\b",
                            re.IGNORECASE)


def _parse_verdict_from_prose(text: str) -> Optional[dict]:
    """Recover verdict/confidence/reasoning when the LLM emits prose instead
    of calling final_verdict. Returns None if no obvious verdict line is found.
    """
    if not text:
        return None
    m = _VERDICT_RE.search(text)
    if not m:
        return None
    verdict = m.group(1).lower()
    cm = _CONFIDENCE_RE.search(text)
    confidence = cm.group(1).lower() if cm else "low"
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasoning": text[:2000],
        "attacker_scenario": "",
        "adjacent_bugs": [],
    }
