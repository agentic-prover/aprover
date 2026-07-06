"""
Phase 5: Spec-quality detection mechanisms (BMC-Agent V2).

Provides layered defenses to surface likely-wrong LLM-generated specs:
1. SpecCoverageChecker  — heuristic checks on spec structure
2. MutationTester       — systematic code mutations, check if spec catches them
3. SpecConsistencyChecker — caller-callee spec consistency via LLM
4. ExecutableSanityChecker — LLM-simulated test inputs vs. spec
5. SpecQualityAnalyzer  — aggregate all checks into a SpecQualityReport
"""
from __future__ import annotations

import re
import logging
import tempfile
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bmc_agent.parser import FunctionInfo, ParsedCFile
    from bmc_agent.spec import Spec
    from bmc_agent.backends.bmc_backend import BMCBackend
    from bmc_agent.llm import LLMClient
    from bmc_agent.config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Spec Coverage Checker
# ---------------------------------------------------------------------------

@dataclass
class CoverageResult:
    function_name: str
    references_return_value: bool   # postcondition mentions return/result
    references_mutated_fields: bool # postcondition mentions fields written in body
    references_parameters: bool     # postcondition mentions at least one parameter
    score: float                    # 0.0–1.0

    def to_dict(self) -> dict:
        return {
            "function_name": self.function_name,
            "references_return_value": self.references_return_value,
            "references_mutated_fields": self.references_mutated_fields,
            "references_parameters": self.references_parameters,
            "score": self.score,
        }


class SpecCoverageChecker:
    """
    Heuristic checks on spec structure.
    Flags specs that are suspiciously weak or incomplete.
    """

    def check(self, func: "FunctionInfo", spec: "Spec") -> CoverageResult:
        post = spec.postcondition.lower()
        body = func.body

        # Check 1: does postcondition reference the return value?
        refs_return = any(
            kw in post
            for kw in ("\\result", "result", "return", "returns")
        )

        # Check 2: does postcondition reference fields mutated in the body?
        # Find left-hand-side assignments in body: `->field =` or `.field =`
        mutated = set(re.findall(r'->(\w+)\s*=|\.(\w+)\s*=', body))
        mutated_names = {g for pair in mutated for g in pair if g}
        refs_mutated = bool(mutated_names) and any(
            name in post for name in mutated_names
        )
        # If no mutations, treat as satisfied (pure function)
        if not mutated_names:
            refs_mutated = True

        # Check 3: does postcondition mention at least one parameter?
        param_names = [pname for _, pname in func.signature.parameters if pname]
        refs_params = any(pname in post for pname in param_names) if param_names else True

        # Score: average of checks
        checks = [refs_return, refs_mutated, refs_params]
        score = sum(1 for c in checks if c) / len(checks) if checks else 0.0

        if score < 1.0:
            logger.warning(
                "SpecCoverage: '%s' score=%.2f "
                "(return=%s, mutated_fields=%s, params=%s)",
                func.name, score, refs_return, refs_mutated, refs_params,
            )

        return CoverageResult(
            function_name=func.name,
            references_return_value=refs_return,
            references_mutated_fields=refs_mutated,
            references_parameters=refs_params,
            score=score,
        )


# ---------------------------------------------------------------------------
# 2. Mutation Tester
# ---------------------------------------------------------------------------

@dataclass
class MutationResult:
    function_name: str
    mutations_tried: int
    mutations_caught: int
    mutation_score: float   # caught / tried; 1.0 = all mutations detected

    def to_dict(self) -> dict:
        return {
            "function_name": self.function_name,
            "mutations_tried": self.mutations_tried,
            "mutations_caught": self.mutations_caught,
            "mutation_score": self.mutation_score,
        }


def _apply_mutations(body: str) -> list[tuple[str, str]]:
    """
    Return a list of (mutation_name, mutated_body) pairs.
    Applies simple string transformations. Skips if pattern not found.
    """
    mutations = []

    # off-by-one: `x < N` → `x <= N`  (handles spaced and unspaced operators)
    m = re.sub(r'(\w)\s*<\s*(?!=)(\w+)', lambda x: f'{x.group(1)} <= {x.group(2)}', body, count=1)
    if m != body:
        mutations.append(("off_by_one_lt", m))

    # off-by-one: `x <= N` → `x < N`
    m2 = re.sub(r'(\w)\s*<=\s*(\w+)', lambda x: f'{x.group(1)} < {x.group(2)}', body, count=1)
    if m2 != body:
        mutations.append(("off_by_one_lte", m2))

    # flip comparison: first `==` in a return or condition → `!=`
    m3 = re.sub(r'==', '!=', body, count=1)
    if m3 != body:
        mutations.append(("flip_eq", m3))

    # drop first null check: remove `if (ptr == NULL) return ...;`
    m4 = re.sub(
        r'if\s*\([^)]*==\s*NULL\)[^;]*;', '', body, count=1, flags=re.DOTALL
    )
    if m4 != body:
        mutations.append(("drop_null_check", m4))

    return mutations


