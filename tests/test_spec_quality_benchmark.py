from __future__ import annotations

import json
import zipfile
from pathlib import Path

from experiments.spec_quality_compare.spec_quality_benchmark import (
    build_pilot_manifest,
    classify_quality_row,
    inspect_autospec_zip,
    render_summary,
    summarize_rows,
)


def test_build_pilot_manifest_has_four_decision_cases(tmp_path: Path) -> None:
    manifest = build_pilot_manifest(size=4, data_dir=tmp_path)

    case_ids = [case["case_id"] for case in manifest["cases"]]

    assert case_ids == [
        "max2_scalar",
        "read_at_bounds",
        "public_acsl_reference_pending",
        "ncdev_bar_read_overconstraint",
    ]
    assert manifest["cases"][0]["methods"][0]["method_type"] == "static_native_acsl"
    assert any(method["method_type"] == "generate_native_acsl" for method in manifest["cases"][0]["methods"])
    assert manifest["warnings"]


def test_inspect_autospec_zip_counts_sources_and_verified_files(tmp_path: Path) -> None:
    archive = tmp_path / "AutoSpec.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("AutoSpec/benchmark/fib_46_benchmark/01.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/fib_46_benchmark_verified/01.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/mutation_set/02_mutation.c", "void main(){}")
        zf.writestr("AutoSpec/raw/result.csv", "x")

    manifest = inspect_autospec_zip(archive)

    assert manifest["counts"]["benchmark_c_files"] == 2
    assert manifest["counts"]["verified_c_files"] == 1
    assert manifest["counts"]["mutation_like_c_files"] == 1
    assert manifest["candidates"][0]["source_in_zip"].endswith("01.c")


def test_classify_quality_row_separates_weak_useful_and_overconstrained() -> None:
    weak = {
        "status": "success",
        "frama_c_validity": {"status": "success"},
        "reference_coverage": {"coverage": 0},
        "mutation_vdr": {"mutation_score": 0},
        "vacuity_warnings": [{"kind": "vacuous_ensures"}],
        "overconstraint_warnings": [],
        "downstream_proof_utility": {"proof_ratio": None},
    }
    useful = {
        "status": "success",
        "frama_c_validity": {"status": "success"},
        "reference_coverage": {"coverage": 1.0},
        "mutation_vdr": {"mutation_score": 0.75},
        "vacuity_warnings": [],
        "overconstraint_warnings": [],
        "downstream_proof_utility": {"proof_ratio": None},
    }
    overconstrained = {
        "status": "success",
        "frama_c_validity": {"status": "not_run"},
        "overconstraint_warnings": [{"name": "rejects_witness"}],
    }

    assert classify_quality_row(weak) == "valid_but_weak"
    assert classify_quality_row(useful) == "useful"
    assert classify_quality_row(overconstrained) == "overconstrained"


def test_summary_rendering_keeps_result_classes_separate() -> None:
    rows = [
        {"case_id": "a", "method_id": "strong", "result_class": "useful", "status": "success", "frama_c_validity": {"status": "success"}},
        {"case_id": "a", "method_id": "weak", "result_class": "valid_but_weak", "status": "success", "frama_c_validity": {"status": "success"}},
        {"case_id": "b", "method_id": "wp", "result_class": "frama_c_unavailable", "status": "frama_c_unavailable", "frama_c_validity": {"status": "not_run"}},
    ]
    report = {
        "manifest": {"protocol_label": "test"},
        "toolchain": {"frama_c": {"status": "frama_c_unavailable"}, "gcc": {"path": "/usr/bin/gcc"}, "llm": {"model": "model"}},
        "summary": summarize_rows(rows),
        "rows": rows,
    }

    summary = render_summary(report)

    assert report["summary"]["result_classes"]["useful"] == 1
    assert report["summary"]["result_classes"]["valid_but_weak"] == 1
    assert "SpecSyn-inspired" in summary
    assert "| a | strong | useful | success |" in summary
