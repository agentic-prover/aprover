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
from bmc_agent.backends import backend_for
from bmc_agent.bmc_engine import BMCEngine, BMCVerdict
from bmc_agent.bug_reporter import BugReport, BugReporter
from bmc_agent.cex_validator import CExOutcome, CExValidator, ValidationResult
from bmc_agent.config import Config
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.llm import LLMClient
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, ParsedCFile, parse_c_file
from bmc_agent.source_parser import detect_language, parse_source_file
from bmc_agent.realism_checker import RealismChecker
from bmc_agent.spec import Spec, SpecStatus
from bmc_agent.spec_generator import SpecGenerator

logger = get_logger("pipeline")


# CBMC property classes that would manifest as a runtime crash if the bug
# were real (signal-fault or assertion).  When dynamic validation reports
# NOT_TRIGGERED on one of these, the CBMC witness is genuinely a model
# artifact and the realism shortcut is safe.
_CRASH_CLASS_PROPERTY_SUFFIXES: tuple[str, ...] = (
    "pointer_dereference",
    "pointer",                # CBMC's older single-property pointer check
    "bounds",
    "null-pointer",
    "NULL-pointer",
    "double-free",
    "use-after-free",
    "assertion",
    "div-by-zero",
    "memory-leak",
    "precondition_instance",  # PROPERTY_OF callee precondition violations
)


# CBMC property classes that are SILENT undefined behaviour — the bug is
# real per the C standard, but the runtime doesn't crash without
# instrumentation (UBSan / ASan-with-pointer-checks).  Dynamic
# NOT_TRIGGERED on one of these is uninformative; we must call the
# realism LLM rather than auto-marking UNREALISTIC.  Misclassifying these
# as artifacts erases real bugs — see the May-7 VibeOS malloc.overflow.1
# regression that exposed this.
_SILENT_UB_PROPERTY_SUFFIXES: tuple[str, ...] = (
    "overflow",
    "conversion",
    "pointer_arithmetic",     # C11 §6.5.6/8 pointer-past-end
    "pointer_overflow",
    "shift",
    "alignment",
)


def _is_crash_class_property(prop: str) -> bool:
    """Return True when the CBMC property would crash at runtime if real.

    The property name looks like ``func.<class>.<index>`` (e.g.
    ``malloc.overflow.1``, ``f.pointer_dereference.13``).  We match on the
    middle token.  Anything not on the crash list is treated conservatively
    — including unknown classes — so the realism-shortcut only fires when
    we positively know a real bug here would manifest as a crash.

    Returns False for empty / malformed property names so the dynamic
    shortcut is gated on a positive identification, not on absence of
    information.
    """
    if not prop:
        return False
    parts = prop.split(".")
    # Walk inward from the property name; allow either ``f.class.N`` or
    # ``f.subdir.class.N`` shape.  We match against any token that equals
    # a crash-class suffix.
    for tok in parts:
        if tok in _CRASH_CLASS_PROPERTY_SUFFIXES:
            return True
    return False


def _ce_class_key(ce) -> str:
    """Coarse equivalence class for a counterexample.

    Two CEs are considered "same class" when they fail the same property
    family (the trailing index is dropped — ``pointer_dereference.1`` and
    ``pointer_dereference.7`` are the same class). Used by the in-sweep
    feedback loop to detect a clause that didn't prune the CE state.
    """
    prop = getattr(ce, "failing_property", "") or ""
    parts = prop.split(".")
    # Drop trailing numeric index, e.g. "f.pointer_dereference.1" → "f.pointer_dereference"
    while parts and parts[-1].isdigit():
        parts.pop()
    base = ".".join(parts) or prop
    loc = getattr(ce, "failure_location", None) or {}
    line = loc.get("line", "") if isinstance(loc, dict) else ""
    return f"{base}@{line}"


