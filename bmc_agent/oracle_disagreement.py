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
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

logger = get_logger("oracle_disagreement")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class DisagreementKind(str, Enum):
    """The structural shape of an oracle disagreement.

    Future kinds we may add:

      * BMC_FAIL_REALISM_UNREAL_DYN_CONFIRMED  — realism missed, dyn-val
        is mechanical truth; promote
      * BMC_PASS_DYN_CONFIRMED                  — would only fire if we
        ever run dyn-val on clean verifications
    """

    #: The canonical case: BMC + realism both think the bug is reachable;
    #: the mechanical reproducer (compiled + linked against the real .so)
    #: ran cleanly without faulting.
    BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED = "bmc_fail_realism_real_dyn_not_triggered"

    #: A subtler case: dyn-val returned INCONCLUSIVE, but the reason is
    #: a ``// UNREPRODUCIBLE`` marker the LLM emitted instead of a
    #: real reproducer — meaning the LLM ITSELF couldn't write a
    #: public-API call sequence that reaches the CEx state. That's
    #: stronger evidence of caller-contract slip than NOT_TRIGGERED
    #: (the LLM looked, gave up, and said so). Triggered on the
    #: archive_match_owner_excluded triage where this exact shape
    #: was a struct-invariant FP that Phase 3d missed.
    BMC_FAIL_REALISM_REAL_REPRODUCER_UNREACHABLE = "bmc_fail_realism_real_reproducer_unreachable"


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

    reproducer_source = str(report.get("reproducer") or "")
    function_name = str(report.get("function_name") or "")
    violated_property = str(report.get("violated_property") or "")
    realism_reasoning = str(
        (report.get("realism_check") or {}).get("reasoning") or ""
    )[:1500]

    # Shape 1: BMC fail + realism REAL + dyn NOT_TRIGGERED
    # — the canonical case (mechanical run came up clean).
    if realism == "realistic" and dyn == "not_triggered":
        return DisagreementCase(
            kind=DisagreementKind.BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED,
            function_name=function_name,
            violated_property=violated_property,
            bmc_verdict="fail",
            realism_verdict=realism,
            dyn_outcome=dyn,
            realism_reasoning=realism_reasoning,
            reproducer_source=reproducer_source[:3000],
        )

    # Shape 2: BMC fail + realism REAL + dyn INCONCLUSIVE *and* the
    # reproducer is UNREPRODUCIBLE-marked. The dyn-val didn't produce
    # a verdict because the LLM ITSELF admitted (via the UNREPRODUCIBLE
    # marker) that the CEx state can't be reached from a public-API
    # call sequence. That's a strong "no real caller can produce
    # this" signal — caught by the cex_validator's
    # _reproducer_uses_public_api gate in 272b854, surfaced here for
    # Phase 3d diagnosis.
    if (
        realism == "realistic"
        and dyn == "inconclusive"
        and reproducer_source.lstrip().startswith("// UNREPRODUCIBLE")
    ):
        return DisagreementCase(
            kind=DisagreementKind.BMC_FAIL_REALISM_REAL_REPRODUCER_UNREACHABLE,
            function_name=function_name,
            violated_property=violated_property,
            bmc_verdict="fail",
            realism_verdict=realism,
            dyn_outcome=dyn,
            realism_reasoning=realism_reasoning,
            reproducer_source=reproducer_source[:3000],
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
    config: "Optional[Config]" = None,
) -> Optional[DiagnosisResult]:
    """Ask the LLM to diagnose a detected disagreement.

    v2: delegates to ``DisagreementDiagnoseAgent`` (the C2 agent
    abstraction). The function survives as a thin wrapper so existing
    callers (pipeline, tests) keep working unchanged. Returns None when
    the agent fails / produces an unparseable response — caller treats
    that the same as INCONCLUSIVE (no action taken).

    ``config`` is optional for back-compat with the old (case, llm)
    signature; when omitted, the agent is built with a synthesized
    Config that's adequate for stateless run() calls.
    """
    # Lazy import — agents package imports oracle_disagreement, so a
    # top-level import here would be circular.
    from bmc_agent.agents.disagreement import DisagreementDiagnoseAgent

    if config is None:
        from bmc_agent.config import Config
        config = Config(llm_api_key="agent-noconfig")

    agent = DisagreementDiagnoseAgent(config=config, llm=llm)
    result = agent.run(case=case)
    if not result.ok:
        if result.error:
            logger.warning(
                "Oracle-disagreement diagnosis failed for '%s': %s",
                case.function_name, result.error[:200],
            )
        return None
    return result.output


