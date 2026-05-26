"""
Three-oracle disagreement detection and diagnosis (Phase 3d).

The pipeline collects three independent signals on every counterexample:

  * BMC verdict          — the symbolic execution result (pass/fail)
  * Realism check        — LLM judgment about whether a real caller
                            could reach the CEx state (realistic /
                            unrealistic / uncertain)
  * Dynamic validation   — mechanical reproduction via real-linked
                            public API (confirmed / not_triggered /
                            inconclusive)

When all three agree, the verdict is well-supported. When they DISAGREE,
that disagreement is itself a signal: something in the pipeline is
wrong, and the LLM can often diagnose which oracle to trust.

The dominant actionable case:

  BMC fails + realism = REALISTIC + dyn-val = NOT_TRIGGERED

→ the harness allows BMC to reach a state the real public-API call
  doesn't actually reach. Either the spec PRE is too loose, the
  harness over-allocates / under-constrains some parameter, or the
  CBMC property check itself is over-cautious. A targeted LLM call
  given (harness summary, reproducer, body) can usually tell us
  which.

For the v1 in this module, the detector covers ONE case (the dominant
one above) and the diagnoser produces a structured remediation that
gets attached to bug_report.json as ``oracle_disagreement_diagnosis``.
Auto-application is a follow-up — for now the diagnoses are visible
diagnostics that the user / a later iteration can act on.

When the diagnosis verdict is ``property-fp`` (the LLM concludes the
BMC CEx is a false positive), the bug's confidence is automatically
downgraded to ``unlikely`` since both dyn-val and the diagnosis agree
the finding isn't real.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

from bmc_agent.logger import get_logger

if TYPE_CHECKING:
    from bmc_agent.llm import LLMClient

logger = get_logger("oracle_disagreement")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class DisagreementKind(str, Enum):
    """The structural shape of an oracle disagreement.

    Only the most-actionable shape is detected today; more shapes can
    be added incrementally without API changes. Future kinds we expect:

      * BMC_FAIL_REALISM_UNREAL_DYN_CONFIRMED  — realism missed, dyn-val
        is mechanical truth; promote
      * BMC_PASS_DYN_CONFIRMED                  — would only fire if we
        ever run dyn-val on clean verifications
    """

    BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED = "bmc_fail_realism_real_dyn_not_triggered"


@dataclass(frozen=True)
class DisagreementCase:
    kind: DisagreementKind
    function_name: str
    violated_property: str
    bmc_verdict: str            # "fail" — included for symmetry with future kinds
    realism_verdict: str        # "realistic" / "unrealistic" / "uncertain"
    dyn_outcome: str            # "confirmed" / "not_triggered" / "inconclusive" / "skipped"
    realism_reasoning: str = ""
    reproducer_source: str = ""


def detect_disagreement(report: dict) -> Optional[DisagreementCase]:
    """Inspect a single bug_report.report dict and decide whether it
    represents a three-oracle disagreement worth diagnosing.

    Returns None when:
      - dyn-val didn't run (skipped / no outcome recorded)
      - the oracles agree
      - the report lacks the fields we need

    The single shape we fire on today is BMC fail (implicit — every
    saved bug_report is from a failed verification) + realism REAL +
    dyn NOT_TRIGGERED. That's the disagreement that yields the most
    diagnostic value per LLM call (the three signals are maximally
    contradictory).
    """
    if not isinstance(report, dict):
        return None
    realism = (report.get("realism_check") or {}).get("verdict")
    if not realism:
        return None
    realism = str(realism).lower().strip()
    dyn = report.get("dynamic_outcome")
    if not dyn:
        return None
    dyn = str(dyn).lower().strip()

    # The actionable disagreement
    if realism == "realistic" and dyn == "not_triggered":
        return DisagreementCase(
            kind=DisagreementKind.BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED,
            function_name=str(report.get("function_name") or ""),
            violated_property=str(report.get("violated_property") or ""),
            bmc_verdict="fail",
            realism_verdict=realism,
            dyn_outcome=dyn,
            realism_reasoning=str(
                (report.get("realism_check") or {}).get("reasoning") or ""
            )[:1500],
            reproducer_source=str(report.get("reproducer") or "")[:3000],
        )
    return None


# ---------------------------------------------------------------------------
# Diagnosis (LLM call)
# ---------------------------------------------------------------------------


class DiagnosisVerdict(str, Enum):
    """What the LLM concluded about the disagreement.

    * SPEC_REFINE     — the precondition is too loose; tighten it. The
      LLM proposes the specific clause.
    * HARNESS_ENCODING — the harness models some parameter / global in
      a way real callers can't produce. Suggests the specific encoding
      change.
    * PROPERTY_FP     — the BMC property check is over-cautious (e.g.,
      reports overflow on saturated arithmetic the source intentionally
      uses). The CEx is a real false positive; downgrade.
    * INCONCLUSIVE    — LLM couldn't decide. No automatic action.
    """

    SPEC_REFINE = "spec_refine"
    HARNESS_ENCODING = "harness_encoding"
    PROPERTY_FP = "property_fp"
    INCONCLUSIVE = "inconclusive"


@dataclass
class DiagnosisResult:
    verdict: DiagnosisVerdict
    rationale: str = ""
    suggested_clause: str = ""      # populated for SPEC_REFINE
    suggested_encoding: str = ""    # populated for HARNESS_ENCODING
    confidence: str = ""            # "high" / "medium" / "low"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "rationale": self.rationale,
            "suggested_clause": self.suggested_clause,
            "suggested_encoding": self.suggested_encoding,
            "confidence": self.confidence,
        }


_DISAGREEMENT_PROMPT = """\
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
- Dynamic validation:        NOT_TRIGGERED — the LLM-generated
                             reproducer (which uses only the project's
                             public API) compiled, linked against the
                             real library, and ran to completion
                             without faulting.

