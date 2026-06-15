"""
Phase 3: Counterexample Confirmation + Spec Refiner [AGENTIC].

LLM agent that classifies each BMC counterexample into three outcomes:
  REAL_BUG    — concretization succeeds and violation reproduces
  SPURIOUS    — concretization succeeds but violation does not reproduce
  UNRESOLVED  — concretization fails after all attempts (never silently dropped)

When SPURIOUS: the Spec Refiner (also agentic) proposes a tighter precondition;
an over-refinement guard rejects refinements that would exclude caller-reachable
states, preventing silent suppression of real bugs.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from bmc_agent.artifacts import ArtifactStore
from bmc_agent.cbmc import Counterexample, run_cbmc
from bmc_agent.config import Config
from bmc_agent.dynamic_validator import DynamicOutcome, DynamicValidationResult, DynamicValidator
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.llm import LLMClient, LLMError
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, FunctionSignature, ParsedCFile
from bmc_agent.prompts import (
    GENERIC_REPRODUCER_PROMPT,
    OVER_REFINEMENT_CHECK_PROMPT,
    REACHABILITY_PROMPT,
    REFINEMENT_PROMPT,
    REPRODUCER_PROMPT,
)
from bmc_agent.scenario_reproducer import _is_libarchive_target
from bmc_agent.spec import Spec

logger = get_logger("cex_validator")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CExOutcome(Enum):
    REAL_BUG   = "real_bug"
    SPURIOUS   = "spurious"
    UNRESOLVED = "unresolved"
    # LATENT — structural panic (slice OOB, integer overflow, etc.) on a
    # publicly callable function. No in-tree caller produces the CEx state,
    # but the function is part of the `pub` API surface, so cargo-fuzz or
    # a future caller can hit it. This is the "implicit contract" case
    # where the function lacks a defensive guard that every existing
    # caller satisfies via surrounding invariants.
    LATENT     = "latent"


# Failing-property substrings that count as "structural panics" — i.e.
# real Rust panic sites that any caller passing adversarial inputs can
# trigger, as opposed to Kani-modelling artifacts. Matches both the Kani
# property names and the human-readable trace messages.
_STRUCTURAL_PANIC_MARKERS = (
    "attempt to add with overflow",
    "attempt to subtract with overflow",
    "attempt to multiply with overflow",
    "attempt to shift",
    "attempt to divide by zero",
    "attempt to calculate the remainder with a divisor of zero",
    "slice_index_fail",
    "index out of bounds",
    "out of bounds",
    "capacity_overflow",
    "raw_vec",
    "panicked at",
    # Rust stdlib panic helpers for failed unwrap / expect on Option/Result.
    # The property name lands as either ``std::result::unwrap_failed.assertion.N``
    # or ``core::option::expect_failed.assertion.N`` — both are pure panic
    # sites reachable via cargo-fuzz, so treat them as structural.
    "unwrap_failed",
    "expect_failed",
    "called `Option::unwrap()` on a `None` value",
    "called `Result::unwrap()` on an `Err`",
    "unreachable code",
)


def _is_structural_panic(failing_property: str, trace: "list[str] | None" = None) -> bool:
    """True iff the CEx looks like a real Rust/C panic (slice OOB,
    overflow, divide-by-zero, alloc-capacity) — i.e. cargo-fuzz could
    trigger it with adversarial inputs. False for everything else
    (custom postcondition violations, Kani modelling artifacts, etc.)."""
    needle = (failing_property or "")
    if trace:
        needle = needle + " " + " ".join(trace)
    n = needle.lower()
    return any(marker.lower() in n for marker in _STRUCTURAL_PANIC_MARKERS)


def _is_publicly_callable(func) -> bool:
    """True iff *func* is on the public API surface — i.e. exposed to
    callers outside its defining file. For Rust this means ``pub fn``;
    for C it means a non-``static`` function (no storage-class modifier).
    Duck-typed against both FunctionInfo shapes.

    Detection: Rust signatures define ``is_pub`` (default False) AND
    ``is_static`` (kept for duck-typing). C signatures define only
    ``is_static``. So presence of ``is_pub`` => Rust path; else C path.
    """
    sig = getattr(func, "signature", None) or func
    if hasattr(sig, "is_pub"):
        return bool(getattr(sig, "is_pub", False))
    # C path: not declared `static` → externally visible
    return not bool(getattr(sig, "is_static", False))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class ValidationResult:
    """
    Result of validating a counterexample.

    Accepts either the new ``outcome`` kwarg (CExOutcome) or the legacy
    ``is_real_bug`` bool kwarg for backward compatibility.
    """

    def __init__(
        self,
        function_name: str,
        counterexample: Counterexample,
        caller_path: list[str],
        system_entry_input: str | None,
        refinement_history: list[str],
        final_precondition: str | None,
        reasoning: str,
        outcome: CExOutcome = CExOutcome.REAL_BUG,
        over_refinement_rejected: bool = False,
        system_entry_reached: bool = False,
        # Legacy backward-compat param
        is_real_bug: bool | None = None,
    ) -> None:
        self.function_name = function_name
        self.counterexample = counterexample
        self.caller_path = caller_path
        self.system_entry_input = system_entry_input
        self.refinement_history = refinement_history
        self.final_precondition = final_precondition
        self.reasoning = reasoning
        self.over_refinement_rejected = over_refinement_rejected
        # True when the full call chain was traced back to a system entry point
        # (a function with no callers in the analysed file, e.g. kernel_main,
        # an IRQ handler, a syscall dispatcher).  Drives the confirmed_system_entry
        # confidence tier, which ranks above confirmed_bmc.
        self.system_entry_reached: bool = system_entry_reached
        # If caller passed legacy is_real_bug, derive outcome from it
        if is_real_bug is not None:
            self.outcome = CExOutcome.REAL_BUG if is_real_bug else CExOutcome.SPURIOUS
        else:
            self.outcome = outcome
        # Dynamic validation result, populated after construction if enabled
        self.dynamic_result: DynamicValidationResult | None = None
        # Per-check error flags. When CBMC errors on a sub-check (reachability
        # or callee feasibility) and even the LLM fallback couldn't establish
        # a confident answer, we mark the corresponding flag. Used by
        # _try_dynamic_validation to skip dynamic when BOTH failed — running
        # the GCC harness on a blind entry produces low-signal results that
        # can mislead the realism check.
        self.reachability_check_errored: bool = False
        self.feasibility_check_errored: bool = False

    # ------------------------------------------------------------------
    # Backward-compat property
    # ------------------------------------------------------------------

    @property
    def is_real_bug(self) -> bool:
        return self.outcome == CExOutcome.REAL_BUG

    @property
    def is_latent_bug(self) -> bool:
        """True iff this CEx is a latent panic on the public API — the
        function panics on inputs no in-tree caller produces, but
        cargo-fuzz / a future caller can hit it. See
        :class:`CExOutcome.LATENT`."""
        return self.outcome == CExOutcome.LATENT

    def to_dict(self) -> dict:
        return {
            "function_name": self.function_name,
            "counterexample": {
                "failing_property": self.counterexample.failing_property,
                "variable_assignments": self.counterexample.variable_assignments,
                "trace": self.counterexample.trace,
            },
            "caller_path": self.caller_path,
            "system_entry_input": self.system_entry_input,
            "refinement_history": self.refinement_history,
            "final_precondition": self.final_precondition,
            "reasoning": self.reasoning,
            "outcome": self.outcome.value,
            "over_refinement_rejected": self.over_refinement_rejected,
            "system_entry_reached": self.system_entry_reached,
            "is_real_bug": self.is_real_bug,
            "is_latent_bug": self.is_latent_bug,
            "dynamic_result": self.dynamic_result.to_dict() if self.dynamic_result else None,
        }


# ---------------------------------------------------------------------------
# CExValidator
# ---------------------------------------------------------------------------


class CExValidator:
    """Validates counterexamples from BMC and refines specs when spurious."""

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        store: ArtifactStore,
        harness_gen: HarnessGenerator,
    ) -> None:
        self.config = config
        self.llm = llm
        self.store = store
        self.harness_gen = harness_gen
        self._dynamic_validator: DynamicValidator | None = (
            DynamicValidator(config, harness_gen, llm=llm)
            if getattr(config, "enable_dynamic_validation", False)
            else None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        func: FunctionInfo,
        spec: Spec,
        counterexample: Counterexample,
        all_funcs: dict[str, FunctionInfo],
        all_specs: dict[str, Spec],
        parsed_file: ParsedCFile,
        driver_name: str,
        cross_file_callers: set[str] | None = None,
        cross_file_caller_contexts: "dict[str, list[tuple[FunctionInfo, ParsedCFile]]] | None" = None,
    ) -> ValidationResult:
        """
        Validate a counterexample.

        Returns a ValidationResult indicating whether it is a real bug or
        spurious (with a refined spec).
        """
        func_name = func.name

        # CEx VALIDATION always runs: reachability + feasibility via CBMC,
        # then REAL/SPURIOUS classification + the spurious->refinement loop.
        # (Formerly gated by enable_classifier; that disable path was removed
        # -- there is no sound reason to skip validation. The flag/env are
        # kept as deprecated no-ops for backward compatibility.)
        logger.info("Validating counterexample for '%s'", func_name)

        # Per-CEx error flags for the gate in _try_dynamic_validation.
        # Set to True by the reachability/feasibility check methods when
        # CBMC errors out so the validator falls back to the LLM. Reset
        # at every validate() entry because each CEx gets its own check
        # outcomes.
        self._reach_errored = False
        self._feas_errored = False

        # Pre-classifier artifact filter REMOVED 2026-05-25 (per
        # [[feedback-llm-as-judge]]). It used to pattern-match the witness
        # shape and pre-decide SPURIOUS before the classifier LLM ran, which
        # in practice killed real libarchive seed bugs whose witnesses match
        # artifact patterns. The LLM judge is the correct place to weigh
        # this — don't pre-filter in Python.

        # Unwind filter: CBMC unwinding assertions (.unwind.N) indicate that a loop
        # exceeded the BMC bound.  These are not directly reportable as bugs, but we
        # must not suppress them outright: falling through to _handle_spurious lets the
        # refiner tighten the precondition (e.g. constrain loop-input ranges) and puts
        # the function back on the CEGAR re-check queue.  Suppressing with
        # final_precondition=None bypasses that queue and can mask real bugs (e.g. the
        # calloc integer-overflow found only after the refiner constrains nmemb/size).
        if re.search(r"\.unwind\.\d+$", counterexample.failing_property):
            logger.info(
                "Loop-bound artifact '%s' for '%s' — delegating to spec refiner via CEGAR",
                counterexample.failing_property,
                func_name,
            )
            return self._handle_spurious(
                func=func,
                spec=spec,
                counterexample=counterexample,
                callers=self._find_callers(func_name, all_funcs),
                all_specs=all_specs,
                parsed_file=parsed_file,
            )

        # Step 1: find all callers of func_name in all_funcs
        callers = self._find_callers(func_name, all_funcs)
        logger.debug("Callers of '%s': %s", func_name, list(callers.keys()))

        # Step 2 / Step 3: no callers within file → check if cross-file callers exist
        if not callers:
            # Step 2a: detect functions taken by address. The call graph
            # only records direct calls (``foo(...)``); functions passed
            # to qsort / bsearch / pthread_create / signal / atexit etc.
            # appear as bare identifiers and the call-graph misses them.
            # Classifying such a function as a "system entry point"
            # promotes its CEx straight to REAL_BUG even though the
            # real invocation goes through a library function with a
            # controlled argument contract (qsort guarantees non-NULL
            # pointers to array elements). Mark UNRESOLVED so the
            # finding doesn't masquerade as a confirmed bug.
            # Regression: ggml-quants.c run 2026-05-19 raised 3
            # confirmed_dynamic SIGSEGV findings on qsort comparators
            # (iq1_sort_helper, iq2_compare_func, iq3_compare_func)
            # that are exactly this FP class.
            if _is_address_taken(func_name, parsed_file):
                # Check whether the address is taken in an IN-PROJECT
                # function (i.e. one we've parsed). If yes, that
                # function is the indirect caller for vtable-dispatch
                # patterns (libarchive format readers, plugin
                # registries). We run the normal caller-feasibility
                # flow on it as if it were a direct caller.
                #
                # Without this, libarchive's
                # ``archive_read_format_cpio_read_header`` (taken in
                # ``archive_read_support_format_cpio``) and every other
                # format-reader callback was marked UNRESOLVED, hiding
                # bugs in the format-parsing code paths where most
                # real CVEs live.
                #
                # The legacy "qsort callback" case (no in-project
                # address-taker — only appears in a preprocessed
                # libc/glibc header) still falls through to UNRESOLVED.
                indirect_callers_set: set[str] = set(
                    getattr(parsed_file, "address_taken_in", {}).get(func_name, set())
                )
                # Only consider in-project address-takers that have a
                # FunctionInfo we can pass to the feasibility check.
                indirect_callers: dict[str, FunctionInfo] = {}
                if indirect_callers_set and all_funcs:
                    for ic_name in indirect_callers_set:
                        if ic_name in all_funcs:
                            indirect_callers[ic_name] = all_funcs[ic_name]
                if indirect_callers:
                    logger.info(
                        "'%s' has no direct callers but its address is "
                        "taken in %d in-project function(s); treating "
                        "as vtable-dispatched and using indirect callers "
                        "for feasibility check: %s",
                        func_name,
                        len(indirect_callers),
                        sorted(indirect_callers.keys())[:3],
                    )
                    # Re-bind ``callers`` to the indirect ones so the
                    # normal flow below picks them up.
                    callers = indirect_callers
                else:
                    logger.info(
                        "'%s' has no direct callers but is taken by address "
                        "outside the project (likely a libc callback like "
                        "qsort/bsearch/pthread_create) — marking UNRESOLVED",
                        func_name,
                    )
                    return ValidationResult(
                        function_name=func_name,
                        counterexample=counterexample,
                        caller_path=[func_name],
                        system_entry_input=None,
                        refinement_history=[],
                        final_precondition=None,
                        reasoning=(
                            f"'{func_name}' has no direct callers and its "
                            "address is taken only outside the project "
                            "(libc callback pattern). The CEx assumed direct "
                            "invocation with arbitrary nondet inputs; real "
                            "invocation goes through a library function with "
                            "a controlled contract that may exclude the "
                            "violating state. Marking UNRESOLVED."
                        ),
                        outcome=CExOutcome.UNRESOLVED,
                    )

            has_cross_file_caller = bool(
                cross_file_callers and func_name in cross_file_callers
            )
            if has_cross_file_caller:
                # Callers exist in other files — run CBMC reachability against
                # them using their own ParsedCFile context (if available).
                cross_contexts = (
                    cross_file_caller_contexts.get(func_name, [])
                    if cross_file_caller_contexts else []
                )
                reachable_cross: list[tuple[str, FunctionInfo, ParsedCFile]] = []
                for caller_fi, caller_parsed in cross_contexts:
                    caller_spec = all_specs.get(
                        caller_fi.name,
                        Spec(function_name=caller_fi.name, precondition="true", postcondition="true"),
                    )
                    can_reach = self._check_caller_reachability(
                        caller=caller_fi,
                        callee_name=func_name,
                        counterexample=counterexample,
                        callee_spec=spec,
                        parsed_file=caller_parsed,
                        driver_name=driver_name,
                        caller_spec=caller_spec,
                        all_specs=all_specs,
                        callee_sig=func.signature,
                    )
                    if can_reach:
                        logger.info(
                            "Cross-file caller '%s' CAN reach '%s' CEx state",
                            caller_fi.name, func_name,
                        )
                        reachable_cross.append((caller_fi.name, caller_fi, caller_parsed))

                if reachable_cross:
                    caller_name_cf, caller_fi_cf, caller_parsed_cf = reachable_cross[0]
                    caller_all_funcs_cf = {
                        n: caller_parsed_cf.get_function_info(n)
                        for n in caller_parsed_cf.functions
                        if caller_parsed_cf.get_function_info(n) is not None
                    }
                    is_system_reachable, call_chain = self._propagate_upward(
                        func_name=caller_name_cf,
                        counterexample=counterexample,
                        all_funcs=caller_all_funcs_cf,
                        all_specs=all_specs,
                        parsed_file=caller_parsed_cf,
                        driver_name=driver_name,
                        cross_file_callers=cross_file_callers,
                        cross_file_caller_contexts=cross_file_caller_contexts,
                    )
                    full_chain = call_chain + [func_name]
                    reproducer = self._generate_system_entry_reproducer(
                        call_chain=full_chain,
                        counterexample=counterexample,
                        all_funcs=all_funcs,
                        parsed_file=parsed_file,
                    )
                    result = ValidationResult(
                        function_name=func_name,
                        counterexample=counterexample,
                        caller_path=full_chain,
                        system_entry_input=reproducer,
                        refinement_history=[],
                        final_precondition=None,
                        reasoning=(
                            f"Cross-file caller '{caller_name_cf}' can reach the CEx state. "
                            f"Call chain: {full_chain}."
                            + (" Full chain traced to system entry." if is_system_reachable else "")
                        ),
                        outcome=CExOutcome.REAL_BUG,
                        system_entry_reached=is_system_reachable,
                    )
                    self._try_dynamic_validation(result, func, all_funcs, all_specs, parsed_file)
                    return result

                # Cross-file callers exist but none confirmed reachable — fall back
                logger.info(
                    "'%s' has cross-file callers but none confirmed reachable — "
                    "reporting as confirmed_bmc",
                    func_name,
                )
                reproducer = self._generate_system_entry_reproducer(
                    call_chain=[func_name],
                    counterexample=counterexample,
                    all_funcs=all_funcs,
                    parsed_file=parsed_file,
                )
                result = ValidationResult(
                    function_name=func_name,
                    counterexample=counterexample,
                    caller_path=[func_name],
                    system_entry_input=reproducer,
                    refinement_history=[],
                    final_precondition=None,
                    reasoning=(
                        f"'{func_name}' has cross-file callers but no reachability "
                        "was confirmed via CBMC — reporting as confirmed_bmc."
                    ),
                    outcome=CExOutcome.REAL_BUG,
                    system_entry_reached=False,
                )
                self._try_dynamic_validation(result, func, all_funcs, all_specs, parsed_file)
                return result

            logger.info(
                "'%s' has no callers — checking callee feasibility before confirming real bug",
                func_name,
            )
            feasible = self._check_cex_feasibility(
                func=func,
                spec=spec,
                counterexample=counterexample,
                parsed_file=parsed_file,
                all_specs=all_specs,
            )
            if feasible is False:
                if not func.signature.is_static:
                    # Non-static function: external linkage means it IS callable from
                    # outside the corpus with arbitrary inputs.  INFEASIBLE from the
                    # feasibility check is not grounds for UNRESOLVED — it may simply
                    # reflect that the feasibility harness couldn't reproduce the exact
                    # callee state (e.g. unsigned-overflow assertion not re-enabled, or
                    # the violation occurs before any callee is invoked).
                    logger.info(
                        "Feasibility check INFEASIBLE for non-static '%s' — "
                        "external linkage means it is a system entry point; "
                        "treating as confirmed_system_entry",
                        func_name,
                    )
                    feasible = None  # fall through to confirmed_system_entry below
                else:
                    logger.info(
                        "Feasibility check: violation absent with real callees for '%s' → UNRESOLVED",
                        func_name,
                    )
                    return ValidationResult(
                        function_name=func_name,
                        counterexample=counterexample,
                        caller_path=[func_name],
                        system_entry_input=None,
                        refinement_history=[],
                        final_precondition=None,
                        reasoning=(
                            f"'{func_name}' is a static entry function. "
                            "CEx inputs are reachable but the violation does not occur "
                            "with real callee implementations — callee return values "
                            "assumed by the stubs are not achievable. Marking UNRESOLVED."
                        ),
                        outcome=CExOutcome.UNRESOLVED,
                    )
            reproducer = self._generate_system_entry_reproducer(
                call_chain=[func_name],
                counterexample=counterexample,
                all_funcs=all_funcs,
                parsed_file=parsed_file,
            )
            # Entry function (no in-scope callers) + public API + structural
            # panic = LATENT or REAL_BUG depending on threat model.
            #
            # Under threat_model='security': attacker-controlled inputs
            # cross the public API boundary. Any pub fn that panics on some
            # input is REAL_BUG — the attacker IS a current caller. This
            # matches the cargo-fuzz standard the user set originally.
            #
            # Under threat_model='safety' or 'functional': we only care
            # about in-tree-reachable crashes. Pub-API-reachable-only panics
            # are LATENT — hardening task, not active crash.
            #
            # Non-public entry points (C `static` fns called from out-of-
            # scope code, or genuine system entries like kernel handlers
            # whose callers are below the analysed boundary) always go to
            # REAL_BUG — we can't tell those apart from `pub` API surface
            # without additional ground truth.
            threat_model = getattr(self.config, "threat_model", "security").lower()
            if (
                _is_publicly_callable(func)
                and _is_structural_panic(
                    counterexample.failing_property,
                    getattr(counterexample, "trace", None),
                )
                and threat_model != "security"
            ):
                latent_reason = (
                    f"'{func_name}' is a public-API entry function (no callers "
                    f"in any file in scope). The CEx panic "
                    f"'{counterexample.failing_property}' is a structural Rust/C "
                    f"panic reachable via cargo-fuzz / future-caller through the "
                    f"public API, but no in-tree call site produces the state. "
                    f"Threat model is '{threat_model}' (not security), so "
                    f"classified as LATENT — separate from REAL_BUG (which "
                    f"requires an in-tree-reachable call chain under non-security "
                    f"threat models)."
                )
                result = ValidationResult(
                    function_name=func_name,
                    counterexample=counterexample,
                    caller_path=[func_name],
                    system_entry_input=reproducer,
                    refinement_history=[],
                    final_precondition=None,
                    reasoning=latent_reason,
                    outcome=CExOutcome.LATENT,
                    system_entry_reached=True,
                )
                self._try_dynamic_validation(result, func, all_funcs, all_specs, parsed_file)
                return result

            # Entry-fn + Kani harness-wrapper postcondition violation (not a
            # structural panic) is almost always a bad LLM spec rather than
            # a real bug. The function doesn't panic on its own (no body
            # OOB/overflow/null); the only way to "fail" is to violate the
            # LLM's invented postcondition. If we mark this REAL_BUG we're
            # claiming the *function* is broken when actually the SPEC is.
            # Concrete v17 evidence: itoa::u128_ext::mulhi computes
            # `mulhi(x,y) = upper 128 bits of x*y` correctly, but the LLM
            # spec said `result == ((x as u128) * (y as u128)) >> 64` --
            # which truncates u128*u128 to u128 and can't represent the
            # answer at all. Same with ryu::log10_pow2.
            #
            # Detect this specifically as Kani's `check_<fn>.assertion.N`
            # pattern (with optional `<module>::` prefix), which is exactly
            # what our harness wrapper generates. Body assertions like
            # `assertion.<fn>.N` (CBMC-style) stay REAL_BUG -- a body
            # assertion firing IS a real bug, only the LLM-injected
            # postcondition is suspect.
            import re as _re_pp
            fp = counterexample.failing_property or ""
            is_kani_harness_postcondition = bool(
                _re_pp.search(rf"(^|::)check_{_re_pp.escape(func_name)}\.assertion\.\d+$", fp)
            )
            is_postcondition_only = (
                is_kani_harness_postcondition
                and not _is_structural_panic(
                    counterexample.failing_property,
                    getattr(counterexample, "trace", None),
                )
            )
            if is_postcondition_only:
                return ValidationResult(
                    function_name=func_name,
                    counterexample=counterexample,
                    caller_path=[func_name],
                    system_entry_input=reproducer,
                    refinement_history=[],
                    final_precondition=None,
                    reasoning=(
                        f"'{func_name}' is an entry function. The failing "
                        f"property '{counterexample.failing_property}' is the "
                        f"harness-injected postcondition, not a structural "
                        f"Rust panic. With no in-scope callers AND no body "
                        f"panic, the most likely explanation is an over-/"
                        f"mis-specified LLM postcondition rather than a real "
                        f"function bug. Classifying SPURIOUS."
                    ),
                    outcome=CExOutcome.SPURIOUS,
                    system_entry_reached=True,
                )
            result = ValidationResult(
                function_name=func_name,
                counterexample=counterexample,
                caller_path=[func_name],
                system_entry_input=reproducer,
                refinement_history=[],
                final_precondition=None,
                reasoning=(
                    f"'{func_name}' is an entry function (no callers in any file). "
                    "The counterexample is directly reachable from the system boundary."
                    + (" Callee feasibility confirmed." if feasible is True else "")
                ),
                outcome=CExOutcome.REAL_BUG,
                system_entry_reached=True,
            )
            self._try_dynamic_validation(result, func, all_funcs, all_specs, parsed_file)
            return result

        # Step 4: check if any caller can produce the counterexample state
        reachable_from: list[str] = []
        for caller_name, caller_func in callers.items():
            caller_spec = all_specs.get(caller_name, Spec(
                function_name=caller_name,
                precondition="true",
                postcondition="true",
            ))
            can_reach = self._check_caller_reachability(
                caller=caller_func,
                callee_name=func_name,
                counterexample=counterexample,
                callee_spec=spec,
                caller_spec=caller_spec,
                parsed_file=parsed_file,
                driver_name=driver_name,
                all_specs=all_specs,
            )
            if can_reach:
                reachable_from.append(caller_name)
                logger.info(
                    "Caller '%s' CAN produce the counterexample state → real bug",
                    caller_name,
                )
            else:
                logger.info(
                    "Caller '%s' CANNOT produce the counterexample state",
                    caller_name,
                )

        if reachable_from:
            # Stage 2: check callee feasibility before confirming real bug
            feasible = self._check_cex_feasibility(
                func=func,
                spec=spec,
                counterexample=counterexample,
                parsed_file=parsed_file,
                all_specs=all_specs,
            )
            if feasible is False:
                logger.info(
                    "Feasibility check: violation absent with real callees for '%s' → UNRESOLVED",
                    func_name,
                )
                return ValidationResult(
                    function_name=func_name,
                    counterexample=counterexample,
                    caller_path=[func_name],
                    system_entry_input=None,
                    refinement_history=[],
                    final_precondition=None,
                    reasoning=(
                        f"CEx inputs are reachable from caller(s) {reachable_from}, "
                        "but the violation does not occur with real callee implementations "
                        "— callee return values assumed by the stubs are not achievable. "
                        "Marking UNRESOLVED."
                    ),
                    outcome=CExOutcome.UNRESOLVED,
                )

            # Propagate upward through the first reachable caller
            is_system_reachable, call_chain = self._propagate_upward(
                func_name=reachable_from[0],
                counterexample=counterexample,
                all_funcs=all_funcs,
                all_specs=all_specs,
                parsed_file=parsed_file,
                driver_name=driver_name,
                cross_file_callers=cross_file_callers,
                cross_file_caller_contexts=cross_file_caller_contexts,
            )
            # Append the current function to the chain
            full_chain = call_chain + [func_name]

            reproducer = self._generate_system_entry_reproducer(
                call_chain=full_chain,
                counterexample=counterexample,
                all_funcs=all_funcs,
                parsed_file=parsed_file,
            )
            # Implicit-NULL-precondition downgrade. CBMC's
            # `<fn>.precondition_instance.<N>` property fires when a stdlib
            # call inside <fn>'s body (e.g. memset(model->grads, ...)) has
            # an implicit non-null precondition that the harness witnesses
            # violated. When the caller-chain hasn't traced to system
            # entry, we can't confirm that any real flow actually produces
            # the violating pointer state -- the immediate caller just
            # forwards the parameter and propagating the precondition
            # transitively would need cross-function analysis we don't yet
            # do. Concrete v23 victims:
            #   gpt2_zero_grad.precondition_instance.1 (caller chain
            #     [gpt2_backward, gpt2_zero_grad] -- gpt2_backward also
            #     derefs `model`, so it never passes NULL in practice)
            #   fill_in_parameter_sizes.pointer_dereference.11 (same shape)
            # Mark UNRESOLVED rather than REAL_BUG so reviewers can audit
            # without a confirmed-bug claim sitting on a likely-FP.
            _fp = (counterexample.failing_property or "")
            _implicit_pc = (
                ".precondition_instance." in _fp
                or ".pointer_dereference." in _fp
            )
            # Gate on is_system_reachable=False: the implicit precondition
            # only matters if the witness's pointer state can be produced
            # by a real flow from system entry. When the upward walk fails
            # to reach main (the chain stops at the immediate caller or
            # the analyzed corpus top), the CEx parameter (e.g.
            # `_model_obj`) shows up as a fully-populated struct whose
            # pointer fields are NULL only because the harness's default
            # object init zero-fills them. Without a concrete system-entry
            # reproducer we can't claim the violation is reachable in
            # practice -- the immediate caller just forwards the parameter
            # without constructing it.
            # Vtable-dispatch escape hatch: when ANY function in the
            # upward chain is registered as a callback via function-pointer
            # dispatch (its address is taken in another in-project function,
            # e.g. ``.read_header = cab_read_header`` in a
            # ``struct archive_format_descriptor`` definition), the chain
            # IS effectively system-reachable via the framework's dispatch
            # loop. Without this, libarchive's format-reader callbacks
            # (rar5_cleanup, cab_checksum_finish, record_hardlink, etc.)
            # all hit the implicit-NULL downgrade because the upward walk
            # stops at the registrar function rather than `main` — losing
            # documented seed-bug matches that prior sweeps confirmed.
            address_taken = getattr(parsed_file, "address_taken_in", {}) or {}
            _vtable_dispatched = any(
                fn in address_taken and address_taken[fn]
                for fn in full_chain
            )
            if _implicit_pc and not is_system_reachable and not _vtable_dispatched:
                result = ValidationResult(
                    function_name=func_name,
                    counterexample=counterexample,
                    caller_path=full_chain,
                    system_entry_input=reproducer,
                    refinement_history=[],
                    final_precondition=None,
                    reasoning=(
                        f"Implicit-precondition CEx on '{func_name}' "
                        f"(property '{_fp}'): the function lacks an explicit "
                        f"NULL/validity check on a pointer parameter, the "
                        f"immediate caller {reachable_from} just forwards the "
                        f"parameter without constructing it, and the upward "
                        f"chain {full_chain} did not reach system entry. "
                        f"Real callers along a complete chain to main "
                        f"typically maintain the implicit invariant (e.g. "
                        f"main constructs the struct via a build/init "
                        f"routine). Without a system-entry reproducer we "
                        f"can't confirm this is reachable in practice -- "
                        f"classifying UNRESOLVED rather than REAL_BUG to "
                        f"avoid the v23-class false-positive (gpt2_zero_grad "
                        f"/ fill_in_parameter_sizes pattern)."
                    ),
                    outcome=CExOutcome.UNRESOLVED,
                    system_entry_reached=False,
                )
                self._try_dynamic_validation(result, func, all_funcs, all_specs, parsed_file)
                return result
            result = ValidationResult(
                function_name=func_name,
                counterexample=counterexample,
                caller_path=full_chain,
                system_entry_input=reproducer,
                refinement_history=[],
                final_precondition=None,
                reasoning=(
                    f"Counterexample state is reachable from caller(s): "
                    f"{reachable_from}. Call chain: {full_chain}."
                    + (" Full chain traced to system entry." if is_system_reachable else "")
                    + (" Callee feasibility confirmed." if feasible is True else "")
                ),
                outcome=CExOutcome.REAL_BUG,
                system_entry_reached=is_system_reachable,
            )
            self._try_dynamic_validation(result, func, all_funcs, all_specs, parsed_file)
            return result

        # Vtable-dispatch escape hatch (mirror of the precondition-branch
        # escape above). When the function is registered as a callback in a
        # vtable (its address is taken in another in-project function), it
        # is invoked by the framework's dispatch loop — which is outside
        # the current file scope and can produce arbitrary input states.
        # Classifying such CExes as SPURIOUS just because the registrar
        # callers can't produce the state misses documented seed bugs
        # (cab_checksum_finish, find_newc_header, parse_rockridge, etc.).
        #
        # Vtable escape (TIGHTENED): only fire when the function ITSELF
        # is registered as a callback via function-pointer dispatch
        # (its address is taken in another in-project function). The
        # earlier "or _file_has_vtable_callbacks" clause was too broad —
        # it promoted EVERY spurious in a file with any vtable callback,
        # producing massive precision regression on byte-swap helpers
        # and other unrelated artifacts.
        address_taken = getattr(parsed_file, "address_taken_in", {}) or {}
        if address_taken.get(func_name):
            indirect = sorted(address_taken[func_name])[:4]
            logger.info(
                "Vtable-dispatched callback '%s' (address taken in %s) — "
                "treating CEx as REAL_BUG reachable via framework dispatch",
                func_name, indirect,
            )
            result = ValidationResult(
                function_name=func_name,
                counterexample=counterexample,
                caller_path=[func_name],
                system_entry_input=None,
                refinement_history=[],
                final_precondition=None,
                reasoning=(
                    f"'{func_name}' is registered as a callback via vtable "
                    f"(its address is taken in {indirect}). The framework "
                    f"dispatch loop (outside this file) can pass arbitrary "
                    f"input state to the callback, so the CEx state IS "
                    f"reachable through framework invocation even though "
                    f"no in-file caller can produce it directly. "
                    f"Classifying as REAL_BUG."
                ),
                outcome=CExOutcome.REAL_BUG,
                system_entry_reached=True,
            )
            self._try_dynamic_validation(result, func, all_funcs, all_specs, parsed_file)
            return result

        # Step 5: no caller can reach the state → spurious → refine
        logger.info(
            "No caller can produce the state for '%s' → spurious counterexample; refining",
            func_name,
        )
        return self._handle_spurious(
            func=func,
            spec=spec,
            counterexample=counterexample,
            callers=callers,
            all_specs=all_specs,
            parsed_file=parsed_file,
        )

    # ------------------------------------------------------------------
    # CEx feasibility check (Stage 2: real callee bodies)
    # ------------------------------------------------------------------

    def _check_cex_feasibility(
        self,
        func: FunctionInfo,
        spec: Spec,
        counterexample: Counterexample,
        parsed_file: ParsedCFile,
        all_specs: "dict[str, Spec] | None" = None,
    ) -> "bool | None":
        """
        Check whether the CEx violation still occurs with real callee bodies.

        The feasibility harness fixes scalar inputs to CEx witness values,
        inlines local callees, and stubs external callees with postcondition
        constraints.  If CBMC finds a violation → CEx is feasible (real bug).
        If CBMC verifies → callee return values assumed by the stub were not
        achievable by real callees.

        Returns
        -------
        True   — violation confirmed with real callees (feasible, real bug)
        False  — violation absent with real callees (callee values unachievable)
        None   — inconclusive (CBMC unavailable, harness error, CBMC error)
        """
        import shutil

        if not shutil.which(self.config.cbmc_path):
            return None

        # Only meaningful when the function actually has callees defined locally;
        # if there are none, the original BMC result is already exact.
        has_local_callees = bool(
            func.callees & set(parsed_file.functions.keys())
        )
        if not has_local_callees:
            logger.debug(
                "'%s' has no local callees — feasibility check skipped (BMC already exact)",
                func.name,
            )
            return None

        try:
            harness_src = self.harness_gen.generate_feasibility_harness(
                func=func,
                spec=spec,
                counterexample=counterexample,
                parsed_file=parsed_file,
                all_specs=all_specs,
            )
        except Exception as exc:
            logger.warning(
                "Feasibility harness generation failed for '%s': %s",
                func.name, exc,
            )
            return None

        with tempfile.NamedTemporaryFile(
            suffix=".c", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp.write(harness_src)
            tmp_path = tmp.name

        try:
            result = run_cbmc(
                harness_path=tmp_path,
                unwind=self.config.cbmc_unwind,
                timeout=self.config.cbmc_timeout,
                cbmc_path=self.config.cbmc_path,
                include_dirs=getattr(self.config, "include_dirs", None),
            )
        except Exception as exc:
            logger.warning(
                "CBMC feasibility check raised for '%s': %s", func.name, exc
            )
            return None
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

        if result.error:
            logger.warning(
                "CBMC feasibility check error for '%s': %s", func.name, result.error
            )
            self._feas_errored = True
            return None

        # CBMC found a violation → postcondition violated with real callees → feasible
        # CBMC verified → no violation with real callees → callee values unachievable
        feasible = not result.verified
        logger.info(
            "Feasibility check for '%s': %s",
            func.name, "FEASIBLE" if feasible else "INFEASIBLE",
        )
        return feasible

    # ------------------------------------------------------------------
    # Reachability check
    # ------------------------------------------------------------------

    def _check_caller_reachability(
        self,
        caller: FunctionInfo,
        callee_name: str,
        counterexample: Counterexample,
        callee_spec: Spec,
        parsed_file: ParsedCFile,
        driver_name: str,
        caller_spec: "Spec | None" = None,
        all_specs: "dict[str, Spec] | None" = None,
        callee_sig: "FunctionSignature | None" = None,
    ) -> bool:
        """
        Check if ``caller`` can produce the state described by ``counterexample``
        at its call site to ``callee_name``.

        First tries CBMC (if available); falls back to LLM reasoning.
        Returns True if the state IS reachable.
        """
        import shutil

        cbmc_available = bool(shutil.which(self.config.cbmc_path))

        if cbmc_available:
            return self._check_reachability_with_cbmc(
                caller=caller,
                callee_name=callee_name,
                counterexample=counterexample,
                callee_spec=callee_spec,
                parsed_file=parsed_file,
                driver_name=driver_name,
                caller_spec=caller_spec,
                all_specs=all_specs,
                callee_sig=callee_sig,
            )
        else:
            return self._check_reachability_with_llm(
                caller=caller,
                callee_name=callee_name,
                counterexample=counterexample,
                callee_spec=callee_spec,
                parsed_file=parsed_file,
            )

    def _check_reachability_with_cbmc(
        self,
        caller: FunctionInfo,
        callee_name: str,
        counterexample: Counterexample,
        callee_spec: Spec,
        parsed_file: ParsedCFile,
        driver_name: str,
        caller_spec: "Spec | None" = None,
        all_specs: "dict[str, Spec] | None" = None,
        callee_sig: "FunctionSignature | None" = None,
    ) -> bool:
        """
        Generate a reachability harness and run CBMC on it.

        If CBMC finds a counterexample (for the always-failing assert(0)), the
        state IS reachable. If CBMC verifies (no path reaches assert(0) or the
        __CPROVER_assume constraints are contradictory), the state is NOT reachable.

        ``caller_spec`` is used to constrain the caller's inputs (precondition),
        which is essential for soundly ruling out spurious counterexamples: a CEx
        is spurious only if no caller state satisfying the caller's precondition
        can reach the callee with the CEx variable assignments.
        """
        if caller_spec is None:
            caller_spec = Spec(
                function_name=caller.name,
                precondition="true",
                postcondition="true",
            )

        try:
            harness_src = self.harness_gen.generate_reachability_harness(
                caller=caller,
                callee_name=callee_name,
                counterexample=counterexample,
                caller_spec=caller_spec,
                parsed_file=parsed_file,
                all_specs=all_specs,
                callee_sig=callee_sig,
            )
        except Exception as exc:
            logger.warning(
                "Reachability harness generation failed for '%s' → '%s': %s",
                caller.name, callee_name, exc,
            )
            logger.debug(
                "Reachability harness traceback for '%s' → '%s':",
                caller.name, callee_name, exc_info=True,
            )
            # Fall back to LLM
            return self._check_reachability_with_llm(
                caller=caller,
                callee_name=callee_name,
                counterexample=counterexample,
                callee_spec=callee_spec,
                parsed_file=parsed_file,
            )

        # Write harness to a temporary file
        with tempfile.NamedTemporaryFile(
            suffix=".c", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp.write(harness_src)
            tmp_path = tmp.name

        try:
            result = run_cbmc(
                harness_path=tmp_path,
                unwind=self.config.cbmc_unwind,
                timeout=self.config.cbmc_timeout,
                cbmc_path=self.config.cbmc_path,
                include_dirs=getattr(self.config, "include_dirs", None),
            )
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

        if result.error:
            logger.warning(
                "CBMC reachability check error for '%s' → '%s': %s; falling back to LLM",
                caller.name, callee_name, result.error,
            )
            self._reach_errored = True
            return self._check_reachability_with_llm(
                caller=caller,
                callee_name=callee_name,
                counterexample=counterexample,
                callee_spec=callee_spec,
                parsed_file=parsed_file,
            )

        # If CBMC found a counterexample for assert(0), it means a path exists
        # through the caller that satisfied the __CPROVER_assume constraints →
        # the target state IS reachable.
        return not result.verified

    def _check_reachability_with_llm(
        self,
        caller: FunctionInfo,
        callee_name: str,
        counterexample: Counterexample,
        callee_spec: Spec,
        parsed_file: ParsedCFile,
    ) -> bool:
        """LLM-based fallback for reachability analysis."""
        state_str = ", ".join(
            f"{k} = {v}"
            for k, v in counterexample.variable_assignments.items()
        )
        caller_spec = Spec(
            function_name=caller.name,
            precondition="true",
            postcondition="true",
        )

        system_prompt = "You are a formal verification expert for C programs."
        user_prompt = REACHABILITY_PROMPT.format(
            caller_name=caller.name,
            callee_name=callee_name,
            caller_body=caller.body,
            target_state=state_str,
            caller_precondition=caller_spec.precondition,
        )

        try:
            response = self.llm.complete(system_prompt, user_prompt, role="realism",
                                         cache_prefix=getattr(self.config, "domain_summary", ""))
            data = _parse_json_response(response)
            if data is not None:
                is_reachable = bool(data.get("is_reachable", False))
                reasoning = data.get("reasoning", "")
                logger.debug(
                    "LLM reachability for '%s' → '%s': %s (%s)",
                    caller.name, callee_name, is_reachable, reasoning[:80],
                )
                return is_reachable
        except LLMError as exc:
            logger.warning("LLM reachability check failed: %s", exc)

        # Default: conservatively assume NOT reachable (avoid false bug reports)
        return False

    # ------------------------------------------------------------------
    # Upward propagation
    # ------------------------------------------------------------------

    def _propagate_upward(
        self,
        func_name: str,
        counterexample: Counterexample,
        all_funcs: dict[str, FunctionInfo],
        all_specs: dict[str, Spec],
        parsed_file: ParsedCFile,
        driver_name: str,
        visited: set[str] | None = None,
        cross_file_callers: set[str] | None = None,
        cross_file_caller_contexts: "dict[str, list[tuple[FunctionInfo, ParsedCFile]]] | None" = None,
    ) -> tuple[bool, list[str]]:
        """
        Recursively propagate upward through callers.

        Returns (is_reachable_from_entry, call_chain).
        A function is a true system entry point only if it has no callers in
        all_funcs AND no cross-file callers are known for it.
        When cross_file_caller_contexts is available, also runs CBMC reachability
        against cross-file callers and continues propagation in their file context.
        """
        if visited is None:
            visited = set()
        if func_name in visited:
            return False, []
        visited.add(func_name)

        callers = self._find_callers(func_name, all_funcs)

        if not callers:
            # No in-file callers — check for cross-file callers
            if cross_file_callers and func_name in cross_file_callers:
                # Try to continue the chain upward through cross-file callers
                cross_contexts = (
                    cross_file_caller_contexts.get(func_name, [])
                    if cross_file_caller_contexts else []
                )
                for caller_fi, caller_parsed in cross_contexts:
                    if caller_fi.name in visited:
                        continue
                    caller_spec = all_specs.get(
                        caller_fi.name,
                        Spec(function_name=caller_fi.name, precondition="true", postcondition="true"),
                    )
                    # callee_sig: the signature of func_name (the callee being checked).
                    # all_funcs contains FunctionInfo for funcs in the current file scope.
                    _callee_fi = all_funcs.get(func_name)
                    can_reach = self._check_caller_reachability(
                        caller=caller_fi,
                        callee_name=func_name,
                        counterexample=counterexample,
                        callee_spec=all_specs.get(
                            func_name,
                            Spec(function_name=func_name, precondition="true", postcondition="true"),
                        ),
                        parsed_file=caller_parsed,
                        driver_name=driver_name,
                        caller_spec=caller_spec,
                        all_specs=all_specs,
                        callee_sig=_callee_fi.signature if _callee_fi else None,
                    )
                    if can_reach:
                        reachable, chain = self._propagate_upward(
                            func_name=caller_fi.name,
                            counterexample=counterexample,
                            all_funcs={
                                n: caller_parsed.get_function_info(n)
                                for n in caller_parsed.functions
                                if caller_parsed.get_function_info(n) is not None
                            },
                            all_specs=all_specs,
                            parsed_file=caller_parsed,
                            driver_name=driver_name,
                            visited=visited,
                            cross_file_callers=cross_file_callers,
                            cross_file_caller_contexts=cross_file_caller_contexts,
                        )
                        if reachable:
                            return True, chain + [func_name]
                # No cross-file chain leads to entry
                return False, [func_name]
            return True, [func_name]

        for caller_name, caller_func in callers.items():
            if caller_name in visited:
                continue
            caller_spec = all_specs.get(caller_name, Spec(
                function_name=caller_name,
                precondition="true",
                postcondition="true",
            ))
            can_reach = self._check_caller_reachability(
                caller=caller_func,
                callee_name=func_name,
                counterexample=counterexample,
                callee_spec=all_specs.get(func_name, Spec(
                    function_name=func_name,
                    precondition="true",
                    postcondition="true",
                )),
                parsed_file=parsed_file,
                driver_name=driver_name,
                all_specs=all_specs,
            )
            if can_reach:
                reachable, chain = self._propagate_upward(
                    func_name=caller_name,
                    counterexample=counterexample,
                    all_funcs=all_funcs,
                    all_specs=all_specs,
                    parsed_file=parsed_file,
                    driver_name=driver_name,
                    visited=visited,
                    cross_file_callers=cross_file_callers,
                    cross_file_caller_contexts=cross_file_caller_contexts,
                )
                if reachable:
                    return True, chain + [func_name]

        # No caller chain leads to entry
        return False, [func_name]

    # ------------------------------------------------------------------
    # Spurious handling / refinement
    # ------------------------------------------------------------------

    def _handle_spurious(
        self,
        func: FunctionInfo,
        spec: Spec,
        counterexample: Counterexample,
        callers: dict[str, FunctionInfo],
        all_specs: dict[str, Spec],
        parsed_file: ParsedCFile,
    ) -> ValidationResult:
        """Handle a spurious counterexample by refining the precondition."""
        func_name = func.name
        refinement_history: list[dict] = []
        current_spec = spec

        # Collect caller-reachable states for the over-refinement guard
        caller_expected_preconditions = [
            all_specs[cname].precondition
            for cname in callers
            if cname in all_specs
        ]
        # Fallback: if we have no caller spec info
        if not caller_expected_preconditions:
            caller_expected_preconditions = ["true"]

        caller_reachable_states = "\n".join(
            f"- Caller '{cname}': {all_specs.get(cname, Spec(cname, 'true', 'true')).precondition}"
            for cname in callers
        ) or "No caller state information available."

        stalled = False
        budget_exhausted = False

        for iteration in range(self.config.max_refinement_iters):
            logger.info(
                "Refinement iteration %d/%d for '%s'",
                iteration + 1,
                self.config.max_refinement_iters,
                func_name,
            )

            new_precondition = self._refine_precondition(
                original_spec=current_spec,
                spurious_state=counterexample,
                caller_reachable_states=caller_reachable_states,
                iteration=iteration,
            )

            if not new_precondition or new_precondition == current_spec.precondition:
                logger.info(
                    "Refinement stalled at iteration %d (same precondition)", iteration
                )
                stalled = True
                break

            # Check over-refinement: try CBMC first, fall back to LLM
            cbmc_guard = self._check_over_refinement_with_cbmc(
                new_precondition=new_precondition,
                original_precondition=current_spec.precondition,
                spurious_counterexample=counterexample,
                func=func,
            )
            if cbmc_guard is not None:
                is_safe = cbmc_guard
                guard_method = "cbmc"
                logger.info("Soundness guard (CBMC): safe=%s for '%s'", is_safe, func_name)
            else:
                # Agentic caller-grounded guard (opt-in) replaces the weak
                # string-compare when it can decide; reads the real callers.
                agentic_guard = self._agentic_soundness_guard(
                    func=func,
                    new_precondition=new_precondition,
                    counterexample=counterexample,
                )
                if agentic_guard is not None:
                    is_safe = agentic_guard
                    guard_method = "agentic"
                    logger.info("Soundness guard (agentic): safe=%s for '%s'", is_safe, func_name)
                else:
                    is_safe = self._check_over_refinement(
                        new_precondition=new_precondition,
                        caller_expected_preconditions=caller_expected_preconditions,
                    )
                    guard_method = "llm"
                    logger.info("Soundness guard (LLM): safe=%s for '%s'", is_safe, func_name)

            refinement_history.append({
                "iteration": iteration + 1,
                "proposed_precondition": new_precondition,
                "guard_method": guard_method,
                "accepted": is_safe,
            })

            if not is_safe:
                logger.warning(
                    "Over-refinement detected at iteration %d — rejecting refinement",
                    iteration,
                )
                # Choose the safe fallback outcome by what kind of CEX this is:
                # * STRUCTURAL panic (slice OOB, overflow, unwrap_failed, …):
                #   the function genuinely can panic on the spurious input -- the
                #   over-refinement guard rejecting our tightening means the body
                #   has a real exploit shape we just can't tightly constrain. Keep
                #   the existing REAL_BUG verdict (under security; LATENT under
                #   safety) so the finding isn't lost.
                # * POSTCONDITION violation (`check_<fn>.assertion`, trace =
                #   "postcondition violated"): the LLM's functional spec is the
                #   thing being violated, not the function's own safety check.
                #   When the over-refinement guard rejects our tightening, the
                #   most likely explanation is that the SPEC is over-strict --
                #   the function is correct but the LLM-generated postcondition
                #   misses a legitimate edge case. Downgrade to SPURIOUS. Live
                #   hybrid CCC sweep produced three such false positives in the
                #   first 80 files (peephole::xreg_name with contradictory
                #   clauses, asm_expr::char_escape_value with over-narrow result
                #   range, asm_preprocess::split_field_on_whitespace missing the
                #   all-whitespace case) -- all genuinely correct functions.
                is_panic = _is_structural_panic(
                    counterexample.failing_property,
                    getattr(counterexample, "trace", None),
                )
                is_post_violation = (
                    counterexample.failing_property or ""
                ).startswith("check_") and "postcondition violated" in " ".join(
                    getattr(counterexample, "trace", None) or []
                )

                if is_post_violation and not is_panic:
                    fallback_outcome = CExOutcome.SPURIOUS
                    reasoning = (
                        f"Over-refinement guard rejected at iteration {iteration + 1}: "
                        "would exclude legitimate caller states. Failing property is a "
                        "postcondition violation (not a structural panic), so the most "
                        "likely cause is an over-strict LLM-generated functional spec "
                        "rather than a real function bug. Classifying SPURIOUS."
                    )
                else:
                    # Structural panic + over-refinement rejected: previously
                    # this branch promoted to REAL_BUG "to be safe", which
                    # produced false positives whenever the LLM-generated
                    # harness omitted a constraint that real callers maintain.
                    # Concrete example: base64::add_padding panics when
                    # output.len() < rem_len, but every base64 caller sizes
                    # the buffer correctly. The harness's omission of that
                    # constraint isn't a function bug.
                    # Mark UNRESOLVED instead: surface the finding for human
                    # review without claiming it's a confirmed real bug.
                    fallback_outcome = CExOutcome.UNRESOLVED
                    reasoning = (
                        f"Over-refinement guard rejected at iteration {iteration + 1}: "
                        "could not tighten the precondition without excluding states "
                        "callers can produce. Failing property is a structural panic. "
                        "We can't determine whether real callers maintain the "
                        "implicit invariant that would prevent the panic, so "
                        "classifying UNRESOLVED (not REAL_BUG) -- the spec generator "
                        "didn't capture the constraint, but that doesn't mean callers "
                        "violate it."
                    )
                over_result = ValidationResult(
                    function_name=func_name,
                    counterexample=counterexample,
                    caller_path=[],
                    system_entry_input=None,
                    refinement_history=refinement_history,
                    final_precondition=None,
                    reasoning=reasoning,
                    outcome=fallback_outcome,
                    over_refinement_rejected=True,
                )
                # Note: over-refinement case doesn't have access to all_funcs/all_specs
                # from _handle_spurious params; dynamic validation skipped here.
                return over_result

            # Accept the refinement
            current_spec = Spec(
                function_name=func_name,
                precondition=new_precondition,
                postcondition=spec.postcondition,
                callee_specs=spec.callee_specs,
                loop_invariants=spec.loop_invariants,
            )
            logger.info(
                "Accepted refined precondition at iteration %d: %s",
                iteration + 1,
                new_precondition[:80],
            )
        else:
            # Loop completed without break → budget exhausted without stalling
            budget_exhausted = True

        # Determine final outcome:
        # - stalled (same precondition returned) → SPURIOUS
        # - budget exhausted (max iters hit without stalling) → UNRESOLVED
        if budget_exhausted:
            final_outcome = CExOutcome.UNRESOLVED
            reasoning = (
                f"Refinement budget exhausted after {self.config.max_refinement_iters} "
                f"iteration(s) for '{func_name}' — could not reach a stable precondition. "
                f"Counterexample left unresolved."
            )
        else:
            threat_model = getattr(self.config, "threat_model", "security").lower()
            is_panic = _is_structural_panic(
                counterexample.failing_property,
                getattr(counterexample, "trace", None),
            )
            is_pub = _is_publicly_callable(func)
            # Under threat_model='security', any function that has at least one
            # caller anywhere in the codebase is treated as transitively reachable
            # from an attacker-controlled input path (CCC, VibeOS, and similar
            # binary-crate-style codebases have no useful 'pub fn' boundary —
            # every fn in the crate is reachable from main()). This keeps the
            # gate strict for true dead code (no callers at all) but stops the
            # classifier from silently dropping real OOB / overflow bugs on
            # private helpers whose Kani CEx lacks concrete variable_assignments.
            reachable_security = (threat_model == "security") and bool(callers)

            if is_panic and (is_pub or reachable_security):
                if threat_model == "security":
                    if is_pub:
                        final_outcome = CExOutcome.REAL_BUG
                        reasoning = (
                            f"Security-threat-model real bug in '{func_name}': no in-tree "
                            f"caller produces the CEx state {counterexample.variable_assignments}, "
                            f"but the function is publicly callable and the failing property "
                            f"'{counterexample.failing_property}' is a structural Rust/C panic. "
                            f"Under threat_model='security', the public API IS the attacker's "
                            f"interface — adversarial inputs can trigger this panic. "
                            f"Refinement stabilised after {len(refinement_history)} iteration(s)."
                        )
                    else:
                        # Private helper with at least one caller -- LATENT under security
                        # (real panic on adversarial input, but BMC didn't extract a concrete
                        # reproducer chain through callers). Distinct from REAL_BUG, which
                        # requires either a pub-API entry or an explicit caller chain.
                        final_outcome = CExOutcome.LATENT
                        caller_names = ", ".join(list(callers.keys())[:3])
                        if len(callers) > 3:
                            caller_names += f", … (+{len(callers)-3} more)"
                        reasoning = (
                            f"Latent security panic in private helper '{func_name}': "
                            f"failing property '{counterexample.failing_property}' is a "
                            f"structural Rust/C panic, and the function is reachable from "
                            f"in-scope callers ({caller_names}). BMC counterexample lacked "
                            f"concrete variable_assignments so the input-reachability stage "
                            f"could not construct a propagated reproducer chain, but under "
                            f"threat_model='security' a panic reachable from the codebase "
                            f"is exploitable if any upstream caller transitively processes "
                            f"attacker input. Refinement stabilised after "
                            f"{len(refinement_history)} iteration(s)."
                        )
                else:
                    # Non-security threat model + structural panic + publicly callable
                    # -> LATENT (hardening task, no in-tree path proven).
                    final_outcome = CExOutcome.LATENT
                    reasoning = (
                        f"Latent panic on the public API of '{func_name}': no in-tree "
                        f"caller produces the CEx state {counterexample.variable_assignments}, "
                        f"but the function is publicly callable and the failing property "
                        f"'{counterexample.failing_property}' is a structural Rust/C panic. "
                        f"Threat model '{threat_model}' (not security): cargo-fuzz / future "
                        f"caller can trigger via the public API; in-tree callers implicitly "
                        f"satisfy the missing precondition through surrounding invariants. "
                        f"Refinement stabilised after {len(refinement_history)} iteration(s)."
                    )
            else:
                final_outcome = CExOutcome.SPURIOUS
                reasoning = (
                    f"Counterexample is spurious — no caller can produce the state "
                    f"{counterexample.variable_assignments}. "
                    f"Precondition refined over {len(refinement_history)} iteration(s)."
                )

        return ValidationResult(
            function_name=func_name,
            counterexample=counterexample,
            caller_path=[],
            system_entry_input=None,
            refinement_history=refinement_history,
            final_precondition=current_spec.precondition,
            reasoning=reasoning,
            outcome=final_outcome,
        )

    def _refine_precondition(
        self,
        original_spec: Spec,
        spurious_state: Counterexample,
        caller_reachable_states: str,
        iteration: int,
    ) -> str:
        """Use LLM to generate a tightened precondition.

        If the first response is unchanged (or empty), and we are on the
        openai/K2 path, re-prompt the model with self-critique. Same pattern
        as the Phase 1 vacuous-spec critique: reasoning models on K2 routinely
        default to "no change" / "true" when asked to refine, which stalls
        the refinement loop at iteration 1 and produces a SPURIOUS verdict
        with empty refinement_history. Critique converts that quiet stall
        into a second attempt anchored on the CEX state.
        """
        state_str = ", ".join(
            f"{k} = {v}"
            for k, v in spurious_state.variable_assignments.items()
        )

        system_prompt = "You are a formal verification expert for C programs."
        user_prompt = REFINEMENT_PROMPT.format(
            function_name=original_spec.function_name,
            original_precondition=original_spec.precondition,
            spurious_state=state_str,
            caller_reachable_states=caller_reachable_states,
            iteration=iteration + 1,
        )

        refined = self._refine_call_with_critique(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            original_precondition=original_spec.precondition,
            spurious_state=state_str,
            caller_reachable_states=caller_reachable_states,
        )
        if refined:
            logger.debug("LLM refined precondition: %s", refined[:80])
            return refined
        # Fallback: return original unchanged
        return original_spec.precondition

    def _refine_call_with_critique(
        self,
        system_prompt: str,
        user_prompt: str,
        original_precondition: str,
        spurious_state: str,
        caller_reachable_states: str,
    ) -> str:
        """One refinement call, with a vacuous-output critique retry on K2."""
        try:
            response = self.llm.complete(system_prompt, user_prompt, role="refinement",
                                         cache_prefix=getattr(self.config, "domain_summary", ""))
        except LLMError as exc:
            logger.warning("LLM refinement failed: %s", exc)
            return ""
        data = _parse_json_response(response)
        first = (data or {}).get("refined_precondition", "").strip() if data else ""

        # Did the model produce any meaningful change?
        first_stripped = (first or "").strip()
        orig_stripped = (original_precondition or "").strip()
        vacuous = (
            not first_stripped
            or first_stripped == orig_stripped
            or first_stripped in ("true", "True")
        )
        if not vacuous:
            return first

        # Provider gate: only critique on the openai/K2 path; Claude rarely
        # stalls on refinement, so the extra call would just double cost.
        provider = (
            self.config.resolved_provider()
            if hasattr(self.config, "resolved_provider") else "anthropic"
        )
        if provider != "openai":
            return first

        critique = (
            "You returned the same precondition as the original (no change). That stalls\n"
            "the refinement loop. The original spec admits the SPURIOUS counterexample\n"
            "state below, which means at least one parameter combination satisfies the\n"
            "precondition but no real caller produces it. Your job: tighten the\n"
            "precondition so that combination is excluded WITHOUT excluding the legitimate\n"
            "caller states.\n\n"
            f"  original precondition: {original_precondition}\n"
            f"  spurious CEX state:    {spurious_state or '(empty — Kani gave no concrete values)'}\n"
            f"  callers reach states:  {caller_reachable_states}\n\n"
            "Propose ONE concrete clause to AND into the precondition. Examples of\n"
            "useful clauses for structural-panic CEXs:\n"
            "  * `pos < buf.len()`            — slice OOB\n"
            "  * `offset + N <= data.len()`   — multi-byte read OOB\n"
            "  * `align.is_power_of_two()`    — alignment helpers\n"
            "  * `n <= slice.len()`           — n/len mismatch\n"
            "  * `denom != 0`                 — divide-by-zero\n"
            "  * `val.checked_add(off).is_some()` — overflow add\n\n"
            "Output ONLY a JSON object with a single `refined_precondition` field. The\n"
            "value MUST be a valid Rust/C boolean expression DIFFERENT from the original."
        )
        try:
            critique_response = self.llm.complete(
                system_prompt, critique, max_tokens=32768, role="refinement",
                cache_prefix=getattr(self.config, "domain_summary", ""),
            )
        except LLMError as exc:
            logger.debug("Refinement critique LLM call failed: %s -- using original", exc)
            return first
        cdata = _parse_json_response(critique_response)
        if cdata is None:
            logger.debug("Refinement critique produced unparseable response")
            return first
        second = (cdata.get("refined_precondition") or "").strip()
        if not second or second == orig_stripped or second in ("true", "True"):
            logger.debug("Refinement critique still vacuous -- accepting stall")
            return first
        logger.info(
            "Refinement critique upgraded precondition: %s",
            second[:80],
        )
        return second

    def _agentic_soundness_guard(
        self,
        func: FunctionInfo,
        new_precondition: str,
        counterexample: Counterexample,
    ) -> "bool | None":
        """Caller-grounded soundness check on a proposed (tighter) precondition.

        Upgrades the over-refinement guard: instead of comparing the new
        precondition against caller *spec strings*, an agentic ``SoundnessAgent``
        (claude-code with Read/Grep, when routed there) reads the actual call
        sites and decides whether the tightening is caller-guaranteed.

        Returns:
          * True  — SOUND (caller-guaranteed): the tightening is safe to accept.
          * False — UNSOUND (a caller can violate it): over-refinement; reject
                    so the counterexample survives as a real-bug lead.
          * None  — disabled / UNKNOWN / fabricated-caller / error: defer to the
                    CBMC and string-based guards (graceful degradation; a
                    non-agentic backend returns UNKNOWN here).
        """
        if not getattr(self.config, "enable_soundness_gate", False):
            return None
        try:
            from bmc_agent.agents.soundness import SoundnessAgent, caller_is_fabricated
            res = SoundnessAgent(self.config, self.llm).run(
                func_info=func,
                proposed_clause=new_precondition,
                rejected_cex=counterexample,
            )
            if not res.ok or res.output is None:
                return None
            v = res.output
            if v.verdict == "SOUND":
                return True
            if v.verdict == "UNSOUND":
                src = getattr(func, "source_file", "") or ""
                if caller_is_fabricated(v.implicated_caller, src):
                    logger.info(
                        "agentic soundness guard [%s]: UNSOUND but cited caller "
                        "%r not found in tree — deferring",
                        func.name, v.implicated_caller,
                    )
                    return None
                logger.info(
                    "agentic soundness guard [%s]: UNSOUND (caller %s) — "
                    "over-refinement, keeping counterexample as a lead",
                    func.name, v.implicated_caller or (v.rationale or "")[:80],
                )
                return False
            return None  # UNKNOWN → defer
        except Exception as exc:
            logger.warning(
                "agentic soundness guard [%s] error: %s — deferring",
                getattr(func, "name", "?"), exc,
            )
            return None

    def _check_over_refinement_with_cbmc(
        self,
        new_precondition: str,
        original_precondition: str,
        spurious_counterexample: Counterexample,
        func: FunctionInfo,
    ) -> "bool | None":
        """
        CBMC-based soundness guard (conventional component).

        Checks: ∃ params s.t. original_precond(params) ∧ ¬spurious_state(params) ∧ ¬new_precond(params)
        i.e., are there non-spurious states that satisfy the old precondition but are
        excluded by the proposed new precondition?

        Returns True (safe to apply), False (over-refined), or None (inconclusive — fall back to LLM).
        """
        import shutil
        import tempfile

        from bmc_agent.dsl_to_cbmc import translate_atom

        if not shutil.which(self.config.cbmc_path):
            return None

        sig = func.signature

        # Only attempt CBMC check for scalar (non-pointer, non-struct) parameters —
        # pointer preconditions require heap modelling that this harness doesn't provide.
        for ptype, _ in sig.parameters:
            ptype_stripped = ptype.strip()
            if "*" in ptype_stripped or "struct" in ptype_stripped or "..." in ptype_stripped:
                return None

        # Try translating new_precondition via DSL translator.
        try:
            new_assert = translate_atom(new_precondition.strip(), context="assert")
            if not new_assert or new_assert.startswith("/* condition:"):
                return None
        except Exception:
            return None

        # Build C harness
        lines: list[str] = [
            "#include <stdint.h>",
            "#include <stddef.h>",
            "void __CPROVER_assume(_Bool);",
            "",
            "int main(void) {",
        ]

        # Declare nondeterministic parameters
        param_names: list[str] = []
        for idx, (ptype, pname) in enumerate(sig.parameters):
            var = pname if pname else f"arg{idx}"
            lines.append(f"    {ptype.strip()} {var};")
            param_names.append(var)

        # Assume original precondition (if translatable)
        try:
            old_assume = translate_atom(original_precondition.strip(), context="assume")
            if old_assume and not old_assume.startswith("/* condition:"):
                lines.append(f"    {old_assume}")
        except Exception:
            pass  # skip — no assumption on original precondition

        # Assume NOT the spurious state (we're looking for other states excluded by new precond)
        # Build a conjunction of the counterexample variable assignments that match parameters
        spurious_excludes: list[str] = []
        for pname in param_names:
            val = spurious_counterexample.variable_assignments.get(pname)
            if val and val not in ("NULL", "unknown", "{}"):
                # Keep it simple: only scalar integer literals
                val_stripped = str(val).strip().rstrip("ul").rstrip("l")
                if val_stripped.lstrip("-").isdigit():
                    spurious_excludes.append(f"{pname} == {val}")
        if spurious_excludes:
            conjunction = " && ".join(f"({e})" for e in spurious_excludes)
            lines.append(f"    __CPROVER_assume(!({conjunction}));")

        # Assert new precondition — CBMC CEX means over-refinement
        lines.append(f"    {new_assert}")
        lines.append("    return 0;")
        lines.append("}")

        harness_src = "\n".join(lines) + "\n"

        with tempfile.NamedTemporaryFile(
            suffix=".c", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp.write(harness_src)
            tmp_path = tmp.name

        try:
            result = run_cbmc(
                harness_path=tmp_path,
                unwind=self.config.cbmc_unwind,
                timeout=min(self.config.cbmc_timeout, 30),
                cbmc_path=self.config.cbmc_path,
                include_dirs=getattr(self.config, "include_dirs", None),
            )
        except Exception:
            return None
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

        if result.error:
            return None  # inconclusive

        # CBMC verified → no non-spurious state violates new_precond → safe
        # CBMC found CEX → some non-spurious state is excluded → over-refined
        return result.verified

    def _check_over_refinement(
        self,
        new_precondition: str,
        caller_expected_preconditions: list[str],
    ) -> bool:
        """
        Check if the new precondition would exclude states that callers can produce.

        Returns True if safe (not over-refined), False if over-refined.
        """
        if not caller_expected_preconditions:
            return True  # no callers → safe by definition

        callers_str = "\n".join(
            f"- {pre}" for pre in caller_expected_preconditions
        )

        system_prompt = "You are a formal verification expert for C programs."
        user_prompt = OVER_REFINEMENT_CHECK_PROMPT.format(
            new_precondition=new_precondition,
            caller_expected_preconditions=callers_str,
        )

        try:
            response = self.llm.complete(system_prompt, user_prompt, role="refinement",
                                         cache_prefix=getattr(self.config, "domain_summary", ""))
            data = _parse_json_response(response)
            if data is not None:
                is_over_refined = bool(data.get("is_over_refined", False))
                reasoning = data.get("reasoning", "")
                logger.debug(
                    "Over-refinement check: %s (%s)",
                    is_over_refined,
                    reasoning[:80],
                )
                return not is_over_refined  # True = safe
        except LLMError as exc:
            logger.warning("LLM over-refinement check failed: %s", exc)

        # Default: conservatively say it IS safe (don't block refinement)
        return True

    # ------------------------------------------------------------------
    # System entry reproducer generation
    # ------------------------------------------------------------------

    def _reproducer_agent_attempt(
        self,
        call_chain: list[str],
        counterexample: Counterexample,
        all_funcs: dict[str, FunctionInfo],
        parsed_file: ParsedCFile,
    ) -> "Optional[str]":
        """Phase 2-REPRO: try the tool-using ReproducerAgent. Returns a real
        reproducer source on success, or None to fall through to the one-shot
        generator (agent error, empty output, or UNREPRODUCIBLE). Never raises."""
        fut = call_chain[-1] if call_chain else "unknown"
        try:
            from bmc_agent.agents.reproducer_tools import ReproducerAgent
            fi = all_funcs.get(fut) or parsed_file.get_function_info(fut)
            fsrc = (getattr(fi, "body", "") if fi else "") or ""
            state_str = ", ".join(
                f"{k} = {v}" for k, v in counterexample.variable_assignments.items()
            )
            agent = ReproducerAgent(
                self.config, self.llm,
                parsed_file=parsed_file,
                corpus_paths=list(getattr(self, "corpus_paths", []) or []),
            )
            res = agent.run(
                function_name=fut,
                cbmc_property=counterexample.failing_property,
                counterexample=state_str,
                call_chain=list(call_chain),
                function_source=fsrc,
                threat_context=getattr(self.config, "threat_model_context", None),
            )
        except Exception as exc:
            logger.debug("ReproducerAgent raised for '%s': %s", fut, exc)
            return None
        src = res.output if res is not None else None
        # Require a real source string: a healthy agent always returns one, but
        # guard against a non-str (a misbehaving agent / test double) so it can
        # never leak downstream into the dynamic-validation include sanitizer —
        # fall through to the one-shot generator instead.
        if not isinstance(src, str) or not src or "UNREPRODUCIBLE" in src:
            return None
        logger.info("ReproducerAgent produced a system-entry reproducer for '%s'", fut)
        return src

    def _generate_system_entry_reproducer(
        self,
        call_chain: list[str],
        counterexample: Counterexample,
        all_funcs: dict[str, FunctionInfo],
        parsed_file: ParsedCFile,
    ) -> str:
        """Generate a C test case that triggers the bug from the system entry point."""
        # Phase 2-REPRO: when the tool-using reproducer agent is enabled, try it
        # FIRST (it loops compile->run->read-error->fix, reading real headers /
        # structs / the call chain). Fall through to the one-shot path below on
        # any miss (agent error or UNREPRODUCIBLE), so behaviour is unchanged when
        # off and never worse when on.
        if getattr(self.config, "enable_reproducer_agent", False):
            _agent_src = self._reproducer_agent_attempt(
                call_chain, counterexample, all_funcs, parsed_file
            )
            if _agent_src is not None:
                return _agent_src

        state_str = ", ".join(
            f"{k} = {v}"
            for k, v in counterexample.variable_assignments.items()
        )

        # Build function signatures string
        sigs: list[str] = []
        for fn_name in call_chain:
            fi = all_funcs.get(fn_name) or parsed_file.get_function_info(fn_name)
            if fi is not None:
                sig = fi.signature
                params = ", ".join(
                    f"{pt} {pn}".strip() for pt, pn in sig.parameters
                )
                sigs.append(f"{sig.return_type} {fn_name}({params})")
        sigs_str = "\n".join(sigs) or "No signature information available."

        system_prompt = "You are a formal verification expert for C programs."
        # De-anchor from libarchive: the system-entry reproducer prompt hardcodes
        # `#include <archive.h>` and the libarchive public API. That is only
        # correct when the target really IS libarchive (a .so behind a public
        # API). For any other project — e.g. VibeOS kernel internals compiled
        # DIRECTLY into the harness — that framing makes the LLM emit
        # `archive_read_new()` / `ARCHIVE_OK` for a `calloc`/`vfs_*` target,
        # which fails to compile/link and silently loses the system-entry
        # dynamic tier. Mirror scenario_reproducer's _is_libarchive_target switch.
        _libarchive_target = _is_libarchive_target(parsed_file)
        _prompt = REPRODUCER_PROMPT if _libarchive_target else GENERIC_REPRODUCER_PROMPT
        user_prompt = _prompt.format(
            buggy_function=call_chain[-1] if call_chain else "unknown",
            call_chain=" → ".join(call_chain),
            counterexample_state=state_str,
            function_signatures=sigs_str,
        )

        try:
            response = self.llm.complete(system_prompt, user_prompt, role="realism",
                                         cache_prefix=getattr(self.config, "domain_summary", ""))
            data = _parse_json_response(response)
            if data is not None:
                code = data.get("reproducer_code", "").strip()
                if code:
                    # Guard against LLM-fabricated synthetic reproducers that
                    # reimplement the library inline instead of linking against
                    # the real .so. The REPRODUCER_PROMPT forbids this, but
                    # past sweeps showed the model sometimes does it anyway
                    # (e.g., reimplementing entry_list_add as a standalone
                    # buggy stub, then "crashing" on the stub) — producing
                    # misleading confirmed_dynamic verdicts on synthetic
                    # crashes that prove nothing about the real library.
                    #
                    # The HONEST `// UNREPRODUCIBLE: …` answer is fine and
                    # gets passed through verbatim so dyn-val skips it
                    # cleanly. Anything else that lacks `#include <archive.h>`
                    # is rejected as synthetic — return the explicit
                    # UNREPRODUCIBLE marker so downstream knows nothing got
                    # validated against real linked code.
                    if code.startswith("// UNREPRODUCIBLE"):
                        return code
                    # The public-API-header guard below only makes sense for a
                    # library-behind-a-.so target (where a missing `#include
                    # <archive.h>` means the LLM fabricated inline stubs instead
                    # of linking real symbols). For a direct-compiled internal
                    # target (VibeOS kernel etc.) the function under test IS
                    # compiled into the harness and is called directly with NO
                    # public-API header — enforcing the include would wrongly
                    # reject every legitimate generic reproducer. Skip the guard
                    # there; the generic prompt's "do not re-implement" rule plus
                    # compile-together linkage already cover the synthetic case.
                    if not _libarchive_target:
                        return code
                    # Project-agnostic public-header detection: derive the
                    # allowlist from config.include_dirs (same heuristic
                    # boundary_detector uses for public-fn detection). Falls
                    # back to a built-in set if no headers are auto-detected.
                    project_headers = _autodiscover_public_headers(
                        getattr(self.config, "include_dirs", None) or []
                    ) or None  # None → use _reproducer_uses_public_api's fallback
                    if not _reproducer_uses_public_api(code, project_headers):
                        sample = (project_headers or ["<built-in list>"])[:3]
                        logger.warning(
                            "Reproducer for '%s' lacks any project public-API "
                            "header (looked for: %s) and would test "
                            "LLM-fabricated stubs instead of real linked code "
                            "— rejecting as synthetic; marking UNREPRODUCIBLE.",
                            call_chain[-1] if call_chain else "?",
                            sample,
                        )
                        return (
                            "// UNREPRODUCIBLE: reproducer did not include any "
                            "project public-API header; would have tested "
                            "fabricated stubs instead of real linked code"
                        )
                    return code
        except LLMError as exc:
            logger.warning("LLM reproducer generation failed: %s", exc)

        # Fallback: minimal stub reproducer (UNREPRODUCIBLE marker variant
        # so downstream treats it as "no reproducer available" instead of
        # attempting to compile + run a no-op).
        chain_str = " → ".join(call_chain)
        return (
            f"// UNREPRODUCIBLE: LLM did not produce a reproducer; "
            f"call chain was {chain_str}, witness {state_str[:120]}…"
        )

    # ------------------------------------------------------------------
    # Dynamic validation (Stage 3)
    # ------------------------------------------------------------------

    def _try_dynamic_validation(
        self,
        validation_result: ValidationResult,
        func: FunctionInfo,
        all_funcs: dict[str, FunctionInfo],
        all_specs: dict[str, Spec],
        parsed_file: ParsedCFile,
    ) -> None:
        """Run dynamic validation and attach the result to validation_result in-place."""
        if self._dynamic_validator is None:
            return

        # When BOTH static checks (reachability + callee feasibility)
        # errored, the historical policy was to SKIP dynamic validation
        # entirely, on the theory that a blind harness on a guessed
        # entry produces low-signal results.
        #
        # That policy is INVERTED here: when CBMC errors and we have a
        # public-API-validated reproducer, the LLM-built dynamic harness
        # becomes the BEST available oracle — it's the only mechanical
        # truth check that doesn't depend on the broken CBMC checks.
        # Without it, the only signal left is LLM reachability (which
        # we've seen confabulate). With it, we get an actual
        # crash-or-not signal from real linked code driven by the real
        # public API.
        #
        # The skip is preserved only when the reproducer is
        # UNREPRODUCIBLE-marked (LLM declined / synthetic stub
        # rejected), since running a fake harness is genuinely
        # low-signal regardless of CBMC state.
        if (
            getattr(self, "_reach_errored", False)
            and getattr(self, "_feas_errored", False)
        ):
            reproducer = (validation_result.system_entry_input or "").strip()
            is_unreproducible = (
                not reproducer
                or reproducer.startswith("// UNREPRODUCIBLE")
            )
            if is_unreproducible:
                logger.info(
                    "Skipping dynamic validation for '%s' — both CBMC checks "
                    "errored AND reproducer is UNREPRODUCIBLE-marked; no "
                    "ground-truth oracle available",
                    func.name,
                )
                return
            logger.info(
                "CBMC reachability + feasibility errored for '%s' — leaning "
                "on dynamic validation as the only remaining ground-truth "
                "oracle (reproducer passed public-API validation, so a "
                "crash/no-crash signal will be authoritative)",
                func.name,
            )
            # Fall through to run dyn-val.

        caller_path = validation_result.caller_path
        entry_name = caller_path[0] if caller_path else func.name
        entry_func = all_funcs.get(entry_name) or parsed_file.get_function_info(entry_name)
        if entry_func is None:
            logger.warning(
                "Dynamic validation: entry function '%s' not found", entry_name
            )
            return

        logger.info(
            "Running dynamic validation for '%s' (entry: '%s')",
            func.name, entry_func.name,
        )
        dynamic_result = self._dynamic_validator.validate(
            entry_func=entry_func,
            counterexample=validation_result.counterexample,
            parsed_file=parsed_file,
            all_funcs=all_funcs,
            all_specs=all_specs,
            caller_path=caller_path,
            system_entry_reproducer=validation_result.system_entry_input,
            corpus_paths=list(getattr(self, "corpus_paths", []) or []),
        )
        validation_result.dynamic_result = dynamic_result

        # Downgrade REAL_BUG → UNRESOLVED when dyn-val explicitly came
        # up clean on a crash-class property. Without this, the static
        # ``system_entry_reached=True`` caller-chain trace overrides
        # the dyn-val NOT_TRIGGERED signal — yielding the FP class
        # surfaced in postfix7 (e.g. ``append_id_w.pointer_dereference.11``:
        # caller chain traced to ``archive_acl_to_text_w``, but the LLM-
        # generated public-API reproducer compiled and ran to completion
        # without crashing). The earlier pipeline-level realism-skip
        # short-circuit (pipeline.py:_is_crash_class_property) only
        # affects the realism field, not the classifier outcome — fixing
        # only one of the two leaves the persisted ``outcome=real_bug``
        # standing despite the contradiction.
        #
        # Sound: a crash-class CBMC property (pointer-deref, bounds,
        # double-free, recursion/unwind) describes a fault that would
        # SIGFAULT at runtime. If the dyn-val harness compiled, ran
        # the same input under real libc, and finished cleanly in
        # bounded time, the CBMC witness is a verification-model
        # artifact (unconstrained allocator returns, symbolic-only
        # aliasing, etc.) — NOT a real bug.
        #
        # Conservative: only fire on crash-class properties (the
        # silent-UB classes like overflow/conversion don't crash at
        # runtime, so dyn-val NOT_TRIGGERED there is uninformative
        # and the existing realism-LLM path still handles them).
        try:
            from bmc_agent.dynamic_validator import DynamicOutcome
            from bmc_agent.pipeline import _is_crash_class_property
        except Exception:
            return
        # Defensive: some test fixtures pass a stripped-down stand-in
        # for ValidationResult without an ``outcome`` field. Only fire
        # the downgrade when the field is present and equals REAL_BUG.
        current_outcome = getattr(validation_result, "outcome", None)
        _ra_cfg = getattr(self, "config", None) or getattr(getattr(self, "llm", None), "config", None)
        if _ra_cfg is not None and getattr(_ra_cfg, "realism_authoritative", True):
            return  # realism authoritative: dyn-val NOT_TRIGGERED must NOT downgrade a finding
        if (
            current_outcome == CExOutcome.REAL_BUG
            and dynamic_result is not None
            and dynamic_result.outcome == DynamicOutcome.NOT_TRIGGERED
            and _is_crash_class_property(
                getattr(validation_result.counterexample, "failing_property", "") or ""
            )
        ):
            _cfg = getattr(self, "config", None) or getattr(getattr(self, "llm", None), "config", None)
            if _cfg is not None and getattr(_cfg, "enable_classifier_tools", False):
                try:
                    from bmc_agent.agents.classifier_tools import ClassifierAdjudicatorAgent
                    _cex = validation_result.counterexample
                    _wit = "\n".join(
                        f"{k}={v}" for k, v in
                        (getattr(_cex, "variable_assignments", {}) or {}).items()
                    )[:1500]
                    if ClassifierAdjudicatorAgent(_cfg, self.llm).keeps_real_bug(
                        fn=func.name,
                        prop=getattr(_cex, "failing_property", "") or "",
                        reasoning=getattr(validation_result, "reasoning", "") or "",
                        witness=_wit,
                    ):
                        logger.info(
                            "Classifier adjudicator OVERRIDE: '%s' kept REAL_BUG "
                            "(agentic code review found a reachable caller path; "
                            "deterministic dyn-val downgrade skipped)",
                            func.name,
                        )
                        return
                except Exception as _exc:  # noqa: BLE001
                    logger.warning(
                        "classifier adjudicator failed (%r); deferring to "
                        "deterministic downgrade", _exc,
                    )
            logger.info(
                "Validation downgrade: '%s' outcome REAL_BUG → UNRESOLVED "
                "(dyn-val NOT_TRIGGERED on crash-class property '%s' "
                "contradicts the static caller-chain trace; classifying as "
                "model artifact)",
                func.name,
                getattr(validation_result.counterexample, "failing_property", ""),
            )
            validation_result.outcome = CExOutcome.UNRESOLVED
            validation_result.reasoning = (
                (validation_result.reasoning or "")
                + " [Downgraded REAL_BUG → UNRESOLVED: dyn-val NOT_TRIGGERED "
                "on crash-class property contradicts the static caller-chain "
                "trace; classifying as model artifact.]"
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_callers(
        self,
        func_name: str,
        all_funcs: dict[str, FunctionInfo],
    ) -> dict[str, FunctionInfo]:
        """Return all functions in all_funcs that call func_name."""
        callers: dict[str, FunctionInfo] = {}
        for fname, finfo in all_funcs.items():
            if func_name in finfo.callees:
                callers[fname] = finfo
        return callers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_c_comments_and_strings(text: str) -> str:
    """Replace C/C++ comments and string/char literals with spaces of
    equal length so identifier offsets stay aligned. Used by
    ``_is_address_taken`` so a function name appearing inside a comment
    (``/* foo */``) doesn't get mistaken for an address-taking
    reference."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        if c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                j = n
            else:
                j += 2
            out.append(" " * (j - i))
            i = j
            continue
        if c == '"' or c == "'":
            quote = c
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _is_address_taken(func_name: str, parsed_file) -> bool:
    """Return True if ``func_name`` appears anywhere in the parsed source
    as an address (i.e. NOT followed by ``(``).

    Used to detect callback functions passed to qsort/bsearch/
    pthread_create/atexit/signal etc. — the call graph only records
    direct calls, so these functions look caller-less even though they
    are reachable via a library indirection. Marking such a function
    a "system entry point" promotes its CEx to a confirmed real_bug
    even though the library function's contract typically excludes
    the violating state (qsort guarantees non-NULL element pointers).

    Detection: strip comments and string literals first (so a function
    name mentioned in a ``/* foo */`` doesn't count), then scan every
    occurrence of ``func_name`` as a whole word. If at least one
    occurrence is NOT immediately followed by ``(``, the function is
    address-taken.

    Conservative: false positives here only push a finding from
    REAL_BUG to UNRESOLVED, which is safe (under-confirming a bug is
    preferable to over-confirming one).
    """
    if not func_name or not func_name.isidentifier():
        return False

    # Prefer the full preprocessed source (covers function-pointer
    # initializers in static structs and global tables); fall back to
    # the concatenation of function bodies.
    text = getattr(parsed_file, "preprocessed_source", None)
    if not text:
        bodies = getattr(parsed_file, "function_bodies", None) or {}
        text = "\n".join(bodies.values()) if bodies else ""
    if not text:
        return False

    text = _strip_c_comments_and_strings(text)
    pattern = re.compile(r"\b" + re.escape(func_name) + r"\b")
    for m in pattern.finditer(text):
        end = m.end()
        # Skip whitespace after the match — ``foo (`` is still a call.
        i = end
        while i < len(text) and text[i] in " \t":
            i += 1
        if i >= len(text) or text[i] != "(":
            return True
    return False


