"""``RefinementAgent`` — migrates spec_refiner's LLM call.

When realism rejects a CEx as UNREALISTIC with an actionable
key_concern, this agent proposes the precise PRE clause to add so the
rejected witness state is excluded. The caller (``SpecRefiner``) is
responsible for the in-loop re-verification flow (apply clause →
re-run BMC → check acceptance) — the agent itself just owns the
LLM-driven clause-proposal step.

This is C2 step 3 — same shape as FeedbackDistillAgent (single
structured-JSON call with custom max_tokens) but with refinement-
specific gating semantics handled by the orchestrating SpecRefiner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.spec_refiner import (
    RefinementProposal,
    _REFINE_PROMPT,
    _parse_refinement_response,
)

if TYPE_CHECKING:
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo
    from bmc_agent.realism_checker import RealismCheckResult
    from bmc_agent.spec import Spec


class RefinementAgent(BaseAgent[RefinementProposal]):
    """Proposes a single PRE clause to tighten a function's spec so a
    rejected CEx state is excluded.

    Routing: ``BMC_AGENT_LLM_REFINEMENT_*`` env vars. The SpecRefiner
    orchestrator handles gating (verdict + actionable key_concern) and
    the post-refine BMC re-verification loop.
    """

    name = "refinement"

    def __init__(self, config: "Config", llm: "LLMClient") -> None:
        from bmc_agent.prompts import SPEC_SYSTEM_PROMPT
        self.system_prompt = SPEC_SYSTEM_PROMPT
        super().__init__(config, llm)

    def _llm_call_kwargs(self) -> dict:
        # Per the original spec_refiner.propose_refinement — 4096 is
        # plenty for a single-clause proposal, and thinking is off
        # since the structured JSON already includes a rationale field.
        return {"max_tokens": 4096, "thinking": False}

    def build_prompt(
        self,
        *,
        func_info: "FunctionInfo",
        current_spec: "Spec",
        rejected_cex: "Counterexample",
        realism: "RealismCheckResult",
        **_: Any,
    ) -> str:
        sig = func_info.signature
        params_str = ", ".join(
            f"{t} {n}" for t, n in sig.parameters
        ) or "void"
        fn_signature = f"{sig.return_type} {sig.name}({params_str})"

        witness = "\n".join(
            f"  {k} = {v}"
            for k, v in (rejected_cex.variable_assignments or {}).items()
        )[:2000] or "  (no witness state)"

        # property_class extraction — last non-numeric segment of
        # ``foo.pointer_dereference.5`` is the property class. Used by
        # the evidence_tag suggestion in the prompt.
        prop = rejected_cex.failing_property or ""
        parts = prop.split(".")
        prop_class = "unknown"
        for p in reversed(parts):
            if not p.isdigit():
                prop_class = p
                break

        return _REFINE_PROMPT.format(
            fn_name=func_info.name,
            fn_signature=fn_signature,
            fn_body=(func_info.body or "(unavailable)")[:4000],
            pre_validity=current_spec.pre_validity or "(empty)",
            pre_protocol=current_spec.pre_protocol or "(empty)",
            postcondition=current_spec.postcondition or "(empty)",
            failing_property=prop or "(unknown)",
            witness_state=witness,
            key_concern=realism.key_concern,
            realism_reasoning=(realism.reasoning or "")[:1500],
            property_class=prop_class,
        )

    def parse(self, response: str) -> Optional[RefinementProposal]:
        if not response:
            return None
        # Reuse the existing parser — preserves identical behaviour for
        # every JSON envelope variant (fenced, prose-embedded,
        # unparseable). The parser returns RefinementProposal with
        # scope="cannot-refine" when the LLM declined; that's still a
        # valid parsed answer and is returned as-is.
        return _parse_refinement_response(response, fn_name="<via-agent>")
