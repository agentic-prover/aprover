"""``RealismToolsAgent`` — tool-augmented realism reconsideration.

When the primary realism check returns a verdict that the orchestrator
wants to verify with deeper evidence, this agent re-runs the check
with a tool registry the LLM can call mid-reasoning (``lookup_function``,
``lookup_callee_postcondition``). The tool-use loop is bounded
(max_iterations=6, max_tool_calls=3) to keep cost and recursion in
check.

The downstream grounding audit (commit 94fc64d) reads the agent's
``tool_use_result.messages`` to verify the LLM actually called
``lookup_function`` on the target before voting REALISTIC — this is
why ``AgentResult.tool_use_result`` is exposed.

This is C2 step 7 — first migration of a ``complete_with_tools``
call site. The pattern (subclass overrides ``_call_llm`` to dispatch
the tool-use loop, stash ``ToolUseResult`` on the instance for the
base ``run()`` to surface) is reusable for SpecGenWithToolsAgent
(next).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.realism_checker import (
    RealismCheckResult,
    _parse_result,
)

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.spec import Spec


class RealismToolsAgent(BaseAgent[RealismCheckResult]):
    """Tool-augmented realism reconsideration.

    Inputs to ``run()``:
        * ``user_prompt`` (str)  — pre-rendered prompt with the base
                                    verdict and TOOL_USE_PROMPT_ADDENDUM
        * ``func_name`` (str)    — for parse log lines

    Output: ``RealismCheckResult`` (the refined verdict).
    AgentResult.tool_use_result carries the full ToolUseResult so the
    orchestrator can run the grounding audit (which tools were called,
    whether lookup_function was called on the target).
    """

    name = "realism"

    #: Bounded tool-use loop. Matches the pre-existing realism_checker
    #: call site exactly (commit 6e4544c established these limits).
    max_iterations_param: int = 6
    max_tool_calls_param: int = 3
    max_tokens_per_turn_param: int = 4096

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        *,
        system_prompt: str,
        parsed_file: "ParsedCFile",
        all_specs: "dict[str, Spec]",
    ) -> None:
        # Per-instance state: the realism tools need access to the
        # parsed source + already-generated specs to look up callee
        # bodies / postconditions.
        self.system_prompt = system_prompt
        self.parsed_file = parsed_file
        self.all_specs = all_specs
        super().__init__(config, llm)
        self._func_name: str = ""

    def build_prompt(
        self,
        *,
        user_prompt: str,
        func_name: str = "",
        **_: Any,
    ) -> str:
        # The orchestrator (RealismChecker._augment_with_tools) renders
        # the full reconsider prompt including the base verdict + tool
        # addendum; the agent is a pass-through.
        self._func_name = func_name
        return user_prompt

    def _call_llm(self, prompt: str) -> tuple[str, Optional[str]]:
        # Override the default single-call path with the tool-use loop.
        # Builds tools on-demand using the per-call parsed_file +
        # all_specs context. Stashes the full ToolUseResult on the
        # instance so BaseAgent.run() can surface it via AgentResult.
        from bmc_agent.llm import LLMError
        from bmc_agent.realism_tools import (
            RealismToolContext, build_realism_tools,
        )
        ctx = RealismToolContext(
            parsed=self.parsed_file,
            all_specs=dict(self.all_specs),
        )
        tools, handlers = build_realism_tools(ctx)
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
        # Stash the full ToolUseResult for run() to surface.
        self._last_tool_use_result = result
        if result.error:
            # Treat tool-loop caps (max_iterations / max_tool_calls)
            # as a soft error — the partial response may still be
            # parseable, but the orchestrator wants to know.
            return result.text or "", f"tool_use_terminated: {result.error}"
        return result.text or "", None

    def parse(self, response: str) -> Optional[RealismCheckResult]:
        if not response:
            return None
        return _parse_result(response, self._func_name or "<via-agent>")