def _body_has_same_panic_shape(body: str, trace_text: str) -> bool:
    """Heuristic: does *body* contain the kind of arithmetic / indexing
    pattern that would produce the same panic class as *trace_text*?

    Used to keep the spec-evaluation filter from masking real bugs where
    both the spec and the function body have unguarded arithmetic on the
    same inputs. We're conservative: any plausible pattern returns True,
    because false-positive on the BODY side just means the finding goes
    through the normal classifier (which might still mark SPURIOUS), but
    false-negative loses real bugs (the elf/io.rs class).
    """
    import re as _re
    # Slice / index OOB on the function side: ``arr[expr]`` with non-trivial expr,
    # or use of patterns like ``data[off + N]``, ``buf[idx]``, ``windows(k)``.
    if "index out of bounds" in trace_text or "slice_index_fail" in trace_text:
        if _re.search(r"\w+\s*\[\s*\w+\s*[+\-*]", body):  # `data[off+1]` shape
            return True
        if _re.search(r"\.\s*windows\s*\(", body):
            return True
        if _re.search(r"\.\s*get\s*\(\s*\w+\s*[+\-]", body):
            return True
    # Arithmetic overflow on the function side.
    if any(m in trace_text for m in (
        "attempt to add with overflow",
        "attempt to subtract with overflow",
        "attempt to multiply with overflow",
    )):
        # Look for non-checked arithmetic on integer params: `x + N`, `x * N`, etc.
        # Skip the body if it ONLY uses `wrapping_*` / `checked_*` / `saturating_*`.
        if _re.search(r"\b\w+\s*[+\-*]\s*\w", body):
            # Heuristic: at least one non-wrapped arithmetic site exists.
            # We don't try to prove the body is fully-wrapped -- false positives
            # on safe code just route through the normal classifier.
            return True
    if "attempt to shift" in trace_text and _re.search(r"<<|>>", body):
        return True
    if "attempt to divide by zero" in trace_text and _re.search(r"/\s*\w|\.\s*rem_", body):
        return True
    return False


