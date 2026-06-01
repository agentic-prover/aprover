from bmc_agent.acsl import translate_spec_to_acsl
from bmc_agent.parser import FunctionSignature
from bmc_agent.spec import Spec
from bmc_agent.acsl_native import NativeAcslSpec
from experiments.acsl_dsl_outcome_compare.acsl_dsl_outcome_compare import (
    BranchResult,
    Case,
    _compare_case,
    _contract_to_artifact,
    _include_dirs_for_source,
    _overconstraint_report,
    _summary,
    _spec_projection_report,
    discover_comprehensive_cases,
    project_contract_to_spec,
    project_native_acsl_to_spec,
    run_manifest,
)
from pathlib import Path


def test_supported_dsl_spec_projects_back_to_equivalent_spec() -> None:
    sig = FunctionSignature(
        name="max2",
        return_type="int",
        parameters=[("int", "x"), ("int", "y")],
    )
    spec = Spec(
        function_name="max2",
        precondition="true",
        postcondition="result >= x && result >= y && (result == x || result == y)",
    )

    contract = translate_spec_to_acsl(spec, sig)
    artifact = _contract_to_artifact(contract)
    projected = project_contract_to_spec(spec, artifact)
    report = _spec_projection_report(spec, projected, artifact)

    assert projected.function_name == "max2"
    assert projected.precondition == "true"
    assert projected.postcondition == spec.postcondition
    assert report["unsupported_clause_count"] == 0
    assert report["postcondition_changed"] is False


def test_unsupported_dsl_clause_is_reported_during_projection() -> None:
    sig = FunctionSignature(
        name="f",
        return_type="int",
        parameters=[("int", "x")],
    )
    spec = Spec(
        function_name="f",
        precondition="no_overflow(x + 1)",
        postcondition="result >= x",
    )

    artifact = _contract_to_artifact(translate_spec_to_acsl(spec, sig))
    projected = project_contract_to_spec(spec, artifact)
    report = _spec_projection_report(spec, projected, artifact)

    assert projected.precondition == "true"
    assert report["unsupported_clause_count"] == 1
    assert report["unsupported"][0]["reason"] == "unsupported DSL primitive: no_overflow"


def test_read_at_synthetic_control_flags_overconstraint() -> None:
    case = Case(
        case_id="synthetic_read_at_overconstraint",
        source="read_at.c",
        driver="synthetic",
        function="read_at",
        case_kind="synthetic_overconstraint",
        expected_behavior="overconstraint_control",
    )
    spec = Spec(
        function_name="read_at",
        precondition="valid_range(arr, 0, len) && idx >= 0 && idx < len",
        postcondition="result == arr[idx]",
    )

    report = _overconstraint_report(case, spec)

    assert report["status"] == "warning"
    assert any("idx >= len" in warning for warning in report["warnings"])


def test_compare_case_classifies_confirmed_bug_loss() -> None:
    case = Case(
        case_id="confirmed_bug",
        source="bug.c",
        driver="driver",
        function="bug",
        case_kind="confirmed_bug",
        expected_behavior="bug",
    )
    dsl = BranchResult(
        branch="dsl",
        status="success",
        final_label="bug_found",
        runtime_s=1.0,
    )
    acsl = BranchResult(
        branch="acsl",
        status="success",
        final_label="verified_clean",
        runtime_s=1.1,
    )

    comparison = _compare_case(
        case,
        dsl,
        acsl,
        projection_report={"unsupported_clause_count": 0},
        harness_diff={"status": "different"},
        overconstraint={"status": "ok", "warnings": []},
    )

    assert comparison["outcome_class"] == "acsl_missed_confirmed_bug"
    assert comparison["final_label_agreement"] is False


def test_compare_case_prioritizes_unsupported_clause() -> None:
    case = Case(
        case_id="unsupported",
        source="f.c",
        driver="driver",
        function="f",
        case_kind="synthetic",
        expected_behavior="clean",
    )
    dsl = BranchResult("dsl", "success", "verified_clean", 1.0)
    acsl = BranchResult("acsl", "success", "verified_clean", 1.0)

    comparison = _compare_case(
        case,
        dsl,
        acsl,
        projection_report={"unsupported_clause_count": 1},
        harness_diff={"status": "identical"},
        overconstraint={"status": "ok", "warnings": []},
    )

    assert comparison["outcome_class"] == "unsupported_clause"


def test_native_acsl_projection_normalizes_simple_clauses() -> None:
    native = NativeAcslSpec(
        function_name="f",
        requires=["x != \\null"],
        ensures=["\\result >= 0"],
    )

    projected, artifact = project_native_acsl_to_spec(native)

    assert projected.precondition == "x != NULL"
    assert projected.postcondition == "result >= 0"
    assert artifact["unsupported_clause_count"] == 0


def test_native_acsl_projection_rejects_assigns_and_quantifiers() -> None:
    native = NativeAcslSpec(
        function_name="f",
        requires=["\\forall integer i; i >= 0"],
        ensures=["\\true"],
        assigns=["x"],
    )

    projected, artifact = project_native_acsl_to_spec(native)

    assert projected.precondition == "true"
    assert projected.postcondition == "true"
    assert artifact["unsupported_clause_count"] == 2
    assert {item["kind"] for item in artifact["unsupported"]} == {"requires", "assigns"}


def test_comprehensive_discovery_has_separate_modes() -> None:
    cases = discover_comprehensive_cases(limit=80)
    modes = {case.mode for case in cases}

    assert "spec_replay" in modes
    assert "direct_harness" in modes
    assert "projection_only" in modes


def test_vibeos_include_resolver_adds_kernel_dirs() -> None:
    dirs = _include_dirs_for_source(Path("/mnt/disk7/jw_bmc/vibeos/kernel/kapi.c"))

    assert "/mnt/disk7/jw_bmc/vibeos/kernel" in dirs


def test_summary_excludes_branch_errors_from_interpretable_agreement() -> None:
    case = Case(
        case_id="c",
        source="x.c",
        driver="d",
        function="f",
        case_kind="k",
        expected_behavior="unknown",
    )
    result = {
        "case": case.to_dict(),
        "dsl": BranchResult("dsl", "error", "error", 0.0).to_dict(),
        "acsl": BranchResult("acsl", "error", "error", 0.0).to_dict(),
        "projection": {},
        "comparison": {"outcome_class": "inconclusive", "final_label_agreement": True},
    }

    summary = _summary([result])

    assert summary["runnable_case_count"] == 1
    assert summary["interpretable_case_count"] == 0
    assert summary["final_label_agreement"]["denominator"] == 0
    assert summary["raw_final_label_agreement_including_errors"]["denominator"] == 1


def test_stage_b_requires_explicit_llm_permission(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"cases": [{"case_id": "c", "source": "x.c", "driver": "d", '
        '"function": "f", "case_kind": "k", "expected_behavior": "unknown", '
        '"mode": "native_acsl_e2e"}]}',
        encoding="utf-8",
    )

    try:
        run_manifest(
            manifest=manifest,
            output=tmp_path / "out",
            timeout=1,
            unwind=1,
            cbmc_path="cbmc",
            stage="stage_b",
            allow_llm=False,
        )
    except RuntimeError as exc:
        assert "--allow-llm" in str(exc)
    else:
        raise AssertionError("run-stage-b must fail without --allow-llm")
