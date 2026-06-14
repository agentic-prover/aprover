"""``BmcConfigAgent`` — one tool-using "BMC configuration agent".

This merges the two existing single-LLM-call configurators into a single
tool-augmented agent that READS THE REAL CODE before deciding:

  * ``FlagSelector`` (``bmc_agent/flag_selector.py``) — per-function CBMC
    flag selection plus per-function unwind / timeout overrides.
  * ``InliningAdvisor`` (``bmc_agent/inlining_advisor.py``) — inline-vs-stub
    promotions for callees the mechanical rule left as stubs.

The two originals each made ONE flat LLM call over a truncated function
body (1500 / 2000 chars). They never saw the FULL callee bodies, the real
array sizes / ``#define`` bounds, or struct layouts. This agent gets the
same bounded in-process tool loop the triage agent uses, so it can:

  * ``lookup_function(name)`` — read any callee's FULL body (no truncation)
    before deciding inline-vs-stub, and read array-size / loop-bound
    structure before picking an unwind override.
  * ``grep_corpus(pattern, k)`` — find real array sizes, ``#define`` bounds,
    and loop limits across the corpus.
  * ``lookup_struct(tag)`` — struct field details / sizes.
  * ``find_more_callers(name, k)`` — reach callers when needed.

It emits, per function, BOTH outputs at once:
  (a) the per-function ``FlagSelection`` (flag bits + unwind/timeout), and
  (b) an ``inline_overrides`` map of stub-candidate callee -> ``InlineDecision``
      (only promotions matter; default is STUB).

Design principle (unchanged from both originals): agents propose, CBMC
disposes. Do not over-enable flags (noise hides real bugs). Inline default
is STUB; promote only when clearly safe.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.flag_selector import (
    FlagSelection,
    _MAX_UNWIND_OVERRIDE,
    _MIN_TIMEOUT_OVERRIDE,
    _MAX_TIMEOUT_OVERRIDE,
)
from bmc_agent.inlining_advisor import InlineDecision
from bmc_agent.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo, ParsedCFile
    from bmc_agent.spec import Spec

logger = get_logger("bmc_config_agent")


# ---------- merged output ---------------------------------------------------


@dataclass
class BmcConfig:
    """Combined per-function BMC configuration produced by the agent.

    ``flags`` carries the CBMC flag bits + per-function unwind/timeout
    overrides (same semantics as ``FlagSelector``'s ``FlagSelection``).
    ``inline_overrides`` maps a stub-candidate callee name to its
    inline-or-stub decision (only promotions to inline are load-bearing;
    everything else stays STUB by default).
    """

    flags: "FlagSelection" = field(default_factory=FlagSelection)
    inline_overrides: dict[str, "InlineDecision"] = field(default_factory=dict)
    reasoning: str = ""

    def promotions(self) -> dict[str, "InlineDecision"]:
        """The subset of inline_overrides that actually promote to INLINE."""
        return {n: d for n, d in self.inline_overrides.items() if d.inline}


# Safe default: nothing enabled, no promotions. Used when the agent fails
# or produces unparseable output (fail-safe spirit of both originals).
def _default_config(reasoning: str = "default (bmc config skipped)") -> BmcConfig:
    return BmcConfig(
        flags=FlagSelection(reasoning=reasoning),
        inline_overrides={},
        reasoning=reasoning,
    )


# ---------- merged system prompt -------------------------------------------

_FLAG_GUIDANCE = """\
You select per-function CBMC verification configuration. Only enable a flag \
when it is semantically meaningful for THIS function — enabling flags \
everywhere creates noise that hides real bugs.

FLAG 1: --unsigned-overflow-check (unsigned_overflow_check)
Enable when the function multiplies size/count/length values, computes an \
allocation size feeding malloc/calloc/realloc/mmap, or does arithmetic on \
lengths/sizes from network packets, filesystem data, hardware registers, or \
user input. Do NOT enable for plain loop counters or provably-bounded index \
increments.

FLAG 2: --signed-overflow-check (signed_overflow_check)
Enable when the function does signed arithmetic on external-source values \
(packet fields, file offsets, ioctl parameters) where wrap-around would be \
exploitable, or computes array offsets / buffer positions using signed \
integers derived from untrusted input. Do NOT enable for simple loop \
counters or comparisons with no downstream security consequence.

FLAG 3: --conversion-check (conversion_check)
Enable when the function explicitly casts a wider integer type to a narrower \
one (uint32->uint16, int64->int32, long->int) on external-source values, or \
truncates packet length fields / register values / filesystem sizes. Do NOT \
enable when all casts are between same-width types or involve only internal \
constants.

FLAG 4: --pointer-overflow-check (pointer_overflow_check)
Enable when the function computes buffer addresses via pointer arithmetic \
with externally-controlled offsets (base + offset, ptr + count*stride), or \
walks memory using pointer increments where the step/count comes from \
external data. Do NOT enable for simple array iteration with provably-bounded \
indices.

FLAG 5: --undefined-shift-check (undefined_shift_check)
Enable when the function uses bit-shift operators on external-source values \
where the shift count could be negative, zero, or >= operand width; combines \
fields via shift-then-OR for packed binary formats with attacker-controlled \
bytes; or shifts signed integers left. Do NOT enable when shifts are by \
constant amounts known to be in [0, width-1] or the operand is provably from \
a small constrained domain.

FLAG 6: per-function unwind override (unwind_override, integer or null)
Override the global default ONLY when you can read a CONCRETE loop bound from \
the function body or signature. `for (i = 0; i < N; i++)` where N is a \
parameter/struct-field/constant -> propose unwind = max(N + 2, default); if N \
is unbounded user input return null. `for (i = 0; i < ARRAY_SIZE; i++)` with \
ARRAY_SIZE a small fixed constant -> propose ARRAY_SIZE + 1. Nested loops -> \
the SMALLEST unwind covering the largest loop (state-space cost is \
multiplicative). `while (1)` / unbounded -> null. No loops -> null. Cap at \
64. USE lookup_function / grep_corpus to read the REAL array size or #define \
bound rather than guessing.

FLAG 7: per-function timeout override (timeout_override, seconds or null)
Override ONLY when the function's shape gives a CONCRETE reason to expect \
very different runtime cost. Trivial getter/predicate -> null. Large parser / \
state machine (>200 LoC, nested loops) -> higher timeout (300-600s). Very \
wide call graph (many stubbed callees) -> higher timeout. High unwind \
override (>16) -> scale timeout up proportionally. Cap at 600. Return null \
when the global default is appropriate (most cases).
"""

_INLINE_GUIDANCE = """\
You also decide, for the listed STUB CANDIDATES, whether to INLINE the \
callee's real implementation into the CBMC harness or keep it STUBbed with a \
nondet contract.

Context: the mechanical rule (file-local static, <=30 LoC, no loops, no \
malloc, no recursion) already marked these callees as STUB. Reconsider each. \
Promote a callee to INLINE when:
  (a) it is a small predicate / getter / accessor whose body trivially \
constrains the return — the stub would return arbitrary nondet and trip \
"stub disconnect" FPs (e.g. a tag getter, a single-bit predicate, a \
struct-field accessor);
  (b) it is a 30-80 LoC helper with no loops, no allocations, and no \
recursion — the LoC bound was tripped but the body is still analytically \
simple;
  (c) the body is essentially one switch / chain of comparisons over input \
values, with deterministic return.

DO NOT inline when the body has loops (unwind cost multiplies), does \
allocation / file I/O / any side effect, calls other functions that would \
themselves need inlining (transitive cost), has any recursion (depth \
explosion), or is genuinely complex (>80 LoC, multiple state machines, \
nested control flow) — let the stub absorb it.

Hard rule: if you are unsure, emit "inline": false. The default is STUB. The \
LLM-write-the-harness path failed historically; LLM-pick-a-bit on a \
mechanical scaffold is what we trust.
"""

_TOOLS_GUIDANCE = """\
TOOLS (you have a bounded budget — use it before deciding):
  * lookup_function(name) — fetch any function's FULL signature + body. Use \
this to read each STUB CANDIDATE callee's COMPLETE body (not a truncation) \
before voting inline-vs-stub, and to read the function-under-test's loop \
structure for the unwind override.
  * grep_corpus(pattern, k) — regex search across the corpus. Use this to \
find the REAL array sizes, `#define` bounds, and loop limits (e.g. grep for \
the array's declaration or the macro definition) instead of guessing the \
unwind override.
  * lookup_struct(tag) — struct field list / sizes.
  * find_more_callers(name, k) — discover callers when a precondition or \
size depends on the caller.

Procedure: (1) read the function-under-test's loops and array indexing; \
grep_corpus / lookup_struct to bound them, then pick the flag bits + unwind / \
timeout. (2) For each stub candidate, lookup_function its full body and apply \
the inline-vs-stub rules above. THEN answer.

Remember: agents propose, CBMC disposes. Do not over-enable flags (noise \
hides real bugs). Inline default is STUB; promote only when clearly safe \
(small pure predicate/getter, or <=80 LoC with no loop / no alloc / no \
recursion).
"""

_OUTPUT_GUIDANCE = """\
Respond with ONLY ONE valid JSON object — no markdown, no extra text:
{
  "unsigned_overflow_check": true | false,
  "signed_overflow_check": true | false,
  "conversion_check": true | false,
  "pointer_overflow_check": true | false,
  "undefined_shift_check": true | false,
  "unwind_override": <integer 2-64> | null,
  "timeout_override": <integer 30-600> | null,
  "inline": {
    "<callee_name>": {"inline": true | false, "reason": "<one short sentence>"},
    ...
  },
  "reasoning": "<one concise sentence covering enabled flags + unwind + timeout + any promotions>"
}
"""

_SYSTEM_PROMPT = (
    "You are a formal-verification configuration expert for CBMC bounded "
    "model checking. For one C function you produce BOTH the per-function "
    "CBMC flag/unwind/timeout selection AND the inline-vs-stub decisions for "
    "its stubbed callees, after READING THE REAL CODE via tools.\n\n"
    + _FLAG_GUIDANCE
    + "\n"
    + _INLINE_GUIDANCE
    + "\n"
    + _TOOLS_GUIDANCE
    + "\n"
    + _OUTPUT_GUIDANCE
)


# ---------- the agent ------------------------------------------------------


class BmcConfigAgent(BaseAgent[BmcConfig]):
    """Tool-augmented BMC configuration agent.

    Replaces the two single-LLM-call configurators (``FlagSelector`` and
    ``InliningAdvisor``) with one agent that reads full callee bodies, real
    array sizes, loop bounds and struct layouts via tools before deciding.

    Uses ``name = "cbmc_driver"`` (the existing routing role both originals
    used) so per-role env-var routing (BMC_AGENT_LLM_CBMC_DRIVER_*) applies.
    """

    name = "cbmc_driver"
    system_prompt = _SYSTEM_PROMPT

    #: Bounded tool-use loop. Reading full callee bodies + grepping for
    #: array sizes is the heavy work; keep it tighter than triage.
    max_iterations_param: int = 8
    max_tool_calls_param: int = 10
    max_tokens_per_turn_param: int = 2048

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        *,
        parsed_file: "ParsedCFile",
        corpus_paths: "list[Path]",
        all_specs: "Optional[dict[str, Spec]]" = None,
    ) -> None:
        # Per-instance state for SpecToolContext (same shape as
        # TriageToolsAgent). The agent walks the parsed source + corpus to
        # read full callee bodies and find real array/loop bounds.
        self.parsed_file = parsed_file
        self.corpus_paths = list(corpus_paths)
        self.all_specs = dict(all_specs or {})
        super().__init__(config, llm)
        self._last_tool_use_result = None

    def _llm_call_kwargs(self) -> dict:
        return {}  # tool-use loop manages its own per-turn budget

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------

    def build_prompt(
        self,
        *,
        func: "FunctionInfo",
        global_unwind: int,
        global_timeout: int,
        stub_candidates: "list[str]",
        **_: Any,
    ) -> str:
        """Render the function under test + global defaults + stub-candidate
        callee names. The FULL body is included (the tool loop reads more on
        demand, but the body is given up front, NOT truncated).
        """
        sig = func.signature
        params = ", ".join(
            f"{pt} {pn}".strip() for pt, pn in sig.parameters
        ) or "void"
        signature_str = f"{sig.return_type} {sig.name}({params})"
        body = func.body or "(body unavailable)"

        cand = list(stub_candidates or [])
        if cand:
            candidates_block = "\n".join(f"  - {n}" for n in cand)
        else:
            candidates_block = "  (none — emit an empty \"inline\" object)"

        return (
            f"FUNCTION: {func.name}\n"
            f"SIGNATURE: {signature_str}\n"
            f"GLOBAL UNWIND DEFAULT: {global_unwind}\n"
            f"GLOBAL TIMEOUT DEFAULT: {global_timeout}s\n"
            f"FULL BODY:\n{body}\n\n"
            f"STUB CANDIDATE CALLEES (mechanical rule said STUB; reconsider "
            f"each for promotion — use lookup_function to read each one's "
            f"FULL body):\n{candidates_block}\n\n"
            f"Use the tools to read the real callee bodies, array sizes, and "
            f"loop bounds, then respond with the single JSON object."
        )

    # ------------------------------------------------------------------
    # Tool loop (mirrors TriageToolsAgent._call_llm)
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str) -> tuple[str, Optional[str]]:
        # Under --agentic, run on the Claude Code agent instead of bmc's
        # in-process tool loop.
        if self._agent_runs_on_claude_code():
            return super()._call_llm(prompt)
        from bmc_agent.llm import LLMError
        from bmc_agent.spec_gen_tools import (
            SpecToolContext, build_spec_gen_tools,
        )
        ctx = SpecToolContext(
            parsed=self.parsed_file,
            corpus_paths=self.corpus_paths,
            all_specs_so_far=self.all_specs,
            boundary_detector=None,
        )
        tools, handlers = build_spec_gen_tools(ctx)
        try:
            result = self.llm.complete_with_tools(
                system_prompt=self.system_prompt,
                user_prompt=prompt,
                tools=tools,
                tool_handlers=handlers,
                max_iterations=self.max_iterations_param,
                max_tool_calls=self.max_tool_calls_param,
                max_tokens_per_turn=self.max_tokens_per_turn_param,
                role=self.name,
            )
        except LLMError as exc:
            return "", f"LLMError: {exc!r}"
        except Exception as exc:
            return "", f"unexpected: {exc!r}"
        self._last_tool_use_result = result
        if result.error:
            return result.text or "", f"tool_use_terminated: {result.error}"
        return result.text or "", None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse(self, response: str) -> Optional[BmcConfig]:
        """Parse the merged JSON object into a ``BmcConfig``.

        Fail-safe spirit of both originals:
          * empty / whitespace-only response -> return None (so
            ``BaseAgent.run`` records an error and may retry);
          * response had text but no parseable JSON object -> return a SAFE
            DEFAULT (flags all-off, no promotions), never raise.
        """
        if not response or not response.strip():
            return None

        data = _extract_json_object(response)
        if data is None:
            logger.warning(
                "bmc config: could not parse JSON object — using safe default"
            )
            return _default_config("default (unparseable bmc-config response)")

        flags = _parse_flags(data)
        inline_overrides = _parse_inline(data)
        reasoning = str(data.get("reasoning", "")).strip()
        if reasoning:
            flags.reasoning = flags.reasoning or reasoning

        cfg = BmcConfig(
            flags=flags,
            inline_overrides=inline_overrides,
            reasoning=reasoning,
        )
        promos = cfg.promotions()
        if promos:
            for name, d in promos.items():
                logger.info(
                    "bmc config: promoted '%s' to INLINE: %s", name, d.reason,
                )
        if flags.any_enabled():
            logger.debug(
                "bmc config flags: %s — %s",
                ", ".join(flags.enabled_flags()), reasoning,
            )
        return cfg

    # ------------------------------------------------------------------
    # Convenience driver (what the pipeline calls)
    # ------------------------------------------------------------------

    def select_all(
        self,
        funcs: "dict[str, FunctionInfo]",
        parsed_file: "Optional[ParsedCFile]" = None,
        stub_candidates_by_func: "Optional[dict[str, list[str]]]" = None,
    ) -> "dict[str, BmcConfig]":
        """Produce a ``BmcConfig`` for every function.

        Sequential (the tool loop is heavy per function). Falls back to a
        safe default per function on any failure so the pipeline is never
        blocked. Logs like ``FlagSelector.select_all`` does.

        ``parsed_file`` overrides the instance ``parsed_file`` for this call
        when given (the pipeline may pass the active file explicitly).
        ``stub_candidates_by_func`` maps function name -> the callees the
        mechanical rule left as stubs (reconsider for promotion); missing /
        None means no candidates for that function.
        """
        if not funcs:
            return {}

        if parsed_file is not None:
            self.parsed_file = parsed_file

        cand_map = stub_candidates_by_func or {}
        global_unwind = int(getattr(self.config, "cbmc_unwind", 4))
        global_timeout = int(getattr(self.config, "cbmc_timeout", 120))

        results: dict[str, BmcConfig] = {}
        for name, func in funcs.items():
            stub_candidates = list(cand_map.get(name, []))
            try:
                res = self.run(
                    func=func,
                    global_unwind=global_unwind,
                    global_timeout=global_timeout,
                    stub_candidates=stub_candidates,
                )
            except Exception as exc:
                logger.warning(
                    "bmc config failed for '%s': %s — using defaults",
                    name, exc,
                )
                results[name] = _default_config()
                continue
            if res.ok and res.output is not None:
                results[name] = res.output
            else:
                logger.warning(
                    "bmc config for '%s' produced no output (%s) — using "
                    "defaults", name, res.error,
                )
                results[name] = _default_config()

        enabled = [n for n, c in results.items() if c.flags.any_enabled()]
        promoted = [n for n, c in results.items() if c.promotions()]
        if enabled:
            logger.info(
                "bmc config: extra flags enabled for %d/%d function(s): %s",
                len(enabled), len(funcs), ", ".join(sorted(enabled)),
            )
        if promoted:
            logger.info(
                "bmc config: inline promotions in %d/%d function(s): %s",
                len(promoted), len(funcs), ", ".join(sorted(promoted)),
            )
        if not enabled and not promoted:
            logger.debug("bmc config: no extra flags / no promotions selected")
        return results


# ---------- parsing helpers ------------------------------------------------


def _extract_json_object(raw: str) -> "Optional[dict]":
    """Strip code fences, find the outermost ``{...}``, parse it.

    Combines the robust-parse approaches of both originals (fence strip +
    outermost-brace search + balanced-brace fallback). Returns a ``dict`` or
    None when no JSON object can be recovered.
    """
    if not raw:
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = re.sub(r"```", "", cleaned).strip()

    # Fast path: the whole cleaned text is the object.
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    blob = m.group(0)
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    # Balanced-brace fallback: find the first complete top-level object.
    depth = 0
    start = blob.find("{")
    if start < 0:
        return None
    for i, ch in enumerate(blob[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(blob[start : i + 1])
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _parse_flags(data: dict) -> FlagSelection:
    """Build a ``FlagSelection`` from the parsed object, applying the SAME
    clamps as ``flag_selector._parse_response``."""
    uoc = bool(data.get("unsigned_overflow_check", False))
    soc = bool(data.get("signed_overflow_check", False))
    cc = bool(data.get("conversion_check", False))
    poc = bool(data.get("pointer_overflow_check", False))
    usc = bool(data.get("undefined_shift_check", False))

    raw_unwind = data.get("unwind_override")
    unwind_override: Optional[int] = None
    if isinstance(raw_unwind, bool):
        pass
    elif isinstance(raw_unwind, int) and raw_unwind >= 2:
        unwind_override = min(raw_unwind, _MAX_UNWIND_OVERRIDE)
    elif isinstance(raw_unwind, str):
        try:
            n = int(raw_unwind.strip())
            if n >= 2:
                unwind_override = min(n, _MAX_UNWIND_OVERRIDE)
        except ValueError:
            unwind_override = None

    raw_timeout = data.get("timeout_override")
    timeout_override: Optional[int] = None
    if isinstance(raw_timeout, bool):
        pass
    elif isinstance(raw_timeout, int) and raw_timeout >= _MIN_TIMEOUT_OVERRIDE:
        timeout_override = min(raw_timeout, _MAX_TIMEOUT_OVERRIDE)
    elif isinstance(raw_timeout, str):
        try:
            n = int(raw_timeout.strip())
            if n >= _MIN_TIMEOUT_OVERRIDE:
                timeout_override = min(n, _MAX_TIMEOUT_OVERRIDE)
        except ValueError:
            timeout_override = None

    reasoning = str(data.get("reasoning", "")).strip()

    return FlagSelection(
        unsigned_overflow_check=uoc,
        signed_overflow_check=soc,
        conversion_check=cc,
        pointer_overflow_check=poc,
        undefined_shift_check=usc,
        unwind_override=unwind_override,
        timeout_override=timeout_override,
        reasoning=reasoning,
    )


def _parse_inline(data: dict) -> dict[str, InlineDecision]:
    """Build the inline-overrides map from the ``inline`` sub-object.

    Default is STUB (inline=False) — only well-formed entries that name a
    callee are recorded. Malformed entries are skipped. Mirrors the
    promote-only / default-stub / if-unsure-false semantics of the original
    inlining advisor.
    """
    block = data.get("inline")
    out: dict[str, InlineDecision] = {}
    if not isinstance(block, dict):
        return out
    for name, payload in block.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(payload, dict):
            continue
        inline = bool(payload.get("inline", False))
        reason = str(payload.get("reason", "")).strip()
        out[name] = InlineDecision(inline=inline, reason=reason)
    return out
