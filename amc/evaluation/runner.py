"""
Evaluation runner for GRACE.

Runs GRACE and baselines on a corpus of C programs and aggregates results.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from amc.evaluation.corpus import Corpus
    from amc.evaluation.metrics import EvaluationSummary

from amc.config import Config
from amc.evaluation.baselines import BaselineResult, CBMCAloneBaseline
from amc.logger import get_logger
from amc.pipeline import AMCPipeline

logger = get_logger("evaluation.runner")


class EvaluationRunner:
    """
    Orchestrates evaluation of GRACE and baselines on a corpus.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def run_corpus(
        self,
        corpus: "Corpus",
        output_dir: str,
        run_baselines: bool = True,
    ) -> "EvaluationSummary":
        """
        Run GRACE and optionally baselines on all corpus entries.

        For each entry:
          1. Run full AMC pipeline
          2. Run CBMC-alone baseline (if run_baselines=True)
          3. Run GRACE-ablation baseline (if run_baselines=True)
          4. Collect per-driver metrics
          5. Generate per-driver report

        Then generates a summary report.

        Returns the aggregated EvaluationSummary.
        """
        from amc.artifacts import ArtifactStore
        from amc.bmc_engine import BMCVerdict
        from amc.bug_reporter import BugReport
        from amc.cex_validator import ValidationResult
        from amc.evaluation.metrics import DriverMetrics, MetricsCollector
        from amc.evaluation.report import ReportGenerator
        from amc.spec import Spec

        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        eval_config = Config(
            llm_model=self.config.llm_model,
            llm_api_key=self.config.llm_api_key,
            llm_base_url=self.config.llm_base_url,
            cbmc_path=self.config.cbmc_path,
            cbmc_unwind=self.config.cbmc_unwind,
            cbmc_timeout=self.config.cbmc_timeout,
            artifact_dir=str(out_path / "artifacts"),
            max_spec_retries=self.config.max_spec_retries,
            max_refinement_iters=self.config.max_refinement_iters,
            batch_size=self.config.batch_size,
        )

        store = ArtifactStore(eval_config.artifact_dir)
        collector = MetricsCollector(store)
        reporter = ReportGenerator(store)

        entries = corpus.load()
        logger.info("Loaded %d corpus entries from %s", len(entries), corpus.corpus_dir)

        all_metrics: list[DriverMetrics] = []
        all_bug_reports: dict[str, list[BugReport]] = {}
        baseline_results: dict[str, list[BaselineResult]] = {
            "cbmc_alone": [],
            "grace_ablation": [],
        }

        for entry in entries:
            driver_name = entry.name
            source_file = entry.source_file

            logger.info("=== Evaluating: %s (%s) ===", driver_name, source_file)

            # ---- Step 1: Run full AMC pipeline ----
            pipeline = AMCPipeline(eval_config)
            grace_start = time.monotonic()
            grace_specs: dict[str, Spec] = {}
            grace_verdicts: dict[str, BMCVerdict] = {}
            grace_validations: list[ValidationResult] = []
            grace_bugs: list[BugReport] = []

            try:
                # Instrument pipeline to capture intermediate results
                original_check_all = pipeline.bmc_engine.check_all
                original_validate = pipeline.validator.validate

                captured_specs: dict[str, Spec] = {}
                captured_verdicts: dict[str, BMCVerdict] = {}
                captured_validations: list[ValidationResult] = []

                def _patched_check_all(funcs, specs, parsed_file, driver_name_arg):  # type: ignore[no-untyped-def]
                    captured_specs.update(specs)
                    result = original_check_all(funcs, specs, parsed_file, driver_name_arg)
                    captured_verdicts.update(result)
                    return result

                def _patched_validate(**kwargs):  # type: ignore[no-untyped-def]
                    result = original_validate(**kwargs)
                    captured_validations.append(result)
                    return result

                pipeline.bmc_engine.check_all = _patched_check_all  # type: ignore[method-assign]
                pipeline.validator.validate = _patched_validate  # type: ignore[method-assign]

                grace_bugs = pipeline.run(
                    source_file=source_file,
                    driver_name=driver_name,
                )
                grace_specs = captured_specs
                grace_verdicts = captured_verdicts
                grace_validations = captured_validations

            except Exception as exc:
                logger.error("AMC pipeline failed for '%s': %s", driver_name, exc)

            grace_runtime = time.monotonic() - grace_start
            all_bug_reports[driver_name] = grace_bugs

            # ---- Step 2: Collect metrics ----
            try:
                metrics = collector.collect_driver_metrics(
                    driver_name=driver_name,
                    specs=grace_specs,
                    verdicts=grace_verdicts,
                    validation_results=grace_validations,
                    bug_reports=grace_bugs,
                    runtime=grace_runtime,
                )
                all_metrics.append(metrics)
            except Exception as exc:
                logger.error("Metrics collection failed for '%s': %s", driver_name, exc)

            # ---- Step 3: Run baselines ----
            if run_baselines:
                # CBMC-alone
                try:
                    cbmc_baseline = CBMCAloneBaseline()
                    bl_result = cbmc_baseline.run(
                        source_file=source_file,
                        driver_name=driver_name,
                        config=eval_config,
                        store=store,
                    )
                    baseline_results["cbmc_alone"].append(bl_result)
                    logger.info(
                        "CBMC-alone baseline for '%s': %d bugs, error=%s",
                        driver_name,
                        len(bl_result.bugs_found),
                        bl_result.error,
                    )
                except Exception as exc:
                    logger.error("CBMC baseline failed for '%s': %s", driver_name, exc)
                    baseline_results["cbmc_alone"].append(
                        BaselineResult(
                            name="cbmc_alone",
                            driver_name=driver_name,
                            error=str(exc),
                        )
                    )

        # ---- Step 4: Compute summary ----
        summary = collector.compute_summary(all_metrics, baseline_results)

        # ---- Step 5: Generate reports ----
        try:
            reporter.save_reports(summary, all_metrics, all_bug_reports)
            logger.info("Reports saved to %s", out_path)
        except Exception as exc:
            logger.error("Report generation failed: %s", exc)

        return summary
