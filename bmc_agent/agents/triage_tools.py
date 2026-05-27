"""``TriageToolsAgent`` — tool-augmented independent triage.

Extends the post-hoc ``TriageAgent`` with a bounded multi-turn tool-use
loop. The triage agent's v1 (one-shot, function-source-only) missed
the libarchive ``archive_acl_to_text_*`` heap-overflow bug because
the witness CEx fires on the internal helper ``append_id`` while the
actual bug lives THREE frames upstream in ``archive_acl_text_len``'s
size-budget calculation versus ``append_entry``'s write paths. Without
tool access to walk the call chain and audit sibling helpers, the
agent (correctly given its inputs) judged ``harness_pointer_offset_
unconstrained / likely_fp`` — the wrong answer for the right reason.

This v2 agent gets:

  * ``lookup_function(name)`` — fetch any function's signature + body
    from the parsed corpus.
  * ``find_more_callers(name, k)`` — discover callers beyond the
    pipeline-provided caller_path (which truncates after 2 frames).
  * ``lookup_struct(tag)`` — struct field details.
  * ``grep_corpus(pattern, k)`` — pattern search across the corpus
    (the only way to find ``archive_acl_text_len``-style upstream
    helpers without knowing their name a priori).

System prompt is extended with a new directive: "the CBMC property
failure may be a SYMPTOM of a bug N frames upstream. If the
function-under-test's writes look size-bounded by a caller's
precondition, fetch the caller's body and audit the size calculation
against every write path the callee can take."

Reuses ``SpecToolContext`` + ``build_spec_gen_tools`` since those
already build the exact tool set; only the system prompt and the
expected output (TriageResult) differ.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.agents.triage import (
    TriageResult,
    TriageVerdict,
    _SYSTEM_PROMPT as _BASE_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.spec import Spec


_TOOLS_PROMPT_ADDENDUM = (
    "\n\nTOOLS:\n"
    "  * lookup_function(name) — full source + callees of any function.\n"
    "  * find_more_callers(name, k) — discover callers beyond the 2-frame\n"
    "    caller_path in the prompt.\n"
    "  * lookup_struct(tag) — struct field details.\n"
    "  * grep_corpus(pattern, k) — regex search across the corpus.\n\n"
    "BEFORE you vote, do this audit (you have a 10-iteration / 8-tool-call\n"
    "budget — use it):\n\n"
    "1. Use ``find_more_callers`` (or follow the existing caller_path)\n"
    "   to reach the public-API entry point. Use ``lookup_function``\n"
    "   on every intermediate caller.\n\n"
    "2. Find the SIZE CALCULATOR (often named ``*_text_len``,\n"
    "   ``*_compute_size``, ``*_bytes_needed`` — use ``grep_corpus``\n"
    "   with patterns like ``[a-z_]+_text_len`` or ``[a-z_]+_size`` if\n"
    "   you don't know the name). Read its full body via\n"
    "   ``lookup_function``.\n\n"
    "3. Find the WRITER (the function that actually writes into the\n"
    "   buffer the size calculator sized). Read its full body via\n"
    "   ``lookup_function``.\n\n"
    "4. **BUILD AN EXPLICIT AUDIT TABLE in your final reasoning** —\n"
    "   one row per write site in the writer, paired with the matching\n"
    "   length-accumulation in the size calculator:\n\n"
    "       Write site (file:line)           | Budget site (file:line)\n"
    "       writer.c:101 *p++ = ':'          | calc.c:42 length += 1\n"
    "       writer.c:104 strcpy(*p, name)    | calc.c:48 length += strlen(name)\n"
    "       writer.c:109 *(*p)++ = ':'       | (NONE — UNBUDGETED) ← REAL_BUG\n"
    "       writer.c:110 append_id(p, id)    | (NONE — UNBUDGETED) ← REAL_BUG\n\n"
    "   For every UNBUDGETED row, identify the public-API caller path\n"
    "   that exercises it and vote REAL_BUG.\n\n"
    "5. If a write is conditional on a flag/type (e.g. only fires when\n"
    "   ``type == NFS4``), check whether the calculator has the SAME\n"
    "   conditional check. A write under ``if(NFS4)`` matched only by\n"
    "   ``if(EXTRA_ID)`` in the calculator is a missed-branch REAL_BUG.\n\n"
    "DO NOT default to LIKELY_FP just because (a) the harness is over-\n"
    "permissive, or (b) a size calculator exists, or (c) there is a\n"
    "runtime guard like ``__archive_errx(\"Buffer overrun\")``. The\n"
    "existence of a runtime guard is EVIDENCE THE DEVELOPERS WERE\n"
    "WORRIED ABOUT THIS CLASS OF BUG — it is not evidence the system\n"
    "is safe. Always do the audit before voting.\n\n"
    "REACHABILITY GATES BEFORE A REAL_BUG VOTE — two systematic FP\n"
    "classes the tool's local analysis can't see without explicit\n"
    "checks. Both are about whether the precondition the CEx requires\n"
    "is reachable from in-tree call paths. Run BOTH and document them\n"
    "in your reasoning before voting REAL_BUG:\n\n"
    "G1. PRIVATE-HEADER REACHABILITY GATE. If your argument depends\n"
    "    on a function being callable from outside the library (e.g.\n"
    "    \"non-static, callable from external code\"), VERIFY the\n"
    "    prototype's header is not internal-only. Run:\n"
    "      grep_corpus(\"<fn_name>\\\\s*\\\\(\")            → find the .h\n"
    "      grep_corpus(\"#error.*internal\")          → private-header marker\n"
    "      grep_corpus(\"__LIBARCHIVE_BUILD|__LIBFOO_INTERNAL\")\n"
    "    If the prototype lives ONLY in a header gated by an\n"
    "    ``#error`` / ``#ifdef LIBFOO_BUILD`` guard, external callers\n"
    "    CANNOT include it. \"Non-static\" alone does NOT widen the\n"
    "    reachable-caller set — the published headers do. Treat the\n"
    "    in-tree callers as the entire reachable set when this gate\n"
    "    fires.\n\n"
    "G2. ALLOCATION-SITE INVARIANT GATE. If your REAL_BUG argument\n"
    "    relies on a struct field being uninitialized/garbage/non-NULL\n"
    "    at function entry (e.g. \"free(s->buf) breaks because buf\n"
    "    contains a garbage pointer\"), VERIFY no in-tree caller can\n"
    "    actually produce that state. Run:\n"
    "      grep_corpus(\"calloc.*sizeof\\\\(struct <tag>\\\\)|calloc.*sizeof\\\\(\\\\*[a-z_]+\\\\).*<tag>\")\n"
    "      grep_corpus(\"memset.*0,.*sizeof.*<tag>\")\n"
    "      grep_corpus(\"struct <tag>\\\\b\")           → all carriers / decl sites\n"
    "    If 100% of in-tree allocation sites zero-init the struct\n"
    "    (calloc, memset(...,0,...), or embedded in another calloc'd\n"
    "    struct), the \"garbage field\" precondition is unreachable\n"
    "    from in-tree callers. Also remember: ``free(NULL)`` is a\n"
    "    no-op per C99 §7.22.3.3 — a NULL pointer field is NEVER an\n"
    "    unsafe argument to free.\n\n"
    "G3. INTRA-FUNCTION LOOP-BOUND INVARIANT GATE. If your REAL_BUG\n"
    "    argument requires a walking pointer to step PAST a boundary\n"
    "    set elsewhere in the SAME function (e.g. \"trim loop\n"
    "    decrements ``*end`` below ``*start``\", \"scan loop runs past\n"
    "    the buffer NUL\"), VERIFY no PRECEDING loop in the same\n"
    "    function has already established an invariant on the\n"
    "    boundary character that the current loop's continuation\n"
    "    condition cannot satisfy.\n\n"
    "    Procedure:\n"
    "      1. Identify the current loop's CONTINUATION SET C (the\n"
    "         set of char/byte values at which it iterates).\n"
    "      2. Identify the boundary pointer X (the one the CEx says\n"
    "         is walked past). Read the function backwards to where\n"
    "         X was assigned. If X was set from ``*p`` immediately\n"
    "         after a PRECEDING loop, that loop's exit established\n"
    "         ``**X ∉ <preceding-loop continuation set L>``.\n"
    "      3. If L ⊇ C, the current loop's body is unreachable on\n"
    "         the FIRST iteration at X — the loop CANNOT walk past\n"
    "         X. (L == C is the common case; L being a superset of\n"
    "         C also works.)\n\n"
    "    Worked example (libarchive ``next_field_w``):\n"
    "      Leading skip:  ``while (**wp == L' '||L'\\t'||L'\\n') (*wp)++;``\n"
    "      then           ``*start = *wp;``\n"
    "      Trim loop:     ``while (**end == L' '||L'\\t'||L'\\n') (*end)--;``\n"
    "      Preceding-loop continuation set L = {L' ',L'\\t',L'\\n'};\n"
    "      after exit, ``**start ∉ L``. Trim's C = L. So ``**start``\n"
    "      cannot satisfy the trim condition — trim is bounded by\n"
    "      ``*start``. CBMC's CEx is a nondeterministic-input artifact\n"
    "      (input at X already in C, preceding loop \"didn't run\"),\n"
    "      not an in-tree state. Vote LIKELY_FP with fp_class=\n"
    "      \"intra-function-loop-invariant\".\n\n"
    "G4. CALLER-ESTABLISHED PRECONDITION GATE. If your REAL_BUG\n"
    "    argument requires a function parameter to take a value the\n"
    "    in-tree caller never produces (e.g. \"the bug fires when\n"
    "    ``*l > strlen(*p)``\" but every caller sets ``*l = strlen(*p)``;\n"
    "    or \"the bug fires when ``flag`` is outside its enum range\" but\n"
    "    every caller passes a literal enum value), VERIFY by reading\n"
    "    every in-tree caller.\n\n"
    "    Procedure:\n"
    "      1. Identify the parameter the CEx says is \"wrong\" — the\n"
    "         value the witness shows that the harness's assumption\n"
    "         space allows but the function's correctness depends on\n"
    "         the caller NOT producing. Call this the CEx-required\n"
    "         precondition P.\n"
    "      2. Use ``find_more_callers`` + ``lookup_function`` to\n"
    "         enumerate every in-tree caller. For each, locate the\n"
    "         line that sets the argument bound to the parameter.\n"
    "      3. If 100% of in-tree callers establish P (literal value,\n"
    "         ``strlen()`` call, prior ``!= NULL`` check on the same\n"
    "         variable, etc.), the CEx state is unreachable from any\n"
    "         in-tree call path. Vote LIKELY_FP with fp_class=\n"
    "         \"caller-established-precondition\".\n\n"
    "    Worked example (libarchive ``next_field``):\n"
    "      CEx requires ``*l > strlen(*p)`` (length parameter larger\n"
    "      than the buffer's NUL position). Sole in-tree caller is\n"
    "      ``archive_acl_from_text_nl`` at archive_acl.c:1655, which\n"
    "      sets ``*l = strlen(text)`` immediately before the call. So\n"
    "      ``*l <= strlen(*p)`` always; the CEx state is unreachable\n"
    "      from any in-tree call path. Vote LIKELY_FP with fp_class=\n"
    "      \"caller-established-precondition\".\n\n"
    "    G4 is NOT triggered by \"harness is over-permissive\" alone —\n"
    "    that's necessary but not sufficient. It IS triggered by\n"
    "    \"caller invariably establishes the missing precondition.\"\n"
    "    The audit must cite the line in each in-tree caller that\n"
    "    establishes P. If you find even ONE caller that doesn't, do\n"
    "    NOT vote G4 — fall through to the normal REAL_BUG audit.\n\n"
    "Any of these gates ABORTS the REAL_BUG vote — vote LIKELY_FP\n"
    "with one of:\n"
    "  fp_class=\"private-header-gated\"\n"
    "  fp_class=\"calloc-zero-init-invariant\"\n"
    "  fp_class=\"intra-function-loop-invariant\"\n"
    "  fp_class=\"caller-established-precondition\"\n"
    "respectively, citing the source/grep evidence that proved the\n"
    "gate. If a gate can't be run (no corpus, function name doesn't\n"
    "appear in any header), proceed with the rest of the audit —\n"
    "they're reachability filters on confident REAL_BUG votes, not\n"
    "on LIKELY_FP votes.\n"
)


_SYSTEM_PROMPT_TOOLS = _BASE_SYSTEM_PROMPT + _TOOLS_PROMPT_ADDENDUM


class TriageToolsAgent(BaseAgent[TriageResult]):
    """Tool-augmented independent triage.

    Same TriageResult output schema as ``TriageAgent``; difference is
    the agent can call tools to inspect the broader call chain before
    deciding. Uses ``role="triage"`` so dedicated env-var routing
    applies (BMC_AGENT_LLM_TRIAGE_*).

    Inputs to ``run()`` are the SAME as TriageAgent — caller-script
    code path is identical except for the agent class chosen.
    """

    name = "triage"
    system_prompt = _SYSTEM_PROMPT_TOOLS

    #: Bounded tool-use loop. Triage benefits from a longer loop than
    #: realism (more upstream-frame reading), shorter than spec-gen
    #: (no synthesis step).
    max_iterations_param: int = 10
    max_tool_calls_param: int = 8
    max_tokens_per_turn_param: int = 4096

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        *,
        parsed_file: "ParsedCFile",
        corpus_paths: "list[Path]",
        all_specs: "Optional[dict[str, Spec]]" = None,
    ) -> None:
        # Per-instance state for SpecToolContext. Triage needs full
        # access to the parsed source + corpus to walk upstream
        # callers; the specs dict is optional (used by lookup_caller_
        # spec but we don't have specs in standalone-script mode —
        # pass {} when not available).
        self.parsed_file = parsed_file
        self.corpus_paths = list(corpus_paths)
        self.all_specs = dict(all_specs or {})
        super().__init__(config, llm)
        self._last_tool_use_result = None

    def _llm_call_kwargs(self) -> dict:
        return {}  # tool-use loop manages its own per-turn budget

    def build_prompt(
        self,
        *,
        function_name: str,
        function_source: str,
        cbmc_property: str,
        harness_source: str,
        witness_text: str,
        caller_path: list,
        dyn_outcome: Optional[str],
        dyn_reasoning: Optional[str],
        reproducer_source: Optional[str],
        realism_verdict: Optional[str],
        realism_reasoning: Optional[str],
        pipeline_reasoning: str,
        sys_entry_reached: bool,
        **_: Any,
    ) -> str:
        # Identical to TriageAgent.build_prompt — share the structure
        # by importing the base agent's renderer rather than copy.
        from bmc_agent.agents.triage import TriageAgent
        base = TriageAgent(self.config, self.llm)
        return base.build_prompt(
            function_name=function_name,
            function_source=function_source,
            cbmc_property=cbmc_property,
            harness_source=harness_source,
            witness_text=witness_text,
            caller_path=caller_path,
            dyn_outcome=dyn_outcome,
            dyn_reasoning=dyn_reasoning,
            reproducer_source=reproducer_source,
            realism_verdict=realism_verdict,
            realism_reasoning=realism_reasoning,
            pipeline_reasoning=pipeline_reasoning,
            sys_entry_reached=sys_entry_reached,
        )

    def _call_llm(self, prompt: str) -> tuple[str, Optional[str]]:
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

    def parse(self, response: str) -> Optional[TriageResult]:
        # Same parser as the base agent — same JSON output schema.
        from bmc_agent.agents.triage import TriageAgent
        return TriageAgent(self.config, self.llm).parse(response)
