"""``AdjacentBugAgent`` — independent second LLM call for finding bugs
the primary CBMC counterexample didn't capture.

Fires only when the primary realism check rejected the CBMC CEx
(verdict=UNREALISTIC) — the rationale being that when a function is
clean of the specific CBMC finding it may still contain a different
exploitable defect worth flagging. Returns a list of candidate adjacent
bugs (each is a dict with function, location, attacker_scenario,
severity) that the orchestrator attaches to the primary
``RealismCheckResult.adjacent_bugs``.

Routing: ``BMC_AGENT_LLM_REALISM_*`` — same role as the primary
realism check, so users upgrading realism to a stronger backbone
get the adjacent-bug call upgraded automatically.

This is the C2 step 6 (post-step-5 mop-up): completes migration of
the realism-side LLM calls. The primary verdict (Pass 1) is
``RealismAgent``; this is the secondary discovery pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.realism_checker import _parse_adjacent_bugs

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient


class AdjacentBugAgent(BaseAgent[list]):
    """Second LLM call: hunt for adjacent exploitable defects when the
    primary realism check rejected the CBMC CEx.

    Inputs to ``run()``:
        * ``user_prompt`` (str)  — pre-rendered adjacent-bug prompt
                                    (built by RealismChecker's
                                    _build_adjacent_bug_prompt)
        * ``func_name`` (str)    — for parser log lines

    Output: ``list[dict]`` — each dict describes one candidate adjacent
    bug (function, location, attacker_scenario, severity). Empty list
    when the LLM finds no candidates (which is a valid answer, not a
    failure).
    """

    name = "realism"

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        *,
        system_prompt: str,
    ) -> None:
        # Per-instance system_prompt (same render as RealismChecker's
        # primary call — varies with spec mode + threat model).
        self.system_prompt = system_prompt
        super().__init__(config, llm)
        self._func_name: str = ""

    def _llm_call_kwargs(self) -> dict:
        # Mirrors the pre-existing realism_checker adjacent-bug
        # call: 4096 max_tokens, thinking off (discovery is a
        # narrower task than verdict reasoning).
        return {"max_tokens": 4096, "thinking": False}

    def build_prompt(
        self,
        *,
        user_prompt: str,
        func_name: str = "",
        **_: Any,
    ) -> str:
        self._func_name = func_name
        return user_prompt

    def parse(self, response: str) -> Optional[list]:
        # _parse_adjacent_bugs returns a (possibly empty) list. An empty
        # list is a valid answer — the LLM said "no adjacent bugs" —
        # so we surface it as a non-None result.
        if not response:
            return None
        return _parse_adjacent_bugs(response, self._func_name or "<via-agent>")
