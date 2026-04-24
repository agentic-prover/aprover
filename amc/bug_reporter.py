"""
Phase 3: Bug Reporter for GRACE.

Converts ValidationResult objects into structured BugReport records,
saves them to the artifact store, and generates human-readable summaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from amc.artifacts import ArtifactStore
from amc.cbmc import Counterexample
from amc.cex_validator import CExOutcome, ValidationResult
from amc.logger import get_logger
from amc.parser import FunctionInfo

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
    reproducer: str | None   # C code that triggers the bug
    reasoning_trail: str     # step-by-step explanation
    confidence: str          # "confirmed" | "likely" | "possible"
    cex_outcome: CExOutcome | None = None

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
        }


# ---------------------------------------------------------------------------
# BugReporter
# ---------------------------------------------------------------------------


class BugReporter:
    """Creates BugReport objects and saves them to the artifact store."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        self._reports: list[BugReport] = []
        self._unresolved: list = []

    def create_report(
        self,
        validation_result: ValidationResult,
        func: FunctionInfo,
    ) -> BugReport:
        """
        Create a BugReport from a ValidationResult.

        Only call this when validation_result.is_real_bug is True.
        """
        cex = validation_result.counterexample
        bug_type = _classify_bug_type(cex.failing_property)

        # Determine confidence:
        # - call chain found → "confirmed"
        # - no call chain but entry function → "confirmed"
        # - over-refinement triggered → "likely"
        if validation_result.caller_path:
            confidence = "confirmed"
        elif "over-refined" in validation_result.reasoning.lower():
            confidence = "likely"
        else:
            confidence = "confirmed"

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