def _witness_obvious_artifact(counterexample, func=None) -> Optional[str]:
    """Cheap, deterministic check: does the witness state match a known
    model-artifact pattern? Returns a one-line cause when it does, None
    otherwise. Mirrors the realism-checker's pre-LLM detectors so the
    classifier can skip expensive LLM reachability calls on findings
    that the realism stage would reject anyway.

    When *func* is supplied, the spec-evaluation panic filter is
    narrowed: if the function body itself contains the same arithmetic/
    indexing pattern as the spec, the filter does NOT fire (caller will
    then classify as LATENT/REAL — the function has the exploit shape
    too). Without *func*, the filter behaves as before (always fires
    on ``check_<fn>.assertion`` + structural panic combos).
    """
    # Spec-evaluation panic: when the failing property is
    # ``check_<fn>.assertion.N`` (note the ``check_`` prefix — the
    # generated harness wrapper) AND the trace is a structural panic
    # (overflow, slice OOB, divide-by-zero), the panic is inside the
    # SPEC expression Kani evaluates on its nondeterministic inputs,
    # not inside the function body. This is a Phase 1 functional-spec
    # false positive — the spec author wrote an unguarded slice index
    # (``input[pos]``) or non-wrapping arithmetic and the spec itself
    # panics during verification.
    #
    # Exception: when the function body has the SAME unguarded
    # arithmetic / indexing shape, the overflow is genuinely reachable
    # via the function too — the spec just happens to evaluate it. In
    # that case we should NOT short-circuit to SPURIOUS; let the
    # downstream classifier promote to LATENT/REAL. CCC's elf/io.rs
    # byte-reader family is the canonical case: spec evaluates
    # ``off + 4 <= data.len()`` and overflows on usize::MAX, but the
    # function body itself has ``data[offset+1]`` etc. that overflows
    # the same way. Losing those to SPURIOUS hides 9+ known-real bugs.
    fp = (getattr(counterexample, "failing_property", "") or "")
    trace_text = " ".join(getattr(counterexample, "trace", None) or [])
    arith_markers = (
        "attempt to add with overflow",
        "attempt to subtract with overflow",
        "attempt to multiply with overflow",
        "attempt to shift left with overflow",
        "attempt to shift right with overflow",
        "attempt to divide by zero",
    )
    index_markers = (
        "index out of bounds",
        "slice_index_fail",
    )
    if fp.startswith("check_") and any(m in trace_text for m in arith_markers + index_markers):
        # Decide whether the FUNCTION BODY has the same overflow shape.
        # If so, the spec is just amplifying a real exposure -- don't
        # mark SPURIOUS. If the body is clean, the panic is genuinely
        # spec-only.
        body = (getattr(func, "body", "") or "") if func is not None else ""
        if body and _body_has_same_panic_shape(body, trace_text):
            return None  # let the downstream classifier handle it
        return "spec-evaluation panic (functional spec performs unguarded arithmetic/indexing on Kani's nondet inputs)"

    # Kani modelling artifact: ``unsupported_construct.N`` properties
    # fire when Kani sees a call to a foreign function (libc, syscall,
    # FFI) that it can't symbolically execute. These are limitations of
    # the verifier, not bugs in the function — the function would behave
    # correctly at runtime; Kani just can't prove it. Common triggers in
    # CCC: getpid, getenv, gettimeofday, file I/O on tempfiles.
    if "unsupported_construct" in fp or "unsupported_construct" in trace_text:
        return "kani modelling artifact (call to foreign / unsupported construct — outside BMC's reach)"

    try:
        from bmc_agent.realism_checker import (
            _witness_indicates_uninitialized_library,
            _witness_indicates_path_divergent_unwind,
            _witness_indicates_jv_stub_disconnect,
        )
    except Exception:
        return None
    cause = _witness_indicates_uninitialized_library(counterexample)
    if cause:
        return cause
    cause = _witness_indicates_path_divergent_unwind(counterexample)
    if cause:
        return cause
    cause = _witness_indicates_jv_stub_disconnect(counterexample)
    if cause:
        return cause
    return None


