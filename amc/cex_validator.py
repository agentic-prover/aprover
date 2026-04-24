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

from amc.artifacts import ArtifactStore
from amc.cbmc import Counterexample, run_cbmc
from amc.config import Config
from amc.harness_generator import HarnessGenerator
from amc.llm import LLMClient, LLMError
from amc.logger import get_logger
from amc.parser import FunctionInfo, ParsedCFile
from amc.prompts import (
    OVER_REFINEMENT_CHECK_PROMPT,
    REACHABILITY_PROMPT,
    REFINEMENT_PROMPT,
    REPRODUCER_PROMPT,
)
from amc.spec import Spec

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
        # If caller passed legacy is_real_bug, derive outcome from it
        if is_real_bug is not None:
            self.outcome = CExOutcome.REAL_BUG if is_real_bug else CExOutcome.SPURIOUS
        else:
            self.outcome = outcome

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
            "is_real_bug": self.is_real_bug,
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
    ) -> ValidationResult:
        """
        Validate a counterexample.

        Returns a ValidationResult indicating whether it is a real bug or
        spurious (with a refined spec).
        """
        func_name = func.name
        logger.info("Validating counterexample for '%s'", func_name)

        # Step 1: find all callers of func_name in all_funcs
        callers = self._find_callers(func_name, all_funcs)
        logger.debug("Callers of '%s': %s", func_name, list(callers.keys()))

        # Step 2 / Step 3: no callers → entry function → real bug directly
        if not callers:
            logger.info(
                "'%s' has no callers — counterexample is directly reachable (real bug)",
                func_name,
            )
            reproducer = self._generate_system_entry_reproducer(
                call_chain=[func_name],
                counterexample=counterexample,
                all_funcs=all_funcs,
                parsed_file=parsed_file,
            )
            return ValidationResult(
                function_name=func_name,
                counterexample=counterexample,
                caller_path=[func_name],
                system_entry_input=reproducer,
                refinement_history=[],
                final_precondition=None,
                reasoning=(
                    f"'{func_name}' is an entry function (no callers). "
                    "The counterexample is directly reachable from the system boundary."
                ),
                outcome=CExOutcome.REAL_BUG,
            )

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
            # Propagate upward through the first reachable caller
            is_system_reachable, call_chain = self._propagate_upward(
                func_name=reachable_from[0],
                counterexample=counterexample,
                all_funcs=all_funcs,
                all_specs=all_specs,
                parsed_file=parsed_file,
                driver_name=driver_name,
            )
            # Append the current function to the chain
            full_chain = call_chain + [func_name]

            reproducer = self._generate_system_entry_reproducer(
                call_chain=full_chain,
                counterexample=counterexample,
                all_funcs=all_funcs,
                parsed_file=parsed_file,
            )
            return ValidationResult(
                function_name=func_name,
                counterexample=counterexample,
                caller_path=full_chain,
                system_entry_input=reproducer,
                refinement_history=[],
                final_precondition=None,
                reasoning=(
                    f"Counterexample state is reachable from caller(s): "
                    f"{reachable_from}. Call chain: {full_chain}."
                ),
                outcome=CExOutcome.REAL_BUG,
            )

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
    ) -> tuple[bool, list[str]]:
        """
        Recursively propagate upward through callers.

        Returns (is_reachable_from_entry, call_chain).
        A function is an entry function if it has no callers in all_funcs.
        """
        if visited is None:
            visited = set()
        if func_name in visited:
            return False, []
        visited.add(func_name)

        callers = self._find_callers(func_name, all_funcs)

        if not callers:
            # This is an entry function
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
        refinement_history: list[str] = []
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

            refinement_history.append(new_precondition)

            # Check over-refinement
            is_safe = self._check_over_refinement(
                new_precondition=new_precondition,
                caller_expected_preconditions=caller_expected_preconditions,
            )

            if not is_safe:
                logger.warning(
                    "Over-refinement detected at iteration %d — rejecting refinement "
                    "and marking as real bug",
                    iteration,
                )
                return ValidationResult(
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
