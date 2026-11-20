"""``DisagreementDiagnoseAgent`` — Phase 3d's three-oracle disagreement
diagnoser, as the first concrete BaseAgent implementation.

Pre-existing functionality (in ``oracle_disagreement.diagnose``):
  1. Build a prompt describing the BMC/realism/dyn-val disagreement
  2. Call the LLM with role="disagreement_diagnose"
  3. Parse the JSON response into a ``DiagnosisResult``
  4. Tolerate fenced markdown / prose-embedded JSON / unparseable input

The standalone function becomes a thin wrapper around this agent so
existing callers (and the integration tests in test_oracle_disagreement.py)
keep working unchanged.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.oracle_disagreement import (
    DiagnosisResult,
    DiagnosisVerdict,
    DisagreementCase,
)

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient


_PROMPT_TEMPLATE = """\
The verification pipeline ran three independent oracles on a function
and got contradictory results. Your job is to diagnose which oracle
is wrong and propose a concrete fix.

=== FUNCTION ===
{function_name}

=== FAILING PROPERTY ===
{violated_property}

=== ORACLE SIGNALS ===
- BMC (symbolic execution):  FAILED — reported a CEx for the above
                             property.
- Realism check (LLM):       REALISTIC — concluded a real public-API
                             caller could reach the CEx state.
- Dynamic validation:        Either NOT_TRIGGERED (the LLM-generated
                             reproducer compiled, linked against the
                             real library, and ran without faulting),
                             OR the LLM emitted a ``// UNREPRODUCIBLE``
                             marker — i.e. it tried to write a
                             public-API reproducer for the CEx state
                             and admitted it couldn't construct one.
                             The reproducer block below shows which.

This is a structured disagreement: BMC and realism say the bug is
reachable from real callers; the mechanical / LLM-side evidence
says it isn't.

=== REALISM REASONING (truncated) ===
{realism_reasoning}

=== REPRODUCER (NOT_TRIGGERED case: the one that didn't fault;
                UNREPRODUCIBLE case: the marker + LLM's explanation) ===
```c
{reproducer_source}
```

=== POSSIBLE CAUSES ===
Pick exactly ONE:

  (a) SPEC_REFINE — the function's precondition is too loose. BMC
      explored a state real callers don't actually produce, but the
      reproducer obeys the implicit contract and so doesn't reproduce
      the fault. Fix: add a precondition clause that excludes the
      witness state. Required field: suggested_clause (one C boolean
      expression in the DSL: !null / valid / valid_string / valid_range
      / in_bounds / no_overflow).

  (b) HARNESS_ENCODING — the CBMC harness models some parameter or
      global in a way no real call can produce (e.g. a struct field
      with arbitrary nondet bytes when real-API initialisation
      guarantees a specific value). Fix: a targeted assumption in the
      harness. Required field: suggested_encoding (a __CPROVER_assume
      clause or buffer-init description).

  (c) PROPERTY_FP — the BMC property check itself flagged something
      the source intentionally permits (e.g. saturated arithmetic,
      defensive truncation). The CEx is a true BMC false positive;
      downgrade the finding. No clause needed.

  (d) INCONCLUSIVE — you genuinely can't tell which oracle is wrong
      from the information given. No fix applied.

=== OUTPUT FORMAT (JSON only) ===
{{
  "verdict": "spec_refine" | "harness_encoding" | "property_fp" | "inconclusive",
  "rationale": "<one paragraph explaining which oracle is wrong and why>",
  "suggested_clause": "<C boolean expression — only for spec_refine>",
  "suggested_encoding": "<__CPROVER_assume(...) or buffer-init line — only for harness_encoding>",
  "confidence": "high" | "medium" | "low"
}}
"""


def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of an LLM response (handles fenced /
    prose-embedded forms). Returns None when no valid JSON found."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        inner: list[str] = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                inner.append(line)
        t = "\n".join(inner).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m is None:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


class DisagreementDiagnoseAgent(BaseAgent[DiagnosisResult]):
    """Diagnoses three-oracle disagreements (BMC fail + realism realistic
    + dyn-val not_triggered) and proposes a spec / harness / property
    fix.

    Routing: ``BMC_AGENT_LLM_DISAGREEMENT_DIAGNOSE_*`` env vars control
    the backbone model. Falls back to the global default.
    """

    name = "disagreement_diagnose"
    system_prompt = (
        "You are a formal verification expert diagnosing a three-oracle "
        "disagreement in a C verification pipeline. Return only valid JSON."
    )

    def build_prompt(self, *, case: DisagreementCase, **_: Any) -> str:
        return _PROMPT_TEMPLATE.format(
            function_name=case.function_name or "(unknown)",
            violated_property=case.violated_property or "(unknown)",
            realism_reasoning=(
                case.realism_reasoning or "(none recorded)"
            )[:1500],
            reproducer_source=(
                case.reproducer_source or "(no reproducer)"
            )[:3000],
        )

    def parse(self, response: str) -> Optional[DiagnosisResult]:
        data = _extract_json(response)
        if not data:
            return None
        raw_verdict = str(data.get("verdict") or "").lower().strip()
        try:
            verdict = DiagnosisVerdict(raw_verdict)
        except ValueError:
            # Defensive: unknown verdict from LLM → INCONCLUSIVE so we
            # at least record the response without crashing the pipeline.
            verdict = DiagnosisVerdict.INCONCLUSIVE
        return DiagnosisResult(
            verdict=verdict,
            rationale=str(data.get("rationale") or "")[:2000],
            suggested_clause=str(data.get("suggested_clause") or "")[:400],
            suggested_encoding=str(data.get("suggested_encoding") or "")[:400],
            confidence=str(data.get("confidence") or "").lower().strip(),
        )
