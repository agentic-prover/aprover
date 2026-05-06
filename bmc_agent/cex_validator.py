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
    OVER_REFINEMENT_CHECK_PROMPT,
    REACHABILITY_PROMPT,
    REFINEMENT_PROMPT,
    REPRODUCER_PROMPT,
)
from bmc_agent.spec import Spec

logger = get_logger("cex_validator")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CExOutcome(Enum):
    REAL_BUG   = "real_bug"
    SPURIOUS   = "spurious"
    UNRESOLVED = "unresolved"


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

    # ------------------------------------------------------------------
    # Backward-compat property
    # ------------------------------------------------------------------

    @property
    def is_real_bug(self) -> bool:
        return self.outcome == CExOutcome.REAL_BUG

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
            DynamicValidator(config, harness_gen)
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
        logger.info("Validating counterexample for '%s'", func_name)

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
                unsigned_overflow_check=getattr(self.config, "cbmc_unsigned_overflow_check", False),
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
            response = self.llm.complete(system_prompt, user_prompt)
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
                    "Over-refinement detected at iteration %d — rejecting refinement "
                    "and marking as real bug",
                    iteration,
                )
                over_result = ValidationResult(
                    function_name=func_name,
                    counterexample=counterexample,
                    caller_path=[],
                    system_entry_input=None,
                    refinement_history=refinement_history,
                    final_precondition=None,
                    reasoning=(
                        f"Refinement was over-restrictive at iteration {iteration + 1} "
                        "— would exclude states that callers can actually produce. "
                        "Treating as real bug to be safe."
                    ),
                    outcome=CExOutcome.REAL_BUG,
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
        """Use LLM to generate a tightened precondition."""
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

        try:
            response = self.llm.complete(system_prompt, user_prompt)
            data = _parse_json_response(response)
            if data is not None:
                refined = data.get("refined_precondition", "").strip()
                if refined:
                    logger.debug("LLM refined precondition: %s", refined[:80])
                    return refined
        except LLMError as exc:
            logger.warning("LLM refinement failed: %s", exc)

        # Fallback: return original unchanged
        return original_spec.precondition

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
            response = self.llm.complete(system_prompt, user_prompt)
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

    def _generate_system_entry_reproducer(
        self,
        call_chain: list[str],
        counterexample: Counterexample,
        all_funcs: dict[str, FunctionInfo],
        parsed_file: ParsedCFile,
    ) -> str:
        """Generate a C test case that triggers the bug from the system entry point."""
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
        user_prompt = REPRODUCER_PROMPT.format(
            buggy_function=call_chain[-1] if call_chain else "unknown",
            call_chain=" → ".join(call_chain),
            counterexample_state=state_str,
            function_signatures=sigs_str,
        )

        try:
            response = self.llm.complete(system_prompt, user_prompt)
            data = _parse_json_response(response)
            if data is not None:
                code = data.get("reproducer_code", "").strip()
                if code:
                    return code
        except LLMError as exc:
            logger.warning("LLM reproducer generation failed: %s", exc)

        # Fallback: minimal stub reproducer
        chain_str = " → ".join(call_chain)
        return (
            f"/* Minimal reproducer for bug in '{call_chain[-1] if call_chain else 'unknown'}'\n"
            f"   Call chain: {chain_str}\n"
            f"   Counterexample state: {state_str}\n"
            f"   TODO: fill in concrete values from counterexample */\n"
            f"void trigger_bug(void) {{\n"
            f"    /* Initialize variables using counterexample values:\n"
            f"       {state_str} */\n"
            f"}}\n"
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
        )
        validation_result.dynamic_result = dynamic_result

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