This is a structured disagreement: BMC and realism say the bug is
reachable from real callers; the actual mechanical run with a real
reproducer says it isn't.

=== REALISM REASONING (truncated) ===
{realism_reasoning}

=== REPRODUCER (the one that DID NOT fault) ===
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


_SYSTEM_PROMPT = (
    "You are a formal verification expert diagnosing a three-oracle "
    "disagreement in a C verification pipeline. Return only valid JSON."
)


def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of an LLM response (handles fenced /
    prose-embedded forms)."""
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


def diagnose(
    case: DisagreementCase,
    llm: "LLMClient",
) -> Optional[DiagnosisResult]:
    """Ask the LLM to diagnose a detected disagreement. Returns None
    when the LLM call fails / produces an unparseable response — caller
    treats that the same as INCONCLUSIVE (no action taken).
    """
    from bmc_agent.llm import LLMError

    prompt = _DISAGREEMENT_PROMPT.format(
        function_name=case.function_name or "(unknown)",
        violated_property=case.violated_property or "(unknown)",
        realism_reasoning=(case.realism_reasoning or "(none recorded)")[:1500],
        reproducer_source=(case.reproducer_source or "(no reproducer)")[:3000],
    )
    try:
        response = llm.complete(
            _SYSTEM_PROMPT, prompt, role="realism",
        )
    except LLMError as exc:
        logger.warning(
            "Oracle-disagreement diagnosis LLM call failed for '%s': %s",
            case.function_name, exc,
        )
        return None

    data = _extract_json(response or "")
    if not data:
        return None
    raw_verdict = str(data.get("verdict") or "").lower().strip()
    try:
        verdict = DiagnosisVerdict(raw_verdict)
    except ValueError:
        verdict = DiagnosisVerdict.INCONCLUSIVE
    return DiagnosisResult(
        verdict=verdict,
        rationale=str(data.get("rationale") or "")[:2000],
        suggested_clause=str(data.get("suggested_clause") or "")[:400],
        suggested_encoding=str(data.get("suggested_encoding") or "")[:400],
        confidence=str(data.get("confidence") or "").lower().strip(),
    )


# ---------------------------------------------------------------------------
# Application (writes back to bug_report.json)
# ---------------------------------------------------------------------------


def apply_diagnosis(report: dict, diagnosis: DiagnosisResult) -> dict:
    """Decorate ``report`` (the inner ``report`` dict from bug_report.json)
    with the diagnosis and, for ``property_fp``, downgrade confidence
    to ``unlikely``. Returns the modified report (in-place).

    SPEC_REFINE / HARNESS_ENCODING diagnoses are ATTACHED only — not
    auto-applied. A future iteration can wire them into spec_refiner /
    harness regeneration; for now the diagnosis surfaces visibly so a
    human (or auto-application logic) can act on it.
    """
    report["oracle_disagreement_diagnosis"] = diagnosis.to_dict()

    if diagnosis.verdict == DiagnosisVerdict.PROPERTY_FP:
        original = report.get("confidence")
        downgrade_from = {"confirmed_dynamic", "confirmed_system_entry",
                          "confirmed_bmc", "realistic"}
        if original in downgrade_from:
            report["confidence"] = "unlikely"
            note = (
                f"\n\n[ORACLE-DISAGREEMENT] Three-oracle diagnosis "
                f"flagged this as PROPERTY_FP "
                f"(confidence={diagnosis.confidence}): "
                f"{diagnosis.rationale[:400]}. "
                f"BMC reported a violation that the mechanical "
                f"reproducer could not actually trigger; the LLM "
                f"diagnosis confirms the property check is over-cautious. "
                f"Confidence downgraded from '{original}' to 'unlikely'."
            )
            report["reasoning_trail"] = (
                (report.get("reasoning_trail") or "") + note
            )
            logger.warning(
                "Oracle disagreement on '%s' diagnosed as PROPERTY_FP "
                "(%s confidence) — downgrading from '%s' to 'unlikely'",
                report.get("function_name", "?"),
                diagnosis.confidence,
                original,
            )
    return report
