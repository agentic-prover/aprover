"""``FeedbackDistillAgent`` — migrates feedback_loop's distill step.

Turns an UNREALISTIC / UNCERTAIN realism rejection into a structured
``Remediation`` proposing one of:
  * scope=function-spec          — add a clause to the function's PRE
  * scope=project-invariant      — global invariant for the whole project
  * scope=function-post-relax    — drop an over-tight POST clause
  * scope=code-change            — bmc-agent itself is missing a feature
  * scope=none                   — no safe fix

This is C2 step 2 — a single-LLM-call agent with structured JSON
output, similar shape to DisagreementDiagnoseAgent but with
``max_tokens=16384`` and ``thinking=False`` because the distill
response includes a long reasoning block before the JSON.

The standalone ``feedback_loop.learn_from_rejection`` function
survives as a thin wrapper that constructs and runs this agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.feedback_loop import (
    Remediation,
    RemediationScope,
    _DISTILL_PROMPT,
    _parse_remediation,
)

if TYPE_CHECKING:
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo
    from bmc_agent.realism_checker import RealismCheckResult


class FeedbackDistillAgent(BaseAgent[Remediation]):
    """Distills an UNREALISTIC / UNCERTAIN realism verdict into a
    structured remediation. Caller is responsible for gating on the
    realism verdict — the agent itself runs unconditionally on whatever
    input it's given (matches the simpler agent contract).

    Routing: ``BMC_AGENT_LLM_FEEDBACK_DISTILL_*`` env vars. The hybrid
    quick-start (``BMC_AGENT_HYBRID_SPEC_GEN_KEY``) also routes this
    role to Claude by default, since the distill task wants a smart
    model even when the global default is something cheaper.
    """

    name = "feedback_distill"

    def __init__(self, config: "Config", llm: "LLMClient") -> None:
        # Set as instance attribute (not class attribute) so multiple
        # FeedbackDistillAgent instances can't accidentally mutate
        # each other's state if a future change makes the prompt
        # per-config.
        from bmc_agent.prompts import SPEC_SYSTEM_PROMPT
        self.system_prompt = SPEC_SYSTEM_PROMPT
        super().__init__(config, llm)

    def _llm_call_kwargs(self) -> dict:
        # K2 Think exhausts a 2048 budget on its <think> trace before
        # emitting the JSON remediation — live sweep showed 16 calls
        # failing with finish_reason=length. Bump budget so K2 has
        # headroom; also turn extended-thinking off since the distill
        # response already includes structured reasoning in the
        # rationale field.
        return {"max_tokens": 16384, "thinking": False}

    def build_prompt(
        self,
        *,
        func: "FunctionInfo",
        counterexample: "Counterexample",
        realism: "RealismCheckResult",
        existing_project_clauses: list[str],
        **_: Any,
    ) -> str:
        body = (getattr(func, "body", None) or "(unavailable)")[:6000]
        var_state = "\n".join(
            f"  {k} = {v}"
            for k, v in (counterexample.variable_assignments or {}).items()
        )[:3000] or "  (no witness variables)"

        return _DISTILL_PROMPT.format(
            verdict=realism.verdict.value.upper(),
            function_name=func.name,
            function_body=body,
            violated_property=counterexample.failing_property,
            witness_state=var_state,
            rejection_reasoning=(realism.reasoning or "")[:1500],
            key_concern=(realism.key_concern or "")[:300],
            existing_project_clauses=(
                "\n".join(f"  {c}" for c in existing_project_clauses) or "  (none)"
            ),
        )

    def parse(self, response: str) -> Optional[Remediation]:
        # Reuse the existing parser — preserves identical behaviour for
        # every JSON envelope variant (fenced, prose-embedded,
        # unparseable). Empty input → caller treats as INCONCLUSIVE
        # via the Remediation(scope=NONE) sentinel.
        if not response:
            return None
        rem = _parse_remediation(response, func_name="<via-agent>")
        # The agent contract says parse() returns None for "couldn't
        # parse" (so BaseAgent.run() reports a clean error). A
        # Remediation with scope=NONE is a VALID parsed answer
        # ("LLM said no safe fix"); pass it through unchanged.
        return rem
