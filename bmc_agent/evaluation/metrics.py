"""
Metrics collection and aggregation for BMC-Agent evaluation.

Collects per-driver metrics and computes evaluation summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCVerdict
    from bmc_agent.bug_reporter import BugReport
    from bmc_agent.cex_validator import ValidationResult
    from bmc_agent.evaluation.baselines import BaselineResult
    from bmc_agent.pipeline import PropagationEvent
    from bmc_agent.spec import Spec


@dataclass
class DriverMetrics:
    """Metrics for a single driver/source file."""

    driver_name: str
    total_functions: int
    functions_specified: int          # successfully got a non-fallback spec
    functions_checked: int            # successfully ran BMC
    functions_verified: int           # BMC returned verified
    counterexamples_found: int
    real_bugs_confirmed: int
    spurious_cex_count: int
    false_positive_rate: float        # spurious / total counterexamples
    refinement_iterations: list[int]  # one entry per spurious cex
    avg_refinement_iters: float
    spec_coverage: float              # functions_specified / total_functions
    runtime_seconds: float
    token_cost: int                   # total tokens used (if available)
    bugs_by_type: dict[str, int] = field(default_factory=dict)
    # {"memory_safety": 2, "arithmetic": 1, ...}
    unresolved_cex_count: int = 0
    cex_resolution_rate: float = 0.0
    over_refinement_detections: int = 0
    spec_quality_score: float = 0.0
    # V3 RQ3: compositional propagation
    propagation_events: list["PropagationEvent"] = field(default_factory=list)


@dataclass
class EvaluationSummary:
    """Aggregate evaluation metrics across all drivers."""

    total_drivers: int
    total_functions: int
    total_bugs_found: int
    avg_false_positive_rate: float
    avg_spec_coverage: float
    avg_refinement_iters: float
    total_token_cost: int
    bugs_by_type: dict[str, int] = field(default_factory=dict)
    per_driver: list[DriverMetrics] = field(default_factory=list)

    # Comparison vs baselines
    amc_unique_bugs: int = 0
    baseline_unique_bugs: dict[str, int] = field(default_factory=dict)

    # V2 Track 1 metrics
    avg_cex_resolution_rate: float = 0.0
    total_over_refinement_detections: int = 0
    # V3 RQ3: compositional propagation metrics
    total_propagation_events: int = 0
    callers_with_changed_outcome: int = 0
    bugs_via_propagation: int = 0


class MetricsCollector:
    """Collects and aggregates evaluation metrics."""

    def __init__(self, store: "ArtifactStore") -> None:
        self.store = store

    def collect_driver_metrics(
        self,
        driver_name: str,
        specs: "dict[str, Spec]",
        verdicts: "dict[str, BMCVerdict]",
        validation_results: "list[ValidationResult]",
        bug_reports: "list[BugReport]",
        runtime: float,
        propagation_events: "list[PropagationEvent] | None" = None,
    ) -> DriverMetrics:
        """
        Compute metrics for a single driver run.

        Parameters
        ----------
        driver_name:    Name of the driver.
        specs:          Mapping fn_name → Spec from spec generation.
        verdicts:       Mapping fn_name → BMCVerdict from BMC engine.
        validation_results:
                        All ValidationResult objects from Phase 3.
        bug_reports:    Confirmed BugReport objects for this driver.
        runtime:        Wall-clock seconds for the full pipeline run.
        """
        from bmc_agent.cex_validator import CExOutcome

        total_functions = len(specs)
        functions_specified = _count_non_fallback_specs(specs)
        functions_checked = sum(
            1 for v in verdicts.values() if v.error is None or v.verified
        )
        functions_verified = sum(1 for v in verdicts.values() if v.verified)

        # Count counterexamples
        total_cexes = sum(len(v.counterexamples) for v in verdicts.values())
        real_bugs = len([r for r in validation_results if r.is_real_bug])
        spurious = len([r for r in validation_results if not r.is_real_bug])

        false_positive_rate = (spurious / total_cexes) if total_cexes > 0 else 0.0

        # Refinement iterations per spurious/unresolved cex
        refinement_iters: list[int] = [
            len(r.refinement_history)
            for r in validation_results
            if not r.is_real_bug
        ]
        avg_refinement = (
            sum(refinement_iters) / len(refinement_iters) if refinement_iters else 0.0
        )

        spec_coverage = (
            functions_specified / total_functions if total_functions > 0 else 0.0
        )

        # Bug type breakdown
        bugs_by_type: dict[str, int] = {}
        for report in bug_reports:
            btype = report.bug_type
            bugs_by_type[btype] = bugs_by_type.get(btype, 0) + 1

        # Token cost: sum from any spec that has token_cost attribute
        token_cost = 0
        for spec in specs.values():
            token_cost += getattr(spec, "token_cost", 0)

        # V2 Track 1: unresolved cex count
        unresolved_cex_count = len([
            r for r in validation_results
            if hasattr(r, "outcome") and r.outcome == CExOutcome.UNRESOLVED
        ])

        # V2 Track 1: cex resolution rate = resolved / total cexes validated
        total_validated = len(validation_results)
        resolved = total_validated - unresolved_cex_count
        cex_resolution_rate = (resolved / total_validated) if total_validated > 0 else 1.0

        # V2 Track 1: over-refinement detections
        over_refinement_detections = len([
            r for r in validation_results
            if hasattr(r, "over_refinement_rejected") and r.over_refinement_rejected
        ])

        # V2 Track 1: spec quality score = spec_coverage * cex_resolution_rate
        spec_quality_score = spec_coverage * cex_resolution_rate

        return DriverMetrics(
            driver_name=driver_name,
            total_functions=total_functions,
            functions_specified=functions_specified,
            functions_checked=functions_checked,
            functions_verified=functions_verified,
            counterexamples_found=total_cexes,
            real_bugs_confirmed=real_bugs,
            spurious_cex_count=spurious,
            false_positive_rate=false_positive_rate,
            refinement_iterations=refinement_iters,
            avg_refinement_iters=avg_refinement,
            spec_coverage=spec_coverage,
            runtime_seconds=runtime,
            token_cost=token_cost,
            bugs_by_type=bugs_by_type,
            unresolved_cex_count=unresolved_cex_count,
            cex_resolution_rate=cex_resolution_rate,
            over_refinement_detections=over_refinement_detections,
            spec_quality_score=spec_quality_score,
            propagation_events=list(propagation_events) if propagation_events else [],
        )

    def compute_summary(
        self,
        all_metrics: "list[DriverMetrics]",
        baseline_results: "dict[str, list[BaselineResult]]",
    ) -> EvaluationSummary:
        """
        Compute an aggregate EvaluationSummary across all driver metrics
        and compare against baseline results.

        Parameters
        ----------
        all_metrics:
            List of DriverMetrics, one per driver.
        baseline_results:
            Mapping baseline_name → list of BaselineResult (one per driver).
        """
        if not all_metrics:
            return EvaluationSummary(
                total_drivers=0,
                total_functions=0,
                total_bugs_found=0,
                avg_false_positive_rate=0.0,
                avg_spec_coverage=0.0,
                avg_refinement_iters=0.0,
                total_token_cost=0,
            )

        total_drivers = len(all_metrics)
        total_functions = sum(m.total_functions for m in all_metrics)
        total_bugs = sum(m.real_bugs_confirmed for m in all_metrics)
        avg_fp = sum(m.false_positive_rate for m in all_metrics) / total_drivers
        avg_cov = sum(m.spec_coverage for m in all_metrics) / total_drivers
        avg_ref = sum(m.avg_refinement_iters for m in all_metrics) / total_drivers
        total_tokens = sum(m.token_cost for m in all_metrics)

        # Aggregate bug types
        bugs_by_type: dict[str, int] = {}
        for m in all_metrics:
            for btype, count in m.bugs_by_type.items():
                bugs_by_type[btype] = bugs_by_type.get(btype, 0) + count

        # Compute amc_unique_bugs: bugs BMC-Agent found that no baseline found
        amc_bug_set = _amc_bug_set(all_metrics)
        baseline_bug_sets: dict[str, set[str]] = {}
        for bl_name, bl_results in baseline_results.items():
            bl_set: set[str] = set()
            for bl_res in bl_results:
                for b in bl_res.bugs_found:
                    bl_set.add(f"{bl_res.driver_name}:{b}")
            baseline_bug_sets[bl_name] = bl_set

        all_baseline_bugs: set[str] = set()
        for bl_set in baseline_bug_sets.values():
            all_baseline_bugs |= bl_set

        amc_unique = len(amc_bug_set - all_baseline_bugs)

        baseline_unique: dict[str, int] = {}
        for bl_name, bl_set in baseline_bug_sets.items():
            baseline_unique[bl_name] = len(bl_set - amc_bug_set)

        # V2 Track 1 aggregates
        avg_cex_resolution_rate = (
            sum(m.cex_resolution_rate for m in all_metrics) / total_drivers
        )
        total_over_refinement_detections = sum(
            m.over_refinement_detections for m in all_metrics
        )

        # V3 RQ3: compositional propagation aggregates
        all_events = [e for m in all_metrics for e in m.propagation_events]
        total_propagation_events = len(all_events)
        callers_with_changed_outcome = sum(
            len(e.outcome_changes) for e in all_events
        )
        bugs_via_propagation = sum(
            len(e.bugs_found_via_propagation) for e in all_events
        )

        return EvaluationSummary(
            total_drivers=total_drivers,
            total_functions=total_functions,
            total_bugs_found=total_bugs,
            avg_false_positive_rate=avg_fp,
            avg_spec_coverage=avg_cov,
            avg_refinement_iters=avg_ref,
            total_token_cost=total_tokens,
            bugs_by_type=bugs_by_type,
            per_driver=list(all_metrics),
            amc_unique_bugs=amc_unique,
            baseline_unique_bugs=baseline_unique,
            avg_cex_resolution_rate=avg_cex_resolution_rate,
            total_over_refinement_detections=total_over_refinement_detections,
            total_propagation_events=total_propagation_events,
            callers_with_changed_outcome=callers_with_changed_outcome,
            bugs_via_propagation=bugs_via_propagation,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_non_fallback_specs(specs: "dict[str, Spec]") -> int:
    """Count specs that are not fallback (trivially empty) specs."""
    count = 0
    for spec in specs.values():
        is_fallback = getattr(spec, "fallback", False)
        if not is_fallback and spec.precondition.strip() and spec.postcondition.strip():
            count += 1
    return count


def _amc_bug_set(all_metrics: "list[DriverMetrics]") -> set[str]:
    """Build a set of 'driver:bug_type' keys for all BMC-Agent-found bugs."""
    result: set[str] = set()
    for m in all_metrics:
        for btype, count in m.bugs_by_type.items():
            for i in range(count):
                result.add(f"{m.driver_name}:{btype}:{i}")
    return result