class MutationTester:
    """
    Inject systematic mutations into the function body, re-run BMC, check
    if the spec detects the mutant as a counterexample.
    """

    def __init__(self, backend: "BMCBackend", config: "Config") -> None:
        self._backend = backend
        self._config = config

    def test(
        self,
        func: "FunctionInfo",
        spec: "Spec",
        parsed_file: "ParsedCFile",
        driver_name: str,
    ) -> MutationResult:
        from bmc_agent.parser import FunctionInfo

        mutations = _apply_mutations(func.body)
        caught = 0

        for mut_name, mut_body in mutations:
            # Create a mutated FunctionInfo with the altered body
            mutated_func = FunctionInfo(
                name=func.name,
                signature=func.signature,
                body=mut_body,
                callees=func.callees,
                source_file=func.source_file,
            )
            try:
                harness_src = self._backend.generate_harness(
                    mutated_func, spec, {}, parsed_file
                )
                with tempfile.NamedTemporaryFile(
                    suffix=".c", delete=False, mode="w", encoding="utf-8"
                ) as f:
                    f.write(harness_src)
                    tmp = f.name
                try:
                    result = self._backend.check(tmp)
                    if not result.verified:
                        # Counterexample found → mutation was caught
                        caught += 1
                        logger.debug(
                            "Mutation '%s' in '%s' caught by spec",
                            mut_name, func.name,
                        )
                    else:
                        logger.warning(
                            "Mutation '%s' in '%s' NOT caught — spec may be too weak",
                            mut_name, func.name,
                        )
                finally:
                    os.unlink(tmp)
            except Exception as exc:
                logger.debug("Mutation '%s' in '%s' errored: %s", mut_name, func.name, exc)
                # Harness error counts as caught (mutation made code uncompilable)
                caught += 1

        tried = len(mutations)
        score = (caught / tried) if tried > 0 else 1.0

        return MutationResult(
            function_name=func.name,
            mutations_tried=tried,
            mutations_caught=caught,
            mutation_score=score,
        )


# ---------------------------------------------------------------------------
# 3. Spec Consistency Checker
# ---------------------------------------------------------------------------

@dataclass
class ConsistencyResult:
    caller_name: str
    callee_name: str
    consistent: bool
    reasoning: str

    def to_dict(self) -> dict:
        return {
            "caller_name": self.caller_name,
            "callee_name": self.callee_name,
            "consistent": self.consistent,
            "reasoning": self.reasoning,
        }


class SpecConsistencyChecker:
    """
    Checks whether a callee's spec is consistent with how its caller uses it.
    Uses LLM judgment.
    """

    def __init__(self, llm: "LLMClient") -> None:
        self._llm = llm

    def check(
        self,
        caller: "FunctionInfo",
        caller_spec: "Spec",
        callee_name: str,
        callee_spec: "Spec",
    ) -> ConsistencyResult:
        import json
        from bmc_agent.prompts import SPEC_CONSISTENCY_PROMPT

        system_prompt = "You are a formal verification expert for C programs."
        user_prompt = SPEC_CONSISTENCY_PROMPT.format(
            caller_name=caller.name,
            caller_pre=caller_spec.precondition,
            caller_post=caller_spec.postcondition,
            call_site=caller.body,
            callee_name=callee_name,
            callee_pre=callee_spec.precondition,
            callee_post=callee_spec.postcondition,
        )

        try:
            from bmc_agent.llm import agentic_system_prompt
            from bmc_agent.json_utils import extract_json_object
            response = self._llm.complete(
                agentic_system_prompt(self._llm.config, "spec_gen", system_prompt),
                user_prompt, role="spec_gen",
                validate=lambda t: extract_json_object(t) is not None,
            )
            parsed = extract_json_object(response) or {}
            consistent = bool(parsed.get("consistent", True))
            reasoning = parsed.get("reasoning", "")
        except Exception as exc:
            logger.warning("SpecConsistency check failed for %s→%s: %s", caller.name, callee_name, exc)
            consistent = True
            reasoning = f"Check failed: {exc}"

        if not consistent:
            logger.warning(
                "SpecConsistency mismatch: %s→%s — %s",
                caller.name, callee_name, reasoning,
            )

        return ConsistencyResult(
            caller_name=caller.name,
            callee_name=callee_name,
            consistent=consistent,
            reasoning=reasoning,
        )


# ---------------------------------------------------------------------------
# 4. Executable Sanity Checker (LLM-simulated)
# ---------------------------------------------------------------------------

@dataclass
class SanityResult:
    function_name: str
    inputs_tested: int
    violations_found: int   # LLM says spec disagrees with behavior
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "function_name": self.function_name,
            "inputs_tested": self.inputs_tested,
            "violations_found": self.violations_found,
            "notes": self.notes,
        }


