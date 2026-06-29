"""Agentic CBMC harness generator: LLM writes the harness directly.

The deterministic ``HarnessGenerator`` emits a generic main() with nondet
inputs, which over-approximates the input space and triggers structural
FPs (5-byte buffers passed to APIs whose real callers pre-size buffers
via a sibling length function; opaque-external returns of UINT64_MAX-10
for wcslen; etc.). This module instead hands the LLM:

  * The function under verification (signature + body)
  * Each callee's body (in-corpus) or signature (external)
  * Direct callers in the corpus (so the LLM sees how inputs are
    constrained in practice)
  * Tools to fetch additional context on demand (read_function,
    read_struct, read_file_region, grep_corpus, list_callers,
    compile_check)

The LLM emits a complete C harness via ``emit_harness``. The harness is
compile-checked (CBMC parses it with ``--show-properties`` rather than
full verification, to validate syntax/semantics cheaply). On parse/compile
failure the error is fed back to the LLM for a retry, up to
``MAX_RETRIES`` rounds.

Public entry point::

    AgenticHarnessGen(config, parsed_files, corpus_root)
        .generate(func, all_funcs_global,
                  include_dirs=None, defines=None) -> str
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.config import Config
from bmc_agent.llm import LLMClient
from bmc_agent.llm_tool_loop import LLMToolClient
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, ParsedCFile

logger = get_logger("agentic_harness_gen")


MAX_TURNS = 24
MAX_RETRIES = 8
MAX_TOOL_RESULT_CHARS = 12000


SYSTEM_PROMPT = """\
You are writing a CBMC verification harness for a single C function in
a real-world library.

