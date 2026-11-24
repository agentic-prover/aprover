"""Tests for the SpecGen Java/JML experiment adapter."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "experiments"
    / "specgen_compare"
    / "run_bmc_jml_specgen.py"
)
REPLAY_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "experiments"
    / "specgen_compare"
    / "replay_jml_postprocess.py"
)


def load_runner():
    spec = importlib.util.spec_from_file_location("run_bmc_jml_specgen", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_replay_runner():
    spec = importlib.util.spec_from_file_location("replay_jml_postprocess", REPLAY_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_discover_cases_prefers_case_named_java_file(tmp_path: Path):
    mod = load_runner()
    common = tmp_path / "common"
    oracle = tmp_path / "oracle"
    (common / "Return100").mkdir(parents=True)
    (oracle / "Return100").mkdir(parents=True)
    (common / "Return100" / "Return100.java").write_text("class Return100 {}\n")
    (common / "Return100" / "Return100Driver.java").write_text("class Return100Driver {}\n")
    (oracle / "Return100" / "Return100.java").write_text("class Return100 {}\n")

    cases = mod.discover_cases(common, oracle)

    assert [c.name for c in cases] == ["Return100"]
    assert cases[0].source.endswith("Return100.java")
    assert cases[0].oracle.endswith("Return100.java")


def test_select_cases_reports_missing_case(tmp_path: Path):
    mod = load_runner()
    cases = [mod.SpecGenCase("A", "/a/A.java", "")]
    try:
        mod.select_cases(cases, ["B"], None)
    except SystemExit as exc:
        assert "unknown SpecGen case" in str(exc)
    else:
        raise AssertionError("expected SystemExit for missing case")


def test_summarize_counts_statuses():
    mod = load_runner()
    rows = [
        mod.CaseRow("A", "A.java", "", "passed", True, 1, 1.0, "a", "r", "o"),
        mod.CaseRow("B", "B.java", "", "verification_failed", False, 2, 2.0, "b", "r", "o"),
    ]
    summary = mod.summarize(rows)
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["by_status"] == {"passed": 1, "verification_failed": 1}
    assert summary["trial_passes"] == 1
    assert summary["trial_total"] == 2


def test_summarize_reports_actionability_counts():
    mod = load_runner()
    rows = [
        mod.CaseRow("Pass", "Pass.java", "", "passed", True, 1, 1.0, "a", "r", "o"),
        mod.CaseRow(
            "Spec",
            "Spec.java",
            "",
            "verification_failed",
            False,
            1,
            1.0,
            "a",
            "r",
            "o",
            failure_class="spec_not_sufficient",
        ),
        mod.CaseRow(
            "Tool",
            "Tool.java",
            "",
            "tool_error",
            False,
            1,
            1.0,
            "a",
            "r",
            "o",
            failure_class="source_frontend_or_tool",
        ),
        mod.CaseRow(
            "LLM",
            "LLM.java",
            "",
            "llm_unavailable",
            False,
            0,
            0.0,
            "",
            "",
            "",
            failure_class="llm_unavailable",
            attempts=0,
            trials=0,
            trial_passes=0,
        ),
    ]

    summary = mod.summarize(rows)

    assert summary["generated_spec_issue_count"] == 1
    assert summary["source_or_tool_boundary_count"] == 1
    assert summary["llm_or_runner_issue_count"] == 1
    assert summary["by_actionability"] == {
        "generated_spec_issue": 1,
        "llm_or_runner_issue": 1,
        "passed": 1,
        "source_or_tool_boundary": 1,
    }
    assert summary["passed_with_zero_trial_passes_count"] == 0
    assert summary["passed_without_trial_stats_count"] == 0


def test_summarize_reports_overlay_pass_trial_stat_mismatches():
    mod = load_runner()
    rows = [
        mod.CaseRow(
            "OverlayPass",
            "OverlayPass.java",
            "",
            "passed",
            True,
            1,
            1.0,
            "a",
            "r",
            "o",
            trials=10,
            trial_passes=0,
        ),
        mod.CaseRow(
            "PreflightPass",
            "PreflightPass.java",
            "",
            "passed",
            True,
            1,
            1.0,
            "a",
            "r",
            "o",
            trials=0,
            trial_passes=0,
        ),
        mod.CaseRow(
            "NormalPass",
            "NormalPass.java",
            "",
            "passed",
            True,
            1,
            1.0,
            "a",
            "r",
            "o",
            trials=10,
            trial_passes=3,
        ),
    ]

    summary = mod.summarize(rows)

    assert summary["passed"] == 3
    assert summary["passed_with_zero_trial_passes_count"] == 1
    assert summary["passed_with_zero_trial_passes_cases"] == ["OverlayPass"]
    assert summary["passed_without_trial_stats_count"] == 1
    assert summary["passed_without_trial_stats_cases"] == ["PreflightPass"]


def test_format_trial_cell_marks_overlay_trial_stat_mismatches():
    mod = load_runner()

    assert mod.format_trial_cell({"passed": True, "trial_passes": 3, "trials": 10}) == "3/10"
    assert (
        mod.format_trial_cell({"passed": True, "trial_passes": 0, "trials": 10})
        == "0/10 (base; overlay pass)"
    )
    assert (
        mod.format_trial_cell({"passed": True, "trial_passes": 0, "trials": 0})
        == "n/a (source-preflight pass)"
    )
    assert (
        mod.format_trial_cell({"passed": True, "attempts": 0, "trial_passes": 0, "trials": 0})
        == "n/a (source-preflight pass)"
    )
    assert mod.format_trial_cell({"passed": False, "trial_passes": 0, "trials": 10}) == "0/10"
    assert mod.format_trial_cell({"attempts": 0, "trial_passes": 0, "trials": 0}) == "skipped"
    assert mod.format_trial_cell({"passed": False, "trial_passes": None, "trials": 1}) == ""


def test_summarize_excludes_preflight_skipped_rows_from_trial_total():
    mod = load_runner()
    rows = [
        mod.CaseRow(
            "Bad",
            "Bad.java",
            "",
            "source_invalid",
            False,
            0,
            0.1,
            "",
            "",
            "source_preflight.out",
            attempts=0,
            trials=0,
            trial_passes=0,
        ),
        mod.CaseRow("Good", "Good.java", "", "passed", True, 1, 1.0, "a", "r", "o"),
    ]

    summary = mod.summarize(rows)

    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["trial_total"] == 1
    assert summary["trial_passes"] == 1


def test_replay_trial_treats_empty_generated_path_as_missing_artifact(tmp_path: Path):
    mod = load_replay_runner()
    source = tmp_path / "A.java"
    source.write_text("class A {}\n", encoding="utf-8")
    row = {"case": "A", "source": str(source)}
    trial = {
        "source": str(source),
        "final_annotated_path": "",
        "status": "source_invalid",
        "passed": False,
    }
    args = SimpleNamespace(replay_passed=False, include_timeouts=False)

    replayed = mod.replay_trial(row, trial, tmp_path / "case", args)

    assert replayed["replay_note"] == "missing source or generated artifact"


def test_process_row_can_replay_only_prior_passing_trials(tmp_path: Path, monkeypatch):
    mod = load_replay_runner()
    source = tmp_path / "A.java"
    generated_fail = tmp_path / "A_fail.java"
    generated_pass = tmp_path / "A_pass.java"
    source.write_text("class A {}\n", encoding="utf-8")
    generated_fail.write_text("class A {}\n", encoding="utf-8")
    generated_pass.write_text("class A {}\n", encoding="utf-8")
    seen: list[str] = []

    def fake_replay_trial(row, trial, case_dir, args):
        seen.append(Path(trial["final_annotated_path"]).name)
        updated = dict(trial)
        updated["passed"] = True
        updated["status"] = "passed"
        updated["runtime_s"] = 0.0
        return updated

    monkeypatch.setattr(mod, "replay_trial", fake_replay_trial)
    row = {
        "case": "A",
        "source": str(source),
        "passed": True,
        "trial_rows": [
            {"source": str(source), "final_annotated_path": str(generated_fail), "passed": False, "status": "verification_failed"},
            {"source": str(source), "final_annotated_path": str(generated_pass), "passed": True, "status": "passed"},
        ],
    }
    args = SimpleNamespace(
        preflight_source=False,
        only_passed_trials=True,
        max_trials_per_case=1,
    )

    _, new_row, _ = mod.process_row(1, 1, row, tmp_path / "out", args)

    assert seen == ["A_pass.java"]
    assert new_row["passed"] is True


def test_replay_generated_source_rejects_untransplantable_executable_source_changes(tmp_path: Path, monkeypatch):
    mod = load_replay_runner()
    source = tmp_path / "A.java"
    generated = tmp_path / "A_generated.java"
    source.write_text(
        """