_SANITY_PROMPT = """\
You are testing a C function against its specification.

Function:
```c
{signature}
{body}
```

Specification:
  Precondition:  {precondition}
  Postcondition: {postcondition}

Generate 3 representative concrete test inputs that satisfy the precondition.
For each input, simulate execution mentally and determine if the postcondition holds.

Respond with ONLY valid JSON:
{{
  "tests": [
    {{"input": "<description of input>", "satisfies_postcondition": true/false, "explanation": "..."}}
  ],
  "violations_found": <count of false entries>
}}
"""


class ExecutableSanityChecker:
    """
    Uses LLM to generate concrete test inputs and simulate execution,
    checking whether results satisfy the spec.
    """

    def __init__(self, llm: "LLMClient") -> None:
        self._llm = llm

    def check(self, func: "FunctionInfo", spec: "Spec") -> SanityResult:
        import json

        sig_str = f"{func.signature.return_type} {func.name}({', '.join(f'{t} {n}' for t, n in func.signature.parameters)})"

        prompt = _SANITY_PROMPT.format(
            signature=sig_str,
            body=func.body,
            precondition=spec.precondition,
            postcondition=spec.postcondition,
        )

        try:
            from bmc_agent.llm import agentic_system_prompt
            from bmc_agent.json_utils import extract_json_object
            response = self._llm.complete(
                agentic_system_prompt(
                    self._llm.config, "spec_gen",
                    "You are a formal verification expert for C programs.",
                ),
                prompt, role="spec_gen",
                validate=lambda t: extract_json_object(t) is not None,
            )
            parsed = extract_json_object(response) or {}
            tests = parsed.get("tests", [])
            violations = int(parsed.get("violations_found", 0))
            notes = f"{len(tests)} test(s) generated"
            if violations > 0:
                logger.warning(
                    "ExecutableSanity: %d violation(s) found for '%s' — spec may be wrong or too strong",
                    violations, func.name,
                )
            return SanityResult(
                function_name=func.name,
                inputs_tested=len(tests),
                violations_found=violations,
                notes=notes,
            )
        except Exception as exc:
            logger.debug("ExecutableSanity check failed for '%s': %s", func.name, exc)
            return SanityResult(
                function_name=func.name,
                inputs_tested=0,
                violations_found=0,
                notes=f"Check failed: {exc}",
            )


# ---------------------------------------------------------------------------
# 5. Aggregate: SpecQualityReport + SpecQualityAnalyzer
# ---------------------------------------------------------------------------

@dataclass
class SpecQualityReport:
    function_name: str
    coverage: CoverageResult
    mutation: MutationResult
    consistency: list[ConsistencyResult] = field(default_factory=list)
    sanity: SanityResult = field(default_factory=lambda: SanityResult("", 0, 0))
    overall_score: float = 0.0   # weighted average of component scores

    def to_dict(self) -> dict:
        return {
            "function_name": self.function_name,
            "overall_score": self.overall_score,
            "coverage": self.coverage.to_dict(),
            "mutation": self.mutation.to_dict(),
            "consistency": [c.to_dict() for c in self.consistency],
            "sanity": self.sanity.to_dict(),
        }


class SpecQualityAnalyzer:
    """
    Runs all spec-quality checks and aggregates into a SpecQualityReport.
    Controlled by config.enable_spec_quality.
    """

    def __init__(
        self,
        backend: "BMCBackend",
        llm: "LLMClient",
        config: "Config",
    ) -> None:
        self._coverage = SpecCoverageChecker()
        self._mutation = MutationTester(backend, config)
        self._consistency = SpecConsistencyChecker(llm)
        self._sanity = ExecutableSanityChecker(llm)

    def analyze(
        self,
        func: "FunctionInfo",
        spec: "Spec",
        all_funcs: "dict[str, FunctionInfo]",
        all_specs: "dict[str, Spec]",
        parsed_file: "ParsedCFile",
        driver_name: str,
    ) -> SpecQualityReport:
        logger.info("SpecQuality: analyzing '%s'", func.name)

        coverage = self._coverage.check(func, spec)
        mutation = self._mutation.test(func, spec, parsed_file, driver_name)
        sanity = self._sanity.check(func, spec)

        # Consistency check against all callees
        consistency_results = []
        for callee_name in func.callees:
            if callee_name in all_specs and callee_name in all_funcs:
                result = self._consistency.check(
                    func, spec, callee_name, all_specs[callee_name]
                )
                consistency_results.append(result)

        # Compute overall score
        consistency_score = (
            sum(1 for c in consistency_results if c.consistent) / len(consistency_results)
            if consistency_results else 1.0
        )
        sanity_score = 1.0 if sanity.violations_found == 0 else 0.5

        overall = (
            coverage.score * 0.3
            + mutation.mutation_score * 0.4
            + consistency_score * 0.2
            + sanity_score * 0.1
        )

        return SpecQualityReport(
            function_name=func.name,
            coverage=coverage,
            mutation=mutation,
            consistency=consistency_results,
            sanity=sanity,
            overall_score=overall,
        )
