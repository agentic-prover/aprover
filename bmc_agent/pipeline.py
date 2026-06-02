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

import re
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


def _cited_caller_is_fabricated(cited: str, func_source_file: str) -> bool:
    """Thin wrapper around ``agents.soundness.caller_is_fabricated`` (kept here
    for the soundness gate's call site + tests). True iff ``cited`` names a
    ``*.c``/``*.h`` that doesn't exist anywhere in the function's source tree."""
    from bmc_agent.agents.soundness import caller_is_fabricated
    return caller_is_fabricated(cited, func_source_file)


# CBMC property classes that would manifest as a runtime crash if the bug
# were real (signal-fault, assertion, or a verification-bound exceeded by
# input that would in practice DoS the program). When dynamic validation
# reports NOT_TRIGGERED on one of these, the CBMC witness is genuinely a
# model artifact and the realism shortcut is safe.
#
# Bound-class additions (recursion / unwind) cover the case where CBMC's
# unwinding bound is tripped by an input that, in concrete runtime, would
# either be unreachable through the public API or would manifest as a
# crash / hang / RSS blowup that a dyn-val harness ALSO sees. If the
# dyn-val harness runs the same input under real libc and finishes cleanly
# in bounded time, the bound was a verification-budget artifact, not a
# real bug. Without this, the postfix7 sweep's append_id_w.recursion
# finding was being classified real_bug despite dyn-val explicitly running
# clean — see classification gap analysis 2026-05-27.
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
    "recursion",              # CBMC --unwind cap exceeded by symbolic input
    "unwind",                 # generic loop-unwinding bound exceeded
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


def _all_applied_clauses(config, func_name: str, spec) -> list[str]:
    """Return every `__CPROVER_assume(...)` clause the next harness will
    inject, in source order: project invariants (Step 1.6), function
    invariants (Step 1.7), then the spec precondition (Step 2). Used by
    the feedback loop's clean-proof log line to make explicit what the
    "VERIFIED CLEAN" verdict is conditional on. Trivial preconditions
    (``true`` / ``1`` / empty) are dropped so the log doesn't say
    "verified clean under {true}".
    """
    clauses: list[str] = []
    if getattr(config, "enable_feedback_loop", False):
        try:
            from bmc_agent.feedback_loop import LearnedConstraintsStore
            store = LearnedConstraintsStore(config.artifact_dir)
            clauses.extend(store.project_clauses())
            clauses.extend(store.function_clauses(func_name))
        except Exception:
            pass
    pre = getattr(spec, "precondition", None) if spec else None
    if isinstance(pre, str):
        clauses.append(pre)
    return [c for c in clauses if isinstance(c, str) and c.strip() not in ("", "true", "1")]


def _flag_summary(flag_selection) -> str:
    """One-line summary of which CBMC checks the harness was run with.
    Returns "pointer-check, bounds-check, ..." or "default" when no
    extra Phase-1.5 flags are enabled."""
    if flag_selection is None:
        return "default (pointer-check, bounds-check)"
    try:
        enabled = flag_selection.enabled_flags()
    except Exception:
        return "default (pointer-check, bounds-check)"
    if not enabled:
        return "default (pointer-check, bounds-check)"
    return "+ " + ", ".join(f.lstrip("-") for f in enabled)


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
        refinement_history=list(getattr(prev_validation, "refinement_history", []) or []),
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


DEFAULT_DEDUP_PER_TYPE = 3