def _verified_clean_validation(prev_validation):
    """Build a ValidationResult that records the function as verified
    clean after feedback convergence. Reuses the previous validation's
    caller_path / system_entry_input so downstream serialization works.
    """
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import CExOutcome, ValidationResult
    # Synthesise a "no-CE" counterexample for downstream code that
    # expects a CE shape.
    synthetic = Counterexample(failing_property="(verified clean after feedback loop)")
    return ValidationResult(
        outcome=CExOutcome.SPURIOUS,
        function_name=getattr(prev_validation, "function_name", ""),
        counterexample=synthetic,
        reasoning="Verified clean after in-sweep feedback-loop iteration.",
        caller_path=getattr(prev_validation, "caller_path", []) or [],
        system_entry_input=getattr(prev_validation, "system_entry_input", "") or "",
        final_precondition=getattr(prev_validation, "final_precondition", None),
        is_real_bug=False,
        system_entry_reached=False,
        over_refinement_rejected=False,
    )


def _realism_verified():
    """Realism result for a function that verified clean after feedback.

    Marks the verdict UNREALISTIC (no real bug) with explicit reasoning
    so the bug-reporter downgrades it appropriately.
    """
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    return RealismCheckResult(
        verdict=RealismVerdict.UNREALISTIC,
        reasoning=(
            "After applying learned constraints in-sweep, CBMC verified "
            "the function clean. The earlier counterexample required a "
            "state that the constraint excludes; no real bug remains."
        ),
        key_concern="[feedback-converged] in-sweep iteration succeeded",
        llm_confidence="high",
    )


def _dedup_counterexamples(cexs: list) -> list:
    """Keep one counterexample per property type, preserving order.

    CBMC emits a separate property ID for every loop unrolling and every
    dereference site, so a single root bug can produce dozens of
    pointer_arithmetic.N / overflow.N / unwind.N entries.  We keep only the
    first representative per type.  'assertion' properties are kept in full
    because each index corresponds to a distinct spec postcondition.
    """
    seen: set[str] = set()
    result = []
    for cex in cexs:
        parts = cex.failing_property.split(".")
        # property type is the second-to-last segment (e.g. "pointer_arithmetic")
        prop_type = parts[-2] if len(parts) >= 2 else cex.failing_property
        if prop_type == "assertion":
            result.append(cex)
        elif prop_type not in seen:
            seen.add(prop_type)
            result.append(cex)
    if len(result) < len(cexs):
        logger.debug(
            "Deduped %d counterexamples → %d (by property type)",
            len(cexs), len(result),
        )
    return result


