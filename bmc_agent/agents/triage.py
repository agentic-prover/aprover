"""``TriageAgent`` — independent second-opinion verdict on an UNRESOLVED CEx.

When the pipeline classifies a counterexample as ``UNRESOLVED`` (the
caller-chain trace + dyn-val signals don't agree, the spec-refiner
declined to over-tighten, the realism LLM had mixed signals), a human
still has to decide REAL_BUG vs FP. This agent automates that triage
step: given ALL available evidence (the CEx witness, the function
source, the call chain, the dyn-val result, the realism verdict, the
LLM-generated public-API reproducer), it produces a structured verdict
the human can spot-check or trust.

The agent is INTENTIONALLY independent of the rest of the pipeline:

  * It does NOT see the per-oracle verdicts the pipeline produced —
    instead it gets the raw signals and re-evaluates from scratch.
  * It uses its OWN routing role (``triage``) so you can dial a
    stronger model for this task without inflating other budgets.
  * It is post-hoc by default — runs over classification.json files
    on disk, not in-pipeline. Wiring into the pipeline as a Phase 3e
    after the spec-refiner gives up is a follow-up.

Returns a ``TriageVerdict`` dataclass with:
  * ``verdict``: one of REAL_BUG / LIKELY_FP / NEEDS_HUMAN
  * ``confidence``: low / medium / high
  * ``reasoning``: structured analysis (cited evidence)
  * ``fp_class`` (optional): which known FP shape this matches
    (caller-contract-slip, harness-over-permissive-pointer, …)

Routing: ``BMC_AGENT_LLM_TRIAGE_*`` env vars; falls back to default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient


class TriageVerdict(str, Enum):
    REAL_BUG = "real_bug"
    LIKELY_FP = "likely_fp"
    NEEDS_HUMAN = "needs_human"


@dataclass
class TriageResult:
    verdict: TriageVerdict
    confidence: str  # low / medium / high
    reasoning: str
    fp_class: Optional[str] = None
    raw_response: str = ""


_SYSTEM_PROMPT = (
    "You are an expert C verification engineer reviewing a CBMC "
    "counterexample. Your job is to give an INDEPENDENT verdict on "
    "whether the finding is a real bug in the source code or a "
    "false positive caused by harness construction / verification "
    "model limitations.\n\n"
    "You will receive:\n"
    "  * The function under test (source code)\n"
    "  * The CBMC counterexample witness (concrete or symbolic state)\n"
    "  * The CBMC harness that produced it\n"
    "  * The static caller-chain trace (reachability through callers)\n"
    "  * The dynamic-validation result (compile + run the public-API "
    "reproducer; CONFIRMED / NOT_TRIGGERED / INCONCLUSIVE / no-run)\n"
    "  * The realism-LLM verdict and reasoning (if available)\n"
    "  * The pipeline's own classification reasoning\n\n"
    "Think carefully and produce ONE of three verdicts:\n"
    "  * REAL_BUG  — high confidence the source has a real bug a real "
    "user could trigger via the public API; specify how to reproduce.\n"
    "  * LIKELY_FP — the CEx witness state is unreachable through real "
    "public-API call sequences; the harness over-constrains, the "
    "caller-contract is implicit-but-real, or CBMC's model added the "
    "freedom (nondet allocator returns, symbolic pointer offsets, "
    "inter-object pointer compares, unwinding-bound artifacts, etc.)\n"
    "  * NEEDS_HUMAN — the evidence is genuinely ambiguous; specify "
    "what you'd need to see to decide.\n\n"
    "Known FP CLASSES (use these tags in ``fp_class`` when matched):\n"
    "  * harness_pointer_offset_unconstrained — harness lets pointer "
    "parameter alias a symbolic offset that real callers never produce.\n"
    "  * caller_contract_slip — internal helper trusts an implicit "
    "size/state invariant maintained by the public caller (e.g. "
    "buffer pre-sized via size-precomputation routine).\n"
    "  * inter_object_pointer_compare — harness gives independent "
    "backing buffers for pointer params that real callers keep in "
    "the same buffer (C11 UB on inter-object compare).\n"
    "  * cbmc_recursion_unwind_artifact — CBMC's --unwind bound "
    "exceeded by symbolic input that real public-API inputs cannot "
    "construct.\n"
    "  * silent_ub_unreachable_offset — silent-UB property fires on "
    "a CBMC-chosen offset that the public API cannot drive.\n\n"
    "Respond with ONLY valid JSON, no markdown fences, no commentary:\n"
    "{\n"
    '  "verdict": "real_bug" | "likely_fp" | "needs_human",\n'
    '  "confidence": "low" | "medium" | "high",\n'
    '  "fp_class": "<one of the known classes above OR null>",\n'
    '  "reasoning": "<3-6 sentences citing specific evidence from the '
    "inputs — function source line, witness value, caller-chain "
    'point, dyn-val outcome, etc.>"\n'
    "}"
)


class TriageAgent(BaseAgent[TriageResult]):
    """Returns a ``TriageResult`` for an UNRESOLVED (or low-confidence
    REAL_BUG) CEx. Independent of the pipeline's oracles — re-evaluates
    from raw evidence.

    Routing: ``BMC_AGENT_LLM_TRIAGE_*`` env vars.
    """

    name = "triage"
    system_prompt = _SYSTEM_PROMPT

    def _llm_call_kwargs(self) -> dict:
        # Triage benefits from extended thinking — the LLM has to
        # weigh several streams of evidence. 6000-token budget mirrors
        # the realism check's deep-reasoning mode.
        return {"max_tokens": 6000, "thinking": False}

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
        lines: list[str] = []
        lines.append(f"=== FUNCTION: {function_name} ===")
        lines.append(f"=== CBMC PROPERTY VIOLATED: {cbmc_property} ===")
        lines.append("")
        lines.append("--- function source ---")
        lines.append("```c")
        lines.append((function_source or "(source not available)")[:4000])
        lines.append("```")
        lines.append("")
        lines.append("--- CBMC harness ---")
        lines.append("```c")
        lines.append((harness_source or "(harness not available)")[:3000])
        lines.append("```")
        lines.append("")
        lines.append("--- counterexample witness (function-relevant) ---")
        lines.append("```")
        lines.append((witness_text or "(no witness)")[:2500])
        lines.append("```")
        lines.append("")
        lines.append(f"--- static caller chain ---")
        lines.append(f"  {' → '.join(caller_path) if caller_path else '(no caller path)'}")
        lines.append(f"  system_entry_reached: {sys_entry_reached}")
        lines.append("")
        lines.append(f"--- dynamic validation ---")
        lines.append(f"  outcome: {dyn_outcome or 'no-dyn'}")
        if dyn_reasoning:
            lines.append(f"  reasoning: {dyn_reasoning[:400]}")
        if reproducer_source:
            lines.append("  reproducer (LLM-generated public-API harness):")
            lines.append("```c")
            lines.append(reproducer_source[:2500])
            lines.append("```")
        lines.append("")
        if realism_verdict:
            lines.append(f"--- realism LLM verdict ---")
            lines.append(f"  verdict: {realism_verdict}")
            if realism_reasoning:
                lines.append(f"  reasoning: {realism_reasoning[:600]}")
            lines.append("")
        lines.append("--- pipeline's classification reasoning ---")
        lines.append(f"  {(pipeline_reasoning or '(none)')[:1000]}")
        lines.append("")
        lines.append(
            "Given the above, deliver your INDEPENDENT verdict per "
            "the JSON schema in the system prompt."
        )
        return "\n".join(lines)

    def parse(self, response: str) -> Optional[TriageResult]:
        import json
        import re
        text = (response or "").strip()
        if not text:
            return None
        # Strip fenced markdown defensively.
        if text.startswith("```"):
            lines = text.splitlines()
            inner: list[str] = []
            in_fence = False
            for line in lines:
                if line.startswith("```"):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    inner.append(line)
            text = "\n".join(inner).strip()
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m is None:
                return None
            try:
                data = json.loads(m.group(0))
            except Exception:
                return None
        verdict_str = (data.get("verdict") or "").lower().strip()
        try:
            verdict = TriageVerdict(verdict_str)
        except Exception:
            return None
        confidence = (data.get("confidence") or "low").lower().strip()
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
        fp_class = data.get("fp_class")
        if fp_class in (None, "", "null"):
            fp_class = None
        return TriageResult(
            verdict=verdict,
            confidence=confidence,
            reasoning=str(data.get("reasoning", "")),
            fp_class=fp_class,
            raw_response=response,
        )