def _reproducer_uses_public_api(
    code: str,
    public_headers: "Optional[list[str]]" = None,
) -> bool:
    """Defensive: does the reproducer actually link against the real library?

    Returns True when the source contains at least one ``#include <X>``
    where X is in the project's public header set. Returns False when the
    LLM fabricated a standalone reproducer with inline struct + function
    copies — a pattern observed in the v2.2 sweep where the LLM
    re-implemented ``entry_list_add`` as its own buggy stub and "crashed"
    on the stub, producing a misleading ``confirmed_dynamic`` verdict on
    synthetic code that proved nothing about the real library.

    Project-agnostic via ``public_headers``: pass the list of public-API
    header basenames (e.g. ``["archive.h", "archive_entry.h"]`` for
    libarchive, ``["curl/curl.h"]`` for libcurl, etc.). When None, falls
    back to a built-in set covering libraries bmc-agent has been
    calibrated on. Auto-derivation from ``config.include_dirs`` happens
    upstream in the caller (cex_validator's ``_dynamic_validate_bug``).

    Heuristic: presence of any project header include + nothing more.
    Strict on the include; permissive on everything else (the include
    is the load-bearing signal — if it's there, the link step will use
    real symbols regardless of any duplicate inline definitions).
    """
    if public_headers is None:
        # Fallback set for projects bmc-agent has been calibrated on.
        # The proper path is to pass an autodiscovered set from the
        # caller; this fallback exists so the function still does
        # SOMETHING useful when called bare.
        public_headers = [
            "archive.h", "archive_entry.h",          # libarchive
            "curl/curl.h",                            # libcurl
            "libxml/parser.h", "libxml/xmlreader.h",  # libxml2
            "openssl/ssl.h",                          # openssl
            "zlib.h",                                 # zlib
            "bzlib.h",                                # bzip2
        ]
    for hdr in public_headers:
        # Allow both <hdr> and "hdr" forms; the LLM emits angle-bracketed
        # forms in practice but quote-form is valid C.
        if f"#include <{hdr}>" in code or f'#include "{hdr}"' in code:
            return True
    return False


def _autodiscover_public_headers(include_dirs: "list[str]") -> "list[str]":
    """Walk include_dirs for top-level *.h files, returning basenames.
    Mirrors BoundaryDetector.autodiscover's heuristic: top-level *.h,
    excluding *_private.h / *_internal.h. Used by cex_validator to
    derive the project's public-header allowlist for reproducer
    validation. Returns an empty list when no headers found, in which
    case _reproducer_uses_public_api falls back to its built-in set.
    """
    from pathlib import Path
    names: list[str] = []
    seen: set[str] = set()
    for d in include_dirs or []:
        try:
            for h in sorted(Path(d).glob("*.h")):
                name = h.name
                if name.endswith("_private.h") or name.endswith("_internal.h"):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                names.append(name)
        except OSError:
            continue
    return names


def _parse_json_response(text: str) -> Optional[dict]:
    """Parse a JSON response from the LLM, stripping markdown fences if present."""
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        inner: list[str] = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not lines[0].startswith("```"):
                inner.append(line)
        text = "\n".join(inner).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to extract a JSON object from the text
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    return None
