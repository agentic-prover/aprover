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

from bmc_agent.artifacts import ArtifactStore
from bmc_agent.bmc_engine import BMCEngine, BMCVerdict
from bmc_agent.bug_reporter import BugReport, BugReporter
from bmc_agent.cex_validator import CExOutcome, CExValidator, ValidationResult
from bmc_agent.config import Config
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.llm import LLMClient
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, ParsedCFile, parse_c_file
from bmc_agent.realism_checker import RealismChecker
from bmc_agent.spec import Spec, SpecStatus
from bmc_agent.spec_generator import SpecGenerator

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
        self.realism_checker = RealismChecker(config, self.llm)
        self.propagation_events: list[PropagationEvent] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        source_file: str,
        driver_name: str,
        domain_knowledge: str = "",
        cross_file_callers: set[str] | None = None,
        cross_file_caller_contexts: dict[str, list[tuple[FunctionInfo, ParsedCFile]]] | None = None,
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

        # Optionally preprocess with cc -E before parsing (multi-file mode)
        preprocessed_source: Optional[str] = None
        if self.config.preprocess and self.config.include_dirs:
            from bmc_agent.preprocessor import preprocess
            logger.info("Preprocessing %s with include dirs: %s",
                        source_file, self.config.include_dirs)
            try:
                preprocessed_source = preprocess(
                    source_file,
                    include_dirs=self.config.include_dirs,
                    cc=self.config.cc_path,
                )
            except Exception as exc:
                logger.warning("Preprocessing failed (%s) — parsing file as-is", exc)

        parsed = parse_c_file(source_file, source_text=preprocessed_source)
        self.store.init_driver(driver_name)

        specs = self.spec_gen.generate_specs(
            source_file=source_file,
            driver_name=driver_name,
            domain_knowledge=domain_knowledge,
            source_text=preprocessed_source,
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
            all_funcs=all_funcs,
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
                    cross_file_callers=cross_file_callers,
                    cross_file_caller_contexts=cross_file_caller_contexts,
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
                    report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name)
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
                all_funcs=all_funcs,
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
                        report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name)
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
                all_funcs=all_funcs,
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
                        report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name)
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
            from bmc_agent.spec_quality import SpecQualityAnalyzer
            from bmc_agent.backends.cbmc_backend import CBMCBackend

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_report(
        self,
        validation: ValidationResult,
        func: FunctionInfo,
        spec: Spec,
        parsed: ParsedCFile,
        all_funcs: "dict[str, FunctionInfo]",
        driver_name: str,
    ) -> BugReport:
        """Run realism check then create and save a BugReport."""
        realism = self.realism_checker.check(
            func=func,
            counterexample=validation.counterexample,
            validation_result=validation,
            parsed_file=parsed,
            all_funcs=all_funcs,
            spec=spec,
        )
        realism_arg = realism if self.config.enable_realism_check else None
        return self.reporter.create_report(validation, func, realism_check=realism_arg)

    # ------------------------------------------------------------------
    # Multi-file / whole-codebase entry point
    # ------------------------------------------------------------------

    def run_directory(
        self,
        source_dir: str,
        driver_name: str,
        include_dirs: Optional[list[str]] = None,
        domain_knowledge: str = "",
        exclude_patterns: Optional[list[str]] = None,
    ) -> dict[str, list[BugReport]]:
        """
        Run AMC on every ``.c`` file in *source_dir*.

        Each file is preprocessed with ``cc -E`` (using *include_dirs*) so
        that cross-file ``#include`` references are resolved before parsing.
        Files are verified independently — callees from other files get
        auto-generated havoc stubs.

        Parameters
        ----------
        source_dir:
            Directory containing ``.c`` files to verify.
        driver_name:
            Base name for artifact storage; each file gets its own
            sub-driver ``{driver_name}/{stem}``.
        include_dirs:
            ``-I`` paths forwarded to the C preprocessor.
        domain_knowledge:
            Optional domain knowledge string passed to the LLM.
        exclude_patterns:
            List of filename glob patterns to skip (e.g. ``["*.test.c"]``).

        Returns
        -------
        Mapping of filename → list of BugReport.
        """
        import fnmatch
        from bmc_agent.preprocessor import preprocess

        source_dir = Path(source_dir)
        include_dirs = include_dirs or self.config.include_dirs or []
        exclude_patterns = exclude_patterns or []

        c_files = sorted(source_dir.rglob("*.c"))
        c_files = [
            f for f in c_files
            if not any(fnmatch.fnmatch(f.name, pat) for pat in exclude_patterns)
        ]

        if not c_files:
            logger.warning("No .c files found in '%s'", source_dir)
            return {}

        logger.info(
            "=== run_directory: %d files in '%s' ===", len(c_files), source_dir
        )

        import tempfile, os

        # ------------------------------------------------------------------
        # Pass 1: preprocess + parse every file to build a global call graph.
        # Caches expanded source and ParsedCFile objects so Pass 2 can:
        #   (a) skip re-running cc -E
        #   (b) run CBMC reachability queries against cross-file callers
        # ------------------------------------------------------------------
        file_expanded: dict[str, str] = {}           # stem → preprocessed source
        file_defined: dict[str, set[str]] = {}       # stem → function names defined
        file_callees: dict[str, set[str]] = {}       # stem → all function names called
        file_parsed_c: dict[str, ParsedCFile] = {}   # stem → ParsedCFile

        logger.info("Pass 1: building global call graph across %d files", len(c_files))
        for c_file in c_files:
            try:
                expanded = preprocess(
                    c_file,
                    include_dirs=[str(source_dir)] + include_dirs,
                    cc=self.config.cc_path,
                )
                parsed_pass1 = parse_c_file(c_file, source_text=expanded)
            except Exception as exc:
                logger.warning("Pass 1: failed for %s: %s — skipping", c_file.name, exc)
                continue
            if not parsed_pass1.functions:
                continue
            stem = c_file.stem
            file_expanded[stem] = expanded
            file_parsed_c[stem] = parsed_pass1
            file_defined[stem] = set(parsed_pass1.functions.keys())
            file_callees[stem] = set().union(
                *parsed_pass1.call_graph.values()
            ) if parsed_pass1.call_graph else set()

        # Global set: all functions that have callers in at least one other file.
        global_cross_file_callers: set[str] = set()
        for stem, defined in file_defined.items():
            for fn in defined:
                if any(fn in file_callees[other] for other in file_callees if other != stem):
                    global_cross_file_callers.add(fn)

        # Global contexts: for each callee function name, all (caller_FunctionInfo,
        # caller_ParsedCFile) pairs from OTHER files.  Used by CExValidator to run
        # CBMC reachability queries against cross-file callers.
        global_cross_file_caller_contexts: dict[
            str, list[tuple[FunctionInfo, ParsedCFile]]
        ] = {}
        for stem_a, parsed_a in file_parsed_c.items():
            for caller_name, caller_callees in parsed_a.call_graph.items():
                for callee_name in caller_callees:
                    # Only index if the callee is defined in a different file
                    if any(
                        callee_name in file_defined[stem_b]
                        for stem_b in file_defined
                        if stem_b != stem_a
                    ):
                        caller_fi = parsed_a.get_function_info(caller_name)
                        if caller_fi is not None:
                            global_cross_file_caller_contexts.setdefault(
                                callee_name, []
                            ).append((caller_fi, parsed_a))

        logger.info(
            "Pass 1 complete: %d functions have cross-file callers; "
            "%d cross-file caller relationships indexed",
            len(global_cross_file_callers),
            sum(len(v) for v in global_cross_file_caller_contexts.values()),
        )

        # ------------------------------------------------------------------
        # Pass 2: run the full AMC pipeline per file using cached preprocessed
        # source and cross-file caller information.
        # ------------------------------------------------------------------
        all_results: dict[str, list[BugReport]] = {}
        total_bugs = 0

        orig_preprocess = self.config.preprocess
        self.config.preprocess = False  # preprocessing already done in Pass 1

        for c_file in c_files:
            stem = c_file.stem
            if stem not in file_expanded:
                logger.info("Skipping %s (failed or empty in Pass 1)", c_file.name)
                continue

            file_driver = f"{driver_name}/{stem}"
            logger.info("--- Processing %s (driver=%s) ---", c_file.name, file_driver)

            expanded = file_expanded[stem]

            with tempfile.NamedTemporaryFile(
                suffix=".c", prefix=f"amc_{stem}_",
                mode="w", encoding="utf-8", delete=False
            ) as tmp:
                tmp.write(expanded)
                tmp_path = tmp.name

            try:
                bugs = self.run(
                    source_file=tmp_path,
                    driver_name=file_driver,
                    domain_knowledge=domain_knowledge,
                    cross_file_callers=global_cross_file_callers,
                    cross_file_caller_contexts=global_cross_file_caller_contexts,
                )
            finally:
                os.unlink(tmp_path)

            all_results[c_file.name] = bugs
            total_bugs += len(bugs)
            logger.info(
                "Finished %s: %d bug(s) confirmed", c_file.name, len(bugs)
            )

        self.config.preprocess = orig_preprocess

        logger.info(
            "=== run_directory DONE: %d files, %d total bug(s) ===",
            len(all_results), total_bugs,
        )
        return all_results