Your harness must:
  * Be self-contained, compilable C (cbmc-parsable)
  * Include the function-under-test by ``#include``-ing its .c file
    (the include path is provided in the context)
  * Define an ``int main(void)`` that calls the function with inputs
    representative of what REAL callers actually pass
  * Use ``__CPROVER_assume(...)`` to express realistic preconditions
    (NUL-terminated strings, allocated struct pointers, value ranges
    grounded in real caller behaviour)
  * Allocate buffers large enough to match how real callers size them.
    If a sibling length function (e.g. ``foo_text_len``) pre-computes
    the size, allocate at least that much. Do not pick small magic
    numbers like 5 when callers allocate based on input length.
  * For string-typed parameters (``char *`` / ``const char *``) used
    with strcpy/strlen/strcat/etc., always NUL-terminate the input.
  * For pointer-to-struct parameters that the function dereferences,
    allocate a backing struct and initialise the fields that the
    function reads. Use ``__CPROVER_assume`` for invariants the real
    callers enforce.
  * Decide per callee whether to stub or inline:
      - External (libc / system) callees: leave them as declared
        externs (CBMC will havoc their returns) UNLESS the havoc value
        is the bug source — in that case constrain the return with
        ``__CPROVER_assume`` (e.g., ``__CPROVER_assume(wcslen_ret <
        1024)``) using a wrapper if needed.
      - In-corpus callees: usually let them be inlined (CBMC sees the
        body through the ``#include``). If a callee is large/recursive
        and pulls in too much state, consider stubbing with a wrapper.
  * Keep the harness MINIMAL — no unrelated calls, no print statements.

CRITICAL — the precision principle:
  Your constraints must be **as loose as the loosest real caller chain**.
  Over-constraining (e.g. ``len < 16`` when callers can supply 1024)
  silently kills real bugs. Look at the actual callers and copy their
  weakest invariant — do not invent tighter ones.

CRITICAL — buffer-size correlation:
  If you allow a buffer's size and a write's length to vary independently,
  CBMC will pick size=1 with length=huge and find a trivial overflow that
  is impossible in real callers. When real callers compute size as a
  function of inputs (e.g., via a sibling ``foo_text_len`` that scans
  the same inputs to decide how many bytes are needed), your harness
  MUST express the same correlation. Options, in order of preference:
    1. Call the sibling length function at the top of main(), use its
       result as the malloc size. This is exact and matches reality.
    2. ``__CPROVER_assume(buffer_size >= computed_lower_bound)`` where
       the lower bound is expressed in terms of the other parameters
       (e.g., ``buffer_size >= prefix_len + name_len + TAG_MAX + 32``).
    3. Pick a fixed conservative size (e.g., ``buffer_size = 8192``)
       only if (1) and (2) are impractical — and constrain string
       lengths so total writes can't exceed it.
  Do NOT write ``__CPROVER_assume(size > 0 && size <= MAX)`` alone when
  the function performs writes proportional to OTHER parameters; that's
  the over-approximation that produces FPs.

CRITICAL — enum-like integer parameters:
  If you observe that real callers only pass a small set of values for
  an integer parameter (e.g., tag is one of ARCHIVE_ENTRY_ACL_USER /
  GROUP / MASK / OTHER / etc.; type is POSIX1E or NFS4), encode that:
  ``__CPROVER_assume(tag == 10001 || tag == 10002 || ... );`` Otherwise
  CBMC explores nonsense values (e.g., tag=272147) that produce
  artifact CExes.

You have tools to gather context BEFORE writing the harness:
  read_function(name)        — fetch a callee or caller body
  read_struct(name)          — fetch a struct definition
  read_file_region(...)      — read a source region
  grep_corpus(pattern)       — grep across the corpus
  list_callers(name)         — list in-corpus callers + address-takers
  compile_check(harness)     — cheap parse / preprocess check
                              (returns OK or compile errors)
  emit_harness(harness)      — FINAL ANSWER: submit the harness

Use the read/grep tools generously; the LLM tokens are cheap compared
to a wrong harness that wastes a CBMC run and yields a false positive.

When ready, call ``emit_harness`` with the complete harness text. If
``compile_check`` returns errors, fix them and resubmit. After the retry
budget is exhausted, your last submission is used regardless.
"""


SYSTEM_PROMPT_REFINE = """\
You are refining a CBMC verification harness.

The harness below was already run; CBMC found a counterexample, and an
LLM judge classified it as UNREALISTIC (or UNCERTAIN). You will see the
prior harness, the failing property, the witness values, the judge's
reasoning, and the same investigation tools (read_function, read_struct,
grep_corpus, list_callers).

Your job is to decide:

  A. Is the judge correct that the witness reflects a HARNESS GAP —
     a missing constraint, the wrong buffer size, an unconstrained
     callee return, a struct field that real callers always set?
     If so, rewrite the harness to close that gap WITHOUT
     over-constraining (do not eliminate input shapes that real
     callers could produce).

  B. Or is the judge over-claiming — the witness is genuinely
     reachable from real callers but the judge missed a path?
     In that case re-emit the prior harness UNCHANGED (call
     emit_harness with the original text). Document your reason
     in the rationale field.

DO NOT mechanically apply the judge's prose as a __CPROVER_assume.
The judge writes reasoning, not C. You must encode it correctly —
or decide it shouldn't be encoded at all.

Critical precision principle (unchanged from initial gen):
  Constraints must be as loose as the loosest real caller chain.
  A constraint that makes THIS witness impossible but also makes a
  documented real bug impossible is a regression — refuse it and
  re-emit the prior harness.

Critical efficiency principle — DO NOT write iterative libc stubs:
  When the judge says "CBMC's havoc'd extern X returned an absurd value"
  (e.g., wcslen returning UINT64-1, malloc returning a tiny size,
  read() returning more bytes than requested), the WRONG fix is to
  write an in-harness body for X that iterates (a loop scanning for NUL,
  a copying loop, etc.). Those stubs are correct C but blow CBMC's
  unwind/timeout budget. The RIGHT fix is one of:
    1. ``__CPROVER_assume(X_return < REASONABLE_BOUND);`` — directly
       constrain the havoc value to the range real callers see. For
       wcslen/strlen on a buffer of size N, use ``ret < N``.
    2. If you need a wrapper, write a constant-time one — e.g.,
       ``size_t my_wcslen(const wchar_t *s) {
            size_t n; __CPROVER_assume(n < 256); return n; }`` —
       no loop, just an assumed return value.
    3. Pre-set the buffer contents so the real (CBMC-modeled) libc
       call returns a deterministic value (e.g., place a NUL at
       index K so wcslen returns K).
  Loops inside in-harness stubs are the #1 cause of refinement
  timeouts. Avoid them.

Output protocol is the same as initial gen — call emit_harness with
the (possibly unchanged) harness text and a rationale explaining
what you changed and why (or why you kept it the same).
"""


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_function",
            "description": (
                "Return the full source body of a function in the project "
                "corpus. Use to inspect callees you're deciding whether to "
                "stub or inline, or callers you're learning constraints from."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
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
                "Use to understand what an opaque pointer points to and what "
                "fields you need to initialise."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
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
                "Return a line range from a file in the corpus. "
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
                "function, plus address-takers (vtable / callback). Use to "
                "find which real callers to consult for input constraints."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compile_check",
            "description": (
                "Cheap compile/parse check of a proposed harness. Runs CBMC "
                "with --show-properties (skips the costly verification). "
                "Returns OK or the first error message. Use before emit_harness "
                "to catch typos and obvious mistakes."
            ),
            "parameters": {
                "type": "object",
                "properties": {"harness": {"type": "string"}},
                "required": ["harness"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "emit_harness",
            "description": (
                "FINAL ANSWER. Submit the complete CBMC harness AND your "
                "selection of optional CBMC verification flags. It will be "
                "compile-checked; if it fails, you can retry. The harness "
                "must include the #include of the function's .c file, struct "
                "allocations, __CPROVER_assume preconditions, the call to "
                "the function-under-test, and return 0."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "harness": {"type": "string"},
                    "rationale": {
                        "type": "string",
                        "description": (
                            "1-3 sentence summary of the key design "
                            "decisions: which callees were stubbed vs "
                            "inlined, which inputs were constrained "
                            "and why, what real-caller invariants you "
                            "relied on."
                        ),
                    },
                    "cbmc_flags": {
                        "type": "object",
                        "description": (
                            "Per-function CBMC check selection. Baseline "
                            "checks (--bounds-check --pointer-check "
                            "--signed-overflow-check --div-by-zero-check) "
                            "are ALWAYS on. Set these only when the function "
                            "performs the relevant operation:\n"
                            "  unsigned_overflow_check: set true when the "
                            "function does size_t / unsigned arithmetic "
                            "(allocation size calc, length accumulation).\n"
                            "  conversion_check: set true when the function "
                            "casts narrowing integer types (int→short, "
                            "downcasting void*→typed*).\n"
                            "  pointer_overflow_check: set true when the "
                            "function does explicit pointer arithmetic "
                            "(p + n, p - q, &arr[i]) where i could be "
                            "attacker-influenced.\n"
                            "All three default to false — only enable a "
                            "check if the function actually does the "
                            "corresponding operation."
                        ),
                        "properties": {
                            "unsigned_overflow_check": {"type": "boolean"},
                            "conversion_check": {"type": "boolean"},
                            "pointer_overflow_check": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    },
                    "cbmc_budget": {
                        "type": "object",
                        "description": (
                            "Per-function CBMC budget. You wrote the harness, "
                            "so you know what loops and recursion it exercises.\n"
                            "  unwind: --unwind value. Default 4. Bump to 8-16 "
                            "    when the harness has loops bounded by an input "
                            "    parameter you constrained (e.g., name_len < 64 "
                            "    → unwind 64 would be safe; unwind 16 is usually "
                            "    enough to cover the loop). Bump to 32-64 only "
                            "    when you're hunting a deep-loop bug. Clamped to "
                            "    [1, 64].\n"
                            "  timeout_s: CBMC budget seconds. Default 60. Bump "
                            "    to 120-180 when the harness allocates large "
                            "    buffers OR uses unwind ≥ 16 OR exercises "
                            "    several callees. Bump to 300+ only for "
                            "    deeply recursive functions. Clamped to "
                            "    [10, 600].\n"
                            "If you omit cbmc_budget the runner uses the "
                            "command-line defaults."
                        ),
                        "properties": {
                            "unwind": {"type": "integer"},
                            "timeout_s": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["harness"],
                "additionalProperties": False,
            },
        },
    },
]


# --- Claude Code agent harness path -----------------------------------------

SYSTEM_PROMPT_CLAUDE_CODE = (
    "You are a CBMC harness engineer. You write a single self-contained C harness "
    "(int main(void)) that lets CBMC verify one function for MEMORY SAFETY. You "
    "have read-only tools (Read/Grep/Glob) and the project source on disk — use "
    "them to get the REAL struct/type/header definitions right. The function "
    "under test MUST be DEFINED in the harness (body pasted in, or its .c "
    "#include'd) — never a bare prototype, or CBMC analyses nothing and vacuously "
    "passes. You output ONLY a C harness in a fenced ```c block, nothing else."
)

_CLAUDE_CODE_HARNESS_PROMPT = """\
Write a complete, self-contained CBMC harness for the function below. The
project's deterministic harness generator FAILED to build a harness for it
(usually an opaque/incomplete type or a header it modelled wrong), so model the
types correctly yourself.

Tools: you may Read/Grep/Glob the project source under `{add_dir}` to find the
exact struct/typedef/header definitions this function and its parameters need.

CRITICAL — SOUNDNESS (this is the whole point; a harness that hides the bug is
worse than none):
  * THE FUNCTION-UNDER-TEST MUST HAVE ITS BODY IN THE HARNESS. Either paste the
    BODY shown below verbatim into the harness, OR `#include` its .c file. NEVER
    leave the function under test as a bare prototype / `extern` declaration:
    CBMC would then report "no body for function" and generate 0 verification
    conditions — a VACUOUS `VERIFICATION SUCCESSFUL` that checks nothing and
    silently passes the bug. (Forward-declaring with no body is THE classic way
    this harness goes wrong — do not do it for the function under test.)
  * Make every ATTACKER-CONTROLLED INPUT fully nondeterministic (nondet bytes /
    sizes / fields). Do NOT pin input data to specific values or narrow ranges.
  * Only __CPROVER_assume STRUCTURAL validity the function genuinely requires
    (a pointer is non-null, a backing buffer is N bytes). NEVER assume away a
    data value the function reads from its input — that is exactly where bugs
    live, and over-constraining produces a false `VERIFICATION SUCCESSFUL`.
  * Keep the harness small. The "forward-declare + nondet a small concrete
    stand-in" trick is ONLY for opaque PARAMETER TYPES or EXTERNAL callees whose
    havoc'd return is acceptable — NEVER for the function under test itself,
    which must always be defined (body present) so CBMC actually analyses it.
  * Define `int main(void)` and call the function on the symbolic inputs.

FUNCTION:
```c
{fn_signature}
```
BODY:
```c
{fn_body}
```

Prior CBMC compile error to fix:
{prior_error}

Output ONLY the harness as a single ```c ... ``` block.
"""


def _extract_c_code(text: str) -> str:
    """Pull the C harness out of a claude-code response (fenced ```c block if
    present, else the whole text)."""
    if not text:
        return ""
    m = re.search(r"```(?:c|cpp|cc)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


@dataclass
class HarnessResult:
    harness: str
    rationale: str = ""
    retries: int = 0
    turns_used: int = 0
    tools_invoked: list = None
    last_compile_error: str = ""
    # Optional per-function CBMC flag selection: subset of
    # {"unsigned_overflow_check", "conversion_check", "pointer_overflow_check"}.
    # Populated when emit_harness includes a cbmc_flags object. The agentic
    # generator picks these alongside writing the harness, replacing the
    # separate FlagSelector pass when --agentic-harness is on.
    cbmc_flags: Optional[dict] = None
    # Optional per-function CBMC budget: {"unwind": int, "timeout_s": int}.
    # Picked by the LLM informed by the harness it just wrote (loop bounds,
    # callee depth, buffer sizes). Clamped at the sanitize step.
    cbmc_budget: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "harness": self.harness,
            "rationale": self.rationale,
            "retries": self.retries,
            "turns_used": self.turns_used,
            "tools_invoked": self.tools_invoked or [],
            "last_compile_error": self.last_compile_error,
            "cbmc_flags": self.cbmc_flags or {},
            "cbmc_budget": self.cbmc_budget or {},
        }


class AgenticHarnessGen(BaseAgent[str]):
    """LLM-driven harness builder. One tool-using call per function.

    A ``BaseAgent`` so it routes its LLM via its own ``harness_gen`` role
    (instead of borrowing ``realism``) and is part of the agent inventory /
    ``--agentic`` routing. It is driven by ``generate()`` /
    ``generate_via_claude_code()`` — a multi-tool ``LLMToolClient`` loop — not
    ``BaseAgent.run()``, so ``build_prompt`` / ``parse`` are intentionally
    unused (they raise; harness extraction lives in ``_extract_c_code``).
    """

    name = "harness_gen"
    system_prompt = SYSTEM_PROMPT

    def __init__(
        self,
        config: Config,
        parsed_files: dict[str, ParsedCFile],
        corpus_root: Path,
    ) -> None:
        # This agent drives its own multi-tool LLMToolClient loop (self._llm),
        # not BaseAgent's single-shot self.llm — pass llm=None to the base.
        super().__init__(config, llm=None)  # type: ignore[arg-type]
        self.parsed_files = dict(parsed_files)
        self.corpus_root = Path(corpus_root)

        self._fn_to_file: dict[str, str] = {}
        self._struct_to_file: dict[str, str] = {}
        for path, p in self.parsed_files.items():
            for name in p.functions:
                self._fn_to_file.setdefault(name, path)
            for name in p.struct_definitions:
                self._struct_to_file.setdefault(name, path)

        self._llm = LLMToolClient(
            config=config, tools_schema=_TOOLS, role=self.name,
        )

    # BaseAgent abstract contract — this agent is driven via generate(), not
    # run(); these guard against accidental run() use.
    def build_prompt(self, **kwargs: Any) -> str:
        raise NotImplementedError(
            "AgenticHarnessGen is driven via generate()/generate_via_claude_code(), "
            "not BaseAgent.run()."
        )

    def parse(self, response: str) -> "Optional[str]":
        raise NotImplementedError(
            "AgenticHarnessGen is driven via generate(); harness extraction is "
            "_extract_c_code()."
        )
        self._tools_invoked: list[tuple[str, str]] = []
        # Per-call CBMC compile-check context (include_dirs, defines)
        self._include_dirs: list[str] = []
        self._defines: list[str] = []

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def generate(
        self,
        func: FunctionInfo,
        all_funcs_global: dict,
        include_dirs: Optional[list[str]] = None,
        defines: Optional[list[str]] = None,
    ) -> HarnessResult:
        import time as _time
        _t0 = _time.perf_counter()
        _res = None
        try:
            self._tools_invoked = []
            self._include_dirs = list(include_dirs or [])
            self._defines = list(defines or [])

            initial_ctx = self._build_initial_context(func, all_funcs_global)
            messages: list[dict] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": initial_ctx},
            ]
            _res = self._run_emit_loop(messages)
            return _res
        finally:
            try:
                from bmc_agent import agent_telemetry as _tel
                _tel.record(
                    "harness_gen", _time.perf_counter() - _t0,
                    outcome=("ok" if _res is not None else "error"),
                    tool_calls=len(getattr(self, "_tools_invoked", []) or []),
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Claude Code agent path (stronger harness; reads real code itself)
    # ------------------------------------------------------------------

    def generate_via_claude_code(
        self,
        func: FunctionInfo,
        all_funcs_global: dict,
        include_dirs: Optional[list[str]] = None,
        defines: Optional[list[str]] = None,
    ) -> HarnessResult:
        """Build the harness with the Claude Code agent instead of bmc's
        in-process tool loop. Claude Code runs its OWN Read/Grep loop over the
        source tree to get the real struct/type definitions right; bmc keeps the
        compile-check + retry loop (sound, deterministic). The prompt enforces
        the soundness discipline — keep attacker-controlled inputs nondet so the
        repaired harness can't quietly assume the bug away.
        """
        import copy
        self._include_dirs = list(include_dirs or [])
        self._defines = list(defines or [])

        # Force the claude-code provider + agentic tools for THIS call, scoped to
        # the source tree. role=None so role overrides don't redirect us.
        cc_cfg = copy.copy(self.config)
        cc_cfg.llm_provider = "claude-code"
        cc_cfg.claude_code_agentic = True
        dirs = list(getattr(self.config, "claude_code_add_dirs", None) or [])
        dirs.append(str(self.corpus_root))
        dirs.extend(self._include_dirs)
        seen: set[str] = set()
        cc_cfg.claude_code_add_dirs = [d for d in dirs if d and not (d in seen or seen.add(d))]
        client = LLMClient(cc_cfg)

        sig = self._format_sig(func)
        body = (func.body or "")[:4000]
        last_err = ""
        harness = ""
        for attempt in range(MAX_RETRIES + 1):
            prompt = _CLAUDE_CODE_HARNESS_PROMPT.format(
                fn_signature=sig,
                fn_body=body,
                add_dir=(cc_cfg.claude_code_add_dirs[0] if cc_cfg.claude_code_add_dirs else "(cwd)"),
                prior_error=last_err or "(none — first attempt)",
            )
            try:
                resp = client.complete(SYSTEM_PROMPT_CLAUDE_CODE, prompt, max_tokens=4096)
            except Exception as exc:
                logger.warning(
                    "claude-code harness gen [%s] attempt %d: LLM error: %s",
                    func.name, attempt, exc,
                )
                last_err = f"LLM error: {exc}"
                continue
            harness = _extract_c_code(resp)
            ok, cerr = self._compile_check(harness)
            if ok:
                logger.info(
                    "claude-code harness gen [%s]: compiles after %d retr%s",
                    func.name, attempt, "y" if attempt == 1 else "ies",
                )
                return HarnessResult(
                    harness=harness, retries=attempt, last_compile_error="",
                    rationale="claude-code agent",
                )
            last_err = cerr
        logger.info(
            "claude-code harness gen [%s]: still failing to compile after %d attempts",
            func.name, MAX_RETRIES + 1,
        )
        return HarnessResult(
            harness=harness, retries=MAX_RETRIES, last_compile_error=last_err,
            rationale="claude-code agent",
        )

    # ------------------------------------------------------------------
    # Refinement: rewrite the harness given a judge's UNREALISTIC verdict
    # ------------------------------------------------------------------

    def refine(
        self,
        func: FunctionInfo,
        all_funcs_global: dict,
        prior_harness: str,
        failing_property: str,
        judge_verdict: str,
        judge_reasoning: str,
        witness: Optional[dict] = None,
        cbmc_trace_excerpt: Optional[list[str]] = None,
        include_dirs: Optional[list[str]] = None,
        defines: Optional[list[str]] = None,
    ) -> HarnessResult:
        """Rewrite the harness in response to an UNREALISTIC/UNCERTAIN judge
        verdict. The LLM is given the prior harness, the failing property,
        the witness, and the judge's reasoning, and asked to decide:

          1. Is the judge correct that this is a harness artifact? If so,
             what structural change closes the gap (NUL-termination,
             struct-field invariant, buffer-size correlation, etc.)?
          2. Or is the judge over-claiming, and should the harness stay
             the same (in which case re-emit the prior harness as-is)?

        The contract is the same as ``generate``: returns a HarnessResult
        with a compile-checked harness, or fallback metadata on failure.
        """
        self._tools_invoked = []
        self._include_dirs = list(include_dirs or [])
        self._defines = list(defines or [])

        ctx = self._build_refinement_context(
            func=func,
            all_funcs_global=all_funcs_global,
            prior_harness=prior_harness,
            failing_property=failing_property,
            judge_verdict=judge_verdict,
            judge_reasoning=judge_reasoning,
            witness=witness or {},
            cbmc_trace_excerpt=cbmc_trace_excerpt or [],
        )
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT_REFINE},
            {"role": "user", "content": ctx},
        ]
        return self._run_emit_loop(messages)

    # ------------------------------------------------------------------
    # Shared tool-use loop (used by generate + refine)
    # ------------------------------------------------------------------

    def _run_emit_loop(self, messages: list[dict]) -> HarnessResult:
        last_harness = ""
        last_rationale = ""
        last_compile_error = ""
        last_cbmc_flags: Optional[dict] = None
        last_cbmc_budget: Optional[dict] = None
        retries = 0
        turn = 0

        for turn in range(MAX_TURNS):
            remaining = MAX_TURNS - turn
            if remaining <= 1:
                tool_choice = {
                    "type": "function",
                    "function": {"name": "emit_harness"},
                }
            else:
                tool_choice = "auto"

            try:
                resp = self._llm.call(messages, tool_choice=tool_choice)
            except Exception as exc:
                logger.warning("harness-gen LLM call failed at turn %d: %s",
                               turn + 1, exc)
                return HarnessResult(
                    harness=last_harness,
                    rationale=last_rationale,
                    retries=retries,
                    turns_used=turn + 1,
                    tools_invoked=list(self._tools_invoked),
                    last_compile_error=f"LLM call failed: {exc}",
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
                # LLM emitted prose. Nudge once.
                messages.append({
                    "role": "user",
                    "content": (
                        "You returned prose. Call ``emit_harness`` with the "
                        "complete harness text now (or use the read/grep "
                        "tools first if you still need context)."
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
                self._tools_invoked.append((name, args_compact))

                if name == "emit_harness":
                    candidate = str(args.get("harness", "") or "")
                    last_rationale = str(args.get("rationale", "") or "")
                    last_cbmc_flags = _sanitize_cbmc_flags(args.get("cbmc_flags"))
                    last_cbmc_budget = _sanitize_cbmc_budget(args.get("cbmc_budget"))
                    # Sanity guard: the harness must look like C.
                    if "int main" not in candidate:
                        last_compile_error = (
                            "emit_harness payload did not contain an "
                            "int main(...) — please include a complete harness."
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": last_compile_error,
                        })
                        continue

                    last_harness = candidate
                    ok, err = self._compile_check(candidate)
                    if ok:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": "compile_ok — harness accepted",
                        })
                        return HarnessResult(
                            harness=candidate,
                            rationale=last_rationale,
                            retries=retries,
                            turns_used=turn + 1,
                            tools_invoked=list(self._tools_invoked),
                            last_compile_error="",
                            cbmc_flags=last_cbmc_flags,
                            cbmc_budget=last_cbmc_budget,
                        )

                    last_compile_error = err
                    retries += 1
                    if retries > MAX_RETRIES:
                        # Out of retries; return the latest candidate so the
                        # caller can decide (fallback to deterministic gen).
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": (
                                f"compile_error (final attempt {retries}): "
                                f"{err[:1200]}"
                            ),
                        })
                        return HarnessResult(
                            harness=candidate,
                            rationale=last_rationale,
                            retries=retries,
                            turns_used=turn + 1,
                            tools_invoked=list(self._tools_invoked),
                            last_compile_error=err,
                            cbmc_flags=last_cbmc_flags,
                            cbmc_budget=last_cbmc_budget,
                        )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": (
                            f"compile_error (retry {retries}/{MAX_RETRIES}):\n"
                            f"{err[:2000]}\n\n"
                            "Please fix and re-emit."
                        ),
                    })
                    continue

                # Read-only investigation tool
                result_text = self._dispatch_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": _truncate(result_text, MAX_TOOL_RESULT_CHARS),
                })

            if remaining <= 3:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[reminder] {remaining-1} turn(s) left before "
                        f"emit_harness is forced. If you have enough context, "
                        f"submit the harness now."
                    ),
                })

        # Loop exhausted without success
        return HarnessResult(
            harness=last_harness,
            rationale=last_rationale,
            retries=retries,
            turns_used=MAX_TURNS,
            tools_invoked=list(self._tools_invoked),
            last_compile_error=last_compile_error or "max turns exhausted",
            cbmc_flags=last_cbmc_flags,
            cbmc_budget=last_cbmc_budget,
        )

    # ------------------------------------------------------------------
    # Initial context
    # ------------------------------------------------------------------

    def _build_initial_context(
        self, func: FunctionInfo, all_funcs_global: dict,
    ) -> str:
        sig = self._format_sig(func)
        body = func.body or "(body unavailable)"
        file_path = func.source_file or self._fn_to_file.get(func.name, "?")

        # Direct callers across the corpus
        callers: list[str] = []
        for path, p in self.parsed_files.items():
            for caller, callees in p.call_graph.items():
                if func.name in callees:
                    callers.append(caller)
        callers = sorted(set(callers))

        # Address-takers (for callback-dispatched functions like
        # archive_read_format_*_read_data)
        addr_takers: set[str] = set()
        for path, p in self.parsed_files.items():
            addr_takers |= (p.address_taken_in or {}).get(func.name, set())

        # Callees with body availability + file
        callee_lines = []
        for c in sorted(set(func.callees or set())):
            cfile = self._fn_to_file.get(c)
            if cfile:
                callee_lines.append(
                    f"  {c}  (in-corpus, file={Path(cfile).name}) — "
                    f"consider inlining; fetch with read_function('{c}')"
                )
            else:
                callee_lines.append(
                    f"  {c}  (external/libc) — declared as opaque extern; "
                    f"CBMC will havoc the return"
                )
        callees_block = "\n".join(callee_lines) or "  (none)"

        # Suggested include path for the function's .c file
        include_hint = (
            f'#include "{file_path}"'
            if file_path and file_path != "?" else
            "/* include the function's .c file here */"
        )

        # Caller hint: name the first few so the LLM knows where to look
        caller_hint = ""
        if callers:
            sample = ", ".join(callers[:5])
            caller_hint = (
                f"Look at the first few via read_function — they show "
                f"what real input shapes the function-under-test sees. "
                f"Suggested starting set: {sample}"
            )
        elif addr_takers:
            sample = ", ".join(sorted(addr_takers)[:5])
            caller_hint = (
                f"No direct callers in corpus, but the function's address "
                f"is taken (vtable / callback) in: {sample}. Inspect those "
                f"to understand how the dispatcher invokes it."
            )
        else:
            caller_hint = (
                "No callers or address-takers found in the corpus. The "
                "function is likely called from outside this corpus (public "
                "API or framework callback). Use grep_corpus to find "
                "documentation, related functions, and sibling size "
                "calculators."
            )

        callers_listing = (
            "\n".join(f"  {c}" for c in callers[:20])
            or "  (none in this corpus)"
        )
        if len(callers) > 20:
            callers_listing += f"\n  ... ({len(callers) - 20} more)"

        addr_listing = (
            "\n".join(f"  {n}" for n in sorted(addr_takers)[:10])
            or "  (none — address not taken in this corpus)"
        )

        sections = [
            "=== Function under verification ===",
            f"File: {file_path}",
            f"Signature: {sig}",
            "",
            "```c",
            body,
            "```",
            "",
            "=== Callees ===",
            callees_block,
            "",
            "=== Direct in-corpus callers ===",
            callers_listing,
            "",
            "=== Address-takers (callback dispatch) ===",
            addr_listing,
            "",
            "=== Guidance ===",
            caller_hint,
            "",
            "=== Harness skeleton suggestion ===",
            "```c",
            "/* CBMC harness for: " + func.name + " */",
            include_hint,
            "",
            "int main(void) {",
            "    /* 1. Allocate / nondet inputs, matching real-caller shapes */",
            "    /* 2. __CPROVER_assume(...) for invariants real callers enforce */",
            "    /* 3. Call " + func.name + "(...) */",
            "    /* 4. return 0; */",
            "}",
            "```",
            "",
            "Investigate first with the read/grep tools as needed, then submit "
            "the harness via emit_harness. Use compile_check to validate "
            "before emit if you're unsure about syntax.",
        ]
        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Refinement context builder
    # ------------------------------------------------------------------

    def _build_refinement_context(
        self,
        func: FunctionInfo,
        all_funcs_global: dict,
        prior_harness: str,
        failing_property: str,
        judge_verdict: str,
        judge_reasoning: str,
        witness: dict,
        cbmc_trace_excerpt: list,
    ) -> str:
        # Reuse the initial context (function body, callees, callers) since
        # the refining LLM benefits from the same baseline.
        base = self._build_initial_context(func, all_funcs_global)

        witness_lines = []
        if isinstance(witness, dict):
            for k, v in witness.items():
                witness_lines.append(f"  {k} = {v}")
        witness_block = "\n".join(witness_lines) or "  (no variable assignments)"

        trace_block = ""
        if cbmc_trace_excerpt:
            steps = cbmc_trace_excerpt[:80]
            trace_block = "\n".join(f"  {i+1:3d}. {s}" for i, s in enumerate(steps))
            if len(cbmc_trace_excerpt) > 80:
                trace_block += f"\n  ... ({len(cbmc_trace_excerpt) - 80} more)"

        sections = [
            base,
            "",
            "============================================================",
            "=== REFINEMENT REQUEST ===",
            "============================================================",
            "",
            "=== Prior harness (CBMC found a counterexample on it) ===",
            "```c",
            prior_harness or "(prior harness unavailable)",
            "```",
            "",
            f"=== Failing property === {failing_property}",
            "",
            "=== Witness (variable assignments at failure) ===",
            witness_block,
        ]
        if trace_block:
            sections.extend([
                "",
                "=== CBMC trace excerpt (first 80 steps) ===",
                trace_block,
            ])
        sections.extend([
            "",
            f"=== Judge verdict === {judge_verdict}",
            "",
            "=== Judge reasoning ===",
            judge_reasoning or "(no reasoning supplied)",
            "",
            "=== Refinement task ===",
            "Decide whether the judge's reasoning describes a real HARNESS GAP "
            "(a structural constraint the harness should encode) or whether "
            "the judge is over-claiming. Then call emit_harness with either:",
            "  - a rewritten harness that closes the gap WITHOUT killing real "
            "    bugs, OR",
            "  - the prior harness unchanged, with rationale explaining why "
            "    the judge's constraint shouldn't apply.",
        ])
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
                    int(args.get("start_line", 1) or 1),
                    int(args.get("end_line", 1) or 1),
                )
            if name == "grep_corpus":
                return self._tool_grep_corpus(
                    args.get("pattern", ""),
                    int(args.get("max_matches", 40) or 40),
                )
            if name == "list_callers":
                return self._tool_list_callers(args.get("name", ""))
            if name == "compile_check":
                ok, err = self._compile_check(args.get("harness", "") or "")
                return "compile_ok" if ok else f"compile_error:\n{err[:2400]}"
            return f"ERROR: unknown tool '{name}'"
        except Exception as exc:
            logger.warning("tool %s raised: %s", name, exc)
            return f"ERROR: tool '{name}' raised: {exc}"

    def _tool_read_function(self, fname: str) -> str:
        if not fname:
            return "ERROR: name is required"
        path = self._fn_to_file.get(fname)
        if not path:
            return f"NOT_FOUND: function '{fname}' is not in the corpus"
        parsed = self.parsed_files[path]
        body = parsed.function_bodies.get(fname) or ""
        if not body:
            return f"NOT_FOUND: function '{fname}' has no body recorded"
        sig = parsed.functions.get(fname)
        sig_text = (
            f"{sig.return_type} {fname}("
            + ", ".join(f"{t} {n}" for t, n in (sig.parameters or []))
            + ")"
        ) if sig else f"{fname}(...)"
        return f"// {Path(path).name}\n{sig_text} {{\n{body}\n}}"

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
            return "ERROR: grep timed out (>30s) — pattern too broad"
        if r.returncode > 1:
            return f"ERROR: grep returned {r.returncode}: {r.stderr[:300]}"
        out_lines = (r.stdout or "").splitlines()[:max_matches]
        if not out_lines:
            return f"NO_MATCH for pattern: {pattern}"
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
        lines.append(f"Address takers ({len(addr_takers)}):")
        for n, file_ in sorted(set(addr_takers)):
            lines.append(f"  {n}  ({file_})")
        if not addr_takers:
            lines.append("  (none)")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Compile-check (CBMC parse)
    # ------------------------------------------------------------------

    def _compile_check(self, harness_text: str) -> tuple[bool, str]:
        """Try to preprocess + parse the harness with CBMC. Returns (ok, err).

        Uses ``cbmc --show-properties`` which exercises the front-end without
        running the (expensive) BMC engine. Errors come back on stderr.
        """
        if not harness_text or "int main" not in harness_text:
            return False, "harness must define int main(void)"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".c", delete=False,
        ) as f:
            f.write(harness_text)
            path = f.name
        try:
            cmd = ["cbmc", "--show-properties", path]
            for inc in self._include_dirs:
                cmd.extend(["-I", inc])
            for d in self._defines:
                cmd.extend(["-D", d])
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                return False, "cbmc parse timed out (>60s)"
        finally:
            try: os.unlink(path)
            except OSError: pass

        combined = (r.stderr or "") + "\n" + (r.stdout or "")
        # `cbmc --show-properties` returns 0 on success. On parse error it
        # returns non-zero and emits "error: " lines.
        if r.returncode == 0 and "error:" not in combined.lower():
            return True, ""
        # Extract the first error region (~20 lines)
        err_lines = []
        for line in combined.splitlines():
            if "error:" in line.lower() or err_lines:
                err_lines.append(line)
            if len(err_lines) >= 25:
                break
        if not err_lines:
            err_lines = combined.splitlines()[:25]
        return False, "\n".join(err_lines)

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


