"""
AMC Pipeline Orchestrator.

Runs Phase 1 (spec gen) → Phase 2 (BMC) → Phase 3 (validation/refinement)
and returns a list of confirmed BugReport objects.

Component labeling per AMC architecture:
  Phase 1 Spec Generator    — AGENTIC   (LLM plans, retries, cross-checks)
  Phase 2 BMC Engine        — CONVENTIONAL (deterministic solver invocation)
  Phase 3 CEx Confirmation  — AGENTIC   (LLM classifies, concretizes)
  Phase 3 Spec Refiner      — AGENTIC   (LLM proposes; soundness guard gates)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from amc.artifacts import ArtifactStore
from amc.bmc_engine import BMCEngine, BMCVerdict
from amc.bug_reporter import BugReport, BugReporter
from amc.cex_validator import CExOutcome, CExValidator, ValidationResult
from amc.config import Config
from amc.harness_generator import HarnessGenerator
from amc.llm import LLMClient
from amc.logger import get_logger
from amc.parser import FunctionInfo, ParsedCFile, parse_c_file
from amc.spec import Spec, SpecStatus
from amc.spec_generator import SpecGenerator

logger = get_logger("pipeline")


@dataclass
class PropagationEvent:
    """Records one compositional propagation: a spec refinement and its downstream effect.

    RQ3 data: does refining F's spec improve verification of F's callers?
    """
    refined_function: str
    callers_reverified: list[str]
    outcome_changes: dict[str, tuple[str, str]] = field(default_factory=dict)
    # caller → ("unverified", "verified") when recheck resolved the alarm
    bugs_found_via_propagation: list[str] = field(default_factory=list)
    # Only bugs in callers that were VERIFIED in Phase 2 — bugs that became
    # reachable only after the refined stub constrained callee behaviour.
    # Callers already unverified in Phase 2 are excluded (their bugs were
    # reachable with the original loose stub too).


class AMCPipeline:
    """
    End-to-end AMC verification pipeline.

    Phase 1: Generate specs for all functions.   [AGENTIC]
    Phase 2: Run BMC on every function.          [CONVENTIONAL]
    Phase 3: Validate counterexamples, refine specs, produce bug reports.  [AGENTIC]
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = ArtifactStore(config.artifact_dir)
        self.llm = LLMClient(config)
        self.spec_gen = SpecGenerator(config, self.llm, self.store)
        self.bmc_engine = BMCEngine(config, self.store)
        self.harness_gen = HarnessGenerator(config)
        self.validator = CExValidator(
            config=config,
            llm=self.llm,
            store=self.store,
            harness_gen=self.harness_gen,
        )
        self.reporter = BugReporter(self.store)
        self.propagation_events: list[PropagationEvent] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        source_file: str,
        driver_name: str,
        domain_knowledge: str = "",
    ) -> list[BugReport]:
        """
        Run the full AMC pipeline.

        Parameters
        ----------
        source_file:
            Path to the C source file to verify.
        driver_name:
            Logical name for this verification run (used for artifact storage).
        domain_knowledge:
            Optional domain knowledge string passed to the LLM for spec generation.

        Returns
        -------
        List of confirmed BugReport objects.
        """
        logger.info("=== AMC Pipeline START: %s (driver=%s) ===", source_file, driver_name)

        # ------------------------------------------------------------------
        # Phase 1: Parse + Generate specs
        # ------------------------------------------------------------------
        logger.info("--- Phase 1: Generating specs ---")
        parsed = parse_c_file(source_file)
        self.store.init_driver(driver_name)

        specs = self.spec_gen.generate_specs(
            source_file=source_file,
            driver_name=driver_name,
            domain_knowledge=domain_knowledge,
        )
        logger.info("Phase 1 complete: %d specs generated", len(specs))

        # Build all_funcs mapping
        all_funcs: dict[str, FunctionInfo] = {}
        for fn_name in specs:
            fi = parsed.get_function_info(fn_name)
            if fi is not None:
                all_funcs[fn_name] = fi

        if not all_funcs:
            logger.warning("No functions found in '%s'; aborting pipeline", source_file)
            return []

        # Build callee → callers reverse-dependency map
        callee_to_callers: dict[str, set[str]] = {}
        for fn_name, finfo in all_funcs.items():
            for callee in finfo.callees:
                callee_to_callers.setdefault(callee, set()).add(fn_name)

        # ------------------------------------------------------------------
        # Phase 2: Run BMC on all functions
        # ------------------------------------------------------------------
        logger.info("--- Phase 2: Running BMC on %d functions ---", len(all_funcs))
        verdicts = self.bmc_engine.check_all(
            funcs=all_funcs,
            specs=specs,
            parsed_file=parsed,
            driver_name=driver_name,
        )
        logger.info("Phase 2 complete: %d verdicts", len(verdicts))

        # ------------------------------------------------------------------
        # Phase 3: Validate counterexamples, refine, report bugs
        # ------------------------------------------------------------------
        logger.info("--- Phase 3: Validating counterexamples ---")
        bug_reports: list[BugReport] = []
        current_specs = dict(specs)  # may be updated by refinement
        recheck_queue: set[str] = set()   # callers to re-check after refinement
        self_recheck_queue: set[str] = set()  # refined fns to re-check themselves
        # RQ3: maps caller_name → set of refined functions that queued it for recheck
        recheck_triggered_by: dict[str, set[str]] = {}
        # Global re-queue bound: tracks how many times each function has been re-queued
        requeue_counts: dict[str, int] = {}

        for fn_name, verdict in verdicts.items():
            if verdict.verified:
                logger.debug("'%s' verified — no counterexamples", fn_name)
                continue

            if verdict.error and not verdict.counterexamples:
                logger.debug("'%s' has error but no counterexamples (skipping): %s",
                             fn_name, verdict.error)
                continue

            func = all_funcs.get(fn_name)
            if func is None:
                continue

            spec = current_specs.get(fn_name)
            if spec is None:
                continue

            for cex in verdict.counterexamples:
                logger.info(
                    "Validating counterexample for '%s' (property=%s)",
                    fn_name, cex.failing_property,
                )
                validation = self.validator.validate(
                    func=func,
                    spec=spec,
                    counterexample=cex,
                    all_funcs=all_funcs,
                    all_specs=current_specs,
                    parsed_file=parsed,
                    driver_name=driver_name,
                )

                # Always persist the classification result for this counterexample
                self.store.save_classification(driver_name, fn_name, validation)

                if validation.outcome == CExOutcome.UNRESOLVED:
                    logger.info(
                        "UNRESOLVED counterexample for '%s' — tracking for later",
                        fn_name,
                    )
                    self.reporter._unresolved.append(validation)
                elif validation.is_real_bug:
                    logger.info("REAL BUG confirmed in '%s'", fn_name)
                    report = self.reporter.create_report(validation, func)
                    self.reporter.save_report(report, driver_name)
                    bug_reports.append(report)
                else:
                    logger.info(
                        "Spurious counterexample for '%s' — refined precondition: %s",
                        fn_name,
                        (validation.final_precondition or "")[:80],
                    )
                    if self.config.skip_refinement:
                        # Filtering-only ablation: classify spurious but skip
                        # spec update and caller re-queue (RQ3 baseline).
                        logger.info(
                            "skip_refinement=True: skipping spec update for '%s'", fn_name
                        )
                    elif validation.final_precondition:
                        refined_spec = Spec(
                            function_name=fn_name,
                            precondition=validation.final_precondition,
                            postcondition=spec.postcondition,
                            callee_specs=spec.callee_specs,
                            loop_invariants=spec.loop_invariants,
                            status=SpecStatus.REFINED,
                        )
                        current_specs[fn_name] = refined_spec
                        self.store.save_spec(driver_name, fn_name, refined_spec)
                        # Persist refinement history for this function
                        if validation.refinement_history:
                            self.store.save_refinement_history(
                                driver_name, fn_name, validation.refinement_history
                            )
                        # Queue callers for compositional propagation recheck,
                        # subject to the global per-function re-queue cap.
                        for caller_name in callee_to_callers.get(fn_name, set()):
                            if caller_name in all_funcs:
                                count = requeue_counts.get(caller_name, 0)
                                if count < self.config.max_requeue_per_function:
                                    recheck_queue.add(caller_name)
                                    requeue_counts[caller_name] = count + 1
                                    recheck_triggered_by.setdefault(caller_name, set()).add(fn_name)
                                else:
                                    logger.info(
                                        "Re-queue cap reached for '%s' (count=%d) — skipping",
                                        caller_name, count,
                                    )
                        # Also re-run CBMC on the function itself under the
                        # refined precondition — the spurious CEx may have
                        # masked a real bug (CEGAR: tighten abstract domain,
                        # re-verify).
                        self_recheck_queue.add(fn_name)

        # ------------------------------------------------------------------
        # Phase 3c: Re-run BMC on refined functions (CEGAR loop)
        # A spurious CEx may have masked a real bug lurking behind it.
        # After tightening the precondition, re-verify the function itself.
        # ------------------------------------------------------------------
        if self_recheck_queue:
            logger.info(
                "--- Phase 3c: Re-verifying %d refined function(s) under tighter preconditions ---",
                len(self_recheck_queue),
            )
            self_recheck_funcs = {n: all_funcs[n] for n in self_recheck_queue if n in all_funcs}
            self_recheck_verdicts = self.bmc_engine.check_all(
                funcs=self_recheck_funcs,
                specs=current_specs,
                parsed_file=parsed,
                driver_name=driver_name,
            )
            for fn_name, verdict in self_recheck_verdicts.items():
                if verdict.verified:
                    logger.info("Phase 3c: '%s' verifies under refined precondition", fn_name)
                    continue
                if verdict.error and not verdict.counterexamples:
                    continue
                func = all_funcs.get(fn_name)
                spec = current_specs.get(fn_name)
                if func is None or spec is None:
                    continue
                for cex in verdict.counterexamples:
                    logger.info(
                        "Phase 3c: new counterexample for '%s' (property=%s)",
                        fn_name, cex.failing_property,
                    )
                    validation = self.validator.validate(
                        func=func,
                        spec=spec,
                        counterexample=cex,
                        all_funcs=all_funcs,
                        all_specs=current_specs,
                        parsed_file=parsed,
                        driver_name=driver_name,
                    )
                    if validation.outcome == CExOutcome.UNRESOLVED:
                        self.reporter._unresolved.append(validation)
                    elif validation.is_real_bug:
                        logger.info("REAL BUG (Phase 3c) confirmed in '%s'", fn_name)
                        report = self.reporter.create_report(validation, func)
                        self.reporter.save_report(report, driver_name)
                        bug_reports.append(report)
                    else:
                        logger.info(
                            "Phase 3c: spurious (further refined) for '%s': %s",
                            fn_name,
                            (validation.final_precondition or "")[:80],
                        )
                        if validation.final_precondition:
                            refined_spec = Spec(
                                function_name=fn_name,
                                precondition=validation.final_precondition,
                                postcondition=spec.postcondition,
                                callee_specs=spec.callee_specs,
                                loop_invariants=spec.loop_invariants,
                                status=SpecStatus.REFINED,
                            )
                            current_specs[fn_name] = refined_spec
                            self.store.save_spec(driver_name, fn_name, refined_spec)

        # ------------------------------------------------------------------
        # Phase 3b: Drain recheck queue — re-run BMC on callers of refined fns
        # Compositional propagation: F's refined spec constrains F's stub in
        # callers' harnesses, so callers may now verify (or reveal new bugs).
        # ------------------------------------------------------------------
        if recheck_queue:
            logger.info(
                "--- Phase 3b: Re-checking %d caller(s) after refinement ---",
                len(recheck_queue),
            )
            recheck_funcs = {n: all_funcs[n] for n in recheck_queue if n in all_funcs}
            recheck_verdicts = self.bmc_engine.check_all(
                funcs=recheck_funcs,
                specs=current_specs,
                parsed_file=parsed,
                driver_name=driver_name,
            )

            # RQ3: per-function bugs found in Phase 3b (for PropagationEvent)
            phase3b_bugs_by_fn: dict[str, list[str]] = {}

            for fn_name, verdict in recheck_verdicts.items():
                if verdict.verified or (verdict.error and not verdict.counterexamples):
                    continue
                func = all_funcs.get(fn_name)
                spec = current_specs.get(fn_name)
                if func is None or spec is None:
                    continue
                for cex in verdict.counterexamples:
                    logger.info(
                        "Recheck: validating counterexample for '%s' (property=%s)",
                        fn_name, cex.failing_property,
                    )
                    validation = self.validator.validate(
                        func=func,
                        spec=spec,
                        counterexample=cex,
                        all_funcs=all_funcs,
                        all_specs=current_specs,
                        parsed_file=parsed,
                        driver_name=driver_name,
                    )
                    if validation.outcome == CExOutcome.UNRESOLVED:
                        self.reporter._unresolved.append(validation)
                    elif validation.is_real_bug:
                        logger.info("REAL BUG (recheck) confirmed in '%s'", fn_name)
                        report = self.reporter.create_report(validation, func)
                        self.reporter.save_report(report, driver_name)
                        bug_reports.append(report)
                        phase3b_bugs_by_fn.setdefault(fn_name, []).append(
                            f"{fn_name}:{cex.failing_property}"
                        )

            # RQ3: build PropagationEvent objects grouped by which refinement
            # triggered each caller recheck.
            events_by_refined: dict[str, list[str]] = {}
            for caller_name, refined_set in recheck_triggered_by.items():
                for rf in refined_set:
                    events_by_refined.setdefault(rf, []).append(caller_name)

            for rf, callers in events_by_refined.items():
                outcome_changes: dict[str, tuple[str, str]] = {}
                bugs_via: list[str] = []
                for caller in callers:
                    orig = verdicts.get(caller)
                    rechk = recheck_verdicts.get(caller)
                    if orig is not None and rechk is not None:
                        before = "verified" if orig.verified else "unverified"
                        after = "verified" if rechk.verified else "unverified"
                        if before != after:
                            outcome_changes[caller] = (before, after)
                    # Only count bugs that are genuinely new to propagation:
                    # the caller must have been verified (no bugs) in Phase 2.
                    # A caller already unverified in Phase 2 had its bugs
                    # reachable without the refined stub — not a propagation discovery.
                    if orig is not None and orig.verified:
                        bugs_via.extend(phase3b_bugs_by_fn.get(caller, []))

                event = PropagationEvent(
                    refined_function=rf,
                    callers_reverified=callers,
                    outcome_changes=outcome_changes,
                    bugs_found_via_propagation=bugs_via,
                )
                self.propagation_events.append(event)
                self.store.save_propagation_events(driver_name, rf, [event])
                logger.info(
                    "PropagationEvent: refined='%s', callers=%d, outcome_changes=%d, bugs=%d",
                    rf, len(callers), len(outcome_changes), len(bugs_via),
                )

        # ------------------------------------------------------------------
        # Phase 4: Spec Quality Analysis (optional, expensive)
        # ------------------------------------------------------------------
        if self.config.enable_spec_quality:
            logger.info("--- Phase 4: Spec Quality Analysis ---")
            from amc.spec_quality import SpecQualityAnalyzer
            from amc.backends.cbmc_backend import CBMCBackend

            backend = getattr(self.bmc_engine, 'backend', None) or CBMCBackend(self.config)
            analyzer = SpecQualityAnalyzer(backend=backend, llm=self.llm, config=self.config)

            for fn_name, spec in current_specs.items():
                func = all_funcs.get(fn_name)
                if func:
                    report = analyzer.analyze(
                        func, spec, all_funcs, current_specs, parsed, driver_name
                    )
                    self.store.save_spec_quality(driver_name, fn_name, report)
            logger.info("Phase 4 complete: spec quality analysis done")

        logger.info(
            "=== AMC Pipeline END: %d real bug(s) found, %d unresolved ===",
            len(bug_reports),
            len(self.reporter._unresolved),
        )
        return bug_reports
