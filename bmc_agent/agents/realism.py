"""``RealismAgent`` — Pass 1 (primary verdict) of the realism check.

The realism check is the precision bottleneck of the pipeline — it
classifies each BMC counterexample as realistic / unrealistic /
uncertain. The full ``RealismChecker.check()`` flow is large
(~600 lines) because it does substantial work BEFORE and AFTER the
LLM call:

  Before:  witness-pattern pre-checks, hint injection, prompt construction
  Pass 1:  the primary LLM call (THIS AGENT)
  After:   Pass 2 (rule-based double-check, disabled by default),
           adjacent-bug discovery, tool-use augmentation, grounding audit

This agent owns just the Pass 1 LLM call + parse. Pass 2 / adjacent /
tool-use stay in ``RealismChecker`` for now; they're augmentation
calls layered on top of the primary verdict and can be migrated to
their own agents in follow-up commits.

Routing: ``BMC_AGENT_LLM_REALISM_*`` env vars — same role as the
augmentation calls so they all upgrade together when a user sets
``BMC_AGENT_LLM_REALISM_MODEL`` to a stronger backbone.
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


class RealismAgent(BaseAgent[RealismCheckResult]):
    """Pass 1 of the realism check — the primary classification call.

    Inputs to ``run()``:
        * ``user_prompt`` (str)  — fully rendered prompt (already includes
                                    any hint-injection augmentation done
                                    upstream by RealismChecker)
        * ``func_name`` (str)    — for parsing log lines / result tags
        * ``use_thinking`` (bool) — whether to enable extended thinking
                                     (extends max_tokens budget too)
    """

    name = "realism"

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        *,
        system_prompt: str,
    ) -> None:
        # System prompt is pre-rendered by RealismChecker (varies with
        # spec mode + threat model). Per-instance like SpecGenAgent.
        self.system_prompt = system_prompt
        super().__init__(config, llm)
        # Per-call state set by build_prompt:
        self._func_name: str = ""
        self._use_thinking: bool = False

    def _llm_call_kwargs(self) -> dict:
        # Mirrors the pre-existing complete(...) call site:
        #   max_tokens = 4096 + (4000 if thinking else 0)
        #   thinking_budget = 4000 when thinking on
        # The thinking flag is per-call (depends on user's
        # ``--enable-realism-thinking`` and call-site decision); set
        # via build_prompt() so the kwargs are correct.
        base = 4096
        if self._use_thinking:
            return {
                "max_tokens": base + 4000,
                "thinking": True,
                "thinking_budget": 4000,
            }
        return {
            "max_tokens": base,
            "thinking": False,
        }

    def build_prompt(
        self,
        *,
        user_prompt: str,
        func_name: str = "",
        use_thinking: bool = False,
        **_: Any,
    ) -> str:
        # The orchestrator (RealismChecker) renders the full
        # caller-grounded prompt with witness/hint augmentation; the
        # agent is a pass-through. We stash func_name + use_thinking
        # on the instance so parse() and _llm_call_kwargs see them.
        self._func_name = func_name
        self._use_thinking = use_thinking
        return user_prompt

    def parse(self, response: str) -> Optional[RealismCheckResult]:
        if not response:
            return None
        # Reuse the existing parser — preserves all the JSON-envelope
        # edge cases (fenced markdown, prose-embedded JSON, malformed,
        # unknown verdicts default to UNCERTAIN, REALISTIC requires
        # source-line evidence per the schema).
        result = _parse_result(response, self._func_name or "<via-agent>")
        return result