# ---------------------------------------------------------------------------
# Application (writes back to bug_report.json)
# ---------------------------------------------------------------------------


_CPROVER_ASSUME_WRAP_RE = re.compile(
    r"^\s*__CPROVER_assume\s*\(\s*(?P<inner>.+?)\s*\)\s*;?\s*$",
    re.DOTALL,
)


def _strip_cprover_assume_wrap(clause: str) -> str:
    """Strip a leading ``__CPROVER_assume(...)`` wrapper if present.

    The HARNESS_ENCODING diagnosis tends to return clauses already wrapped
    in ``__CPROVER_assume(...)``, while the learned-constraints store
    expects BARE clauses (its emit path wraps them at harness-gen time).
    Strip exactly one level of wrapping so we don't end up with
    ``__CPROVER_assume(__CPROVER_assume(...))`` after the emit pass.
    """
    if not clause:
        return clause
    m = _CPROVER_ASSUME_WRAP_RE.match(clause.strip())
    if m is None:
        return clause.strip()
    return m.group("inner").strip()


def persist_diagnosis_to_learned_constraints(
    config: "Config",
    function_name: str,
    diagnosis: DiagnosisResult,
    source_property: str = "",
) -> bool:
    """For actionable diagnoses (SPEC_REFINE / HARNESS_ENCODING), turn
    the LLM-proposed clause into a ``Remediation`` and persist it
    via ``LearnedConstraintsStore``. The harness-gen path's
    ``_emit_learned_clauses`` will pick it up on the next BMC run
    for this function.

    Returns True when something was newly persisted (caller can use
    this signal to add the function to the re-verification queue).
    """
    if diagnosis.verdict == DiagnosisVerdict.SPEC_REFINE:
        clause = _strip_cprover_assume_wrap(diagnosis.suggested_clause)
    elif diagnosis.verdict == DiagnosisVerdict.HARNESS_ENCODING:
        clause = _strip_cprover_assume_wrap(diagnosis.suggested_encoding)
    else:
        return False
    if not clause:
        return False

    # Skip the "feedback loop disabled" guard — Phase 3d's diagnoses
    # are themselves part of the feedback loop, and the user opted in
    # by enabling realism + dyn-val. Persistence is still confined to
    # the project's artifact_dir.
    try:
        from bmc_agent.feedback_loop import (
            LearnedConstraintsStore,
            Remediation,
            RemediationScope,
        )
    except Exception as exc:
        logger.warning(
            "Phase 3d persist failed (feedback_loop import error): %s", exc,
        )
        return False

    rem = Remediation(
        scope=RemediationScope.FUNCTION_SPEC,
        clause=clause,
        rationale=(
            f"Phase 3d oracle-disagreement diagnosis "
            f"({diagnosis.verdict.value}, confidence={diagnosis.confidence}): "
            f"{diagnosis.rationale[:300]}"
        ),
        confidence=diagnosis.confidence or "low",
    )
    try:
        store = LearnedConstraintsStore(getattr(config, "artifact_dir", "."))
        return store.record(function_name, rem, source_property=source_property)
    except Exception as exc:
        logger.warning(
            "Phase 3d persist failed (store error) for '%s': %s",
            function_name, exc,
        )
        return False


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
