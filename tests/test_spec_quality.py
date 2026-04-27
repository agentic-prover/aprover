"""
Tests for amc/spec_quality.py.

All tests run without CBMC or ANTHROPIC_API_KEY — BMC backend and LLM are mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sig(name: str, ret: str = "int", params: list | None = None):
    from amc.parser import FunctionSignature

    return FunctionSignature(
        name=name,
        return_type=ret,
        parameters=params if params is not None else [("int", "x")],
    )


def _make_func(
    name: str,
    body: str = "{ return x + 1; }",
    callees: set | None = None,
    params: list | None = None,
    ret: str = "int",
) -> "FunctionInfo":
    from amc.parser import FunctionInfo

    sig = _make_sig(name, ret=ret, params=params)
    return FunctionInfo(
        name=name,
        signature=sig,
        body=body,
        callees=callees or set(),
        source_file="/fake/src.c",
    )


def _make_spec(
    name: str,
    pre: str = "true",
    post: str = "\\result >= 0",
) -> "Spec":
    from amc.spec import Spec, SpecStatus

    return Spec(
        function_name=name,
        precondition=pre,
        postcondition=post,
        status=SpecStatus.GENERATED,
    )


def _make_parsed_file(funcs: dict | None = None) -> "ParsedCFile":
    from amc.parser import ParsedCFile

    sigs = funcs or {}
    return ParsedCFile(
        path="/fake/src.c",
        functions={k: _make_sig(k) for k in sigs},
        call_graph={},
        function_bodies={k: sigs[k] for k in sigs},
    )


# ---------------------------------------------------------------------------
# SpecCoverageChecker
# ---------------------------------------------------------------------------


class TestSpecCoverageChecker:
    def test_full_coverage(self):
        from amc.spec_quality import SpecCoverageChecker

        checker = SpecCoverageChecker()
        func = _make_func("add", body="{ return x + y; }", params=[("int", "x"), ("int", "y")])
        spec = _make_spec("add", post="\\result == x + y")
        result = checker.check(func, spec)

        assert result.function_name == "add"
        assert result.references_return_value is True
        assert result.references_parameters is True
        assert result.score == 1.0

    def test_missing_return_value_reference(self):
        from amc.spec_quality import SpecCoverageChecker

        checker = SpecCoverageChecker()
        func = _make_func("get_val", body="{ return x; }")
        spec = _make_spec("get_val", post="true")  # no mention of result
        result = checker.check(func, spec)

        assert result.references_return_value is False
        assert result.score < 1.0

    def test_pure_function_no_mutations_refs_mutated_satisfied(self):
        from amc.spec_quality import SpecCoverageChecker

        checker = SpecCoverageChecker()
        func = _make_func("pure", body="{ return x * 2; }", params=[("int", "x")])
        spec = _make_spec("pure", post="\\result == x * 2")
        result = checker.check(func, spec)

        # Pure function (no assignments in body) → refs_mutated = True automatically
        assert result.references_mutated_fields is True

    def test_struct_mutation_referenced_in_post(self):
        from amc.spec_quality import SpecCoverageChecker

        checker = SpecCoverageChecker()
        func = _make_func(
            "set_count",
            body="{ rb->count = x; }",
            params=[("ring_buffer_t *", "rb"), ("int", "x")],
            ret="void",
        )
        spec = _make_spec("set_count", post="rb->count == x")
        result = checker.check(func, spec)

        assert result.references_mutated_fields is True

    def test_struct_mutation_not_referenced_in_post(self):
        from amc.spec_quality import SpecCoverageChecker

        checker = SpecCoverageChecker()
        func = _make_func(
            "set_count",
            body="{ rb->count = x; }",
            params=[("ring_buffer_t *", "rb"), ("int", "x")],
            ret="void",
        )
        spec = _make_spec("set_count", post="true")
        result = checker.check(func, spec)

        assert result.references_mutated_fields is False
        assert result.score < 1.0

    def test_no_parameters_refs_params_satisfied(self):
        from amc.spec_quality import SpecCoverageChecker

        checker = SpecCoverageChecker()
        func = _make_func("init", body="{ return 42; }", params=[])
        spec = _make_spec("init", post="\\result == 42")
        result = checker.check(func, spec)

        # No params → refs_params = True automatically
        assert result.references_parameters is True


# ---------------------------------------------------------------------------
# _apply_mutations
# ---------------------------------------------------------------------------


class TestApplyMutations:
    def test_off_by_one_lt_spaced(self):
        from amc.spec_quality import _apply_mutations

        body = "{ if (x < limit) return 1; return 0; }"
        mutations = dict(_apply_mutations(body))
        assert "off_by_one_lt" in mutations
        assert "<= limit" in mutations["off_by_one_lt"]

    def test_off_by_one_lt_nospace(self):
        from amc.spec_quality import _apply_mutations

        body = "{ if (x<limit) return 1; return 0; }"
        mutations = dict(_apply_mutations(body))
        assert "off_by_one_lt" in mutations
        assert "<= limit" in mutations["off_by_one_lt"]

    def test_off_by_one_lte(self):
        from amc.spec_quality import _apply_mutations

        body = "{ if (x <= limit) return 1; return 0; }"
        mutations = dict(_apply_mutations(body))
        assert "off_by_one_lte" in mutations
        assert "< limit" in mutations["off_by_one_lte"]

    def test_flip_eq(self):
        from amc.spec_quality import _apply_mutations

        body = "{ if (x == 0) return -1; return x; }"
        mutations = dict(_apply_mutations(body))
        assert "flip_eq" in mutations
        assert "!= 0" in mutations["flip_eq"]

    def test_drop_null_check(self):
        from amc.spec_quality import _apply_mutations

        body = "{ if (ptr == NULL) return -1; return ptr->val; }"
        mutations = dict(_apply_mutations(body))
        assert "drop_null_check" in mutations
        assert "== NULL" not in mutations["drop_null_check"]

    def test_no_mutations_applicable(self):
        from amc.spec_quality import _apply_mutations

        body = "{ return 42; }"
        mutations = _apply_mutations(body)
        assert mutations == []

    def test_returns_list_of_tuples(self):
        from amc.spec_quality import _apply_mutations

        body = "{ if (x < 10) return 1; return 0; }"
        mutations = _apply_mutations(body)
        assert isinstance(mutations, list)
        for name, mutated in mutations:
            assert isinstance(name, str)
            assert isinstance(mutated, str)


# ---------------------------------------------------------------------------
# MutationTester
# ---------------------------------------------------------------------------


class TestMutationTester:
    def _make_backend_catching(self):
        """Backend that always reports a counterexample (mutation caught)."""
        backend = MagicMock()
        backend.generate_harness.return_value = "int main(){return 0;}"
        verdict = MagicMock()
        verdict.verified = False  # counterexample found → mutation caught
        backend.check.return_value = verdict
        return backend

    def _make_backend_not_catching(self):
        """Backend that always verifies (mutation slips through)."""
        backend = MagicMock()
        backend.generate_harness.return_value = "int main(){return 0;}"
        verdict = MagicMock()
        verdict.verified = True  # no counterexample → mutation not caught
        backend.check.return_value = verdict
        return backend

    def _make_config(self):
        from amc.config import Config

        return Config(artifact_dir="/tmp/test_artifacts", cbmc_path="cbmc", llm_api_key="fake")

    def test_all_mutations_caught(self):
        from amc.spec_quality import MutationTester

        backend = self._make_backend_catching()
        tester = MutationTester(backend, self._make_config())

        # Body uses `x < limit` (spaced) — triggers off_by_one_lt after the regex fix
        func = _make_func(
            "check_bound",
            body="{ if (x < limit) return 1; return 0; }",
            params=[("int", "x"), ("int", "limit")],
        )
        spec = _make_spec("check_bound", post="\\result == 1 || \\result == 0")
        parsed = _make_parsed_file({"check_bound": "{ if (x < limit) return 1; return 0; }"})

        result = tester.test(func, spec, parsed, "test_driver")

        assert result.mutations_tried > 0
        assert result.mutations_caught == result.mutations_tried
        assert result.mutation_score == 1.0

    def test_no_mutations_caught(self):
        from amc.spec_quality import MutationTester

        backend = self._make_backend_not_catching()
        tester = MutationTester(backend, self._make_config())

        func = _make_func(
            "check_bound",
            body="{ if (x < limit) return 1; return 0; }",
            params=[("int", "x"), ("int", "limit")],
        )
        spec = _make_spec("check_bound", post="true")  # trivially weak spec
        parsed = _make_parsed_file({"check_bound": "{ if (x < limit) return 1; return 0; }"})

        result = tester.test(func, spec, parsed, "test_driver")

        assert result.mutations_tried > 0
        assert result.mutations_caught == 0
        assert result.mutation_score == 0.0

    def test_no_applicable_mutations(self):
        from amc.spec_quality import MutationTester

        backend = self._make_backend_catching()
        tester = MutationTester(backend, self._make_config())

        func = _make_func("trivial", body="{ return 42; }")
        spec = _make_spec("trivial", post="\\result == 42")
        parsed = _make_parsed_file({"trivial": "{ return 42; }"})

        result = tester.test(func, spec, parsed, "test_driver")

        assert result.mutations_tried == 0
        assert result.mutation_score == 1.0  # no mutations → score defaults to 1.0

    def test_harness_exception_counts_as_caught(self):
        from amc.spec_quality import MutationTester

        backend = MagicMock()
        backend.generate_harness.side_effect = RuntimeError("compile failure")

        tester = MutationTester(backend, self._make_config())
        func = _make_func("check_bound", body="{ if (x < limit) return 1; return 0; }")
        spec = _make_spec("check_bound")
        parsed = _make_parsed_file({"check_bound": "{ if (x < limit) return 1; return 0; }"})

        result = tester.test(func, spec, parsed, "test_driver")

        # Harness error → mutation counted as caught
        assert result.mutations_caught == result.mutations_tried


# ---------------------------------------------------------------------------
# SpecConsistencyChecker
# ---------------------------------------------------------------------------


class TestSpecConsistencyChecker:
    def _make_llm(self, response: str) -> MagicMock:
        llm = MagicMock()
        llm.complete.return_value = response
        return llm

    def test_consistent_returns_true(self):
        from amc.spec_quality import SpecConsistencyChecker

        llm = self._make_llm('{"consistent": true, "reasoning": "looks fine"}')
        checker = SpecConsistencyChecker(llm)

        caller = _make_func("caller", body="{ return callee(x); }", callees={"callee"})
        caller_spec = _make_spec("caller")
        callee_spec = _make_spec("callee", post="\\result >= 0")

        result = checker.check(caller, caller_spec, "callee", callee_spec)

        assert result.consistent is True
        assert result.reasoning == "looks fine"
        assert result.caller_name == "caller"
        assert result.callee_name == "callee"

    def test_inconsistent_returns_false(self):
        from amc.spec_quality import SpecConsistencyChecker

        llm = self._make_llm('{"consistent": false, "reasoning": "callee post contradicts usage"}')
        checker = SpecConsistencyChecker(llm)

        caller = _make_func("caller", body="{ return callee(x); }", callees={"callee"})
        caller_spec = _make_spec("caller")
        callee_spec = _make_spec("callee", post="\\result < 0")

        result = checker.check(caller, caller_spec, "callee", callee_spec)

        assert result.consistent is False
        assert "contradicts" in result.reasoning

    def test_llm_failure_defaults_to_consistent(self):
        from amc.spec_quality import SpecConsistencyChecker

        llm = MagicMock()
        llm.complete.side_effect = Exception("network error")
        checker = SpecConsistencyChecker(llm)

        caller = _make_func("caller", body="{ return callee(x); }", callees={"callee"})
        caller_spec = _make_spec("caller")
        callee_spec = _make_spec("callee")

        result = checker.check(caller, caller_spec, "callee", callee_spec)

        # On failure, defaults to consistent=True (conservative)
        assert result.consistent is True

    def test_malformed_json_defaults_to_consistent(self):
        from amc.spec_quality import SpecConsistencyChecker

        llm = self._make_llm("this is not json")
        checker = SpecConsistencyChecker(llm)

        caller = _make_func("caller", body="{ return callee(x); }", callees={"callee"})
        caller_spec = _make_spec("caller")
        callee_spec = _make_spec("callee")

        result = checker.check(caller, caller_spec, "callee", callee_spec)

        assert result.consistent is True

    def test_to_dict(self):
        from amc.spec_quality import ConsistencyResult

        cr = ConsistencyResult(
            caller_name="caller",
            callee_name="callee",
            consistent=True,
            reasoning="ok",
        )
        d = cr.to_dict()
        assert d["caller_name"] == "caller"
        assert d["consistent"] is True


# ---------------------------------------------------------------------------
# ExecutableSanityChecker
# ---------------------------------------------------------------------------


class TestExecutableSanityChecker:
    def _make_llm(self, response: str) -> MagicMock:
        llm = MagicMock()
        llm.complete.return_value = response
        return llm

    def test_no_violations_found(self):
        from amc.spec_quality import ExecutableSanityChecker

        response = '{"tests": [{"input": "x=0", "satisfies_postcondition": true, "explanation": "ok"}], "violations_found": 0}'
        llm = self._make_llm(response)
        checker = ExecutableSanityChecker(llm)

        func = _make_func("double_x", body="{ return x * 2; }")
        spec = _make_spec("double_x", post="\\result == x * 2")

        result = checker.check(func, spec)

        assert result.inputs_tested == 1
        assert result.violations_found == 0
        assert result.function_name == "double_x"

    def test_violation_found(self):
        from amc.spec_quality import ExecutableSanityChecker

        response = '{"tests": [{"input": "x=5", "satisfies_postcondition": false, "explanation": "wrong"}, {"input": "x=0", "satisfies_postcondition": true, "explanation": "ok"}], "violations_found": 1}'
        llm = self._make_llm(response)
        checker = ExecutableSanityChecker(llm)

        func = _make_func("bad_fn", body="{ return x + 1; }")
        spec = _make_spec("bad_fn", post="\\result == x * 2")  # wrong spec

        result = checker.check(func, spec)

        assert result.violations_found == 1

    def test_llm_failure_returns_zero_violations(self):
        from amc.spec_quality import ExecutableSanityChecker

        llm = MagicMock()
        llm.complete.side_effect = Exception("timeout")
        checker = ExecutableSanityChecker(llm)

        func = _make_func("fn")
        spec = _make_spec("fn")

        result = checker.check(func, spec)

        assert result.inputs_tested == 0
        assert result.violations_found == 0
        assert "failed" in result.notes.lower()

    def test_to_dict_has_expected_keys(self):
        from amc.spec_quality import SanityResult

        sr = SanityResult(
            function_name="fn",
            inputs_tested=3,
            violations_found=0,
            notes="3 test(s) generated",
        )
        d = sr.to_dict()
        assert set(d.keys()) == {"function_name", "inputs_tested", "violations_found", "notes"}


# ---------------------------------------------------------------------------
# SpecQualityReport + SpecQualityAnalyzer
# ---------------------------------------------------------------------------


class TestSpecQualityReport:
    def test_to_dict_structure(self):
        from amc.spec_quality import (
            ConsistencyResult,
            CoverageResult,
            MutationResult,
            SanityResult,
            SpecQualityReport,
        )

        report = SpecQualityReport(
            function_name="fn",
            coverage=CoverageResult(
                function_name="fn",
                references_return_value=True,
                references_mutated_fields=True,
                references_parameters=True,
                score=1.0,
            ),
            mutation=MutationResult(
                function_name="fn",
                mutations_tried=2,
                mutations_caught=2,
                mutation_score=1.0,
            ),
            consistency=[
                ConsistencyResult("fn", "callee", True, "ok")
            ],
            sanity=SanityResult("fn", 3, 0, "ok"),
            overall_score=0.9,
        )

        d = report.to_dict()
        assert d["function_name"] == "fn"
        assert d["overall_score"] == 0.9
        assert "coverage" in d
        assert "mutation" in d
        assert "consistency" in d
        assert "sanity" in d
        assert len(d["consistency"]) == 1


class TestSpecQualityAnalyzer:
    def _make_analyzer(self, backend_catching: bool = True):
        from amc.config import Config
        from amc.spec_quality import SpecQualityAnalyzer

        backend = MagicMock()
        backend.generate_harness.return_value = "int main(){return 0;}"
        verdict = MagicMock()
        verdict.verified = not backend_catching  # catching → verified=False
        backend.check.return_value = verdict

        llm = MagicMock()
        llm.complete.return_value = '{"consistent": true, "reasoning": "ok", "tests": [{"input": "x=1", "satisfies_postcondition": true, "explanation": "ok"}], "violations_found": 0}'

        config = Config(artifact_dir="/tmp", cbmc_path="cbmc", llm_api_key="fake")
        return SpecQualityAnalyzer(backend=backend, llm=llm, config=config)

    def test_analyze_returns_report(self):
        analyzer = self._make_analyzer(backend_catching=True)

        func = _make_func(
            "check_bound",
            body="{ if (x < limit) return 1; return 0; }",
            params=[("int", "x"), ("int", "limit")],
        )
        spec = _make_spec("check_bound", post="\\result == 1 || \\result == 0")
        parsed = _make_parsed_file({"check_bound": "{ if (x < limit) return 1; return 0; }"})

        report = analyzer.analyze(
            func=func,
            spec=spec,
            all_funcs={"check_bound": func},
            all_specs={"check_bound": spec},
            parsed_file=parsed,
            driver_name="test",
        )

        assert report.function_name == "check_bound"
        assert 0.0 <= report.overall_score <= 1.0
        assert report.mutation.mutations_tried >= 0
        assert report.coverage.score >= 0.0

    def test_analyze_with_callee_consistency_check(self):
        analyzer = self._make_analyzer(backend_catching=True)

        callee = _make_func("helper", body="{ return x * 2; }")
        caller = _make_func("caller", body="{ return helper(x); }", callees={"helper"})
        caller_spec = _make_spec("caller", post="\\result == x * 2")
        callee_spec = _make_spec("helper", post="\\result == x * 2")
        parsed = _make_parsed_file(
            {"caller": "{ return helper(x); }", "helper": "{ return x * 2; }"}
        )

        report = analyzer.analyze(
            func=caller,
            spec=caller_spec,
            all_funcs={"caller": caller, "helper": callee},
            all_specs={"caller": caller_spec, "helper": callee_spec},
            parsed_file=parsed,
            driver_name="test",
        )

        # Consistency check should run for helper callee
        assert len(report.consistency) == 1
        assert report.consistency[0].callee_name == "helper"

    def test_analyze_with_no_callees_skips_consistency(self):
        analyzer = self._make_analyzer()

        func = _make_func("leaf", body="{ return x + 1; }")
        spec = _make_spec("leaf", post="\\result == x + 1")
        parsed = _make_parsed_file({"leaf": "{ return x + 1; }"})

        report = analyzer.analyze(
            func=func,
            spec=spec,
            all_funcs={"leaf": func},
            all_specs={"leaf": spec},
            parsed_file=parsed,
            driver_name="test",
        )

        assert report.consistency == []

    def test_overall_score_weighted(self):
        """overall_score must be a weighted combination in [0, 1]."""
        analyzer = self._make_analyzer(backend_catching=True)

        func = _make_func("fn")
        spec = _make_spec("fn", post="\\result >= 0")
        parsed = _make_parsed_file({"fn": "{ return x + 1; }"})

        report = analyzer.analyze(
            func=func,
            spec=spec,
            all_funcs={"fn": func},
            all_specs={"fn": spec},
            parsed_file=parsed,
            driver_name="test",
        )

        assert 0.0 <= report.overall_score <= 1.0


# ---------------------------------------------------------------------------
# CoverageResult / MutationResult to_dict
# ---------------------------------------------------------------------------


def test_coverage_result_to_dict():
    from amc.spec_quality import CoverageResult

    cr = CoverageResult(
        function_name="fn",
        references_return_value=True,
        references_mutated_fields=False,
        references_parameters=True,
        score=0.67,
    )
    d = cr.to_dict()
    assert d["function_name"] == "fn"
    assert d["references_return_value"] is True
    assert d["references_mutated_fields"] is False
    assert abs(d["score"] - 0.67) < 0.01


def test_mutation_result_to_dict():
    from amc.spec_quality import MutationResult

    mr = MutationResult(
        function_name="fn",
        mutations_tried=4,
        mutations_caught=3,
        mutation_score=0.75,
    )
    d = mr.to_dict()
    assert d["mutations_tried"] == 4
    assert d["mutations_caught"] == 3
    assert d["mutation_score"] == 0.75
