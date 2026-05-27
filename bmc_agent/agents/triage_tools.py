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
    "\n\nYou have these TOOLS available — use them to walk the caller "
    "chain and audit upstream helpers BEFORE deciding:\n\n"
    "  * lookup_function(name) — fetch any function's source + callees.\n"
    "  * find_more_callers(name, k) — discover callers beyond the 2-frame\n"
    "    caller_path provided in the prompt (the pipeline truncates).\n"
    "  * lookup_struct(tag) — fields of a struct type.\n"
    "  * grep_corpus(pattern, k) — regex search across corpus sources.\n\n"
    "ROOT-CAUSE HEURISTIC: when the CBMC property fires on a function "
    "whose writes APPEAR size-bounded by an implicit caller invariant "
    "(e.g. ``*p`` written without explicit size check, internal helper "
    "trusting a pre-allocated buffer), the bug may live N frames "
    "UPSTREAM in the size-calculator that allocated the buffer — NOT "
    "in the function the CEx fires on. Walk the chain:\n"
    "  1. Use ``find_more_callers`` to identify the public-API entry\n"
    "     point reachable from this function.\n"
    "  2. Use ``lookup_function`` on each intermediate caller; note\n"
    "     which write paths exist.\n"
    "  3. Find the size-CALCULATOR (often named ``*_len``, ``*_size``,\n"
    "     ``compute_*_bytes``) and read it via ``lookup_function``\n"
    "     or ``grep_corpus`` (regex like ``[a-z_]+_text_len``).\n"
    "  4. Compare: for every conditional write path in the callees,\n"
    "     does the size-calculator have a matching branch in its\n"
    "     accumulation? If a write path exists with no corresponding\n"
    "     budget accumulation → REAL_BUG (callee_write_exceeds_caller_budget).\n\n"
    "Add a new FP class tag if needed:\n"
    "  * callee_write_exceeds_caller_budget — REAL_BUG class for the\n"
    "    upstream-size-mismatch pattern above. Use this verdict when\n"
    "    you find a write the size-calculator did not budget for.\n\n"
    "Cost-discipline: budget your tool calls (max ~6 calls). Don't\n"
    "fan out exhaustively — pursue the most likely chain first.\n"
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
