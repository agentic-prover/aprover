"""
Phase 3: Bug Reporter for BMC-Agent.

Converts ValidationResult objects into structured BugReport records,
saves them to the artifact store, and generates human-readable summaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from bmc_agent.artifacts import ArtifactStore
from bmc_agent.cbmc import Counterexample
from bmc_agent.cex_validator import CExOutcome, ValidationResult
from bmc_agent.dynamic_validator import DynamicOutcome
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo
from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict

logger = get_logger("bug_reporter")


# ---------------------------------------------------------------------------
# Bug type classification
# ---------------------------------------------------------------------------

_BUG_TYPE_PATTERNS: list[tuple[str, str]] = [
    # Pattern → bug type
    ("overflow", "arithmetic"),
    ("underflow", "arithmetic"),
    ("division", "arithmetic"),
    ("div-by-zero", "arithmetic"),
    ("arith", "arithmetic"),
    ("null-pointer", "memory_safety"),
    ("null_pointer", "memory_safety"),
    ("null-deref", "memory_safety"),
    ("pointer", "memory_safety"),
    ("array-bounds", "memory_safety"),
    ("array_bounds", "memory_safety"),
    ("out-of-bounds", "memory_safety"),
    ("out_of_bounds", "memory_safety"),
    ("bounds", "memory_safety"),
    ("memory", "memory_safety"),
    ("buffer", "memory_safety"),
    ("postcondition", "semantic"),
    ("post", "semantic"),
    ("assertion", "semantic"),
    ("assert", "semantic"),
    ("protocol", "api_protocol"),
    ("api", "api_protocol"),
    ("sequence", "api_protocol"),
    ("order", "api_protocol"),
]


def _classify_bug_type(failing_property: str) -> str:
    """Classify the bug type from the CBMC property name."""
    prop_lower = failing_property.lower()
    for pattern, bug_type in _BUG_TYPE_PATTERNS:
        if pattern in prop_lower:
            return bug_type
    return "semantic"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BugReport:
    """A structured report of a confirmed (or likely) bug."""

    driver_name: str
    function_name: str
    bug_type: str            # "memory_safety", "api_protocol", "arithmetic", "semantic"
    violated_property: str
    counterexample: Counterexample
    call_chain: list[str]
    reproducer: str | None        # C code that triggers the bug
    reasoning_trail: str          # step-by-step explanation
    confidence: str               # "confirmed_dynamic" | "confirmed_system_entry" | "confirmed_bmc" | "likely"
    cex_outcome: CExOutcome | None = None
    dynamic_outcome: DynamicOutcome | None = None
    dynamic_signal: str | None = None
    realism_check: RealismCheckResult | None = None  # None when check is disabled

    def to_dict(self) -> dict:
        return {
            "driver_name": self.driver_name,
            "function_name": self.function_name,
            "bug_type": self.bug_type,
            "violated_property": self.violated_property,
            "counterexample": {
                "failing_property": self.counterexample.failing_property,
                "variable_assignments": self.counterexample.variable_assignments,
                "trace": self.counterexample.trace,
            },
            "call_chain": self.call_chain,
            "reproducer": self.reproducer,
            "reasoning_trail": self.reasoning_trail,
            "confidence": self.confidence,
            "cex_outcome": self.cex_outcome.value if self.cex_outcome else None,
            "dynamic_outcome": self.dynamic_outcome.value if self.dynamic_outcome else None,
            "dynamic_signal": self.dynamic_signal,
            "realism_check": self.realism_check.to_dict() if self.realism_check else None,
        }


# ---------------------------------------------------------------------------
# BugReporter
# ---------------------------------------------------------------------------


class BugReporter:
    """Creates BugReport objects and saves them to the artifact store."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        self._reports: list[BugReport] = []
        self._latent_reports: list[BugReport] = []
        self._unresolved: list = []

    def create_report(
        self,
        validation_result: ValidationResult,
        func: FunctionInfo,
        realism_check: "RealismCheckResult | None" = None,
    ) -> BugReport:
        """
        Create a BugReport from a ValidationResult.

        Only call this when validation_result.is_real_bug is True.

        Parameters
        ----------
        realism_check:
            Optional result from the RealismChecker.  When the verdict is
            UNREALISTIC the confidence tier is downgraded to "unlikely" so
            the finding is still reported but clearly flagged.
        """
        cex = validation_result.counterexample
        bug_type = _classify_bug_type(cex.failing_property)

        # Determine confidence tier (highest wins):
        #   confirmed_dynamic       — runtime fault observed by GCC harness (Stage 3)
        #   confirmed_system_entry  — full call chain traced back to a system entry
        #                             point (no-caller function) via CBMC reachability
        #   confirmed_bmc           — at least one immediate caller can reach the CEx
        #                             state, but chain to system entry not fully traced
        #   likely                  — over-refinement guard triggered; assumed real bug
        #   unlikely                — realism checker judged UNREALISTIC
        dynamic = validation_result.dynamic_result
        if dynamic and dynamic.outcome == DynamicOutcome.CONFIRMED:
            confidence = "confirmed_dynamic"
        elif getattr(validation_result, "system_entry_reached", False):
            confidence = "confirmed_system_entry"
        elif validation_result.caller_path:
            confidence = "confirmed_bmc"
        elif "over-refined" in validation_result.reasoning.lower():
            confidence = "likely"
        else:
            confidence = "confirmed_bmc"

        # Defensive guard: never claim confirmed_dynamic without an actual
        # CONFIRMED dynamic outcome AT WRITE TIME. The historical bug was
        # bug_report.json showing confidence=confirmed_dynamic alongside
        # classification.dynamic_result.outcome=not_triggered (probably from
        # iteration ordering or stale state). If dynamic is missing, skipped,
        # or anything other than CONFIRMED, fall back to the static tier.
        if confidence == "confirmed_dynamic":
            actual = dynamic.outcome if dynamic else None
            if actual != DynamicOutcome.CONFIRMED:
                logger.warning(
                    "Downgrading 'confirmed_dynamic' for '%s' — dynamic outcome "
                    "is %s, not CONFIRMED; falling back to static tier",
                    func.name, actual,
                )
                if getattr(validation_result, "system_entry_reached", False):
                    confidence = "confirmed_system_entry"
                elif validation_result.caller_path:
                    confidence = "confirmed_bmc"
                else:
                    confidence = "confirmed_bmc"

        # Realism check downgrade: UNREALISTIC → "unlikely"
        # confirmed_dynamic is immune only when the crash came from a source-level
        # property (e.g. overflow, null-deref, OOB).  When the failing property is
        # main.assertion.N the crash was caused by a harness-injected assert() that
        # encodes the LLM-generated postcondition — that is harness noise, not ground
        # truth, and the realism checker must be allowed to filter it.
        _harness_assertion = cex.failing_property.startswith("main.assertion")

        # Internal-helper immunity gate (Phase 1). A confirmed_dynamic crash is
        # trustworthy when an attacker can actually drive the function to the
        # faulting state. For a ``static`` (internal-linkage) helper whose crash
        # was NEVER traced to a system entry point, the runtime fault was produced
        # by the unit-level harness feeding NONDET arguments the function's real
        # in-tree callers never pass (e.g. a panic-screen drawing helper handed
        # base_y=INT_MAX, or a formatter handed an undersized buffer). Those are
        # harness artifacts, not attacker-reachable bugs; immunity must NOT shield
        # them from the realism auditor. Public functions (the attack surface) and
        # any crash with a traced system-entry path keep full immunity, so genuine
        # confirmed_dynamic bugs (e.g. vfs_open_handle) are never affected.
        _internal_unreachable = (
            getattr(getattr(func, "signature", None), "is_static", False)
            and not getattr(validation_result, "system_entry_reached", False)
        )
        _immune = (
            confidence == "confirmed_dynamic"
            and not _harness_assertion
            and not _internal_unreachable
        )
        if _internal_unreachable and confidence == "confirmed_dynamic" and not _harness_assertion:
            logger.info(
                "confirmed_dynamic immunity SUSPENDED for '%s': static internal "
                "helper with no system-entry-reachable path — realism may downgrade "
                "(unit-harness nondet-arg artifact, not attacker-reachable)",
                func.name,
            )
        if (
            realism_check is not None
            and realism_check.verdict == RealismVerdict.UNREALISTIC
            and realism_check.llm_confidence in ("high", "medium")
            and not _immune
        ):
            confidence = "unlikely"
            logger.info(
                "Confidence downgraded to 'unlikely' for '%s': %s",
                func.name, realism_check.key_concern[:100],
            )
        elif _immune and realism_check is not None and realism_check.verdict == RealismVerdict.UNREALISTIC:
            logger.info(
                "Realism downgrade suppressed for '%s' (confirmed_dynamic is immune): %s",
                func.name, realism_check.key_concern[:100],
            )

        # Build reasoning trail
        reasoning_parts: list[str] = [
            f"Function '{validation_result.function_name}' failed BMC verification.",
            f"Failing property: {cex.failing_property}",
            f"Bug type classified as: {bug_type}",
            f"Call chain: {' → '.join(validation_result.caller_path) or 'N/A'}",
            f"Validation reasoning: {validation_result.reasoning}",
        ]
        if cex.variable_assignments:
            va_str = ", ".join(f"{k}={v}" for k, v in cex.variable_assignments.items())
            reasoning_parts.append(f"Counterexample state: {va_str}")
        if cex.trace:
            reasoning_parts.append(f"Trace (first 5 steps):\n  " + "\n  ".join(cex.trace[:5]))

        reasoning_trail = "\n".join(reasoning_parts)

        dynamic = validation_result.dynamic_result
        report = BugReport(
            driver_name="",  # filled in by save_report
            function_name=validation_result.function_name,
            bug_type=bug_type,
            violated_property=cex.failing_property,
            counterexample=cex,
            call_chain=validation_result.caller_path,
            reproducer=validation_result.system_entry_input,
            reasoning_trail=reasoning_trail,
            confidence=confidence,
            cex_outcome=validation_result.outcome,
            dynamic_outcome=dynamic.outcome if dynamic else None,
            dynamic_signal=dynamic.signal_name if dynamic else None,
            realism_check=realism_check,
        )
        return report

    def save_report(self, report: BugReport, driver_name: str) -> None:
        """Save a bug report to the artifact store and the in-memory list."""
        report.driver_name = driver_name
        self._reports.append(report)

        try:
            self.store.save_bug_report(
                driver=driver_name,
                function=report.function_name,
                report=report.to_dict(),
            )
            logger.info(
                "Saved bug report for '%s' in driver '%s'",
                report.function_name,
                driver_name,
            )
        except Exception as exc:
            logger.warning("Failed to save bug report: %s", exc)

    def save_latent_report(self, report: BugReport, driver_name: str) -> None:
        """Save a LATENT bug report — panic reachable on the pub API but no
        in-tree caller produces the state. Routed to a separate
        ``latent_report.json`` file so triage can distinguish
        reachable-now from future-caller risk."""
        report.driver_name = driver_name
        if not hasattr(self, "_latent_reports"):
            self._latent_reports = []
        self._latent_reports.append(report)
        try:
            self.store.save_latent_report(
                driver=driver_name,
                function=report.function_name,
                report=report.to_dict(),
            )
            logger.info(
                "Saved LATENT report for '%s' in driver '%s'",
                report.function_name, driver_name,
            )
        except Exception as exc:
            logger.warning("Failed to save latent report: %s", exc)

    def generate_summary(self, driver_name: str) -> str:
        """Generate a human-readable summary of all bugs found for *driver_name*."""
        reports = [r for r in self._reports if r.driver_name == driver_name]

        if not reports:
            return f"No bugs found for driver '{driver_name}'."

        lines: list[str] = [
            f"=== AMC Bug Report Summary: {driver_name} ===",
            f"Total bugs found: {len(reports)}",
            "",
        ]

        # Group by bug type
        by_type: dict[str, list[BugReport]] = {}
        for r in reports:
            by_type.setdefault(r.bug_type, []).append(r)

        for btype, breps in sorted(by_type.items()):
            lines.append(f"[{btype.upper()}] ({len(breps)} bug(s)):")
            for r in breps:
                lines.append(f"  Function: {r.function_name}")
                lines.append(f"  Property: {r.violated_property}")
                lines.append(f"  Confidence: {r.confidence}")
                if r.dynamic_outcome is not None:
                    dyn_str = r.dynamic_outcome.value
                    if r.dynamic_signal:
                        dyn_str += f" signal={r.dynamic_signal}"
                    lines.append(f"  Dynamic: {dyn_str}")
                if r.call_chain:
                    lines.append(f"  Call chain: {' → '.join(r.call_chain)}")
                if r.counterexample.variable_assignments:
                    va = ", ".join(
                        f"{k}={v}"
                        for k, v in r.counterexample.variable_assignments.items()
                    )
                    lines.append(f"  State: {va}")
                if r.reproducer:
                    snippet = r.reproducer[:200].replace("\n", " ")
                    lines.append(f"  Reproducer: {snippet}...")
                lines.append("")

        return "\n".join(lines)