def _prop_type(failing_property: str) -> str:
    """Extract the property-type segment from a CBMC property name.

    E.g. 'find_virtio_input.pointer_arithmetic.5' → 'pointer_arithmetic'
         'main.assertion.2'                        → 'assertion'
    """
    parts = failing_property.split(".")
    return parts[-2] if len(parts) >= 2 else failing_property


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

        parsed = parse_source_file(source_file, source_text=preprocessed_source)

        # Swap in the language-appropriate verification backend in place
        # so that mocks attached to self.bmc_engine survive.  The default
        # (set in __init__) is CBMC; for .rs inputs we want Kani.
        self.bmc_engine.backend = backend_for(detect_language(source_file), self.config)

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
        # Phase 1.5: Per-function CBMC flag selection [AGENTIC]
        # ------------------------------------------------------------------
        flag_selections: dict = {}
        if getattr(self.config, "enable_flag_selection", False):
            from bmc_agent.flag_selector import FlagSelector
            logger.info("--- Phase 1.5: Selecting per-function CBMC flags ---")
            selector = FlagSelector(self.config, self.llm)
            flag_selections = selector.select_all(all_funcs)

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
            flag_selections=flag_selections if flag_selections else None,
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
        # Tracks (fn_name, prop_type) pairs already confirmed as real bugs so
        # CEGAR re-runs don't re-validate and re-report the same root bug.
        confirmed_real_bugs: set[tuple[str, str]] = set()

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

            for cex in _dedup_counterexamples(verdict.counterexamples):
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
                    bug_key = (fn_name, _prop_type(cex.failing_property))
                    if bug_key in confirmed_real_bugs:
                        logger.debug(
                            "Skipping duplicate real bug for '%s' (property type '%s' already confirmed)",
                            fn_name, bug_key[1],
                        )
                    else:
                        confirmed_real_bugs.add(bug_key)
                        logger.info("REAL BUG confirmed in '%s'", fn_name)
                        report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs)
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
                for cex in _dedup_counterexamples(verdict.counterexamples):
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
                        bug_key = (fn_name, _prop_type(cex.failing_property))
                        if bug_key in confirmed_real_bugs:
                            logger.debug(
                                "Phase 3c: skipping duplicate real bug for '%s' (type '%s')",
                                fn_name, bug_key[1],
                            )
                        else:
                            confirmed_real_bugs.add(bug_key)
                            logger.info("REAL BUG (Phase 3c) confirmed in '%s'", fn_name)
                            report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs)
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
                for cex in _dedup_counterexamples(verdict.counterexamples):
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
                        bug_key = (fn_name, _prop_type(cex.failing_property))
                        if bug_key in confirmed_real_bugs:
                            logger.debug(
                                "Recheck: skipping duplicate real bug for '%s' (type '%s')",
                                fn_name, bug_key[1],
                            )
                        else:
                            confirmed_real_bugs.add(bug_key)
                            logger.info("REAL BUG (recheck) confirmed in '%s'", fn_name)
                            report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs)
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
        all_specs: "dict[str, Spec] | None" = None,
    ) -> BugReport:
        """Run realism check then create and save a BugReport.

        Dynamic-validation gate: if dynamic validation already ran for
        this counterexample and the runtime fault did NOT trigger
        (DynamicOutcome.NOT_TRIGGERED), mark the finding UNREALISTIC
        and skip the expensive realism LLM call. This eliminates the
        bsearch / malloc-stub-returns-NULL class of false positives
        wholesale — those CEs require unconstrained allocator returns
        that real libc never produces, so the dynamic harness never
        reaches the fault, and there's nothing for the realism LLM to
        usefully add.
        """
        from bmc_agent.dynamic_validator import DynamicOutcome
        from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict

        dyn = getattr(validation, "dynamic_result", None)
        ce = getattr(validation, "counterexample", None)
        failing_prop = getattr(ce, "failing_property", "") or ""
        if (
            self.config.enable_realism_check
            and dyn is not None
            and dyn.outcome == DynamicOutcome.NOT_TRIGGERED
            and _is_crash_class_property(failing_prop)
        ):
            logger.info(
                "Realism check skipped for '%s': dynamic validation reported "
                "NOT_TRIGGERED on crash-class property '%s', marking finding "
                "as artifact",
                func.name, failing_prop,
            )
            realism = RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    "Dynamic validation harness compiled + executed the "
                    "counterexample state; the runtime fault did not trigger. "
                    "Marked UNREALISTIC without a realism-LLM call: when the "
                    "real runtime can't reproduce the fault for a crash-class "
                    "property (NULL deref, OOB, bounds), the CBMC witness is "
                    "by definition a model artifact (stub return values, "
                    "unconstrained symbolic state, or aliasing impossible "
                    "in real C)."
                ),
                key_concern="dynamic validation did not reproduce the fault",
                llm_confidence="high",
            )
        else:
            realism = self.realism_checker.check(
                func=func,
                counterexample=validation.counterexample,
                validation_result=validation,
                parsed_file=parsed,
                all_funcs=all_funcs,
                spec=spec,
            )
        # Feedback loop: if realism rejected and the loop is enabled,
        # distill the rejection into a remediation (code-change TODO,
        # function-spec clause, or project invariant), persist it,
        # then re-run CBMC on this function so the new clause takes
        # effect in this sweep. Loop until the function verifies
        # clean, a REALISTIC/UNCERTAIN verdict emerges, the same CE
        # class repeats (clause was a no-op), or feedback_max_iters
        # is exhausted. ``validation`` and ``realism`` are mutated
        # by reference through this loop.
        if getattr(self.config, "enable_feedback_loop", False):
            try:
                validation, realism = self._feedback_iterate(
                    validation, realism, func, spec, parsed, all_funcs,
                    driver_name, all_specs or {},
                )
            except Exception as exc:
                logger.warning(
                    "Feedback-loop iteration failed for '%s': %s",
                    func.name, exc,
                )

        realism_arg = realism if self.config.enable_realism_check else None
        return self.reporter.create_report(validation, func, realism_check=realism_arg)

    def _feedback_record(self, func, validation, realism):
        """Distill an UNREALISTIC realism verdict into a Remediation and
        persist via the LearnedConstraintsStore.

        Returns the Remediation so callers can decide whether to
        re-run CBMC. Idempotent: re-recording an existing clause is a
        no-op (returns the remediation; the store reports unchanged).
        """
        from bmc_agent.feedback_loop import (
            LearnedConstraintsStore,
            learn_from_rejection,
        )
        store = LearnedConstraintsStore(self.config.artifact_dir)
        existing = store.project_clauses() + store.function_clauses(func.name)
        remediation = learn_from_rejection(
            self.config,
            self.llm,
            func,
            validation.counterexample,
            realism,
            existing_project_clauses=existing,
        )
        store.record(
            func.name,
            remediation,
            source_property=getattr(validation.counterexample, "failing_property", ""),
        )
        return remediation

    def _feedback_iterate(
        self,
        validation: "ValidationResult",
        realism: "RealismCheckResult | None",
        func: "FunctionInfo",
        spec: "Spec",
        parsed: "ParsedCFile",
        all_funcs: "dict[str, FunctionInfo]",
        driver_name: str,
        all_specs: "dict[str, Spec]",
    ) -> "tuple[ValidationResult, RealismCheckResult | None]":
        """In-sweep convergence loop driven by realism rejections.

        Implements the architecture sketched in
        ``findings/bounty/FP_REFLECTIONS.md``:

          CBMC → CE → UNREALISTIC → distill clause → persist
            → re-harness this function with the clause active
            → re-run CBMC on this function
            → If verified clean: stop (the clause closed the gap)
            → If new CE class: classify + realism, recurse
            → If SAME CE class as before: stop (clause was a no-op)
            → If REALISTIC / UNCERTAIN verdict: stop (return that)
            → After feedback_max_iters: stop (return last verdict)

        Side effects:
          - learned_constraints.json gets a new clause per iteration.
          - The harness file is overwritten in place (last iteration wins).
          - The cbmc_result.json is overwritten in place.

        Logs every iteration so the dev/operator can see convergence.
        """
        from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
        from bmc_agent.feedback_loop import RemediationScope

        max_iters = int(getattr(self.config, "feedback_max_iters", 3) or 0)
        if (
            max_iters <= 0 or realism is None
            or realism.verdict not in (RealismVerdict.UNREALISTIC, RealismVerdict.UNCERTAIN)
        ):
            # Nothing to iterate — REALISTIC findings are kept as-is,
            # and verdicts we can't classify are out of scope.
            return validation, realism

        prev_ce_class = _ce_class_key(validation.counterexample)

        for iteration in range(1, max_iters + 1):
            # (1) Distill + persist.
            remediation = self._feedback_record(func, validation, realism)
            if remediation.scope not in (
                RemediationScope.FUNCTION_SPEC,
                RemediationScope.PROJECT_INVARIANT,
            ):
                logger.info(
                    "Feedback iter %d (%s): nothing to apply in-sweep "
                    "(scope=%s) — stopping",
                    iteration, func.name, remediation.scope.value,
                )
                return validation, realism

            # (2) Re-run CBMC on this function. The harness generator
            # reads learned_constraints.json from disk and emits
            # __CPROVER_assume(clause) at Step 1.7, so the new clause
            # is active on this call.
            new_verdict = self.bmc_engine.check_function(
                func, spec, parsed, driver_name, all_funcs=all_funcs,
            )

            if new_verdict.verified or not new_verdict.counterexamples:
                logger.info(
                    "Feedback iter %d (%s): function VERIFIES CLEAN after "
                    "applying learned clause '%s' — convergence",
                    iteration, func.name, remediation.clause[:80],
                )
                return _verified_clean_validation(validation), _realism_verified()

            # (3) New CE — classify + realism.
            new_ce = new_verdict.counterexamples[0]
            new_ce_class = _ce_class_key(new_ce)
            if new_ce_class == prev_ce_class:
                logger.info(
                    "Feedback iter %d (%s): same CE class repeated "
                    "(clause '%s' was a no-op for CBMC) — stopping to "
                    "avoid loop",
                    iteration, func.name, remediation.clause[:80],
                )
                return validation, realism
            prev_ce_class = new_ce_class

            new_validation = self.cex_validator.validate(
                func=func, spec=spec, counterexample=new_ce,
                all_funcs=all_funcs, all_specs=all_specs,
                parsed_file=parsed, driver_name=driver_name,
            )

            # (4) Realism on the new CE.
            from bmc_agent.dynamic_validator import DynamicOutcome
            dyn = getattr(new_validation, "dynamic_result", None)
            if (
                self.config.enable_realism_check
                and dyn is not None
                and dyn.outcome == DynamicOutcome.NOT_TRIGGERED
            ):
                new_realism = RealismCheckResult(
                    verdict=RealismVerdict.UNREALISTIC,
                    reasoning=(
                        "Feedback iter %d: dynamic validation NOT_TRIGGERED "
                        "on new CE class." % iteration
                    ),
                    key_concern="dynamic validation did not reproduce the fault",
                    llm_confidence="high",
                )
            else:
                new_realism = self.realism_checker.check(
                    func=func, counterexample=new_ce,
                    validation_result=new_validation, parsed_file=parsed,
                    all_funcs=all_funcs, spec=spec,
                )

            validation = new_validation
            realism = new_realism

            if realism.verdict not in (RealismVerdict.UNREALISTIC, RealismVerdict.UNCERTAIN):
                logger.info(
                    "Feedback iter %d (%s): new CE class with verdict=%s "
                    "— escalating",
                    iteration, func.name, realism.verdict.value,
                )
                return validation, realism

            # Still UNREALISTIC or UNCERTAIN — continue loop.
            logger.info(
                "Feedback iter %d (%s): new CE class still UNREALISTIC; "
                "iterating", iteration, func.name,
            )

        logger.info(
            "Feedback iter %d (%s): max iterations exhausted, returning "
            "last UNREALISTIC verdict",
            max_iters, func.name,
        )
        return validation, realism

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

        # In real-libc mode, parse the raw source (without cc -E expansion):
        # otherwise the include path pulls in every header's inline functions
        # — for libssh + OpenSSL, a 700-line agent.c becomes 325 specs of
        # OSSL_FUNC_* dispatch thunks. Cross-file analysis loses fidelity
        # for #ifdef-guarded code without preprocessing, but the spec
        # generation runs only against the project's own functions, which
        # is the bug-finding target anyway.
        use_preprocess = not getattr(self.config, "cbmc_real_libc", False)

        logger.info(
            "Pass 1: building global call graph across %d files (%s)",
            len(c_files),
            "with cc -E preprocessing" if use_preprocess else "raw source, real-libc mode",
        )
        for c_file in c_files:
            try:
                if use_preprocess:
                    expanded = preprocess(
                        c_file,
                        include_dirs=[str(source_dir)] + include_dirs,
                        cc=self.config.cc_path,
                    )
                    parsed_pass1 = parse_c_file(c_file, source_text=expanded)
                else:
                    expanded = c_file.read_text(encoding="utf-8", errors="replace")
                    parsed_pass1 = parse_c_file(c_file)
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
        # Pass 1.5: auto-extract domain knowledge from the codebase unless the
        # caller already supplied a non-empty domain_knowledge string.
        # ------------------------------------------------------------------
        from bmc_agent.domain_analyzer import analyze_codebase as _analyze_domain
        domain_knowledge = _analyze_domain(
            source_dir=source_dir,
            include_dirs=include_dirs,
            file_parsed_c=file_parsed_c,
            file_expanded=file_expanded,
            llm=self.llm,
            user_domain_knowledge=domain_knowledge,
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