def _dedup_counterexamples(cexs: list, max_per_type: int = DEFAULT_DEDUP_PER_TYPE) -> list:
    """Keep up to ``max_per_type`` counterexamples per property type, in order.

    CBMC emits a separate property ID for every loop unrolling and every
    dereference site, so a single root bug can produce dozens of
    pointer_arithmetic.N / overflow.N / unwind.N entries.

    Earlier behaviour kept exactly one representative per type — that
    discarded deeper CEx indices (e.g. ``pointer_dereference.43``) when an
    artifact-flavoured CEx happened to come first (e.g.
    ``pointer_dereference.7`` on a nondet-pointer loop guard). Real bugs
    behind the artifact were never inspected. The fix is to keep a small
    window per type so the classifier+realism pair sees the deeper indices
    too.

    ``assertion`` properties are still kept in full because each index
    corresponds to a distinct spec postcondition (one assertion per
    postcondition).

    Use ``max_per_type=1`` to recover the original behaviour.
    """
    counts: dict[str, int] = {}
    result = []
    for cex in cexs:
        parts = cex.failing_property.split(".")
        prop_type = parts[-2] if len(parts) >= 2 else cex.failing_property
        if prop_type == "assertion":
            result.append(cex)
            continue
        seen = counts.get(prop_type, 0)
        if seen < max_per_type:
            counts[prop_type] = seen + 1
            result.append(cex)
    if len(result) < len(cexs):
        logger.debug(
            "Deduped %d counterexamples → %d (up to %d per property type)",
            len(cexs), len(result), max_per_type,
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
        # v2 (caller-grounded, evidence-tagged) is the default; v1 is the
        # opt-in legacy path under --legacy-spec-gen. v2 starts without
        # boundary_detector / corpus_paths; run() and run_directory()
        # populate them before invoking generate_specs.
        if getattr(config, "use_legacy_spec_gen", False):
            logger.info("spec-gen: using LEGACY (v1) SpecGenerator")
            self.spec_gen = SpecGenerator(config, self.llm, self.store)
        else:
            from bmc_agent.spec_generator_v2 import SpecGeneratorV2
            logger.info("spec-gen: using v2 (caller-grounded) SpecGeneratorV2")
            self.spec_gen = SpecGeneratorV2(config, self.llm, self.store)
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
    # v2 spec-gen plumbing
    # ------------------------------------------------------------------

    def _configure_v2_corpus(
        self,
        *,
        source_dir: Optional[Path] = None,
        corpus_files: Optional[list[Path]] = None,
        explicit_headers: Optional[list[Path]] = None,
    ) -> None:
        """Populate v2 SpecGeneratorV2 with the corpus + boundary context
        for this sweep. No-op for v1 SpecGenerator.
        """
        from bmc_agent.spec_generator_v2 import SpecGeneratorV2
        if not isinstance(self.spec_gen, SpecGeneratorV2):
            return
        if corpus_files is not None:
            self.spec_gen.corpus_paths = list(corpus_files)
        if source_dir is not None:
            try:
                from bmc_agent.boundary_detector import BoundaryDetector
                self.spec_gen.boundary_detector = BoundaryDetector.autodiscover(
                    Path(source_dir), explicit_headers=explicit_headers,
                )
                logger.info(
                    "v2 boundary detector: %d public functions discovered from %s",
                    len(self.spec_gen.boundary_detector), source_dir,
                )
            except Exception as exc:
                logger.warning(
                    "v2: boundary detector autodiscover failed (%r) — every "
                    "function will be treated as internal", exc,
                )

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
        function_hints: dict[str, str] | None = None,
        only_functions: set[str] | None = None,
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
                    defines=list(getattr(self.config, "cbmc_defines", None) or []),
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

        # function_hints carry attacker-scenario text from the previous
        # round's adjacent-bug discovery. Stash on config so downstream
        # components (spec_gen, harness_generator, realism_checker) can
        # opt-in to using them. Empty dict = no hints / normal sweep.
        self.config.function_hints = dict(function_hints or {})

        # v2 spec-gen wants the surrounding corpus + boundary headers so
        # it can caller-ground specs. For single-file run() we use the
        # source file's parent directory as the autodiscover root and
        # treat the file itself as the only corpus member.
        self._configure_v2_corpus(
            source_dir=Path(source_file).parent,
            corpus_files=[Path(source_file)],
        )

        specs = self.spec_gen.generate_specs(
            source_file=source_file,
            driver_name=driver_name,
            domain_knowledge=domain_knowledge,
            source_text=preprocessed_source,
            cross_file_caller_contexts=cross_file_caller_contexts,
            only_functions=only_functions,
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
        # Phase 2 scope (computed FIRST so flag selection can be scoped to it).
        # ------------------------------------------------------------------
        # only_functions restricts WHICH functions get verified (CBMC +
        # refinement), while all_funcs stays complete so spec context and
        # cross-file reachability still see the whole call graph. This is how
        # the audit-flagged-function path (coaudit --check-functions) gets full
        # cross-file gen + refinement without verifying the whole file.
        funcs_to_check = all_funcs
        if only_functions:
            funcs_to_check = {n: f for n, f in all_funcs.items() if n in only_functions}
            missing = [n for n in only_functions if n not in all_funcs]
            if missing:
                logger.warning("only_functions not found in %s: %s", source_file, ", ".join(missing))
            logger.info("only_functions: restricting Phase 2 to %d of %d functions",
                        len(funcs_to_check), len(all_funcs))
            if not funcs_to_check:
                # This file contains NONE of the requested functions (common in
                # verify-dir --functions: the targets live in other files). Skip
                # it — no flag selection, no Phase 2 — instead of crashing or
                # wasting spec-gen. The cross-file call graph from Phase 1 still
                # benefits the files that DO contain the targets.
                logger.info("only_functions: '%s' has none of the requested functions; skipping",
                            source_file)
                return []

        # ------------------------------------------------------------------
        # Phase 1.5: Per-function CBMC flag selection [AGENTIC]
        # ------------------------------------------------------------------
        # Scope flag selection to the functions actually being VERIFIED, not the
        # whole call graph. Otherwise it flag-selects every corpus function even
        # when only_functions checks one — which, when the cbmc_driver role is
        # agentic, would fan out into one claude-code call per corpus function
        # instead of per checked function.
        flag_selections: dict = {}
        if getattr(self.config, "enable_flag_selection", False):
            from bmc_agent.flag_selector import FlagSelector
            logger.info("--- Phase 1.5: Selecting per-function CBMC flags (%d fn) ---",
                        len(funcs_to_check))
            selector = FlagSelector(self.config, self.llm)
            flag_selections = selector.select_all(funcs_to_check)

        logger.info("--- Phase 2: Running BMC on %d functions ---", len(funcs_to_check))
        # Stash on self so the feedback-loop re-invocations of check_function
        # can pass the same per-function selection back through. Without this,
        # the iter-1 CBMC run drops --unsigned-overflow-check (and friends)
        # selected by Phase 1.5, which can silently "verify clean" the very
        # property the bug was on.
        self._flag_selections = flag_selections if flag_selections else {}

        verdicts = self.bmc_engine.check_all(
            funcs=funcs_to_check,
            specs=specs,
            parsed_file=parsed,
            driver_name=driver_name,
            all_funcs=all_funcs,
            flag_selections=flag_selections if flag_selections else None,
        )
        logger.info("Phase 2 complete: %d verdicts", len(verdicts))

        # ------------------------------------------------------------------
        # Phase 2b: Autonomous-mode CBMC-error auto-retry (Phase 1 of the
        # autonomous plan; see PLAN_autonomous_mode.md). For every function
        # whose CBMC run errored with a *known* structural failure mode
        # (parse_syntax_before_id, convert_type_redefinition, …), classify
        # the error, apply a runtime workaround (extend the session-strip
        # set or force-opaque a struct tag), and re-run CBMC for that
        # function. Bounded retry (default 2 attempts per function) so a
        # pathological error doesn't loop. Successful retries replace the
        # original verdict; failed retries leave the error in place for
        # Phase 3 to surface.
        verdicts = self._auto_retry_cbmc_errors(
            verdicts=verdicts,
            funcs=all_funcs,
            specs=specs,
            parsed_file=parsed,
            driver_name=driver_name,
            flag_selections=flag_selections,
        )

        # ------------------------------------------------------------------
        # Phase 3: Validate counterexamples, refine, report bugs
        # ------------------------------------------------------------------
        logger.info("--- Phase 3: Validating counterexamples ---")
        bug_reports: list[BugReport] = []
        # Latent reports: panics reachable on the pub API but no in-tree
        # caller produces the state. cargo-fuzz / future-caller risk; tracked
        # separately from bug_reports so triage can pick severity tier.
        latent_reports: list[BugReport] = []
        confirmed_latent: set[tuple[str, str]] = set()
        current_specs = dict(specs)  # may be updated by refinement
        recheck_queue: set[str] = set()   # callers to re-check after refinement
        self_recheck_queue: set[str] = set()  # refined fns to re-check themselves
        # RQ3: maps caller_name → set of refined functions that queued it for recheck
        recheck_triggered_by: dict[str, set[str]] = {}
        # Global re-queue bound: tracks how many times each function has been re-queued
        requeue_counts: dict[str, int] = {}
        # Cross-file callers (in OTHER files) of functions refined in THIS run.
        # They cannot be re-verified here (run() is per-file), but their refined
        # callee spec is persisted (save_spec) so they pick it up when their own
        # file is processed; record + log them for run_directory propagation.
        cross_file_recheck_needed: set[str] = set()
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

            for cex in _dedup_counterexamples(
                verdict.counterexamples,
                max_per_type=self.config.dedup_max_per_type,
            ):
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
                            "Skipping duplicate real bug for '%s' (property type '%s' already confirmed at non-downgraded confidence)",
                            fn_name, bug_key[1],
                        )
                    else:
                        # Static classifier accepted this CEx as a real-bug
                        # candidate. Realism runs INSIDE _make_report and may
                        # downgrade to 'unlikely' — the actual confirmed
                        # status is whatever survives that audit (visible in
                        # report.confidence after _make_report returns).
                        logger.info("Real-bug candidate (awaiting realism) in '%s'", fn_name)
                        report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs, cbmc_harness_path=getattr(verdict, "harness_path", ""))
                        self.reporter.save_report(report, driver_name)
                        bug_reports.append(report)
                        # Log post-realism outcome so the log shows what
                        # actually survived the audit, not just the static tag.
                        if getattr(report, "confidence", None) == "unlikely":
                            logger.info("Realism downgraded '%s' → unlikely (not counted as confirmed)", fn_name)
                        else:
                            logger.info("Realism upheld '%s' as %s", fn_name, getattr(report, "confidence", "?"))
                        # Only mark this property type as "done" if the report
                        # survived realism (confidence != "unlikely"). When
                        # realism downgrades CEx_1 as an artifact, keep the
                        # door open for CEx_2 / CEx_3 of the same property
                        # type — the deeper indices often expose the real
                        # bug behind the artifact.
                        if getattr(report, "confidence", "") != "unlikely":
                            confirmed_real_bugs.add(bug_key)
                elif validation.is_latent_bug:
                    latent_key = (fn_name, _prop_type(cex.failing_property))
                    if latent_key in confirmed_latent:
                        logger.debug(
                            "Skipping duplicate latent bug for '%s' (type '%s')",
                            fn_name, latent_key[1],
                        )
                    else:
                        confirmed_latent.add(latent_key)
                        logger.info(
                            "LATENT panic on pub API of '%s' — no in-tree caller "
                            "reaches state, future-caller / cargo-fuzz risk",
                            fn_name,
                        )
                        report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs, cbmc_harness_path=getattr(verdict, "harness_path", ""))
                        self.reporter.save_latent_report(report, driver_name)
                        latent_reports.append(report)
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
                        # Cross-file callers: can't re-verify them in this per-file
                        # run, but record them so run_directory can re-process their
                        # files against the now-refined callee spec (persisted above).
                        for xcaller in (cross_file_caller_contexts or {}).get(fn_name, []):
                            xinfo = xcaller[0] if isinstance(xcaller, (tuple, list)) else xcaller
                            xname = getattr(xinfo, "name", None)
                            if xname and xname not in all_funcs:
                                cross_file_recheck_needed.add(xname)
                        # Also re-run CBMC on the function itself under the
                        # refined precondition — the spurious CEx may have
                        # masked a real bug (CEGAR: tighten abstract domain,
                        # re-verify).
                        self_recheck_queue.add(fn_name)

        # ------------------------------------------------------------------
        # Phase 3b: synthesize per-function sibling-CEx summary into
        # bug_report.json so the persisted record reflects ALL property
        # checks for the function, not just the first one that saved.
        # Background: bug_report.json is saved when the first real-bug
        # CEx is classified, but later sibling CExes for the same
        # function may flip to SPURIOUS / UNRESOLVED — leaving the
        # persisted report wrongly looking like a clean confirmed bug.
        # This synthesis decorates the report with sibling stats and
        # downgrades confidence when sibling instability is high.
        # ------------------------------------------------------------------
        for fn_name in {r.function_name for r in bug_reports}:
            try:
                self._synthesize_sibling_cex_summary(driver_name, fn_name)
            except Exception as exc:
                logger.warning(
                    "Sibling-CEx synthesis failed for '%s': %s", fn_name, exc,
                )

        # ------------------------------------------------------------------
        # Phase 3d: three-oracle disagreement diagnosis. When BMC says
        # FAIL, realism says REALISTIC, and dyn-val says NOT_TRIGGERED,
        # the three oracles contradict — most often because the harness
        # admits a state real callers can't produce. Run a single LLM
        # call to diagnose which oracle to trust:
        #   * PROPERTY_FP        → downgrade confidence to 'unlikely'.
        #   * SPEC_REFINE        → persist the suggested clause and
        #                          enqueue for Phase 3c re-verification.
        #   * HARNESS_ENCODING   → persist the suggested __CPROVER_assume
        #                          (bare clause) and enqueue.
        #   * INCONCLUSIVE       → attach for review only.
        # ------------------------------------------------------------------
        for fn_name in {r.function_name for r in bug_reports}:
            try:
                if self._diagnose_oracle_disagreements(driver_name, fn_name):
                    # SPEC_REFINE / HARNESS_ENCODING persisted a clause
                    # — Phase 3c picks it up via _emit_learned_clauses
                    # and re-runs BMC under the tighter PRE.
                    self_recheck_queue.add(fn_name)
            except Exception as exc:
                logger.warning(
                    "Oracle-disagreement diagnosis failed for '%s': %s",
                    fn_name, exc,
                )

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
                for cex in _dedup_counterexamples(
                    verdict.counterexamples,
                    max_per_type=self.config.dedup_max_per_type,
                ):
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
                            report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs, cbmc_harness_path=getattr(verdict, "harness_path", ""))
                            self.reporter.save_report(report, driver_name)
                            bug_reports.append(report)
                    elif validation.is_latent_bug:
                        latent_key = (fn_name, _prop_type(cex.failing_property))
                        if latent_key not in confirmed_latent:
                            confirmed_latent.add(latent_key)
                            logger.info(
                                "LATENT (Phase 3c) panic on pub API of '%s'",
                                fn_name,
                            )
                            report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs, cbmc_harness_path=getattr(verdict, "harness_path", ""))
                            self.reporter.save_latent_report(report, driver_name)
                            latent_reports.append(report)
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
                for cex in _dedup_counterexamples(
                    verdict.counterexamples,
                    max_per_type=self.config.dedup_max_per_type,
                ):
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
                            report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs, cbmc_harness_path=getattr(verdict, "harness_path", ""))
                            self.reporter.save_report(report, driver_name)
                            bug_reports.append(report)
                            phase3b_bugs_by_fn.setdefault(fn_name, []).append(
                                f"{fn_name}:{cex.failing_property}"
                            )
                    elif validation.is_latent_bug:
                        latent_key = (fn_name, _prop_type(cex.failing_property))
                        if latent_key not in confirmed_latent:
                            confirmed_latent.add(latent_key)
                            logger.info(
                                "LATENT (recheck) panic on pub API of '%s'", fn_name,
                            )
                            report = self._make_report(validation, func, spec, parsed, all_funcs, driver_name, current_specs, cbmc_harness_path=getattr(verdict, "harness_path", ""))
                            self.reporter.save_latent_report(report, driver_name)
                            latent_reports.append(report)

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
        # Phase 3e: in-pipeline triage of UNRESOLVED counterexamples.
        # After Phase 3b drains caller-rechecks, every CEx that ended
        # UNRESOLVED (caller-chain trace + dyn-val + spec-refiner
        # disagreed, or the soundness guard refused over-tightening)
        # gets a second-opinion verdict from the tool-augmented
        # TriageToolsAgent. The agent walks the call chain, audits
        # size calculators against writers, and applies reachability
        # gates (private-header, alloc-site-invariant) before voting.
        # REAL_BUG / high promotes to a bug report; LIKELY_FP / high
        # writes a triage sidecar so post-hoc summaries can prune
        # without re-running triage.
        # ------------------------------------------------------------------
        if (
            getattr(self.config, "enable_phase_3e_triage", False)
            and self.reporter._unresolved
        ):
            try:
                self._run_phase_3e_triage(
                    driver_name=driver_name,
                    parsed=parsed,
                    all_funcs=all_funcs,
                    current_specs=current_specs,
                    bug_reports=bug_reports,
                    confirmed_real_bugs=confirmed_real_bugs,
                )
            except Exception as exc:
                logger.warning(
                    "Phase 3e triage block failed: %s", exc, exc_info=True,
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

        self._emit_coverage_diagnostics(driver_name)

        logger.info(
            "=== AMC Pipeline END: %d real bug(s), %d latent, %d unresolved ===",
            len(bug_reports),
            len(latent_reports),
            len(self.reporter._unresolved),
        )
        # Stash latent reports on the pipeline so the CLI can access them
        # without changing the long-stable return type. Callers that don't
        # care (eval scripts, tests) keep working unchanged.
        self.latent_reports = latent_reports
        # Cross-file callers (in other files) whose verification should be
        # re-run against specs refined in this file. Stashed for run_directory
        # to propagate; the refined callee specs are already persisted.
        self.cross_file_recheck_needed = cross_file_recheck_needed
        if cross_file_recheck_needed:
            logger.info(
                "Cross-file propagation: %d caller(s) in other files affected by "
                "refinements here (re-run their files to pick up refined specs): %s",
                len(cross_file_recheck_needed),
                ", ".join(sorted(cross_file_recheck_needed)[:20]),
            )
        return bug_reports

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _diagnose_oracle_disagreements(
        self, driver_name: str, fn_name: str,
    ) -> bool:
        """Phase 3d: detect a three-oracle disagreement on this
        function's saved bug_report and run a single LLM diagnosis when
        one is found. The diagnosis is attached to bug_report.json under
        ``oracle_disagreement_diagnosis``.

        Auto-application policy:

        * PROPERTY_FP → downgrade confidence to ``unlikely``.
        * SPEC_REFINE / HARNESS_ENCODING → persist the LLM-proposed
          clause to ``learned_constraints.json`` (function_clauses
          slot for ``fn_name``). The harness-gen path's
          ``_emit_learned_clauses`` picks it up on the next BMC run.
          Returns True so the caller adds ``fn_name`` to the
          re-verification queue.
        * INCONCLUSIVE → attach but no auto-action.

        Returns True when the function should be re-verified under a
        newly-persisted clause.
        """
        import json as _json
        from pathlib import Path
        from bmc_agent.oracle_disagreement import (
            DiagnosisVerdict,
            apply_diagnosis,
            detect_disagreement,
            diagnose,
            persist_diagnosis_to_learned_constraints,
        )

        fn_dir = Path(self.store._fn_dir(driver_name, fn_name))
        br_path = fn_dir / "bug_report.json"
        if not br_path.exists():
            return False

        try:
            payload = _json.loads(br_path.read_text())
        except Exception:
            return False
        report = payload.get("report") or {}
        if not isinstance(report, dict):
            return False

        case = detect_disagreement(report)
        if case is None:
            return False

        logger.info(
            "Oracle-disagreement detected for '%s' (%s): "
            "BMC fail + realism=%s + dyn=%s — diagnosing",
            fn_name, case.kind.value,
            case.realism_verdict, case.dyn_outcome,
        )

        diagnosis = diagnose(case, self.llm)
        if diagnosis is None:
            logger.info(
                "Oracle-disagreement diagnosis for '%s' returned no result "
                "— leaving finding unchanged",
                fn_name,
            )
            return False

        report = apply_diagnosis(report, diagnosis)
        payload["report"] = report
        br_path.write_text(_json.dumps(payload, indent=2, default=str))

        logger.info(
            "Oracle-disagreement diagnosis for '%s': verdict=%s "
            "confidence=%s",
            fn_name, diagnosis.verdict.value, diagnosis.confidence,
        )

        # Auto-apply for SPEC_REFINE / HARNESS_ENCODING — persist the
        # LLM-proposed clause so Phase 3c re-runs BMC under the tighter
        # spec. PROPERTY_FP and INCONCLUSIVE return False; the
        # downgrade for PROPERTY_FP is enough action on its own.
        if diagnosis.verdict in (
            DiagnosisVerdict.SPEC_REFINE,
            DiagnosisVerdict.HARNESS_ENCODING,
        ):
            persisted = persist_diagnosis_to_learned_constraints(
                self.config,
                fn_name,
                diagnosis,
                source_property=str(report.get("violated_property") or ""),
            )
            if persisted:
                logger.info(
                    "Phase 3d: persisted '%s' clause for '%s' — will re-verify",
                    diagnosis.verdict.value, fn_name,
                )
                return True
        return False

    def _run_phase_3e_triage(
        self,
        *,
        driver_name: str,
        parsed: ParsedCFile,
        all_funcs: dict[str, FunctionInfo],
        current_specs: dict[str, Spec],
        bug_reports: list,
        confirmed_real_bugs: set,
    ) -> None:
        """Phase 3e: independent triage of UNRESOLVED counterexamples.

        Mirrors ``scripts/triage_unresolved.py``'s contract so the
        sidecar layout is identical to the post-hoc path — analysts
        can compare pipeline-time vs post-hoc verdicts without two
        parsers. Promotes only ``REAL_BUG/high`` to bug reports;
        anything weaker stays in ``_unresolved`` so we never inflate
        the confirmed-bug count from a borderline triage call.
        """
        import json as _json

        from bmc_agent.agents.triage import TriageVerdict
        from bmc_agent.agents.triage_tools import TriageToolsAgent

        unresolved_snapshot = list(self.reporter._unresolved)
        if not unresolved_snapshot:
            return

        logger.info(
            "--- Phase 3e: Triaging %d UNRESOLVED counterexample(s) ---",
            len(unresolved_snapshot),
        )

        corpus_paths = list(getattr(self.spec_gen, "corpus_paths", []) or [])
        agent = TriageToolsAgent(
            self.config,
            self.llm,
            parsed_file=parsed,
            corpus_paths=corpus_paths,
            all_specs=current_specs,
        )

        promoted: list[int] = []
        counts = {"real_bug": 0, "likely_fp": 0, "needs_human": 0, "errored": 0}

        for idx, validation in enumerate(unresolved_snapshot):
            fn_name = validation.function_name
            func = all_funcs.get(fn_name)
            spec = current_specs.get(fn_name)
            cex = validation.counterexample
            prop = getattr(cex, "failing_property", "") or ""
            if func is None or spec is None or not prop:
                counts["errored"] += 1
                continue

            try:
                harness_path = self.store._fn_dir(driver_name, fn_name) / "harness.c"
                harness_text = (
                    harness_path.read_text(encoding="utf-8", errors="replace")
                    if harness_path.exists()
                    else ""
                )
            except Exception:
                harness_text = ""

            witness_lines: list[str] = []
            for k, v in (getattr(cex, "variable_assignments", {}) or {}).items():
                if k.startswith("__CPROVER_"):
                    continue
                witness_lines.append(f"  {k} = {v}")
            witness_text = "\n".join(witness_lines)

            dyn = getattr(validation, "dynamic_result", None)
            dyn_outcome = None
            dyn_reasoning = None
            if dyn is not None:
                outcome_val = getattr(dyn, "outcome", None)
                dyn_outcome = (
                    outcome_val.value if hasattr(outcome_val, "value")
                    else (str(outcome_val) if outcome_val is not None else None)
                )
                dyn_reasoning = getattr(dyn, "reasoning", None)

            logger.info("Phase 3e: triaging %s::%s", fn_name, prop)
            result = agent.run(
                function_name=fn_name,
                function_source=func.body or "",
                cbmc_property=prop,
                harness_source=harness_text,
                witness_text=witness_text,
                caller_path=validation.caller_path or [],
                dyn_outcome=dyn_outcome,
                dyn_reasoning=dyn_reasoning,
                reproducer_source=validation.system_entry_input or "",
                realism_verdict=None,
                realism_reasoning=None,
                pipeline_reasoning=validation.reasoning or "",
                sys_entry_reached=bool(
                    getattr(validation, "system_entry_reached", False)
                ),
            )
            if not result.ok or result.output is None:
                counts["errored"] += 1
                logger.warning(
                    "Phase 3e: triage failed for %s::%s (%s)",
                    fn_name, prop, result.error or "no output",
                )
                continue

            tr = result.output
            counts[tr.verdict.value] = counts.get(tr.verdict.value, 0) + 1

            try:
                safe = "".join(
                    ch if ch.isalnum() or ch in "._-" else "_"
                    for ch in str(prop)
                )[:120] or "unnamed"
                cls_dir = (
                    self.store._fn_dir(driver_name, fn_name) / "classifications"
                )
                cls_dir.mkdir(parents=True, exist_ok=True)
                sidecar = cls_dir / f"{safe}.triage.json"
                sidecar.write_text(_json.dumps({
                    "verdict": tr.verdict.value,
                    "confidence": tr.confidence,
                    "fp_class": tr.fp_class,
                    "reasoning": tr.reasoning,
                    "phase": "3e",
                }, indent=2))
            except Exception as exc:
                logger.warning(
                    "Phase 3e: sidecar write failed for %s::%s: %s",
                    fn_name, prop, exc,
                )

            if (
                tr.verdict == TriageVerdict.REAL_BUG
                and tr.confidence == "high"
            ):
                bug_key = (fn_name, _prop_type(prop))
                if bug_key in confirmed_real_bugs:
                    logger.info(
                        "Phase 3e: %s already confirmed (key=%s) — no double-promotion",
                        fn_name, bug_key[1],
                    )
                    promoted.append(idx)  # drop from unresolved either way
                    continue
                validation.outcome = CExOutcome.REAL_BUG
                validation.reasoning = (
                    (validation.reasoning or "")
                    + f"\n\n[Phase 3e triage: {tr.verdict.value}/{tr.confidence}"
                    + (f", fp_class={tr.fp_class}" if tr.fp_class else "")
                    + f"] {tr.reasoning[:600]}"
                )
                try:
                    report = self._make_report(
                        validation, func, spec, parsed, all_funcs,
                        driver_name, current_specs, cbmc_harness_path="",
                    )
                    self.reporter.save_report(report, driver_name)
                    bug_reports.append(report)
                    confirmed_real_bugs.add(bug_key)
                    promoted.append(idx)
                    logger.info(
                        "Phase 3e: PROMOTED %s::%s → REAL_BUG", fn_name, prop,
                    )
                except Exception as exc:
                    logger.warning(
                        "Phase 3e: promotion failed for %s::%s: %s",
                        fn_name, prop, exc,
                    )

        if promoted:
            promoted_set = set(promoted)
            self.reporter._unresolved = [
                v for i, v in enumerate(unresolved_snapshot)
                if i not in promoted_set
            ]

        logger.info(
            "Phase 3e complete: real_bug=%d (promoted=%d), likely_fp=%d, "
            "needs_human=%d, errored=%d",
            counts.get("real_bug", 0), len(promoted),
            counts.get("likely_fp", 0), counts.get("needs_human", 0),
            counts.get("errored", 0),
        )

    def _synthesize_sibling_cex_summary(
        self, driver_name: str, fn_name: str,
    ) -> None:
        """After all CExes for a function have been processed, re-save
        bug_report.json with a sibling_cex_summary field that reflects
        the FULL set of property checks — not just the first one that
        saved the report.

        Two outputs:

        (1) ``report.sibling_cex_summary`` field:
              {
                'total_cexes_validated': N,
                'real_bug_count': k1,
                'unresolved_count': k2,
                'spurious_count': k3,
                'latent_count': k4,
                'instability_signal': bool,
              }
            ``instability_signal`` is True when N > 1 AND
            (unresolved + spurious) / total >= 0.5 — i.e. the majority
            of sibling property checks weren't confirmed real bugs.

        (2) Confidence downgrade: when instability_signal is True AND
            the saved confidence is high (confirmed_*), downgrade to
            ``unlikely`` with a reasoning_trail note.

        Background: bug_report.json gets OVERWRITTEN by each save_report
        call, but only real-bug CExes call save_report. The first
        real-bug CEx wins; later UNRESOLVED/SPURIOUS sibling CExes
        for the same function (often the result of CBMC errors or
        over-refined preconditions) leave no trace in the top-level
        report. This synthesis surfaces that signal.
        """
        import json as _json
        from pathlib import Path

        fn_dir = self.store._fn_dir(driver_name, fn_name)
        cls_dir = Path(fn_dir) / "classifications"
        br_path = Path(fn_dir) / "bug_report.json"

        if not br_path.exists():
            return

        outcomes: list[str] = []
        if cls_dir.exists():
            for f in sorted(cls_dir.glob("*.json")):
                try:
                    payload = _json.loads(f.read_text())
                except Exception:
                    continue
                c = payload.get("classification") or {}
                o = (c.get("outcome") or "").lower().strip()
                if o:
                    outcomes.append(o)

        n_total = len(outcomes)
        if n_total == 0:
            return  # no per-CEx records — nothing to synthesize

        n_real      = sum(1 for o in outcomes if o == "real_bug")
        n_spurious  = sum(1 for o in outcomes if o == "spurious")
        n_unres     = sum(1 for o in outcomes if o == "unresolved")
        n_latent    = sum(1 for o in outcomes if o == "latent")

        instability = (
            n_total > 1
            and (n_unres + n_spurious) >= max(1, n_total // 2)
        )

        summary = {
            "total_cexes_validated": n_total,
            "real_bug_count": n_real,
            "unresolved_count": n_unres,
            "spurious_count": n_spurious,
            "latent_count": n_latent,
            "instability_signal": instability,
        }

        try:
            payload = _json.loads(br_path.read_text())
        except Exception:
            return
        report = payload.get("report") or {}
        if not isinstance(report, dict):
            return

        report["sibling_cex_summary"] = summary

        # Confidence downgrade when sibling instability is high. We
        # only demote from high-confidence tiers; if confidence is
        # already 'unlikely' or absent, leave it alone (no extra info
        # to add).
        #
        # EXCEPTION: if the report's realism check returned 'realistic',
        # don't downgrade. The realism verdict is an INDEPENDENT signal
        # — the LLM read this specific CEx's witness and judged it
        # reachable from real in-tree callers. Sibling CExes being
        # unresolved is noise from CBMC/harness limitations, not
        # evidence against the realistic verdict. (Regression motivating
        # this exception: postfix9 next_field had 3 of 10 CExes marked
        # real_bug at S1 — one with confirmed_bmc + realism=realistic
        # capturing the upstream-known PAX OOB read. The other 7
        # unresolved CExes are CBMC over-permissiveness on the harness's
        # nondet inputs, not evidence the realistic CEx is wrong. Without
        # this exception, the seed bug gets dropped to 'unlikely'.)
        original_confidence = report.get("confidence")
        realism_verdict = (
            (report.get("realism_check") or {}).get("verdict")
        )
        downgrade_targets = {"confirmed_dynamic", "confirmed_system_entry", "realistic"}
        if (
            instability
            and original_confidence in downgrade_targets
            and realism_verdict != "realistic"
        ):
            note = (
                f"\n\n[SIBLING-CEX INSTABILITY] {n_unres} unresolved + "
                f"{n_spurious} spurious sibling counterexamples out of "
                f"{n_total} total for this function. Confidence "
                f"downgraded from '{original_confidence}' to 'unlikely' "
                f"— the verdict is built on only {n_real} confirmed "
                f"CEx(es); the remaining property checks failed to "
                f"reach a clean verdict (often a sign of CBMC errors, "
                f"over-refined preconditions, or LLM-fallback "
                f"confabulation in the per-property reachability check)."
            )
            report["confidence"] = "unlikely"
            report["reasoning_trail"] = (
                (report.get("reasoning_trail") or "") + note
            )
            logger.warning(
                "Sibling-CEx instability for '%s': %d unresolved + %d "
                "spurious / %d total — downgrading confidence from "
                "'%s' to 'unlikely'",
                fn_name, n_unres, n_spurious, n_total, original_confidence,
            )
        elif (
            instability
            and original_confidence in downgrade_targets
            and realism_verdict == "realistic"
        ):
            # Log when we skip the downgrade — useful signal in sweep
            # logs that a realistic-verdict bug survived sibling noise.
            logger.info(
                "Sibling-CEx instability for '%s' (%d unresolved + %d "
                "spurious / %d total) — NOT downgrading because realism "
                "verdict was 'realistic'; realistic is an independent "
                "signal and sibling noise does not override it",
                fn_name, n_unres, n_spurious, n_total,
            )

        payload["report"] = report
        br_path.write_text(_json.dumps(payload, indent=2, default=str))

    def _make_report(
        self,
        validation: ValidationResult,
        func: FunctionInfo,
        spec: Spec,
        parsed: ParsedCFile,
        all_funcs: "dict[str, FunctionInfo]",
        driver_name: str,
        all_specs: "dict[str, Spec] | None" = None,
        cbmc_harness_path: str = "",
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
            # Route through check_with_tools_if_enabled so the realism
            # check optionally augments UNCERTAIN/UNREALISTIC verdicts
            # with a tool-using LLM pass. The wrapper falls back to
            # the base check transparently when the flag is off / fails.
            realism = self.realism_checker.check_with_tools_if_enabled(
                func=func,
                counterexample=validation.counterexample,
                validation_result=validation,
                parsed_file=parsed,
                all_funcs=all_funcs,
                spec=spec,
                all_specs=all_specs,
                cbmc_harness_path=cbmc_harness_path,
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

        # Realism-feedback-driven spec refiner. Complementary to
        # _feedback_iterate: that one drives via the general distill
        # prompt and writes to learned_constraints.json (effective next
        # sweep). This one drives via realism's concrete key_concern,
        # asks the LLM for the precise targeted clause, re-verifies
        # in-sweep, and rejects if the targeted CEx is still present.
        # Opt-in via --enable-spec-refiner.
        if getattr(self.config, "enable_spec_refiner", False):
            try:
                validation, realism = self._spec_refine_iterate(
                    validation, realism, func, spec, parsed, all_funcs,
                    driver_name, all_specs or {},
                )
            except Exception as exc:
                logger.warning(
                    "Spec refiner failed for '%s': %s", func.name, exc,
                )

        # Scenario-guided dynamic re-attempt.
        # When realism says REALISTIC but the dynamic harness either didn't
        # crash (`not_triggered`) or wasn't conclusive, ask the LLM to
        # translate realism's attacker_scenario into a concrete C reproducer
        # and re-run dynamic. If the new run crashes, attach the result so
        # the finding tier is promoted to confirmed_dynamic.
        try:
            self._try_scenario_guided_dynamic(
                validation, realism, func, parsed, all_funcs,
            )
        except Exception as exc:
            logger.warning(
                "Scenario-guided dynamic re-attempt failed for '%s': %s",
                func.name, exc,
            )

        realism_arg = realism if self.config.enable_realism_check else None
        return self.reporter.create_report(validation, func, realism_check=realism_arg)

    def _try_scenario_guided_dynamic(
        self,
        validation,
        realism,
        func,
        parsed,
        all_funcs,
    ) -> None:
        """If realism says REALISTIC but dynamic isn't `confirmed`, fire the
        scenario_reproducer LLM to translate realism's attacker_scenario
        into a concrete C reproducer, then ask DynamicValidator to
        compile+run it. Mutates ``validation.dynamic_result`` in place when
        the new run crashes. No-op otherwise.
        """
        from bmc_agent.realism_checker import RealismVerdict
        from bmc_agent.dynamic_validator import DynamicOutcome

        if not getattr(self.config, "enable_dynamic_validation", False):
            return
        if realism is None or realism.verdict != RealismVerdict.REALISTIC:
            return
        # If dynamic already confirmed, nothing to gain by retrying.
        dyn = getattr(validation, "dynamic_result", None)
        if dyn is not None and dyn.outcome == DynamicOutcome.CONFIRMED:
            return
        scenario = (getattr(realism, "key_concern", "") or "").strip()
        if not scenario or scenario.startswith("["):
            # "[" prefixes are internal tags like "[auto-downgraded]" — not
            # real attacker scenarios.
            return

        # Get a DynamicValidator instance from the existing CExValidator
        # (it already has one when --enable-dynamic-validation is on).
        dyn_validator = getattr(self.validator, "_dynamic_validator", None)
        if dyn_validator is None:
            return

        from bmc_agent.scenario_reproducer import generate_reproducer
        reproducer = generate_reproducer(func, scenario, parsed, self.llm)
        if reproducer is None:
            return

        logger.info(
            "Scenario-guided dynamic: running LLM-derived reproducer for '%s' "
            "(%d chars)", func.name, len(reproducer),
        )
        new_dyn = dyn_validator.validate(
            entry_func=func,
            counterexample=validation.counterexample,
            parsed_file=parsed,
            all_funcs=all_funcs,
            caller_path=validation.caller_path,
            system_entry_reproducer=reproducer,
        )
        if new_dyn.outcome == DynamicOutcome.CONFIRMED:
            logger.info(
                "Scenario-guided dynamic CONFIRMED '%s' (signal=%s)",
                func.name, new_dyn.signal_name,
            )
            validation.dynamic_result = new_dyn
        else:
            logger.info(
                "Scenario-guided dynamic for '%s' did not crash "
                "(outcome=%s); leaving original dynamic_result", func.name,
                new_dyn.outcome.value,
            )

    def _auto_retry_cbmc_errors(
        self,
        verdicts: dict,
        funcs: dict,
        specs: dict,
        parsed_file,
        driver_name: str,
        flag_selections: Optional[dict] = None,
    ) -> dict:
        """Phase 1 of autonomous mode: classify CBMC errors and re-run with
        a structural workaround applied.

        For each function whose Phase 2 verdict is an error (no
        counterexamples, only an ``error`` field), classify the error via
        :mod:`bmc_agent.cbmc_error_classifier`, plan a recovery action via
        :mod:`bmc_agent.auto_retry_registry`, apply it (mutate
        ``config.session_strip_typedefs`` / ``session_strip_structs`` /
        ``session_opaque_param_structs``), and call ``bmc_engine.check_function``
        again. If the retry produces a real verdict (verified or with CEx),
        replace the original verdict.

        Bounded by ``config.auto_retry_max_rounds`` (default 2) globally —
        each round can resolve multiple functions if they share an error
        identifier (e.g. all 1318 functions with ``syntax error before
        'off64_t'`` get fixed by a single ADD_TYPEDEF_TO_STRIP entry).

        Writes ``auto_retries.json`` to the driver artifact directory with
        a per-attempt audit log: function, attempt #, error class, action,
        target, outcome (resolved / still_errored / no_action). Used by
        the autonomous outer loop (Phase 2 of the plan) to promote
        successful entries into the static strip sets after human review.
        """
        from bmc_agent.cbmc_error_classifier import (
            CbmcErrorClass,
            CbmcErrorDiagnosis,
            classify,
        )
        from bmc_agent.auto_retry_registry import RetryAction, plan_retry

        max_rounds = int(getattr(self.config, "auto_retry_max_rounds", 2))
        if max_rounds <= 0:
            return verdicts

        retry_log: list[dict] = []
        verdicts = dict(verdicts)  # mutable copy

        for round_idx in range(max_rounds):
            errored = {
                fn: v for fn, v in verdicts.items()
                if (v.error and not v.counterexamples)
            }
            if not errored:
                break

            # Classify + plan, deduplicated by (action, target). Multiple
            # functions hitting the same root cause (e.g. all referencing
            # the same stripped typedef) share one config mutation.
            plans_by_key: dict[tuple, list[str]] = {}
            for fn_name, verdict in errored.items():
                cbmc_res = verdict.cbmc_result
                if cbmc_res is None:
                    continue
                payload = {
                    "error": getattr(cbmc_res, "error", verdict.error),
                    "raw_output": getattr(cbmc_res, "raw_output", "") or "",
                    "verified": getattr(cbmc_res, "verified", None),
                }
                diag = classify(payload)
                plan = plan_retry(diag)
                key = (plan.action, plan.target)
                plans_by_key.setdefault(key, []).append(fn_name)
                retry_log.append({
                    "round": round_idx,
                    "function": fn_name,
                    "error_class": diag.error_class.value,
                    "action": plan.action.value,
                    "target": plan.target,
                    "reason": plan.reason,
                })

            # Apply each distinct plan to the config session sets.
            # BUMP_TIMEOUT is per-function so it doesn't need a target —
            # the bump happens in the per-function retry loop below
            # using flag_selections[fn_name]. Tag the action as
            # actionable here so the retry round doesn't bail out as
            # "all NO_ACTION".
            actionable_keys = [
                (a, t) for (a, t) in plans_by_key
                if a != RetryAction.NO_ACTION and (
                    t or a in (RetryAction.BUMP_TIMEOUT, RetryAction.STUB_CALLEE)
                )
            ]
            if not actionable_keys:
                # Phase 3 escalation: if every plan in this round was
                # NO_ACTION (typically because the CBMC error class is
                # not yet in the static taxonomy) AND the operator has
                # opted into self-patch via ``config.allow_self_patch``,
                # invoke the agent to propose a structural fix.
                # ``deny`` (default) skips the call entirely — no LLM
                # is asked to edit bmc-agent source.
                self_patch_log = self._maybe_invoke_self_patch(
                    errored=errored,
                    funcs=funcs,
                    parsed_file=parsed_file,
                    driver_name=driver_name,
                    round_idx=round_idx,
                )
                if self_patch_log:
                    retry_log.append({
                        "round": round_idx,
                        "self_patch": self_patch_log,
                    })
                logger.info(
                    "Phase 2b auto-retry round %d: no actionable plans "
                    "(%d errors, all NO_ACTION) — giving up",
                    round_idx, len(errored),
                )
                break

            applied_summary = []
            for (action, target) in actionable_keys:
                if action == RetryAction.ADD_TYPEDEF_TO_STRIP:
                    if target not in self.config.session_strip_typedefs:
                        self.config.session_strip_typedefs.append(target)
                        applied_summary.append(f"strip typedef '{target}'")
                elif action == RetryAction.ADD_STRUCT_TO_STRIP:
                    if target not in self.config.session_strip_structs:
                        self.config.session_strip_structs.append(target)
                        applied_summary.append(f"strip struct '{target}'")
                elif action == RetryAction.FORCE_OPAQUE_PARAM:
                    if target not in self.config.session_opaque_param_structs:
                        self.config.session_opaque_param_structs.append(target)
                        applied_summary.append(f"force-opaque struct '{target}'")

            logger.info(
                "Phase 2b auto-retry round %d: %d errors, %d distinct fixes "
                "applied: %s",
                round_idx,
                len(errored),
                len(actionable_keys),
                "; ".join(applied_summary) or "(none)",
            )

            # Per-function recovery for TIMEOUT errors:
            #
            #   1. PRIMARY: STUB_CALLEE — pick a heavy callee from the
            #      function's call-graph entry, add it to
            #      session_stub_functions. The harness regen consults
            #      that set and post-processes the included source to
            #      replace the callee's body with a nondet stub. Cuts
            #      CBMC's state space at the source (rather than buying
            #      it more time to chew through the same explosion).
            #
            #   2. FALLBACK: BUMP_TIMEOUT — when no callee candidate
            #      exists (function has no local callees, or all
            #      candidates are already stubbed), double the per-
            #      function timeout (capped at 600s).
            #
            # Both actions are per-function; the global session set
            # mutation for STUB_CALLEE is accumulative across rounds.
            from bmc_agent.flag_selector import FlagSelection
            stubbed_callees: dict[str, str] = {}
            bumped_timeouts: dict[str, int] = {}
            for fn_name, verdict in errored.items():
                cbmc_res = verdict.cbmc_result
                if cbmc_res is None:
                    continue
                diag = classify({
                    "error": getattr(cbmc_res, "error", verdict.error),
                    "raw_output": getattr(cbmc_res, "raw_output", "") or "",
                    "verified": getattr(cbmc_res, "verified", None),
                })
                if diag.error_class != CbmcErrorClass.TIMEOUT:
                    continue

                # PRIMARY: try STUB_CALLEE. Pick the longest local
                # callee body that's not already in session_stub_functions.
                # Skip system / libc functions: their names match
                # ``_SYSTEM_FUNCTION_NAMES`` is the obvious filter but
                # we don't have access to it here, so use a simpler
                # heuristic — only stub a callee if its body lives in
                # this same parsed_file's functions dict.
                already_stubbed = set(
                    getattr(self.config, "session_stub_functions", None) or []
                )
                fut = funcs.get(fn_name)
                picked: Optional[str] = None
                if fut is not None and getattr(fut, "callees", None):
                    candidates = []
                    for callee_name in fut.callees:
                        if callee_name in already_stubbed:
                            continue
                        callee_info = funcs.get(callee_name)
                        if callee_info is None:
                            continue  # only stub LOCAL callees (with a body in TU)
                        body = (callee_info.body or "")
                        candidates.append((len(body), callee_name))
                    if candidates:
                        candidates.sort(reverse=True)  # longest body first
                        picked = candidates[0][1]
                if picked:
                    self.config.session_stub_functions.append(picked)
                    stubbed_callees[fn_name] = picked
                    continue  # don't also bump-timeout this fn this round

                # FALLBACK: BUMP_TIMEOUT.
                if flag_selections is None:
                    flag_selections = {}
                fs = flag_selections.get(fn_name)
                current = (
                    fs.timeout_override
                    if (fs is not None and fs.timeout_override is not None)
                    else int(getattr(self.config, "cbmc_timeout", 120))
                )
                new_to = min(current * 2, 600)
                if new_to <= current:
                    continue  # at the 600s cap
                if fs is None:
                    flag_selections[fn_name] = FlagSelection(
                        timeout_override=new_to,
                        reasoning=f"auto-retry: bumped timeout {current}s → {new_to}s",
                    )
                else:
                    fs.timeout_override = new_to
                bumped_timeouts[fn_name] = new_to

            if stubbed_callees:
                logger.info(
                    "Phase 2b auto-retry round %d: stubbed %d callee(s) "
                    "to recover from TIMEOUT: %s",
                    round_idx, len(stubbed_callees),
                    ", ".join(
                        f"{fn}→stub({c})" for fn, c in stubbed_callees.items()
                    ),
                )
            if bumped_timeouts:
                logger.info(
                    "Phase 2b auto-retry round %d: bumped CBMC timeout for "
                    "%d function(s): %s",
                    round_idx, len(bumped_timeouts),
                    ", ".join(f"{n}={t}s" for n, t in bumped_timeouts.items()),
                )

            # Re-run CBMC for every errored function (the session sets are
            # now mutated; harness regen will pick them up).
            for fn_name in list(errored.keys()):
                func = funcs.get(fn_name)
                spec = specs.get(fn_name)
                if func is None or spec is None:
                    continue
                flag_sel = (flag_selections or {}).get(fn_name) if flag_selections else None
                try:
                    new_verdict = self.bmc_engine.check_function(
                        func=func,
                        spec=spec,
                        parsed_file=parsed_file,
                        driver_name=driver_name,
                        all_funcs=funcs,
                        flag_selection=flag_sel,
                    )
                except Exception as exc:
                    logger.warning(
                        "Phase 2b auto-retry: check_function('%s') raised %s; "
                        "leaving original verdict",
                        fn_name, exc,
                    )
                    continue
                verdicts[fn_name] = new_verdict
                # Tag the log entry with the outcome.
                for entry in retry_log:
                    if entry["round"] == round_idx and entry["function"] == fn_name:
                        if new_verdict.error and not new_verdict.counterexamples:
                            entry["outcome"] = "still_errored"
                        else:
                            entry["outcome"] = "resolved"
                        break

        if retry_log:
            self._persist_auto_retries(driver_name, retry_log)
            resolved = sum(
                1 for e in retry_log if e.get("outcome") == "resolved"
            )
            logger.info(
                "Phase 2b auto-retry: total %d retry attempts across %d rounds; "
                "%d resolved",
                len(retry_log),
                max_rounds,
                resolved,
            )

        return verdicts

    def _maybe_invoke_self_patch(
        self,
        errored: dict,
        funcs: dict,
        parsed_file,
        driver_name: str,
        round_idx: int,
    ) -> list[dict]:
        """Phase 3 entry point: when Phase 2b ran out of actionable
        plans for a round, ask the self-patch agent (if enabled) to
        propose a structural fix for *one* UNKNOWN-class error.

        Returns an audit log of the proposals attempted in this round.
        Each entry: ``{function, error_class, status, rejection_reason,
        rationale, artifact_path}``. Empty list when ``allow_self_patch
        == 'deny'`` (the default) — no LLM call is made.

        Only one proposal per round to bound LLM cost. The retry loop
        will pick up the next round naturally if the patch was applied.
        """
        from bmc_agent.self_patch_agent import (
            PatchMode, ProposalStatus, SelfPatchAgent, _resolve_mode,
        )
        from bmc_agent.cbmc_error_classifier import classify

        mode = _resolve_mode(self.config)
        if mode == PatchMode.DENY:
            return []

        # Pick one errored function (the first whose diagnosis was
        # UNKNOWN this round, so the agent has the most novel target).
        target_fn: str | None = None
        target_diag = None
        target_verdict = None
        for fn_name, verdict in errored.items():
            cbmc_res = verdict.cbmc_result
            if cbmc_res is None:
                continue
            payload = {
                "error": getattr(cbmc_res, "error", verdict.error),
                "raw_output": getattr(cbmc_res, "raw_output", "") or "",
                "verified": getattr(cbmc_res, "verified", None),
            }
            diag = classify(payload)
            if not diag.actionable:
                continue
            target_fn = fn_name
            target_diag = diag
            target_verdict = verdict
            break

        if target_fn is None or target_diag is None:
            return []

        from bmc_agent.llm import LLMClient
        from pathlib import Path
        agent = SelfPatchAgent(
            llm=LLMClient(self.config),
            repo_root=Path(__file__).resolve().parent.parent,
            config=self.config,
        )

        # Pull the generator excerpt: read the function in
        # harness_generator.py whose name appears in the diagnosis's
        # raw_message context, or the full type-strip section as a
        # default. Cheap heuristic — could be smarter.
        gen_excerpt = self._read_strip_section_excerpt()

        proposal = agent.propose(
            diagnosis=target_diag,
            function_name=target_fn,
            harness_path=str(getattr(target_verdict, "harness_path", "")),
            generator_excerpt=gen_excerpt,
            known_actions=self._summarize_known_retry_actions(),
        )

        if proposal.status == ProposalStatus.PROPOSED:
            proposal = agent.validate(proposal)
        if proposal.status == ProposalStatus.PROPOSED:
            output_root = Path(self.config.artifact_dir) / driver_name
            proposal = agent.stage_or_apply(
                proposal, output_root=output_root, round_idx=round_idx,
            )

        return [{
            "function": target_fn,
            "error_class": proposal.error_class,
            "error_target": proposal.error_target,
            "status": proposal.status.value,
            "rejection_reason": proposal.rejection_reason,
            "rationale": proposal.rationale[:400],
            "files_touched": proposal.files_touched,
            "lines_changed": proposal.lines_changed,
        }]

    def _read_strip_section_excerpt(self) -> str:
        """Return a focused excerpt of ``harness_generator.py`` for the
        agent prompt. The strip-section is where most new structural
        bugs need addressing.
        """
        from pathlib import Path as _P
        try:
            src = (_P(__file__).resolve().parent / "harness_generator.py").read_text()
        except Exception:
            return ""
        # Grab the SYSTEM_TYPEDEF_NAMES and GLIBC_KNOWN_STRUCTS regions
        # — these are where ~95% of new structural fixes belong.
        marker_a = src.find("_SYSTEM_TYPEDEF_NAMES")
        marker_b = src.find("_GLIBC_KNOWN_STRUCTS")
        if marker_a == -1 or marker_b == -1:
            return src[:6000]
        excerpt_a = src[marker_a:src.find("})", marker_a) + 2]
        excerpt_b = src[marker_b:src.find("})", marker_b) + 2]
        return excerpt_a + "\n\n" + excerpt_b

    def _summarize_known_retry_actions(self) -> str:
        """Brief description of the Phase 1 retry registry so the agent
        doesn't propose duplicates.
        """
        return (
            "* ADD_TYPEDEF_TO_STRIP — extends _SYSTEM_TYPEDEF_NAMES at\n"
            "  runtime. Use this when a typedef is conflict-redefining\n"
            "  or referenced but undefined.\n"
            "* ADD_STRUCT_TO_STRIP — extends _GLIBC_KNOWN_STRUCTS. Use\n"
            "  when a struct/union body redefines CBMC's built-in libc.\n"
            "* FORCE_OPAQUE_PARAM — emits nondet pointer for params of\n"
            "  the named struct tag. Use for 'incomplete type not\n"
            "  permitted here' on opaque-handle params.\n"
            "If the bug doesn't fit one of these, you need a structural\n"
            "patch — propose code changes."
        )

    def _persist_auto_retries(self, driver_name: str, log_entries: list[dict]) -> None:
        """Write the auto-retry audit log to ``<artifact_dir>/<driver>/auto_retries.json``."""
        import json as _json
        from pathlib import Path
        driver_dir = Path(self.config.artifact_dir) / driver_name
        try:
            driver_dir.mkdir(parents=True, exist_ok=True)
            (driver_dir / "auto_retries.json").write_text(
                _json.dumps(log_entries, indent=2)
            )
        except Exception as exc:
            logger.warning("Failed to persist auto_retries.json: %s", exc)

    def _emit_coverage_diagnostics(self, driver_name: str) -> None:
        """Aggregate CBMC parse/conversion errors and surface them.

        A run with 0 real bugs is ambiguous: it could be a genuine
        clean-verify, or every CBMC invocation could have failed at
        parse time (build-config macro missing, harness syntax bug)
        and produced no information. Scan all cbmc_result.json files
        for failure patterns we can recognise and emit a single
        summary log + a JSON artifact so the outcome is unambiguous.
        """
        import json as _json
        import re as _re

        driver_dir = self.store.base_dir / driver_name
        if not driver_dir.exists():
            return

        undef_symbols: dict[str, int] = {}
        bad_assert = 0
        struct_typedef = 0
        other_parse_err = 0
        total_cbmc = 0
        clean = 0
        failed = 0

        sym_re = _re.compile(r"failed to find symbol '([^']+)'")
        assert_re = _re.compile(
            r'macro "assert" passed \d+ arguments?, but takes just 1'
        )
        struct_re = _re.compile(r"syntax error before '='")

        for fn_dir in sorted(driver_dir.iterdir()):
            if not fn_dir.is_dir():
                continue
            cbmc_path = fn_dir / "cbmc_result.json"
            if not cbmc_path.exists():
                continue
            total_cbmc += 1
            try:
                data = _json.loads(cbmc_path.read_text())
            except Exception:
                continue
            result = data.get("result", {})
            raw = result.get("raw_output", "") or ""
            err = result.get("error", "") or ""
            verified = bool(result.get("verified", False))
            cexs = result.get("counterexamples", []) or []
            if verified or cexs:
                clean += 1
                continue
            failed += 1
            for sym in sym_re.findall(raw):
                undef_symbols[sym] = undef_symbols.get(sym, 0) + 1
            if assert_re.search(raw):
                bad_assert += 1
            if struct_re.search(raw) and "PARSING ERROR" in raw:
                struct_typedef += 1
            if (
                not sym_re.search(raw)
                and not assert_re.search(raw)
                and not struct_re.search(raw)
                and ("PARSING ERROR" in raw or "CONVERSION ERROR" in raw or "code 6" in err)
            ):
                other_parse_err += 1

        if total_cbmc == 0:
            return

        diag = {
            "driver": driver_name,
            "total_cbmc_runs": total_cbmc,
            "produced_verdict": clean,
            "failed_before_verdict": failed,
            "undefined_symbols": undef_symbols,
            "bad_assert_arity_count": bad_assert,
            "struct_typedef_syntax_count": struct_typedef,
            "other_parse_or_conv_error_count": other_parse_err,
        }
        try:
            (driver_dir / "coverage_diagnostics.json").write_text(
                _json.dumps(diag, indent=2)
            )
        except Exception:
            pass

        if failed == 0:
            return

        logger.warning(
            "Coverage diagnostics: %d/%d CBMC runs failed before any "
            "verdict was produced.",
            failed,
            total_cbmc,
        )

        if undef_symbols:
            top = sorted(undef_symbols.items(), key=lambda kv: -kv[1])[:5]
            d_flags = " ".join(
                f"-D {sym}='\"undef\"'" for sym, _n in top
            )
            logger.warning(
                "  Build-config macros likely missing: %s. "
                "Re-run with: %s",
                ", ".join(f"{s}({n})" for s, n in top),
                d_flags,
            )
        if bad_assert:
            logger.warning(
                "  %d function(s) hit a bmc-agent harness bug: "
                "multi-arg assert() in postcondition (C assert takes 1 arg).",
                bad_assert,
            )
        if struct_typedef:
            logger.warning(
                "  %d function(s) hit a bmc-agent harness bug: missing "
                "'struct' keyword on a non-typedef'd struct return type.",
                struct_typedef,
            )

        if failed >= max(5, total_cbmc * 0.5):
            logger.warning(
                "  >=50%% of functions failed at parse/convert. The "
                "'0 real bugs found' summary below is uninformative — "
                "treat this run as BLOCKED, not as a clean verify."
            )

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
            #
            # CRITICAL: pass the same Phase-1.5 flag_selection that the
            # iter-0 CBMC run used. Without this, --unsigned-overflow-check
            # / --pointer-overflow-check / etc. get dropped on the re-run
            # and CBMC silently "verifies clean" by no longer checking the
            # very property the bug was on — see the memory.c malloc
            # regression that exposed this.
            iter_flags = (getattr(self, "_flag_selections", {}) or {}).get(func.name)
            new_verdict = self.bmc_engine.check_function(
                func, spec, parsed, driver_name,
                all_funcs=all_funcs,
                flag_selection=iter_flags,
            )

            if new_verdict.verified or not new_verdict.counterexamples:
                applied_clauses = _all_applied_clauses(
                    self.config, func.name, spec
                )
                logger.info(
                    "Feedback iter %d (%s): VERIFIED CLEAN under "
                    "{precondition: %s; CBMC checks: %s} — convergence",
                    iteration, func.name,
                    " && ".join(applied_clauses) or "(spec precondition only)",
                    _flag_summary(iter_flags),
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

            new_validation = self.validator.validate(
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
                    cbmc_harness_path=getattr(new_verdict, "harness_path", ""),
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
    # Realism-feedback-driven in-sweep spec refinement
    # ------------------------------------------------------------------

    def _spec_refine_iterate(
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
        """Single-shot in-sweep spec refinement driven by realism's
        concrete key_concern. Complementary to ``_feedback_iterate``:
        the existing loop uses the general distill prompt and writes
        to learned_constraints.json (next-sweep effect); this method
        uses ``spec_refiner``'s targeted prompt and re-verifies
        in-sweep with the soundness acceptance check.

        Triggers only when:
          * config.enable_spec_refiner is True
          * realism.verdict == UNREALISTIC
          * realism.key_concern is concrete (names a specific identifier
            / field / constraint — not "looks artificial")

        Acceptance: the targeted CEx (failing_property) must be absent
        from the post-refinement CEx set. The full "no previously-
        REALISTIC CEx silently dropped" check requires multi-CEx
        context this method doesn't have at single-CEx scope; that
        guard is implicit here because we only refine on UNREALISTIC
        verdicts (REALISTIC CExs aren't even seen by this codepath).
        """
        from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
        from bmc_agent.spec_refiner import (
            SpecRefiner, _is_actionable_key_concern,
        )

        if (realism is None
            or realism.verdict != RealismVerdict.UNREALISTIC
            or not _is_actionable_key_concern(realism.key_concern)):
            return validation, realism

        refiner = SpecRefiner(self.config, self.llm)
        proposal = refiner.propose_refinement(
            func_info=func, current_spec=spec,
            rejected_cex=validation.counterexample,
            realism=realism,
        )
        if not proposal or not proposal.is_actionable:
            logger.info(
                "spec_refiner (%s): no actionable proposal "
                "(scope=%s) — keeping verdict",
                func.name, getattr(proposal, "scope", "none"),
            )
            return validation, realism

        # Caller-grounded soundness gate. The refiner's clause is derived from
        # the function body and reliably excludes the cex — but that does NOT
        # mean it's guaranteed by the callers. Applying a non-caller-guaranteed
        # clause assumes the bug away (the libucl FP mechanism). Before we tighten
        # the precondition, ask an (agentic) auditor whether the clause holds at
        # every call site. Only a CONFIDENT "UNSOUND" blocks; UNKNOWN/SOUND/agent
        # error all proceed, so a non-agentic backend degrades to the prior
        # behaviour. Opt-in via config.enable_soundness_gate.
        if getattr(self.config, "enable_soundness_gate", False):
            from bmc_agent.agents.soundness import SoundnessAgent
            sres = SoundnessAgent(self.config, self.llm).run(
                func_info=func,
                proposed_clause=proposal.added_clause,
                rejected_cex=validation.counterexample,
            )
            if sres.ok and sres.output.is_unsound:
                # Trust an UNSOUND block only if the cited caller is real. A
                # non-agentic judge can hallucinate a caller (e.g. a file that
                # doesn't exist) and produce a bogus UNSOUND that would re-flood
                # a genuine FP as a lead. If the verdict cites a .c/.h file that
                # isn't in the source tree, treat it as unverified and allow the
                # refinement instead of blocking on a fabricated caller.
                cited = sres.output.implicated_caller or ""
                if _cited_caller_is_fabricated(cited, func.source_file):
                    logger.info(
                        "spec_refiner (%s): soundness gate returned UNSOUND but "
                        "cited a caller that doesn't exist (%r) — treating as "
                        "unverified and ALLOWING refinement",
                        func.name, cited,
                    )
                else:
                    logger.warning(
                        "spec_refiner (%s): SOUNDNESS GATE blocked clause '%s' — "
                        "NOT caller-guaranteed (%s). Keeping counterexample %s as "
                        "a real-bug lead instead of refining it away.",
                        func.name, proposal.added_clause,
                        cited or sres.output.rationale[:160],
                        getattr(validation.counterexample, "failing_property", "?"),
                    )
                    return validation, realism
            if sres.ok:
                logger.info(
                    "spec_refiner (%s): soundness gate verdict=%s for clause "
                    "'%s' — allowing refinement",
                    func.name, sres.output.verdict, proposal.added_clause,
                )
            else:
                logger.info(
                    "spec_refiner (%s): soundness gate inconclusive (%s) — "
                    "allowing refinement",
                    func.name, (sres.error or "no output")[:120],
                )

        refined_spec = refiner.apply_refinement_to_spec(
            spec=spec, proposal=proposal,
        )

        # Re-verify the function under the refined spec. Same flag
        # selection as the iter-0 run so we're comparing apples to
        # apples and don't silently drop a check class.
        iter_flags = (getattr(self, "_flag_selections", {}) or {}).get(func.name)
        try:
            new_verdict = self.bmc_engine.check_function(
                func, refined_spec, parsed, driver_name,
                all_funcs=all_funcs,
                flag_selection=iter_flags,
            )
        except Exception as exc:
            logger.warning(
                "spec_refiner (%s): re-verification failed (%s) — "
                "keeping original verdict", func.name, exc,
            )
            return validation, realism

        targeted_prop = getattr(validation.counterexample,
                                "failing_property", "") or ""
        new_props = {
            (getattr(c, "failing_property", "") or "")
            for c in (new_verdict.counterexamples or [])
        }

        if targeted_prop in new_props:
            logger.info(
                "spec_refiner (%s): added clause '%s' did NOT exclude "
                "the targeted CEx %s — REJECTING refinement",
                func.name, proposal.added_clause, targeted_prop,
            )
            return validation, realism

        # Targeted CEx is gone. Two sub-cases:
        if new_verdict.verified or not new_verdict.counterexamples:
            logger.info(
                "spec_refiner (%s): refined PRE produced VERIFIED "
                "CLEAN (added clause: '%s')",
                func.name, proposal.added_clause,
            )
            return _verified_clean_validation(validation), _realism_verified()

        # Other CExs survived — re-validate the new primary CEx and
        # re-check realism so the downstream report reflects the
        # post-refinement state.
        new_ce = new_verdict.counterexamples[0]
        new_validation = self.validator.validate(
            func=func, spec=refined_spec, counterexample=new_ce,
            all_funcs=all_funcs, all_specs=all_specs,
            parsed_file=parsed, driver_name=driver_name,
        )
        try:
            new_realism = self.realism_checker.check(
                func=func, counterexample=new_ce,
                validation_result=new_validation, parsed_file=parsed,
                all_funcs=all_funcs, spec=refined_spec,
                cbmc_harness_path=getattr(new_verdict, "harness_path", ""),
            )
        except Exception as exc:
            logger.warning(
                "spec_refiner (%s): post-refinement realism failed "
                "(%s) — returning new validation with prior realism",
                func.name, exc,
            )
            new_realism = realism
        logger.info(
            "spec_refiner (%s): refined PRE removed targeted CEx; new "
            "primary CEx=%s, realism=%s",
            func.name,
            getattr(new_ce, "failing_property", "?"),
            new_realism.verdict.value if new_realism else "?",
        )
        return new_validation, new_realism

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
        only_functions: Optional[set[str]] = None,
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

        # v2 spec-gen: corpus = all .c files in the sweep; boundary headers
        # = autodiscovered from source_dir. Set once for the whole sweep.
        self._configure_v2_corpus(
            source_dir=source_dir,
            corpus_files=c_files,
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
                        defines=list(getattr(self.config, "cbmc_defines", None) or []),
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
        # caller already supplied a non-empty domain_knowledge string. Skipped
        # entirely in lite_mode — the domain knowledge feeds spec_gen prompts,
        # which lite_mode bypasses.
        # ------------------------------------------------------------------
        if bool(getattr(self.config, "lite_mode", False)):
            logger.info("Pass 1.5 skipped (lite_mode): domain knowledge not needed for permissive specs")
        else:
            from bmc_agent.domain_analyzer import analyze_codebase as _analyze_domain
            domain_knowledge = _analyze_domain(
                source_dir=source_dir,
                include_dirs=include_dirs,
                file_parsed_c=file_parsed_c,
                file_expanded=file_expanded,
                llm=self.llm,
                user_domain_knowledge=domain_knowledge,
            )

            # Auto-derive an ATTACKER-SURFACE-ONLY trust-boundary note when the
            # user supplied none AND we're agentic (the model reads real code).
            # It only lists what IS attacker-controlled and asserts nothing
            # trusted, so it can only sharpen the conservative default — never
            # mask a bug. A user-supplied --threat-model-context always wins
            # (it may legitimately assert trusted inputs, which a human owns).
            if (
                getattr(self.config, "claude_code_agentic", False)
                and not (getattr(self.config, "threat_model_context", "") or "").strip()
            ):
                from bmc_agent.domain_analyzer import derive_attacker_surface
                surface = derive_attacker_surface(
                    source_dir=source_dir,
                    include_dirs=include_dirs,
                    file_parsed_c=file_parsed_c,
                    file_expanded=file_expanded,
                    llm=self.llm,
                )
                if surface:
                    self.config.threat_model_context = surface
                    logger.info(
                        "Pass 1.5: attacker-surface note auto-derived and wired "
                        "into trust-deciding roles (%d chars)", len(surface),
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
                    only_functions=only_functions,
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