class A {
  int f() { return 1; }
}
""",
        encoding="utf-8",
    )
    generated.write_text("class A { int f() { return 2; } }\n", encoding="utf-8")
    called = {"openjml": 0}

    def fake_run_openjml(*_args, **_kwargs):
        called["openjml"] += 1
        raise AssertionError("source-changed replay must not call OpenJML")

    monkeypatch.setattr(mod, "run_openjml", fake_run_openjml)

    status, passed, _runtime, rounds, error, output_java = mod.replay_generated_source(
        source_path=source,
        generated_path=generated,
        output_java=tmp_path / "out" / "A.java",
        output_log_prefix=tmp_path / "out" / "openjml_replay",
        openjml_path="openjml",
        timeout_s=1,
        max_prune_rounds=5,
    )

    assert status == "source_changed"
    assert passed is False
    assert rounds == 0
    assert "generated source changes executable Java code" in error
    assert output_java.exists()
    assert called["openjml"] == 0


def test_replay_generated_source_transplants_jml_onto_original_source(tmp_path: Path, monkeypatch):
    mod = load_replay_runner()
    source = tmp_path / "A.java"
    generated = tmp_path / "A_generated.java"
    source.write_text(
        """
class A {
  int f() { return 1; }
}
""",
        encoding="utf-8",
    )
    generated.write_text(
        """
