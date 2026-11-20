"""
Report generator for AMC evaluation.

Generates per-driver and summary markdown reports suitable for
inclusion in a research paper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bug_reporter import BugReport
    from bmc_agent.evaluation.metrics import DriverMetrics, EvaluationSummary


class ReportGenerator:
    """Generates evaluation reports in markdown format."""

    def __init__(self, store: "ArtifactStore") -> None:
        self.store = store

    # ------------------------------------------------------------------
    # Per-driver report
    # ------------------------------------------------------------------

    def generate_driver_report(
        self,
        metrics: "DriverMetrics",
        bug_reports: "list[BugReport]",
    ) -> str:
        """Generate a markdown report for a single driver."""
        lines: list[str] = [
            f"# Driver Report: {metrics.driver_name}",
            "",
            "## Summary",
            "",
            f"- **Total functions**: {metrics.total_functions}",
            f"- **Functions with specs**: {metrics.functions_specified}",
            f"- **Spec coverage**: {metrics.spec_coverage * 100:.1f}%",
            f"- **Functions checked by BMC**: {metrics.functions_checked}",
            f"- **Functions verified**: {metrics.functions_verified}",
            f"- **Counterexamples found**: {metrics.counterexamples_found}",
            f"- **Real bugs confirmed**: {metrics.real_bugs_confirmed}",
            f"- **Spurious counterexamples**: {metrics.spurious_cex_count}",
            f"- **False positive rate**: {metrics.false_positive_rate * 100:.1f}%",
            f"- **Average refinement iterations**: {metrics.avg_refinement_iters:.2f}",
            f"- **Runtime**: {metrics.runtime_seconds:.2f}s",
            f"- **Token cost**: {metrics.token_cost:,}",
            "",
        ]

        # Bug type breakdown
        if metrics.bugs_by_type:
            lines += [
                "## Bug Type Breakdown",
                "",
                "| Bug Type | Count |",
                "|----------|-------|",
            ]
            for btype, count in sorted(metrics.bugs_by_type.items()):
                lines.append(f"| {btype} | {count} |")
            lines.append("")

        # Individual bug reports
        if bug_reports:
            lines += [
                "## Confirmed Bugs",
                "",
            ]
            for i, report in enumerate(bug_reports, 1):
                lines += [
                    f"### Bug {i}: `{report.function_name}`",
                    "",
                    f"- **Type**: {report.bug_type}",
                    f"- **Violated property**: `{report.violated_property}`",
                    f"- **Confidence**: {report.confidence}",
                ]
                if report.call_chain:
                    chain_str = " → ".join(report.call_chain)
                    lines.append(f"- **Call chain**: `{chain_str}`")
                if report.reasoning_trail:
                    lines += [
                        "",
                        "**Reasoning**:",
                        "",
                        "```",
                        report.reasoning_trail[:500],
                        "```",
                    ]
                if report.reproducer:
                    snippet = report.reproducer[:300]
                    lines += [
                        "",
                        "**Reproducer** (excerpt):",
                        "",
                        "```c",
                        snippet,
                        "```",
                    ]
                lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Summary report
    # ------------------------------------------------------------------

    def generate_summary_report(self, summary: "EvaluationSummary") -> str:
        """
        Generate a paper-ready markdown summary report.

        Includes overall results, per-driver table, bug type breakdown,
        and comparison against baselines.
        """
        lines: list[str] = [
            "# AMC Evaluation Summary",
            "",
            "## Overall Results",
            "",
            f"- Total drivers analyzed: {summary.total_drivers}",
            f"- Total functions: {summary.total_functions}",
            f"- Total bugs found: {summary.total_bugs_found}",
            f"- Average spec coverage: {summary.avg_spec_coverage * 100:.1f}%",
            f"- Average false positive rate: {summary.avg_false_positive_rate * 100:.1f}%",
            f"- Average refinement iterations: {summary.avg_refinement_iters:.2f}",
            f"- Total token cost: {summary.total_token_cost:,}",
            "",
            "## Per-Driver Results",
            "",
        ]

        # Per-driver table header
        lines += [
            "| Driver | Functions | Bugs | FP Rate | Coverage | Tokens |",
            "|--------|-----------|------|---------|----------|--------|",
        ]
        for m in summary.per_driver:
            fp_pct = f"{m.false_positive_rate * 100:.1f}%"
            cov_pct = f"{m.spec_coverage * 100:.1f}%"
            lines.append(
                f"| {m.driver_name} "
                f"| {m.total_functions} "
                f"| {m.real_bugs_confirmed} "
                f"| {fp_pct} "
                f"| {cov_pct} "
                f"| {m.token_cost:,} |"
            )

        lines.append("")

        # Bug type breakdown
        if summary.bugs_by_type:
            total_bugs = max(summary.total_bugs_found, 1)
            lines += [
                "## Bug Type Breakdown",
                "",
                "| Type | Count | % |",
                "|------|-------|---|",
            ]
            type_labels = {
                "memory_safety": "Memory Safety",
                "arithmetic": "Arithmetic",
                "semantic": "Semantic",
                "api_protocol": "API Protocol",
            }
            for btype, count in sorted(summary.bugs_by_type.items()):
                label = type_labels.get(btype, btype.replace("_", " ").title())
                pct = count / total_bugs * 100
                lines.append(f"| {label} | {count} | {pct:.1f}% |")
            lines.append("")

        # Comparison vs baselines
        lines += [
            "## Comparison vs Baselines",
            "",
            "| System | Bugs Found | Unique Bugs | FP Rate |",
            "|--------|-----------|-------------|---------|",
        ]

        # BMC-Agent row
        amc_fp = f"{summary.avg_false_positive_rate * 100:.1f}%"
        lines.append(
            f"| AMC (ours) "
            f"| {summary.total_bugs_found} "
            f"| {summary.amc_unique_bugs} "
            f"| {amc_fp} |"
        )

        # Baseline rows
        for bl_name, unique_count in sorted(summary.baseline_unique_bugs.items()):
            label = bl_name.replace("_", "-")
            lines.append(
                f"| {label} "
                f"| — "
                f"| {unique_count} "
                f"| — |"
            )

        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Save all reports
    # ------------------------------------------------------------------

    def save_reports(
        self,
        summary: "EvaluationSummary",
        all_metrics: "list[DriverMetrics]",
        all_bug_reports: "dict[str, list[BugReport]]",
    ) -> None:
        """
        Save all reports to the artifact directory.

        Writes:
        - {artifact_dir}/eval_summary.md
        - {artifact_dir}/eval_summary.json
        - {artifact_dir}/{driver_name}/report.md  (per driver)
        """
        base = Path(self.store.base_dir)
        base.mkdir(parents=True, exist_ok=True)

        # Summary markdown
        summary_md = self.generate_summary_report(summary)
        (base / "eval_summary.md").write_text(summary_md, encoding="utf-8")

        # Summary JSON
        summary_dict = {
            "total_drivers": summary.total_drivers,
            "total_functions": summary.total_functions,
            "total_bugs_found": summary.total_bugs_found,
            "avg_false_positive_rate": summary.avg_false_positive_rate,
            "avg_spec_coverage": summary.avg_spec_coverage,
            "avg_refinement_iters": summary.avg_refinement_iters,
            "total_token_cost": summary.total_token_cost,
            "bugs_by_type": summary.bugs_by_type,
            "amc_unique_bugs": summary.amc_unique_bugs,
            "baseline_unique_bugs": summary.baseline_unique_bugs,
            "per_driver": [
                {
                    "driver_name": m.driver_name,
                    "total_functions": m.total_functions,
                    "functions_specified": m.functions_specified,
                    "functions_checked": m.functions_checked,
                    "functions_verified": m.functions_verified,
                    "counterexamples_found": m.counterexamples_found,
                    "real_bugs_confirmed": m.real_bugs_confirmed,
                    "spurious_cex_count": m.spurious_cex_count,
                    "false_positive_rate": m.false_positive_rate,
                    "avg_refinement_iters": m.avg_refinement_iters,
                    "spec_coverage": m.spec_coverage,
                    "runtime_seconds": m.runtime_seconds,
                    "token_cost": m.token_cost,
                    "bugs_by_type": m.bugs_by_type,
                }
                for m in all_metrics
            ],
        }
        with (base / "eval_summary.json").open("w", encoding="utf-8") as fh:
            json.dump(summary_dict, fh, indent=2)

        # Per-driver reports
        for metrics in all_metrics:
            driver_dir = base / metrics.driver_name
            driver_dir.mkdir(parents=True, exist_ok=True)
            bug_reports = all_bug_reports.get(metrics.driver_name, [])
            driver_md = self.generate_driver_report(metrics, bug_reports)
            (driver_dir / "report.md").write_text(driver_md, encoding="utf-8")
