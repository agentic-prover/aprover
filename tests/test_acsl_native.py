from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bmc_agent.acsl_native import (
    NativeAcslSpec,
    _tce_equivalent,
    build_native_acsl_source,
    clause_counts,
    downstream_proof_utility,
    generate_native_acsl_specs,
    load_mutations_json,
    parse_native_acsl_specs,
    vacuity_warnings,
)
from bmc_agent.parser import parse_c_file


def test_parse_native_acsl_specs_from_fenced_json() -> None:
    raw = r"""```json
{
  "function_name": "max2",
  "requires": ["\\true"],
  "assigns": ["\\nothing"],
  "ensures": ["\\result >= x", "\\result >= y"],
  "loop_invariants": [],
  "raw_acsl": ""
}
```"""

    specs = parse_native_acsl_specs(raw)

    assert list(specs) == ["max2"]
    assert specs["max2"].ensures == ["\\result >= x", "\\result >= y"]
    assert specs["max2"].assigns == ["\\nothing"]


def test_parse_native_acsl_specs_rejects_malformed_payload() -> None:
    with pytest.raises(ValueError, match="requires"):
        parse_native_acsl_specs(
            {
                "function_name": "f",
                "requires": {"not": "a list"},
                "ensures": ["\\true"],
            }
        )


def test_build_native_acsl_source_injects_contract_and_loop_invariant() -> None:
    source = (
        "int sum(int *a, int n) {\n"
        "  int s = 0;\n"
        "  for (int i = 0; i < n; i++) {\n"
        "    s += a[i];\n"
        "  }\n"
        "  return s;\n"
        "}\n"
    )
    parsed = parse_c_file(Path("sum.c"), source_text=source)
    spec = NativeAcslSpec(
        function_name="sum",
        requires=["a != \\null", "n >= 0"],
        assigns=["\\nothing"],
        ensures=["\\result >= 0"],
        loop_invariants=["0 <= i <= n"],
    )

    build = build_native_acsl_source(source, parsed, {"sum": spec})

    assert build.inserted_functions == ["sum"]
    assert build.inserted_loop_invariants == {"sum": 1}
    assert build.source_text.startswith("/*@")
    assert "requires a != \\null;" in build.source_text
    assert "assigns \\nothing;" in build.source_text
    assert "ensures \\result >= 0;" in build.source_text
    assert "/*@\n    loop invariant 0 <= i <= n;\n  */" in build.source_text
    assert build.source_text.index("loop invariant") < build.source_text.index("for (")


def test_clause_counts_and_vacuity_warnings() -> None:
    specs = {
        "weak": NativeAcslSpec(function_name="weak", ensures=["\\true"]),
        "bad": NativeAcslSpec(function_name="bad", requires=["\\false"], ensures=[]),
    }

    counts = clause_counts(specs)
    warnings = vacuity_warnings(specs)

    assert counts["total"]["ensures"] == 1
    assert {"function": "weak", "kind": "vacuous_ensures", "detail": "all ensures clauses are true"} in warnings
    assert {"function": "bad", "kind": "missing_ensures", "detail": "no postcondition"} in warnings
    assert {"function": "bad", "kind": "unsatisfiable_requires", "detail": "requires contains false"} in warnings


def test_downstream_proof_utility_reports_recovered_assertion_goals() -> None:
    class Result:
        status = "success"
        proved_goals = 3
        total_goals = 4

    utility = downstream_proof_utility(Result(), recovered_asserts=2)

    assert utility["status"] == "measured_from_recovered_assertions"
    assert utility["target_assertions"] == 2
    assert utility["target_goals_proved"] == 3
    assert utility["target_goals_total"] == 4
    assert utility["proof_ratio"] == 0.75


def test_load_mutations_json(tmp_path: Path) -> None:
    path = tmp_path / "mutations.json"
    path.write_text(
        json.dumps(
            {
                "mutations": [
                    {
                        "name": "return_zero",
                        "old": "return x;",
                        "new": "return 0;",
                        "equivalent_hint": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    mutations = load_mutations_json(path)

    assert len(mutations) == 1
    assert mutations[0].name == "return_zero"
    assert mutations[0].old == "return x;"


def test_generate_native_acsl_specs_uses_native_json_schema() -> None:
    class FakeLLM:
        def __init__(self) -> None:
            self.calls = []

        def complete(self, system, user, **kwargs):
            self.calls.append((system, user, kwargs))
            return json.dumps(
                {
                    "function_name": "inc",
                    "requires": ["\\true"],
                    "assigns": ["\\nothing"],
                    "ensures": ["\\result == x + 1"],
                    "loop_invariants": [],
                    "raw_acsl": ""
                }
            )

    source = "int inc(int x) { return x + 1; }\n"
    parsed = parse_c_file(Path("inc.c"), source_text=source)
    llm = FakeLLM()

    specs = generate_native_acsl_specs(
        source_path="inc.c",
        source_text=source,
        parsed=parsed,
        function_names=["inc"],
        llm=llm,
        model="test-model",
    )

    assert specs["inc"].ensures == ["\\result == x + 1"]
    assert specs["inc"].generation_metadata["model"] == "test-model"
    assert specs["inc"].generation_metadata["source_span"] == [0, len(source.strip())]
    assert llm.calls[0][2]["role"] == "spec_gen"


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc unavailable")
def test_tce_equivalence_detects_equal_object_code(tmp_path: Path) -> None:
    original = "int f(int x) { return x >= 0 ? x : x; }\n"
    mutated = "int f(int x) { return x > 0 ? x : x; }\n"

    result = _tce_equivalent(original, mutated, tmp_path / "same")

    assert result["equivalent"] is True