_CBMC_FLAG_KEYS = (
    "unsigned_overflow_check",
    "conversion_check",
    "pointer_overflow_check",
)


def _sanitize_cbmc_budget(raw) -> Optional[dict]:
    """Clamp the LLM's unwind / timeout_s to safe ranges. Returns None when
    the LLM omitted the argument (caller treats None as "use CLI defaults").
    Bounds chosen empirically: unwind > 64 burns minutes per function for
    no precision gain; timeout > 600 monopolises a CBMC worker."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    if "unwind" in raw:
        try:
            uw = int(raw["unwind"])
        except (TypeError, ValueError):
            uw = None
        if uw is not None:
            out["unwind"] = max(1, min(uw, 64))
    if "timeout_s" in raw:
        try:
            t = int(raw["timeout_s"])
        except (TypeError, ValueError):
            t = None
        if t is not None:
            out["timeout_s"] = max(10, min(t, 600))
    return out or None


def _sanitize_cbmc_flags(raw) -> Optional[dict]:
    """Coerce the LLM's cbmc_flags arg into ``{key: bool}`` with only the
    keys we know how to pass to CBMC. Returns ``None`` if the LLM omitted
    the argument or supplied nothing recognisable (caller treats None as
    "use baseline flags only")."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for k in _CBMC_FLAG_KEYS:
        if k in raw:
            v = raw[k]
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, str):
                out[k] = v.strip().lower() in ("true", "1", "yes", "on")
            else:
                out[k] = bool(v)
    return out or None