class A {
  //@ ensures \\result == 1;
  int f() { return 2; }
}
""",
        encoding="utf-8",
    )
    called = {"openjml": 0}

    def fake_run_openjml(source_path, *_args, **_kwargs):
        called["openjml"] += 1
        replayed = Path(source_path).read_text(encoding="utf-8")
        assert "return 1;" in replayed
        assert "return 2;" not in replayed
        assert "ensures \\result == 1" in replayed
        return SimpleNamespace(status="passed", passed=True, runtime_s=0.1, stdout="", stderr="", error="")

    monkeypatch.setattr(mod, "run_openjml", fake_run_openjml)

    status, passed, _runtime, rounds, error, output_java = mod.replay_generated_source(
        source_path=source,
        generated_path=generated,
        output_java=tmp_path / "out" / "A.java",
        output_log_prefix=tmp_path / "out" / "openjml_replay",
        openjml_path="openjml",
        timeout_s=1,
        max_prune_rounds=5,
    )

    assert status == "passed"
    assert passed is True
    assert rounds == 0
    assert error == ""
    assert output_java.exists()
    assert called["openjml"] == 1


def test_replay_generated_source_does_not_report_unrun_prune_round(tmp_path: Path, monkeypatch):
    mod = load_replay_runner()
    source = tmp_path / "A.java"
    generated = tmp_path / "A_generated.java"
    source.write_text("class A { void f() {} }\n", encoding="utf-8")
    generated.write_text("class A { //@ maintaining true;\n void f() {} }\n", encoding="utf-8")
    calls = {"openjml": 0}

    def fake_run_openjml(*_args, **_kwargs):
        calls["openjml"] += 1
        return SimpleNamespace(
            status="verification_failed",
            passed=False,
            runtime_s=0.1,
            stdout="A.java:2: verify: The prover cannot establish an assertion (LoopInvariant) in method f\n",
            stderr="",
            error="",
        )

    def fake_no_change(source_text, _output):
        return source_text, False

    def fake_loop_prune(source_text, _output):
        return source_text + "\n", True

    monkeypatch.setattr(mod, "run_openjml", fake_run_openjml)
    monkeypatch.setattr(mod.jml_specs, "_annotate_reported_nullable", fake_no_change)
    monkeypatch.setattr(mod.jml_specs, "_prune_reported_precondition", fake_no_change)
    monkeypatch.setattr(mod.jml_specs, "_prune_reported_assignable", fake_no_change)
    monkeypatch.setattr(mod.jml_specs, "_prune_reported_postcondition", fake_no_change)
    monkeypatch.setattr(mod.jml_specs, "_prune_reported_loop_decreases", fake_no_change)
    monkeypatch.setattr(mod.jml_specs, "_prune_reported_loop_invariant", fake_loop_prune)

    status, passed, _runtime, rounds, _error, _output_java = mod.replay_generated_source(
        source_path=source,
        generated_path=generated,
        output_java=tmp_path / "out" / "A.java",
        output_log_prefix=tmp_path / "out" / "openjml_replay",
        openjml_path="openjml",
        timeout_s=1,
        max_prune_rounds=2,
    )

    assert status == "verification_failed"
    assert passed is False
    assert rounds == 2
    assert calls["openjml"] == 3
    assert (tmp_path / "out" / "openjml_replay_round_2.out").exists()
    assert not (tmp_path / "out" / "openjml_replay_round_3.out").exists()


def test_oracle_source_metadata_detects_jml_only_oracle(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "A.java"
    oracle = tmp_path / "A_oracle.java"
    source.write_text("class A { int f(int x) { return x + 1; } }\n", encoding="utf-8")
    oracle.write_text(
        "class A { //@ ensures \\result == x + 1;\n int f(int x) { return x + 1; } }\n",
        encoding="utf-8",
    )
    row = mod.CaseRow("A", str(source), str(oracle), "passed", True, 1, 0.1, "", "", "")

    status, diff_path = mod.oracle_source_metadata(row, tmp_path / "out")

    assert status == "jml_only_or_same_source"
    assert diff_path == ""


def test_oracle_source_metadata_writes_diff_for_source_mismatch(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "A.java"
    oracle = tmp_path / "A_oracle.java"
    source.write_text("class A { int f(int x) { return x + 1; } }\n", encoding="utf-8")
    oracle.write_text("class A { int f(int x) { return x + 2; } }\n", encoding="utf-8")
    row = mod.CaseRow("A", str(source), str(oracle), "verification_failed", False, 1, 0.1, "", "", "")

    status, diff_path = mod.oracle_source_metadata(row, tmp_path / "out")

    assert status == "source_mismatch"
    diff = Path(diff_path)
    assert diff.exists()
    assert "return x + 1" in diff.read_text(encoding="utf-8")
    assert "return x + 2" in diff.read_text(encoding="utf-8")


def test_write_report_attaches_oracle_source_metadata(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "A.java"
    oracle = tmp_path / "A_oracle.java"
    source.write_text("class A { int f(int x) { return x + 1; } }\n", encoding="utf-8")
    oracle.write_text("class A { int f(int x) { return x + 2; } }\n", encoding="utf-8")
    row = mod.CaseRow("A", str(source), str(oracle), "verification_failed", False, 1, 0.1, "", "", "")

    mod.write_report(tmp_path / "report", [row])

    assert row.oracle_source_status == "source_mismatch"
    assert Path(row.oracle_source_diff_path).exists()
    summary = (tmp_path / "report" / "summary.md").read_text(encoding="utf-8")
    assert "Oracle source" in summary
    assert "source_mismatch" in summary


def test_classify_failure_prioritizes_pass_and_oracle_mismatch():
    mod = load_runner()
    passed = mod.CaseRow("A", "A.java", "", "passed", True, 1, 0.1, "", "", "")
    mismatch = mod.CaseRow("B", "B.java", "", "timeout", False, 1, 0.1, "", "", "")
    mismatch.oracle_source_status = "source_mismatch"
    changed = mod.CaseRow(
        "C",
        "C.java",
        "",
        "source_changed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="generated source changes executable Java code",
    )

    assert mod.classify_failure(passed) == "passed"
    assert mod.classify_failure(mismatch) == "oracle_source_mismatch"
    assert mod.classify_failure(changed) == "source_changed"
    assert mod.extract_failure_reason(changed) == "SourceChanged"


def test_classify_failure_detects_library_precondition_and_tool_error(tmp_path: Path):
    mod = load_runner()
    lib = mod.CaseRow(
        "Sqrt",
        "Sqrt.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="verify: The prover cannot establish an assertion (Precondition: /x/openjml/specs/java/lang/Math.jml:264:)",
    )
    out = tmp_path / "openjml.out"
    out.write_text("A catastrophic JML internal error occurred\nReason: Double rewriting of ident\n", encoding="utf-8")
    tool = mod.CaseRow("Matrix", "M.java", "", "tool_error", False, 1, 0.1, "", "", str(out))
    proof_script = mod.CaseRow(
        "Cast",
        "C.java",
        "",
        "tool_error",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error='error: An error while executing a proof script for f: (error "expecting an arithmetic subterm")',
    )
    scanner_null_precondition = mod.CaseRow(
        "Scanner",
        "Scanner.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error=(
            "NULL PRECONDITION FOR Scanner.f(java.lang.String) "
            "java.util.Scanner.next() false java.util.Scanner.next() false public behavior"
        ),
    )
    runtime_diverges = mod.CaseRow(
        "Verifier",
        "Verifier.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error=(
            "/x/openjml/specs/java/lang/Runtime.jml:30: verify: The prover cannot "
            "establish an assertion (Diverges: /tmp/Verifier.java:4:) in method assume"
        ),
    )

    assert mod.classify_failure(lib) == "library_precondition"
    assert mod.classify_failure(scanner_null_precondition) == "library_precondition"
    assert mod.extract_failure_reason(scanner_null_precondition) == "LibraryPrecondition"
    assert mod.classify_failure(runtime_diverges) == "library_precondition"
    assert mod.extract_failure_reason(runtime_diverges) == "LibraryPrecondition"
    assert mod.classify_failure(tool) == "openjml_tool_error"
    assert mod.extract_failure_reason(tool) == "OpenJMLDoubleRewriteIdent"
    assert mod.classify_failure(proof_script) == "openjml_tool_error"
    assert mod.extract_failure_reason(proof_script) == "OpenJMLProofScriptError"


def test_classify_failure_detects_source_frontend_error_from_text(tmp_path: Path):
    mod = load_runner()
    source_frontend = mod.CaseRow(
        "Instanceof",
        "X.java",
        "",
        "annotation_error",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="X.java:11: error: cannot find symbol\n  symbol:   variable args",
    )
    jml_syntax = mod.CaseRow(
        "BadJml",
        "X.java",
        "",
        "annotation_error",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="X.java:2: error: Signals clauses are not permitted in normal specification cases",
    )

    assert mod.classify_failure(source_frontend) == "source_frontend_or_tool"
    assert mod.classify_failure(jml_syntax) == "invalid_generated_jml"


def test_extract_failure_reason_from_openjml_output():
    mod = load_runner()
    assertion = mod.CaseRow(
        "A",
        "A.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="A.java:10: verify: The prover cannot establish an assertion (Assert) in method main",
    )
    library = mod.CaseRow(
        "S",
        "S.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="verify: The prover cannot establish an assertion (Precondition: /x/openjml/specs/java/lang/Math.jml:264:) in method f",
    )
    source = mod.CaseRow(
        "Bad",
        "Bad.java",
        "",
        "annotation_error",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="Bad.java:1: error: cannot find symbol\n  symbol: variable args",
    )
    library_undefined_pre = mod.CaseRow(
        "Str",
        "Str.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error=(
            "verify: The prover cannot establish an assertion "
            "(UndefinedCalledMethodPrecondition: /x/openjml/specs/java/lang/CharSequence.jml:57:) in method f"
        ),
    )

    assert mod.extract_failure_reason(assertion) == "Assert"
    assert mod.extract_failure_reason(library) == "LibraryPrecondition"
    assert mod.extract_failure_reason(source) == "SourceMissingSymbol"
    assert mod.extract_failure_reason(library_undefined_pre) == "LibraryPrecondition"
    assert mod.classify_failure(library_undefined_pre) == "library_precondition"


def test_classify_runner_exception_redacts_key_like_provider_errors():
    mod = load_runner()
    masked_key = "s" + "k-ThisI********************************nKey"
    status, message = mod.classify_runner_exception(
        Exception(f"HTTP 401 Unauthorized: Incorrect API key provided: {masked_key}.")
    )

    assert status == "llm_unavailable"
    assert "sk-" not in message
    assert "[REDACTED_API_KEY]" in message


def test_classify_failure_ignores_timeout_in_artifact_paths():
    mod = load_runner()
    row = mod.CaseRow(
        "A",
        "A.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "/tmp/openjml_timeout_replay/A.java",
        "",
        "",
        error=(
            "/tmp/openjml_timeout_replay/A.java:10: verify: The prover cannot "
            "establish an assertion (Assert) in method main"
        ),
    )

    assert mod.classify_failure(row) == "spec_not_sufficient"
    assert mod.extract_failure_reason(row) == "Assert"


def test_classify_failure_marks_reported_java_assert_line_as_source_assert(tmp_path: Path):
    mod = load_runner()
    artifact = tmp_path / "A.java"
    artifact.write_text(
        "\n".join(
            [
                "class A {",
                "  void f() {",
                "    assert false;",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    row = mod.CaseRow(
        "A",
        str(artifact),
        "",
        "verification_failed",
        False,
        1,
        0.1,
        str(artifact),
        "",
        "",
        error=f"{artifact}:3: verify: The prover cannot establish an assertion (Assert) in method f",
    )

    assert mod.classify_failure(row) == "source_assert_failure"
    assert mod.extract_failure_reason(row) == "SourceAssertFailure"


def test_classify_failure_does_not_mark_jml_assert_as_source_assert(tmp_path: Path):
    mod = load_runner()
    artifact = tmp_path / "A.java"
    artifact.write_text(
        "\n".join(
            [
                "class A {",
                "  void f() {",
                "    //@ assert false;",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    row = mod.CaseRow(
        "A",
        str(artifact),
        "",
        "verification_failed",
        False,
        1,
        0.1,
        str(artifact),
        "",
        "",
        error=f"{artifact}:3: verify: The prover cannot establish an assertion (Assert) in method f",
    )

    assert mod.classify_failure(row) == "spec_not_sufficient"
    assert mod.extract_failure_reason(row) == "Assert"


def test_source_timeout_overrides_generated_verification_failure():
    mod = load_runner()
    row = mod.CaseRow(
        "Hard",
        "Hard.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="Hard.java:10: verify: The prover cannot establish an assertion (Assert) in method f",
        source_preflight_status="timeout",
        source_preflight_assert_failure=False,
        source_preflight_failure_reason="OpenJMLTimeout",
    )

    assert mod.classify_failure(row) == "source_openjml_timeout"
    assert mod.extract_failure_reason(row) == "SourceOpenJMLTimeout"


def test_generated_java_assert_line_overrides_source_timeout(tmp_path: Path):
    mod = load_runner()
    artifact = tmp_path / "A.java"
    artifact.write_text(
        "\n".join(
            [
                "class A {",
                "  void f() {",
                "    assert false;",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    row = mod.CaseRow(
        "A",
        str(artifact),
        "",
        "verification_failed",
        False,
        1,
        0.1,
        str(artifact),
        "",
        "",
        error=f"{artifact}:3: verify: The prover cannot establish an assertion (Assert) in method f",
        source_preflight_status="timeout",
        source_preflight_assert_failure=False,
        source_preflight_failure_reason="NullField",
    )

    assert mod.classify_failure(row) == "source_assert_failure"
    assert mod.extract_failure_reason(row) == "SourceAssertFailure"


def test_concrete_source_timeout_reason_is_source_safety_without_better_replay():
    mod = load_runner()
    row = mod.CaseRow(
        "Trie",
        "Trie.java",
        "",
        "timeout",
        False,
        1,
        95.0,
        "",
        "",
        "",
        error="openjml wall-clock timeout after 95s",
        source_preflight_status="timeout",
        source_preflight_assert_failure=False,
        source_preflight_failure_reason="NullField",
    )

    assert mod.classify_failure(row) == "source_safety_obligation"
    assert mod.extract_failure_reason(row) == "SourceSafety:NullField"


def test_source_preflight_assert_failure_overrides_generated_timeout():
    mod = load_runner()
    row = mod.CaseRow(
        case="AssertSource",
        source="AssertSource.java",
        oracle="",
        status="timeout",
        passed=False,
        iterations=1,
        runtime_s=65.0,
        final_annotated_path="",
        report_path="",
        openjml_output_path="",
        error="openjml wall-clock timeout after 65s",
        source_preflight_status="verification_failed",
        source_preflight_assert_failure=True,
        source_preflight_failure_reason="Assert",
    )

    assert mod.classify_failure(row) == "source_assert_failure"
    assert mod.extract_failure_reason(row) == "SourceAssertFailure"


def test_write_report_attaches_failure_classes(tmp_path: Path):
    mod = load_runner()
    row = mod.CaseRow(
        "Sqrt",
        "Sqrt.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error="verify: The prover cannot establish an assertion (Precondition: /x/openjml/specs/java/lang/Math.jml:264:)",
    )

    mod.write_report(tmp_path / "report", [row])

    assert row.failure_class == "library_precondition"
    assert row.failure_reason == "LibraryPrecondition"
    data = mod.json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["by_failure_class"] == {"library_precondition": 1}
    assert data["summary"]["by_failure_reason"] == {"LibraryPrecondition": 1}
    assert "Failure-class counts" in (tmp_path / "report" / "summary.md").read_text(encoding="utf-8")
    assert "Failure-reason counts" in (tmp_path / "report" / "summary.md").read_text(encoding="utf-8")


def test_annotate_report_tolerates_extra_fields(tmp_path: Path, capsys):
    mod = load_runner()
    source = tmp_path / "A.java"
    oracle = tmp_path / "A_oracle.java"
    source.write_text("class A { int f(int x) { return x + 1; } }\n", encoding="utf-8")
    oracle.write_text("class A { int f(int x) { return x + 2; } }\n", encoding="utf-8")
    report = tmp_path / "input.json"
    report.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source),
                        "oracle": str(oracle),
                        "status": "verification_failed",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                        "extra_replay_field": "ignored",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(input_report=str(report), output=str(tmp_path / "annotated"))

    assert mod.cmd_annotate_report(args) == 0

    output = (tmp_path / "annotated" / "report.json").read_text(encoding="utf-8")
    assert "source_mismatch" in output
    assert "extra_replay_field" not in output
    assert "annotated 1 row" in capsys.readouterr().out


def test_annotate_report_marks_source_assert_failures(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "A.java"
    source.write_text("class A { void f() { assert false; } }\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source),
                        "oracle": "",
                        "status": "verification_failed",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                        "error": "A.java:1: verify: The prover cannot establish an assertion (Assert) in method f",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    preflight = tmp_path / "preflight.json"
    preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "status": "verification_failed",
                        "has_assert_failure": True,
                        "failure_reason": "Assert",
                        "output_path": str(tmp_path / "source_preflight.out"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(report),
        output=str(tmp_path / "annotated"),
        source_preflight_report=str(preflight),
    )

    assert mod.cmd_annotate_report(args) == 0

    data = mod.json.loads((tmp_path / "annotated" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["by_failure_class"] == {"source_assert_failure": 1}
    assert data["summary"]["by_failure_reason"] == {"SourceAssertFailure": 1}
    assert data["rows"][0]["source_preflight_assert_failure"] is True
    assert data["rows"][0]["source_preflight_failure_reason"] == "Assert"


def test_annotate_report_marks_source_openjml_timeouts(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "Slow.java"
    source.write_text("class Slow { void f() {} }\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Slow",
                        "source": str(source),
                        "oracle": "",
                        "status": "timeout",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 35.0,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                        "error": "openjml wall-clock timeout after 35s",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    preflight = tmp_path / "preflight.json"
    preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Slow",
                        "status": "timeout",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "source_preflight.out"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(report),
        output=str(tmp_path / "annotated"),
        source_preflight_report=str(preflight),
    )

    assert mod.cmd_annotate_report(args) == 0

    data = mod.json.loads((tmp_path / "annotated" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["by_failure_class"] == {"source_openjml_timeout": 1}
    assert data["summary"]["by_failure_reason"] == {"SourceOpenJMLTimeout": 1}
    assert data["rows"][0]["source_preflight_status"] == "timeout"


def test_annotate_report_marks_source_safety_obligations(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "Cast.java"
    source.write_text("class Cast { Object x; String f() { return (String)x; } }\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Cast",
                        "source": str(source),
                        "oracle": "",
                        "status": "verification_failed",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                        "error": "Cast.java:1: verify: The prover cannot establish an assertion (PossiblyBadCast) in method f",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    preflight = tmp_path / "preflight.json"
    preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Cast",
                        "status": "verification_failed",
                        "has_assert_failure": False,
                        "failure_reason": "PossiblyBadCast",
                        "output_path": str(tmp_path / "source_preflight.out"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(report),
        output=str(tmp_path / "annotated"),
        source_preflight_report=str(preflight),
    )

    assert mod.cmd_annotate_report(args) == 0

    data = mod.json.loads((tmp_path / "annotated" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["by_failure_class"] == {"source_safety_obligation": 1}
    assert data["summary"]["by_failure_reason"] == {"SourceSafety:PossiblyBadCast": 1}
    assert data["rows"][0]["source_preflight_status"] == "verification_failed"


def test_source_safety_preflight_overrides_generated_timeout(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "Slow.java"
    row = mod.CaseRow(
        "Slow",
        str(source),
        "",
        "timeout",
        False,
        1,
        65.0,
        "",
        "",
        "",
        error="openjml wall-clock timeout after 65s",
        source_preflight_status="verification_failed",
        source_preflight_assert_failure=False,
        source_preflight_failure_reason="PossiblyNegativeIndex",
    )

    assert mod.classify_failure(row) == "source_safety_obligation"
    assert mod.extract_failure_reason(row) == "SourceSafety:PossiblyNegativeIndex"


def test_source_safety_preflight_overrides_generated_java_assert_line(tmp_path: Path):
    mod = load_runner()
    artifact = tmp_path / "A.java"
    artifact.write_text(
        "\n".join(
            [
                "class A {",
                "  void f() {",
                "    assert false;",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    row = mod.CaseRow(
        "A",
        str(artifact),
        "",
        "verification_failed",
        False,
        1,
        0.1,
        str(artifact),
        "",
        "",
        error=f"{artifact}:3: verify: The prover cannot establish an assertion (Assert) in method f",
        source_preflight_status="verification_failed",
        source_preflight_assert_failure=False,
        source_preflight_failure_reason="NullField",
    )

    assert mod.classify_failure(row) == "source_safety_obligation"
    assert mod.extract_failure_reason(row) == "SourceSafety:NullField"


def test_source_safety_preflight_overrides_generated_library_precondition(tmp_path: Path):
    mod = load_runner()
    row = mod.CaseRow(
        "Solve",
        "Solve.java",
        "",
        "verification_failed",
        False,
        1,
        0.1,
        "",
        "",
        "",
        error=(
            "verify: The prover cannot establish an assertion "
            "(Precondition: /x/openjml/specs/java/lang/Math.jml:264:) in method solve"
        ),
        source_preflight_status="verification_failed",
        source_preflight_assert_failure=False,
        source_preflight_failure_reason="PossiblyDivideByZero",
    )

    assert mod.classify_failure(row) == "source_safety_obligation"
    assert mod.extract_failure_reason(row) == "SourceSafety:PossiblyDivideByZero"


def test_source_library_precondition_is_source_boundary():
    mod = load_runner()
    row = mod.CaseRow(
        case="Token",
        source="Token.java",
        oracle="",
        status="verification_failed",
        passed=False,
        iterations=1,
        runtime_s=0.1,
        final_annotated_path="",
        report_path="",
        openjml_output_path="",
        error="verify: The prover cannot establish an assertion (Assert) in method f",
        source_preflight_status="verification_failed",
        source_preflight_assert_failure=False,
        source_preflight_failure_reason="LibraryPrecondition",
    )
    row.failure_class = mod.classify_failure(row)

    assert row.failure_class == "source_library_precondition"
    assert mod.extract_failure_reason(row) == "SourceLibraryPrecondition"
    assert mod.failure_actionability(row) == "source_or_tool_boundary"


def test_build_residual_audit_recommends_continuing_for_generated_spec_issue():
    mod = load_runner()
    rows = [
        mod.CaseRow("Pass", "Pass.java", "", "passed", True, 1, 0.1, "", "", ""),
        mod.CaseRow(
            "NeedsSpec",
            "NeedsSpec.java",
            "",
            "verification_failed",
            False,
            1,
            0.1,
            "",
            "",
            "",
            error="NeedsSpec.java:4: verify: The prover cannot establish an assertion (Postcondition) in method f",
        ),
    ]

    payload = mod.build_residual_audit_payload(rows, "base/report.json")

    assert payload["decision"] == "continue_generation_optimization"
    assert payload["summary"]["generated_spec_issue_count"] == 1
    assert payload["summary"]["by_failure_class"] == {"spec_not_sufficient": 1}
    assert payload["summary"]["by_actionability"] == {"generated_spec_issue": 1}
    assert payload["summary"]["by_recommendation"] == {"continue_spec_generation_optimization": 1}
    assert payload["rows"][0]["case"] == "NeedsSpec"


def test_cmd_audit_residuals_writes_stop_decision_for_source_boundaries(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "Solve.java"
    report_dir = tmp_path / "base"
    out_dir = tmp_path / "audit"
    rows = [
        mod.CaseRow("Pass", "Pass.java", "", "passed", True, 1, 0.1, "", "", ""),
        mod.CaseRow(
            "Solve",
            str(source),
            "",
            "verification_failed",
            False,
            1,
            0.1,
            "",
            "",
            "",
            source_preflight_status="verification_failed",
            source_preflight_failure_reason="PossiblyDivideByZero",
        ),
    ]
    mod.write_report(report_dir, rows)

    args = SimpleNamespace(input_report=str(report_dir / "report.json"), output=str(out_dir))
    assert mod.cmd_audit_residuals(args) == 0

    data = mod.json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert data["decision"] == "stop_generation_optimization_low_marginal_gain"
    assert data["summary"]["source_or_tool_boundary_count"] == 1
    assert data["summary"]["generated_spec_issue_count"] == 0
    assert data["summary"]["by_failure_class"] == {"source_safety_obligation": 1}
    assert data["summary"]["by_actionability"] == {"source_or_tool_boundary": 1}
    assert "Further JML-generation sweeps are unlikely" in summary
    assert summary.count("Failure-reason counts") == 1


def test_annotate_report_merges_multiple_source_preflight_reports(tmp_path: Path):
    mod = load_runner()
    source_a = tmp_path / "A.java"
    source_slow = tmp_path / "Slow.java"
    source_a.write_text("class A { void f() { assert false; } }\n", encoding="utf-8")
    source_slow.write_text("class Slow { void f() {} }\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source_a),
                        "oracle": "",
                        "status": "verification_failed",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                        "error": "A.java:1: verify: The prover cannot establish an assertion (Assert) in method f",
                    },
                    {
                        "case": "Slow",
                        "source": str(source_slow),
                        "oracle": "",
                        "status": "timeout",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 35.0,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                        "error": "openjml wall-clock timeout after 35s",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    timeout_preflight = tmp_path / "timeout_preflight.json"
    timeout_preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "status": "timeout",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "A_timeout.out"),
                    },
                    {
                        "case": "Slow",
                        "status": "timeout",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "Slow_timeout.out"),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    assert_preflight = tmp_path / "assert_preflight.json"
    assert_preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "status": "verification_failed",
                        "has_assert_failure": True,
                        "output_path": str(tmp_path / "A_assert.out"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(report),
        output=str(tmp_path / "annotated"),
        source_preflight_report=[str(timeout_preflight), str(assert_preflight)],
    )

    assert mod.cmd_annotate_report(args) == 0

    data = mod.json.loads((tmp_path / "annotated" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["by_failure_class"] == {
        "source_assert_failure": 1,
        "source_openjml_timeout": 1,
    }
    rows = {row["case"]: row for row in data["rows"]}
    assert rows["A"]["source_preflight_assert_failure"] is True
    assert rows["A"]["source_preflight_output_path"].endswith("A_assert.out")
    assert rows["Slow"]["source_preflight_status"] == "timeout"


def test_source_preflight_metadata_recovers_reason_from_legacy_first_line(tmp_path: Path):
    mod = load_runner()
    preflight = tmp_path / "legacy_preflight.json"
    preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "status": "verification_failed",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "A.out"),
                        "first_line": (
                            "/tmp/A.java:8: verify: The prover cannot establish an assertion "
                            "(Assert) in method f"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    metadata = mod.load_source_preflight_metadata(preflight)

    assert metadata["A"]["source_preflight_failure_reason"] == "Assert"
    assert metadata["A"]["source_preflight_assert_failure"] is True


def test_source_preflight_metadata_first_line_reason_refines_legacy_assert_flag(tmp_path: Path):
    mod = load_runner()
    preflight = tmp_path / "legacy_preflight.json"
    preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Ctor",
                        "status": "verification_failed",
                        "has_assert_failure": True,
                        "output_path": str(tmp_path / "Ctor.out"),
                        "first_line": (
                            "/tmp/Ctor.java:13: verify: The prover cannot establish an assertion "
                            "(NullField) in method A"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    metadata = mod.load_source_preflight_metadata(preflight)

    assert metadata["Ctor"]["source_preflight_failure_reason"] == "NullField"
    assert metadata["Ctor"]["source_preflight_assert_failure"] is False


def test_source_preflight_metadata_prefers_concrete_safety_over_timeout(tmp_path: Path):
    mod = load_runner()
    safety_preflight = tmp_path / "safety_preflight.json"
    safety_preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "InsertionSort",
                        "status": "verification_failed",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "safety.out"),
                        "first_line": (
                            "/tmp/InsertionSort.java:7: verify: The prover cannot establish an assertion "
                            "(PossiblyNegativeIndex) in method sort"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    timeout_preflight = tmp_path / "timeout_preflight.json"
    timeout_preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "InsertionSort",
                        "status": "timeout",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "timeout.out"),
                        "failure_reason": "OpenJMLTimeout",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    metadata = mod.load_source_preflight_metadata([timeout_preflight, safety_preflight])

    assert metadata["InsertionSort"]["source_preflight_status"] == "verification_failed"
    assert metadata["InsertionSort"]["source_preflight_failure_reason"] == "PossiblyNegativeIndex"
    assert metadata["InsertionSort"]["source_preflight_output_path"].endswith("safety.out")


def test_source_preflight_metadata_keeps_concrete_timeout_reason(tmp_path: Path):
    mod = load_runner()
    concrete_timeout = tmp_path / "concrete_timeout.json"
    concrete_timeout.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Trie",
                        "status": "timeout",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "concrete.out"),
                        "failure_reason": "NullField",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    plain_timeout = tmp_path / "plain_timeout.json"
    plain_timeout.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Trie",
                        "status": "timeout",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "plain.out"),
                        "failure_reason": "OpenJMLTimeout",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    empty_verification = tmp_path / "empty_verification.json"
    empty_verification.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Trie",
                        "status": "verification_failed",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "empty.out"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    metadata = mod.load_source_preflight_metadata([concrete_timeout, plain_timeout, empty_verification])

    assert metadata["Trie"]["source_preflight_status"] == "timeout"
    assert metadata["Trie"]["source_preflight_failure_reason"] == "NullField"
    assert metadata["Trie"]["source_preflight_output_path"].endswith("concrete.out")


def test_annotate_report_marks_source_tool_errors(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "Matrix.java"
    source.write_text("class Matrix { int[][] f(int[][] a) { return a; } }\n", encoding="utf-8")
    output = tmp_path / "generated_openjml.out"
    output.write_text(
        "Matrix.java:7: error: A catastrophic JML internal error occurred.\n"
        "Reason: Double rewriting of ident: i i_1 i_2\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    report.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Matrix",
                        "source": str(source),
                        "oracle": "",
                        "status": "tool_error",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": str(output),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    preflight = tmp_path / "preflight.json"
    preflight.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "Matrix",
                        "status": "source_tool_error",
                        "has_assert_failure": False,
                        "output_path": str(tmp_path / "source_preflight.out"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(report),
        output=str(tmp_path / "annotated"),
        source_preflight_report=str(preflight),
    )

    assert mod.cmd_annotate_report(args) == 0

    data = mod.json.loads((tmp_path / "annotated" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["by_failure_class"] == {"source_frontend_or_tool": 1}
    assert data["summary"]["by_failure_reason"] == {"SourceOpenJMLToolError": 1}
    assert data["rows"][0]["source_preflight_status"] == "source_tool_error"


def test_overlay_report_replaces_matching_rows(tmp_path: Path):
    mod = load_runner()
    source_a = tmp_path / "A.java"
    source_b = tmp_path / "B.java"
    source_a.write_text("class A { int f() { return 1; } }\n", encoding="utf-8")
    source_b.write_text("class B { int f() { return 2; } }\n", encoding="utf-8")
    base = tmp_path / "base.json"
    base.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source_a),
                        "oracle": "",
                        "status": "verification_failed",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                    },
                    {
                        "case": "B",
                        "source": str(source_b),
                        "oracle": "",
                        "status": "passed",
                        "passed": True,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source_a),
                        "oracle": "",
                        "status": "passed",
                        "passed": True,
                        "iterations": 1,
                        "runtime_s": 0.2,
                        "final_annotated_path": "new/A.java",
                        "report_path": "new/report.json",
                        "openjml_output_path": "new/openjml.out",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(base),
        output=str(tmp_path / "out"),
        overlay_report=[str(overlay)],
        source_preflight_report=[],
    )

    assert mod.cmd_overlay_report(args) == 0

    data = mod.json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["passed"] == 2
    rows = {row["case"]: row for row in data["rows"]}
    assert rows["A"]["final_annotated_path"] == "new/A.java"
    overlay_sources = mod.json.loads((tmp_path / "out" / "overlay_sources.json").read_text(encoding="utf-8"))
    assert overlay_sources["replaced"] == {"A": str(overlay)}


def test_overlay_report_can_preserve_base_trial_stats(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "A.java"
    source.write_text("class A { int f() { return 1; } }\n", encoding="utf-8")
    base = tmp_path / "base.json"
    base.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source),
                        "oracle": "",
                        "status": "verification_failed",
                        "passed": False,
                        "iterations": 25,
                        "runtime_s": 100.0,
                        "final_annotated_path": "old/A.java",
                        "report_path": "old/report.json",
                        "openjml_output_path": "old/openjml.out",
                        "trials": 10,
                        "trial_passes": 0,
                        "trial_status_counts": {"verification_failed": 10},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source),
                        "oracle": "",
                        "status": "source_changed",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 1.0,
                        "final_annotated_path": "new/A.java",
                        "report_path": "new/report.json",
                        "openjml_output_path": "new/openjml.out",
                        "trials": 1,
                        "trial_passes": 0,
                        "trial_status_counts": {"source_changed": 1},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(base),
        output=str(tmp_path / "out"),
        overlay_report=[str(overlay)],
        source_preflight_report=[],
        require_clean_passing_jml=False,
        preserve_base_trial_stats=True,
    )

    assert mod.cmd_overlay_report(args) == 0

    data = mod.json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    row = data["rows"][0]
    assert row["status"] == "source_changed"
    assert row["trials"] == 10
    assert row["iterations"] == 25
    assert row["runtime_s"] == 100.0
    assert row["final_annotated_path"] == "new/A.java"
    assert data["summary"]["trial_total"] == 10


def test_cmd_source_preflight_filters_and_writes_report(tmp_path: Path, monkeypatch):
    mod = load_runner()
    source_a = tmp_path / "A.java"
    source_b = tmp_path / "B.java"
    source_a.write_text("class A { void f() { assert false; } }\n", encoding="utf-8")
    source_b.write_text("class B {}\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source_a),
                        "oracle": "",
                        "status": "verification_failed",
                        "passed": False,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                        "failure_class": "spec_not_sufficient",
                        "failure_reason": "Assert",
                    },
                    {
                        "case": "B",
                        "source": str(source_b),
                        "oracle": "",
                        "status": "passed",
                        "passed": True,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": "",
                        "report_path": "",
                        "openjml_output_path": "",
                        "failure_class": "passed",
                        "failure_reason": "",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_run_openjml(*_args, **_kwargs):
        return SimpleNamespace(
            status="verification_failed",
            passed=False,
            returncode=1,
            runtime_s=0.1,
            stdout="A.java:1: verify: The prover cannot establish an assertion (Assert) in method f\n",
        )

    monkeypatch.setattr(mod, "run_openjml", fake_run_openjml)
    args = SimpleNamespace(
        input_report=str(report),
        output=str(tmp_path / "preflight"),
        cases=None,
        statuses=None,
        failure_classes=["spec_not_sufficient"],
        failure_reasons=["Assert"],
        limit=None,
        openjml_path="openjml",
        openjml_timeout=5,
    )

    assert mod.cmd_source_preflight(args) == 0

    data = mod.json.loads((tmp_path / "preflight" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 1
    assert data["summary"]["assert_failures"] == 1
    assert data["rows"][0]["case"] == "A"
    assert data["rows"][0]["has_assert_failure"] is True
    assert data["rows"][0]["failure_reason"] == "Assert"


def test_sanitize_report_payload_redacts_provider_error_details():
    mod = load_runner()
    payload = {
        "error": (
            "HTTP 403 Forbidden: {\"error\":{\"message\":\"Key limit " "exceeded "
            "(total limit). Manage it using "
            "https://openrouter.ai/workspaces/default/" "keys/abc123\","
            "\"code\":403},\"user_id\":\"user_abc123\"}"
        ),
        "nested": ["sk-" + "a" * 40],
    }

    sanitized = mod.sanitize_report_payload(payload)
    text = str(sanitized)

    assert "Key limit " + "exceeded" not in text
    assert "workspaces/default/" + "keys" not in text
    assert "user_abc123" not in text
    assert "sk-" + "a" * 40 not in text
    assert "Provider quota limit exceeded" in text


def test_classify_runner_exception_detects_provider_failures():
    mod = load_runner()

    assert mod.classify_runner_exception(Exception("not a valid model ID"))[0] == "llm_config_error"
    assert mod.classify_runner_exception(Exception("HTTP 403 Forbidden: quota"))[0] == "llm_unavailable"
    assert mod.classify_runner_exception(Exception("HTTP 429 rate limit"))[0] == "llm_rate_limited"
    assert mod.classify_runner_exception(Exception("local bug"))[0] == "runner_error"


def test_resume_completion_treats_preflight_skipped_rows_as_terminal():
    mod = load_runner()
    skipped = mod.CaseRow(
        "Bad",
        "Bad.java",
        "",
        "source_invalid",
        False,
        0,
        0.1,
        "",
        "",
        "source_preflight.out",
        attempts=0,
        trials=0,
        trial_passes=0,
    )
    partial_trials = mod.CaseRow(
        "A",
        "A.java",
        "",
        "verification_failed",
        False,
        2,
        1.0,
        "a",
        "r",
        "o",
        trials=2,
        trial_passes=0,
    )
    complete_trials = mod.CaseRow(
        "B",
        "B.java",
        "",
        "passed",
        True,
        3,
        1.0,
        "b",
        "r",
        "o",
        trials=3,
        trial_passes=1,
    )

    assert mod.row_satisfies_requested_run(skipped, 10) is True
    assert mod.row_satisfies_requested_run(partial_trials, 3) is False
    assert mod.row_satisfies_requested_run(complete_trials, 3) is True


def test_load_specgen_4shot_examples_from_artifact_layout(tmp_path: Path):
    mod = load_runner()
    root = tmp_path / "SpecGen-Artifact"
    specgen_bench = root / "benchmark" / "SpecGenBench" / "common"
    svcomp_bench = root / "benchmark" / "SVCOMP"
    specgen_bench.mkdir(parents=True)
    svcomp_bench.mkdir(parents=True)
    for rel in [
        ("prompts/1/1", "class Neg {}\n"),
        ("prompts/1/1_reply", "class Neg { //@ ensures true; }\n"),
        ("prompts/2/1", "class Add {}\n"),
        ("prompts/2/2_reply", "class Add { //@ ensures true; }\n"),
        ("prompts/oracle_clean/AddLoop/AddLoop.java", "class AddLoop {}\n"),
        ("prompts/oracle/AddLoop/AddLoop.java", "class AddLoop { //@ ensures true; }\n"),
        ("prompts/oracle_clean/LinearSearch/LinearSearch.java", "class LinearSearch {}\n"),
        ("prompts/oracle/LinearSearch/LinearSearch.java", "class LinearSearch { //@ ensures true; }\n"),
    ]:
        path = root / rel[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel[1])

    examples = mod.load_prompt_examples("specgen-4shot", specgen_bench)
    svcomp_examples = mod.load_prompt_examples("specgen-4shot", svcomp_bench)

    assert examples.count("Example ") == 8
    assert "class Neg" in examples
    assert "class LinearSearch" in examples
    assert svcomp_examples == examples


def test_load_specgen_4shot_linked_appends_linked_structure_example(tmp_path: Path):
    mod = load_runner()
    root = tmp_path / "SpecGen-Artifact"
    specgen_bench = root / "benchmark" / "SpecGenBench" / "common"
    specgen_bench.mkdir(parents=True)
    for rel in [
        ("prompts/1/1", "class Neg {}\n"),
        ("prompts/1/1_reply", "class Neg { //@ ensures true; }\n"),
        ("prompts/2/1", "class Add {}\n"),
        ("prompts/2/2_reply", "class Add { //@ ensures true; }\n"),
        ("prompts/oracle_clean/AddLoop/AddLoop.java", "class AddLoop {}\n"),
        ("prompts/oracle/AddLoop/AddLoop.java", "class AddLoop { //@ ensures true; }\n"),
        ("prompts/oracle_clean/LinearSearch/LinearSearch.java", "class LinearSearch {}\n"),
        ("prompts/oracle/LinearSearch/LinearSearch.java", "class LinearSearch { //@ ensures true; }\n"),
    ]:
        path = root / rel[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel[1])

    examples = mod.load_prompt_examples("specgen-4shot-linked", specgen_bench)

    assert examples.count("Example ") == 10
    assert "insertAfter" in examples
    assert "data > value ==> next != null" in examples
    assert "public /*@ nullable @*/ Node next;" in examples


def test_aggregate_trial_rows_records_success_probability():
    mod = load_runner()
    case = mod.SpecGenCase("A", "/tmp/A.java", "")
    trials = [
        mod.CaseRow("A", "/tmp/A.java", "", "verification_failed", False, 2, 1.0, "a", "r1", "o1"),
        mod.CaseRow(
            "A",
            "/tmp/A.java",
            "",
            "passed",
            True,
            1,
            2.0,
            "b",
            "r2",
            "o2",
            source_preflight_status="verification_failed",
            source_preflight_assert_failure=True,
            source_preflight_output_path="/tmp/source_preflight.out",
            source_preflight_failure_reason="Assert",
        ),
    ]

    row = mod.aggregate_trial_rows(case, trials)

    assert row.passed is True
    assert row.status == "passed"
    assert row.trials == 2
    assert row.trial_passes == 1
    assert row.trial_status_counts == {"passed": 1, "verification_failed": 1}
    assert len(row.trial_rows) == 2
    assert row.source_preflight_status == "verification_failed"
    assert row.source_preflight_assert_failure is True
    assert row.source_preflight_output_path == "/tmp/source_preflight.out"
    assert row.source_preflight_failure_reason == "Assert"


def test_source_preflight_feedback_context_writes_prompt_context(tmp_path: Path, monkeypatch):
    mod = load_runner()
    source = tmp_path / "A.java"
    source.write_text("class A { void f() { assert false; } }\n", encoding="utf-8")
    case = mod.SpecGenCase("A", str(source), "")
    args = SimpleNamespace(
        output=str(tmp_path / "out"),
        source_preflight_feedback=True,
        preflight_timeout=10,
        openjml_timeout=20,
    )
    config = SimpleNamespace(openjml_path="/fake/openjml")

    def fake_run_openjml(*_args, **_kwargs):
        return SimpleNamespace(
            status="verification_failed",
            passed=False,
            returncode=1,
            runtime_s=0.1,
            stdout="A.java:1: verify: The prover cannot establish an assertion (Assert) in method f",
            stderr="",
            error="",
        )

    monkeypatch.setattr(mod, "run_openjml", fake_run_openjml)

    feedback = mod.source_preflight_feedback_context(case, args, config, "A/trial_1")

    assert feedback["status"] == "verification_failed"
    assert feedback["assert_failure"] is True
    assert feedback["failure_reason"] == "Assert"
    assert "Unannotated-source OpenJML status: verification_failed" in feedback["context"]
    assert Path(feedback["output_path"]).is_file()


def test_write_report_prefers_clean_passing_trial_artifact(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "A.java"
    generated_assume = tmp_path / "A_assume.java"
    generated_clean = tmp_path / "A_clean.java"
    source.write_text("class A { int f(int x) { return x; } }\n", encoding="utf-8")
    generated_assume.write_text(
        "class A { int f(int x) {\n //@ assume x >= 0;\n return x; } }\n",
        encoding="utf-8",
    )
    generated_clean.write_text(
        "class A {\n //@ ensures \\result == x;\n int f(int x) { return x; } }\n",
        encoding="utf-8",
    )
    row = mod.CaseRow(
        "A",
        str(source),
        "",
        "passed",
        True,
        2,
        2.0,
        str(generated_assume),
        "assume/report.json",
        "assume/openjml.out",
        trial_rows=[
            {
                "case": "A",
                "source": str(source),
                "status": "passed",
                "passed": True,
                "final_annotated_path": str(generated_assume),
                "report_path": "assume/report.json",
                "openjml_output_path": "assume/openjml.out",
            },
            {
                "case": "A",
                "source": str(source),
                "status": "passed",
                "passed": True,
                "final_annotated_path": str(generated_clean),
                "report_path": "clean/report.json",
                "openjml_output_path": "clean/openjml.out",
            },
        ],
    )

    mod.write_report(tmp_path / "report", [row])

    data = mod.json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
    out_row = data["rows"][0]
    assert out_row["final_annotated_path"] == str(generated_clean)
    assert out_row["clean_jml_artifact"] is True
    assert out_row["generated_assert_assume_count"] == 0
    trial_by_path = {trial["final_annotated_path"]: trial for trial in out_row["trial_rows"]}
    assert trial_by_path[str(generated_assume)]["generated_assert_assume_count"] == 1
    assert trial_by_path[str(generated_assume)]["clean_jml_artifact"] is False


def test_overlay_report_can_require_clean_passing_jml(tmp_path: Path):
    mod = load_runner()
    source = tmp_path / "A.java"
    generated_assume = tmp_path / "A_assume.java"
    source.write_text("class A { int f(int x) { return x; } }\n", encoding="utf-8")
    generated_assume.write_text(
        "class A { int f(int x) {\n //@ assume x >= 0;\n return x; } }\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    report.write_text(
        mod.json.dumps(
            {
                "rows": [
                    {
                        "case": "A",
                        "source": str(source),
                        "oracle": "",
                        "status": "passed",
                        "passed": True,
                        "iterations": 1,
                        "runtime_s": 0.1,
                        "final_annotated_path": str(generated_assume),
                        "report_path": "assume/report.json",
                        "openjml_output_path": "assume/openjml.out",
                        "trial_rows": [
                            {
                                "case": "A",
                                "source": str(source),
                                "status": "passed",
                                "passed": True,
                                "final_annotated_path": str(generated_assume),
                                "report_path": "assume/report.json",
                                "openjml_output_path": "assume/openjml.out",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(report),
        output=str(tmp_path / "out"),
        overlay_report=[str(report)],
        source_preflight_report=[],
        require_clean_passing_jml=True,
    )

    assert mod.cmd_overlay_report(args) == 0

    data = mod.json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    out_row = data["rows"][0]
    assert data["summary"]["passed"] == 0
    assert out_row["status"] == "verification_failed"
    assert out_row["failure_reason"] == "GeneratedAssertAssumeOnly"


def test_should_stop_attempts_for_repeated_low_information_failures():
    mod = load_runner()

    assert mod.should_stop_attempts(["llm_unavailable"]) is True
    assert mod.should_stop_attempts(["llm_config_error"]) is True
    assert mod.should_stop_attempts(["tool_error"]) is True
    assert mod.should_stop_attempts(["timeout"]) is False
    assert mod.should_stop_attempts(["timeout", "timeout"]) is True
    assert mod.should_stop_attempts(["annotation_error", "annotation_error"]) is False
    assert mod.should_stop_attempts(["annotation_error", "annotation_error", "annotation_error"]) is True
    assert mod.should_stop_attempts(["verification_failed"] * 5) is False


def test_source_preflight_status_classifies_frontend_errors():
    mod = load_runner()

    missing_symbol = SimpleNamespace(
        status="annotation_error",
        stdout="X.java:1: error: cannot find symbol\n",
        stderr="",
        error="",
    )
    internal_error = SimpleNamespace(
        status="tool_error",
        stdout="X.java:1: error: A catastrophic JML internal error occurred.\n",
        stderr="",
        error="",
    )
    verification_failure = SimpleNamespace(
        status="verification_failed",
        stdout="X.java:1: verify: The prover cannot establish an assertion (Postcondition) in method f\n",
        stderr="",
        error="",
    )

    assert mod.source_preflight_status(missing_symbol)[0] == "source_invalid"
    assert mod.source_preflight_status(internal_error)[0] == "source_tool_error"
    assert mod.source_preflight_status(verification_failure) is None


def test_preflight_source_case_writes_skip_artifact(tmp_path: Path, monkeypatch):
    mod = load_runner()
    source = tmp_path / "Bad.java"
    source.write_text("class Bad { Missing x; int f() { return Verifier.nondetInt(); } }\n")
    case = mod.SpecGenCase("Bad", str(source), "")
    args = SimpleNamespace(
        model="",
        provider="",
        base_url="",
        openjml_path="openjml",
        openjml_timeout=1,
        max_iterations=1,
        prompt_examples="none",
        bench_root=str(tmp_path),
        output=str(tmp_path / "out"),
        attempts=5,
        validate_oracle=False,
        preflight_timeout=7,
    )
    config = mod.configure(args)
    seen: dict[str, object] = {}

    def fake_run_openjml(source_arg, *_args, **kwargs):
        seen["timeout"] = kwargs["timeout_s"]
        seen["cwd"] = str(kwargs["cwd"])
        seen["source"] = str(source_arg)
        return SimpleNamespace(
            status="annotation_error",
            stdout="Bad.java:1: error: cannot find symbol\n",
            stderr="",
            error="",
            runtime_s=0.25,
        )

    monkeypatch.setattr(mod, "run_openjml", fake_run_openjml)

    row = mod.preflight_source_case(case, args, config)

    assert row is not None
    assert row.status == "source_invalid"
    assert row.attempts == 0
    assert row.trials == 0
    assert row.trial_passes == 0
    assert seen["timeout"] == 7
    assert Path(str(seen["cwd"])).name == "source_preflight"
    assert Path(str(seen["source"])).parent == Path(str(seen["cwd"]))
    assert (Path(str(seen["cwd"])) / "Verifier.java").exists()
    assert Path(row.openjml_output_path).read_text() == "Bad.java:1: error: cannot find symbol\n"


def test_preflight_timeout_falls_back_to_openjml_timeout():
    mod = load_runner()
    args = SimpleNamespace(openjml_timeout=33)

    assert mod.preflight_timeout(args) == 33


def test_run_one_reports_actual_attempts_after_early_stop(tmp_path: Path, monkeypatch):
    mod = load_runner()
    source = tmp_path / "A.java"
    source.write_text("class A {}\n")
    case = mod.SpecGenCase("A", str(source), "")

    def fake_run_jml_specs_bench(*_args, **_kwargs):
        return SimpleNamespace(
            status="tool_error",
            passed=False,
            iterations=[
                SimpleNamespace(openjml_output_path=str(tmp_path / "openjml.out")),
            ],
            runtime_s=1.0,
            final_annotated_path=str(tmp_path / "A.java"),
            report_path=str(tmp_path / "jml_result.json"),
            error="internal OpenJML error",
            jml_clause_counts={},
        )

    monkeypatch.setattr(mod, "run_jml_specs_bench", fake_run_jml_specs_bench)
    args = SimpleNamespace(
        model="",
        provider="",
        base_url="",
        openjml_path="openjml",
        openjml_timeout=1,
        max_iterations=1,
        prompt_examples="none",
        bench_root=str(tmp_path),
        output=str(tmp_path / "out"),
        attempts=5,
        validate_oracle=False,
    )

    row = mod.run_one(case, args)

    assert row.status == "tool_error"
    assert row.attempts == 1


def test_run_one_trial_classifies_provider_exception(tmp_path: Path, monkeypatch):
    mod = load_runner()
    source = tmp_path / "A.java"
    source.write_text("class A {}\n")
    case = mod.SpecGenCase("A", str(source), "")

    def fake_run_jml_specs_bench(*_args, **_kwargs):
        raise RuntimeError("HTTP 403 Forbidden: " + "Key limit " + "exceeded (total limit)")

    monkeypatch.setattr(mod, "run_jml_specs_bench", fake_run_jml_specs_bench)
    args = SimpleNamespace(
        model="",
        provider="",
        base_url="",
        openjml_path="openjml",
        openjml_timeout=1,
        max_iterations=1,
        prompt_examples="none",
        bench_root=str(tmp_path),
        output=str(tmp_path / "out"),
    )

    row = mod.run_one_trial(case, args, 1)

    assert row.status == "llm_unavailable"
    assert row.trials == 1


def test_mark_remaining_llm_error_preserves_partial_trials(tmp_path: Path):
    mod = load_runner()
    cases = [
        mod.SpecGenCase("A", "/tmp/A.java", ""),
        mod.SpecGenCase("B", "/tmp/B.java", ""),
    ]
    args = SimpleNamespace(
        model="",
        provider="",
        base_url="",
        openjml_path="",
        openjml_timeout=1,
        max_iterations=1,
        prompt_examples="none",
    )
    rows: dict[str, object] = {}
    trial_rows = {
        "A": [
            mod.CaseRow("A", "/tmp/A.java", "", "llm_unavailable", False, 0, 0.0, "", "", "", error="quota")
        ],
        "B": [],
    }

    mod.mark_remaining_llm_error(rows, cases, args, "llm_unavailable", "quota", trial_rows)

    assert rows["A"].status == "llm_unavailable"
    assert rows["A"].trials == 1
    assert rows["B"].status == "llm_unavailable"
    assert rows["B"].attempts == 0


def test_replay_select_rows_filters_cases():
    mod = load_replay_runner()
    rows = [{"case": "A"}, {"case": "B"}, {"case": "C"}]

    assert mod.select_rows(rows, None) == rows
    assert [row["case"] for row in mod.select_rows(rows, ["B", "C"])] == ["B", "C"]


def test_replay_select_trials_limits_trials():
    mod = load_replay_runner()
    trials = [{"trial": 1}, {"trial": 2}, {"trial": 3}]

    assert mod.select_trials(trials, None) == trials
    assert mod.select_trials(trials, 0) == trials
    assert mod.select_trials(trials, 2) == trials[:2]


def test_replay_summarize_reports_actionability_counts():
    mod = load_replay_runner()
    rows = [
        {"case": "Pass", "status": "passed", "passed": True, "failure_class": "passed", "trials": 1, "trial_passes": 1},
        {
            "case": "Spec",
            "status": "verification_failed",
            "passed": False,
            "failure_class": "spec_not_sufficient",
            "trials": 1,
            "trial_passes": 0,
        },
        {
            "case": "Tool",
            "status": "tool_error",
            "passed": False,
            "failure_class": "source_frontend_or_tool",
            "trials": 1,
            "trial_passes": 0,
        },
        {
            "case": "LLM",
            "status": "llm_unavailable",
            "passed": False,
            "failure_class": "llm_unavailable",
            "attempts": 0,
            "trials": 0,
            "trial_passes": 0,
        },
    ]

    summary = mod.summarize(rows)

    assert summary["generated_spec_issue_count"] == 1
    assert summary["source_or_tool_boundary_count"] == 1
    assert summary["llm_or_runner_issue_count"] == 1
    assert summary["by_actionability"] == {
        "generated_spec_issue": 1,
        "llm_or_runner_issue": 1,
        "passed": 1,
        "source_or_tool_boundary": 1,
    }
    assert summary["passed_with_zero_trial_passes_count"] == 0
    assert summary["passed_without_trial_stats_count"] == 0


def test_replay_summarize_reports_overlay_pass_trial_stat_mismatches():
    mod = load_replay_runner()
    rows = [
        {"case": "OverlayPass", "status": "passed", "passed": True, "failure_class": "passed", "trials": 10, "trial_passes": 0},
        {"case": "PreflightPass", "status": "passed", "passed": True, "failure_class": "passed", "trials": 0, "trial_passes": 0},
        {"case": "NormalPass", "status": "passed", "passed": True, "failure_class": "passed", "trials": 10, "trial_passes": 2},
    ]

    summary = mod.summarize(rows)

    assert summary["passed_with_zero_trial_passes_count"] == 1
    assert summary["passed_with_zero_trial_passes_cases"] == ["OverlayPass"]
    assert summary["passed_without_trial_stats_count"] == 1
    assert summary["passed_without_trial_stats_cases"] == ["PreflightPass"]


def test_replay_runner_writes_partial_report_on_interrupt(tmp_path: Path, monkeypatch):
    mod = load_replay_runner()
    input_report = tmp_path / "input.json"
    input_report.write_text(
        '{"rows":[{"case":"A","status":"verification_failed"},{"case":"B","status":"verification_failed"}]}',
        encoding="utf-8",
    )
    args = SimpleNamespace(
        input_report=str(input_report),
        output=str(tmp_path / "out"),
        workers=1,
    )
    cleaned = {"called": 0}

    def fake_process_row(index, total, row, output, args):
        if index == 2:
            raise KeyboardInterrupt
        return index, {
            "case": row["case"],
            "status": "passed",
            "passed": True,
            "iterations": 1,
            "runtime_s": 0.1,
            "trials": 1,
            "trial_passes": 1,
        }, "done"

    monkeypatch.setattr(mod, "process_row", fake_process_row)
    monkeypatch.setattr(mod, "kill_active_openjml_process_groups", lambda: cleaned.__setitem__("called", cleaned["called"] + 1))

    rc = mod.cmd_replay(args)

    report = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    assert rc == 130
    assert cleaned["called"] == 1
    assert '"case": "A"' in report
    assert '"case": "B"' not in report


def test_replay_write_report_attaches_failure_classes(tmp_path: Path):
    mod = load_replay_runner()
    rows = [
        {
            "case": "Missing",
            "source": "Missing.java",
            "oracle": "",
            "status": "annotation_error",
            "passed": False,
            "iterations": 1,
            "runtime_s": 0.1,
            "final_annotated_path": "",
            "report_path": "",
            "openjml_output_path": "",
            "error": "Missing.java:1: error: cannot find symbol\n  symbol: variable args",
            "attempts": 1,
            "trials": 1,
            "trial_passes": 0,
        }
    ]

    mod.write_report(tmp_path / "out", rows, tmp_path / "input.json")

    data = load_runner().json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert data["summary"]["by_failure_class"] == {"source_frontend_or_tool": 1}
    assert data["summary"]["by_failure_reason"] == {"SourceMissingSymbol": 1}
    assert data["rows"][0]["failure_class"] == "source_frontend_or_tool"
    assert data["rows"][0]["failure_reason"] == "SourceMissingSymbol"
    assert "Failure-class counts" in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")


def test_cmd_run_writes_partial_report_on_serial_interrupt(tmp_path: Path, monkeypatch):
    mod = load_runner()
    cases = [
        mod.SpecGenCase("A", str(tmp_path / "A.java"), ""),
        mod.SpecGenCase("B", str(tmp_path / "B.java"), ""),
    ]
    args = SimpleNamespace(
        bench_root=str(tmp_path),
        oracle_root="",
        cases=None,
        limit=None,
        output=str(tmp_path / "out"),
        resume=False,
        trials=1,
        workers=1,
        preflight_source=False,
    )
    cleaned = {"called": 0}

    def fake_run_one(case, args):
        if case.name == "B":
            raise KeyboardInterrupt
        return mod.CaseRow(
            case=case.name,
            source=case.source,
            oracle=case.oracle,
            status="passed",
            passed=True,
            iterations=1,
            runtime_s=0.1,
            final_annotated_path="A.java",
            report_path="jml_result.json",
            openjml_output_path="openjml.out",
        )

    monkeypatch.setattr(mod, "discover_cases", lambda bench_root, oracle_root=None: cases)
    monkeypatch.setattr(mod, "run_one", fake_run_one)
    monkeypatch.setattr(mod, "kill_active_openjml_process_groups", lambda: cleaned.__setitem__("called", cleaned["called"] + 1))

    rc = mod.cmd_run(args)

    report = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    assert rc == 130
    assert cleaned["called"] == 1
    assert '"case": "A"' in report
    assert '"case": "B"' not in report
