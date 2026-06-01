"""``SpecGenWithToolsAgent`` — tool-augmented v2.2 spec drafting.

Second migration of a ``complete_with_tools`` call site (C2 step 8).
Mirrors the pattern from RealismToolsAgent: subclass overrides
``_call_llm`` to dispatch the tool-use loop with a ``SpecToolContext``,
stashes the ``ToolUseResult`` on the instance for ``AgentResult`` to
surface.

The orchestrator (SpecGeneratorV2._generate_with_tools, now thin
wrapper) keeps the gating policy (when to invoke the tool-use branch)
and the post-LLM validation + Spec construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.spec import Spec, SpecStatus
from bmc_agent.spec_generator_v2 import (
    _build_spec_from_validated,
    _extract_json_object,
    _validate_and_extract,
)

if TYPE_CHECKING:
    from bmc_agent.boundary_detector import BoundaryDetector
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import ParsedCFile


class SpecGenWithToolsAgent(BaseAgent[Spec]):
    """v2.2 spec-gen tool-use refinement. Returns a refined Spec when
    the LLM emits one and it passes validation; None when the LLM
    declined or produced something unparseable (caller falls back to
    the base v2 spec).

    Routing: ``BMC_AGENT_LLM_SPEC_GEN_*`` — same role as the base
    SpecGenAgent so upgrading the spec-gen backbone covers both.
    """

    name = "spec_gen"

    #: Bounded tool-use loop. Matches the pre-existing
    #: SpecGeneratorV2._generate_with_tools call site exactly.
    max_iterations_param: int = 8
    max_tool_calls_param: int = 5
    max_tokens_per_turn_param: int = 4096

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        *,
        system_prompt: str,
        parsed: "ParsedCFile",
        corpus_paths: list[Path],
        all_specs_so_far: "dict[str, Spec]",
        boundary_detector: "Optional[BoundaryDetector]" = None,
    ) -> None:
        # Per-instance state for SpecToolContext. The tools fetch
        # additional callers / look up callee bodies / inspect struct
        # fields mid-reasoning, so they need full access to the
        # parsed source + corpus + specs computed so far.
        self.system_prompt = system_prompt
        self.parsed = parsed
        self.corpus_paths = corpus_paths
        self.all_specs_so_far = all_specs_so_far
        self.boundary_detector = boundary_detector
        super().__init__(config, llm)
        self._fn_name: str = ""

    def build_prompt(
        self,
        *,
        prompt: str,
        fn_name: str = "",
        **_: Any,
    ) -> str:
        # The orchestrator already rendered the caller-grounded prompt
        # AND appended TOOL_USE_PROMPT_ADDENDUM — agent is a
        # pass-through. fn_name is stashed for parse() to use.
        self._fn_name = fn_name
        return prompt

    def _call_llm(self, prompt: str) -> tuple[str, Optional[str]]:
        # Under --agentic, run on the Claude Code agent instead of bmc's
        # in-process tool loop (trace not captured here yet — stream-json TODO).
        if self._agent_runs_on_claude_code():
            return super()._call_llm(prompt)
        from bmc_agent.llm import LLMError
        from bmc_agent.spec_gen_tools import (
            SpecToolContext, build_spec_gen_tools,
        )
        ctx = SpecToolContext(
            parsed=self.parsed,
            corpus_paths=self.corpus_paths,
            all_specs_so_far=self.all_specs_so_far,
            boundary_detector=self.boundary_detector,
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

    def parse(self, response: str) -> Optional[Spec]:
        if not response:
            return None
        payload = _extract_json_object(response)
        if payload is None:
            return None
        validated = _validate_and_extract(payload, self._fn_name or "<via-agent>")
        if validated is None:
            return None
        pv, pp, post, loops, disagreement, _notes = validated
        return _build_spec_from_validated(
            self._fn_name or "<via-agent>",
            pv, pp, post, loops, disagreement,
            status=SpecStatus.GENERATED,
        )
