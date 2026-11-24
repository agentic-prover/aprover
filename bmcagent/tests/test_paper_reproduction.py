"""Guards for scientific configuration isolation and honest result collection."""
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "experiments/paper_reproduction" / (name + ".py")
    spec = importlib.util.spec_from_file_location("paper_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_aws_profile_cannot_inherit_ldv_strategy_or_role_routing(monkeypatch, tmp_path):
    run = load("run")
    monkeypatch.setenv("BMC_FRAME_HAVOC", "1")
    monkeypatch.setenv("SVCOMP_ARCH", "ILP32")
    monkeypatch.setenv("BMC_AGENT_LLM_SPEC_GEN_MODEL", "unrelated-model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    args = SimpleNamespace(base_url="http://127.0.0.1:8794/openrouter/aws/api/v1", model="",
                           cbmc_path=None, frama_c_path=None, autospec_root=None, output=tmp_path)
    env, recorded = run.child_environment(run.PROFILES["rq1-aws"], args)
    assert "BMC_FRAME_HAVOC" not in env
    assert "BMC_AGENT_LLM_SPEC_GEN_MODEL" not in env
    assert env["SVCOMP_ARCH"] == "LP64"
    assert env["SVCOMP_PROP"] == "unreach"
    assert "test-key" not in json.dumps(recorded)


def test_no_report_is_unknown_after_solver_timeout(tmp_path):
    collect = load("collect").collect
    manifest = {"profile": "rq1-aws", "pilot": True, "execution_kind": "fresh_generation",
                "source": {"commit": "test"}, "trials": 1,
                "settings": {"kind": "rq1", "strategy": "faithful_main"},
                "cases": [{"name": "task", "expected_verdict": "true"}]}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "execution.json").write_text(json.dumps([{"name": "task", "wall_s": 2, "returncode": 124}]))
    case = tmp_path / "cases/task"
    case.mkdir(parents=True)
    (case / "run.log").write_text("Pipeline END: 0 real bug(s), 0 latent, 0 unresolved\n")
    result = collect(tmp_path)
    assert result["rows"][0]["verdict"] == "timeout"


@pytest.mark.parametrize("returncode,summary,final_count,suppressed,completed", [
    (0, "=== Results ===\n(suppressed 1 stale finding(s): rejected by realism check or re-classified as spurious after refinement)\nNo bugs confirmed.\n", 0, 1, True),
    (0, "=== Results ===\nConfirmed bugs: 1\n", 1, 0, True),
    (124, "", None, None, False),
    (0, "Confirmed bugs: 99\n", None, None, False),
    (124, "=== Results ===\nConfirmed bugs: 1\n", 1, 0, False),
])
def test_rq1_separates_witness_completion_and_final_reports(tmp_path, returncode, summary, final_count, suppressed, completed):
    collect = load("collect").collect
    manifest = {"profile": "rq1-ldv", "pilot": True, "execution_kind": "fresh_generation",
                "source": {"commit": "test"}, "trials": 1,
                "settings": {"kind": "rq1", "strategy": "public_ldv_fallback"},
                "cases": [{"name": "task", "expected_verdict": "false"}]}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "execution.json").write_text(json.dumps([{"name": "task", "wall_s": 2, "returncode": returncode}]))
    case = tmp_path / "cases/task"
    path = case / "artifacts/agdrv/main/cbmc_result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"result": {"verified": False, "counterexamples": [
        {"failing_property": "reach_error.assertion.1"}]}}))
    # Saved report files and the reporter's later summary may both be stale.
    (path.parent / "bug_report.json").write_text(json.dumps({"report": {"confidence": "unlikely"}}))
    (case / "run.log").write_text("Pipeline END: 1 real bug(s), 0 latent, 0 unresolved\n" + summary +
                                  "\n=== AMC Bug Report Summary: agdrv ===\nTotal bugs found: 1\n")
    result = collect(tmp_path)
    row = result["rows"][0]
    assert row["pipeline_report_count"] == 1
    assert row["final_report_count"] == final_count
    assert row["final_filter_suppressed_report_count"] == suppressed
    assert row["pipeline_completed"] is completed
    assert row["reach_error_properties"] == ["reach_error.assertion.1"]
    assert row["verdict"] == ("timeout" if returncode == 124 else "abstract_bug_candidate")
    assert result["report_counts"]["cases_with_reach_error_witness"] == 1
    assert result["report_counts"]["cases_missing_final_report_count"] == int(final_count is None)
    assert result["report_counts"]["pipeline_completed_cases"] == int(completed)


def test_jml_profiles_keep_historical_temperature_and_generation_budget():
    run = load("run")
    for profile in run.PROFILES.values():
        if profile["kind"] == "jml":
            assert (profile["temperature"], profile["trials"], profile["max_iterations"]) == (0.4, 10, 10)


def test_saved_solver_envelope_keeps_abstract_candidate_distinct(tmp_path):
    collect = load("collect").collect
    manifest = {"profile": "rq1-ldv", "pilot": True, "execution_kind": "fresh_generation",
                "source": {"commit": "test"}, "trials": 1,
                "settings": {"kind": "rq1", "strategy": "public_ldv_fallback"},
                "cases": [{"name": "task", "expected_verdict": "false"}]}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "execution.json").write_text(json.dumps([{"name": "task", "wall_s": 2, "returncode": 0}]))
    path = tmp_path / "cases/task/artifacts/agdrv/main/cbmc_result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"saved_at": "test", "result": {"verified": False, "counterexamples": [
        {"failing_property": "reach_error.assertion.1"}]}}))
    result = collect(tmp_path)
    assert result["rows"][0]["verdict"] == "abstract_bug_candidate"
    assert result["rows"][0]["unwinding_complete"] is None


@pytest.mark.parametrize("backend", ["cbmc", "esbmc"])
def test_guarded_ldv_collects_target_evidence_and_completion_separately(tmp_path, backend):
    collector = load("collect")
    target = collector.LDV_TARGET
    expected = {"confirmed": "target_feasibility_confirmed", "unresolved": "unresolved",
                "candidate": "target_candidate", "other_property": "no_target_candidate",
                "harness_error": "tool_error", "timeout": "timeout", "killed": "censored",
                "not_started": "not_run"}
    manifest = {"profile": "rq1-ldv-guarded", "pilot": True, "execution_kind": "fresh_generation",
                "source": {"commit": "test"}, "trials": 1, "c_backend": backend,
                "settings": {"kind": "rq1", "strategy": "ldv_property_guard"},
                "cases": [{"name": n, "expected_verdict": "false"} for n in expected]}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    executions = []
    for name in expected:
        if name == "not_started":
            continue
        executions.append({"name": name, "wall_s": 1,
                           "returncode": -15 if name in ("timeout", "killed") else 0,
                           "completion": "case_timeout" if name == "timeout" else "process_finished"})
        main = tmp_path / "cases" / name / "artifacts/agdrv/main"
        main.mkdir(parents=True)
        if name == "harness_error":
            (main / "bug_report.json").write_text(json.dumps({"report": {
                "function_name": "main", "error": "harness generation failed", "verified": False}}))
            continue
        prop = "poll_wait.pointer_dereference.1" if name == "other_property" else target
        (main / "cbmc_result.json").write_text(json.dumps({"result": {
            "verified": False, "counterexamples": [{"failing_property": prop}]}}))
        if name in ("confirmed", "unresolved", "other_property"):
            (main / "classifications").mkdir()
            (main / "bug_reports").mkdir()
            (main / "classifications" / f"{prop}.json").write_text(json.dumps({"classification": {
                "counterexample": {"failing_property": prop},
                "outcome": "unresolved" if name == "unresolved" else "real_bug",
                "reasoning": "Callee feasibility confirmed" if name != "unresolved" else "inconclusive",
                "dynamic_result": {"outcome": "inconclusive"}}}))
            (main / "bug_reports" / f"{prop}.json").write_text(json.dumps({"report": {
                "violated_property": prop, "confidence": "confirmed_system_entry"}}))
    (tmp_path / "execution.json").write_text(json.dumps(executions))
    result = collector.collect(tmp_path)
    assert {r["case"]: r["verdict"] for r in result["rows"]} == expected
    assert result["target_evidence_counts"] == {
        "target_candidate": 5, "target_feasibility_confirmed": 1, "unresolved": 1}
    assert result["rows"][0]["dynamic_outcome"] == "inconclusive"


def test_inspected_plan_can_execute_without_changing_configuration(tmp_path, monkeypatch):
    run = load("run")
    monkeypatch.syspath_prepend(str(ROOT / "experiments/paper_reproduction"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-placeholder")
    name = "aws_add_size_saturating_harness"
    lock = json.loads((ROOT / "experiments/paper_reproduction/benchmark_inputs.json").read_text())
    case = {"name": name, "sha256": lock["rq1-aws"][name], "expected_verdict": "true"}
    job = {"name": name, "output": str(tmp_path / "cases" / name)}
    monkeypatch.setattr(run, "source_snapshot", lambda: {"commit": "unit-test"})
    monkeypatch.setattr(run, "build_jobs", lambda *_: ([case], [job]))
    monkeypatch.setattr(run, "tool_versions", lambda *_: {"cbmc": {"version_output": "5.95.1"}})
    monkeypatch.setattr(run, "run_job", lambda *_: {"name": name, "returncode": 0, "wall_s": 0})
    argv = ["paper-run", "rq1-aws", "--output", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)
    assert run.main() == 0
    planned = json.loads((tmp_path / "run_manifest.json").read_text())["fingerprint"]
    (tmp_path / "cbmc_invocations.jsonl").write_text(json.dumps({"command": ["cbmc", "harness.c", "--unwind", "64"]}) + "\n")
    monkeypatch.setattr(sys, "argv", argv + ["--execute"])
    assert run.main() == 0
    executed = json.loads((tmp_path / "run_manifest.json").read_text())["fingerprint"]
    assert planned == executed


def test_verifier_replay_rejects_changed_proof_input(tmp_path):
    replay = load("replay_verifiers")
    source = tmp_path / "Accepted.java"
    source.write_text("class Accepted {}")
    row = {"source": str(source), "files_sha256": {source.name: replay.digest(source)}}
    source.write_text("class Accepted { void changed() {} }")
    import pytest
    with pytest.raises(ValueError, match="missing or changed"):
        replay.checked_sources(row, "jml")


def test_zero_wp_goals_cannot_reproduce_an_accepted_proof():
    replay = load("replay_verifiers")
    assert not replay.wp_result("[wp] Proved goals: 0 / 0", 0, False)["passed"]
    assert not replay.wp_result("[wp] Proved goals: 8 / 8", 0, True)["passed"]
    assert not replay.wp_result("[wp] Proved goals: 7 / 8", 0, False)["passed"]


def test_archived_relative_log_is_resolved_from_its_run_root(tmp_path):
    audit = load("audit_published")
    root = tmp_path / "relocated_run"
    log = root / "cases/task/postcheck_framac.log"
    log.parent.mkdir(parents=True)
    log.write_text("[wp] Proved goals: 2 / 2")
    assert audit.evidence_path("artifacts/relocated_run/cases/task/postcheck_framac.log", root) == log
    # An old absolute path must not hide a missing file in the archive being audited.
    old = tmp_path / "old/relocated_run/cases/task/missing.log"
    old.parent.mkdir(parents=True)
    old.write_text("stale evidence")
    assert audit.evidence_path(str(old), root) == log.parent / "missing.log"


def test_ldv_profile_controls_effective_solver_unwind(monkeypatch, tmp_path):
    from bmc_agent.cbmc import run_cbmc
    run = load("run")
    args = SimpleNamespace(base_url="http://127.0.0.1:8794/openrouter/ldv/api/v1", model="",
                           cbmc_path="/bin/true", frama_c_path=None, autospec_root=None,
                           output=tmp_path)
    env, _ = run.child_environment(run.PROFILES["rq1-ldv"], args)
    for key in ("SVCOMP_PROP", "SVCOMP_ARCH", "SVCOMP_UNWIND", "SVCOMP_TIMEOUT", "BMC_UNWIND_CAP", "BMC_BUGHUNT"):
        monkeypatch.setenv(key, env[key])
    captured = []
    def solver(command, **kwargs):
        captured.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr("bmc_agent.cbmc.subprocess.run", solver)
    run_cbmc(tmp_path / "harness.c", unwind=64, cbmc_path="/bin/true")
    command, options = captured[0]
    assert command[command.index("--unwind") + 1] == "1"
    assert "--32" in command
    assert options["timeout"] == 300


@pytest.mark.parametrize("backend,frontend", [("cbmc", False), ("esbmc", False), ("esbmc", True)])
def test_solver_recorder_preserves_pid_arguments_cwd_and_exit(tmp_path, backend, frontend):
    wrapper = ROOT / "experiments/paper_reproduction/tools" / f"{backend}-record"
    fake = tmp_path / "fake cbmc"
    fake.write_text(f"#!{sys.executable}\nimport os,sys\nprint(os.getpid())\nprint(os.getcwd())\nprint(repr(sys.argv[1:]))\nsys.exit(7)\n")
    fake.chmod(0o755)
    source = tmp_path / "input with spaces.c"
    source.write_text("int main() { return 0; }")
    log = tmp_path / "calls.jsonl"
    env = {**os.environ, f"PAPER_REPRO_{backend.upper()}_REAL": str(fake), f"PAPER_REPRO_{backend.upper()}_LOG": str(log)}
    options = ["--goto-functions-only", str(source)] if frontend else [str(source), "--unwind", "1"]
    proc = subprocess.run([str(wrapper), *options], cwd=tmp_path, env=env, text=True, capture_output=True)
    recorded = json.loads(log.read_text())
    assert proc.returncode == 7
    assert int(proc.stdout.splitlines()[0]) == recorded["pid"]
    assert proc.stdout.splitlines()[1] == str(tmp_path)
    assert recorded["command"] == [str(fake), *options]
    assert recorded["harness_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize("backend", ["cbmc", "esbmc"])
def test_result_audit_detects_effective_unwind_drift(tmp_path, backend):
    collect = load("collect").collect
    manifest = {"profile": "rq1-ldv", "pilot": True, "execution_kind": "fresh_generation",
                "source": {"commit": "test"}, "trials": 1,
                "settings": {"kind": "rq1", "strategy": "public_ldv_fallback", "unwind": 1, "arch": "ILP32"},
                "cases": [], "c_backend": backend}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / f"{backend}_invocations.jsonl").write_text(json.dumps({"command": [backend, "harness.c", "--unwind", "4", "--32"]}) + "\n")
    result = collect(tmp_path)
    assert not result["solver_configuration"]["matches_profile"]


@pytest.mark.parametrize("profile_name", ["rq1-aws", "rq1-ldv", "rq1-ldv-guarded"])
def test_esbmc_selection_reaches_rq1_jobs_and_checks_only_selected_engine(tmp_path, monkeypatch, profile_name):
    run = load("run")
    args = SimpleNamespace(base_url="http://localhost:8794/api/v1", model="",
                           cbmc_path=None, frama_c_path=None, autospec_root=None,
                           c_backend="esbmc", esbmc_path=Path("bin/esbmc"), output=tmp_path,
                           trials=None, artifact_root=tmp_path, data_dir=tmp_path, cases=None, limit=None)
    profile = {**run.PROFILES[profile_name], "tasks": 1}
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "case.i"
    source.write_text("int main(void) { return 0; }")
    task_list = tmp_path / "rq1/tier-a-aws/task_list.tsv"
    task_list.parent.mkdir(parents=True)
    task_list.write_text("case.i\ttrue\n")
    monkeypatch.setattr(run, "extract_rq1", lambda *_: tmp_path)
    monkeypatch.setenv("BMC_AGENT_C_BACKEND", "cbmc")
    monkeypatch.setenv("PAPER_REPRO_CBMC_REAL", "/unrelated/cbmc")
    env, settings = run.child_environment(profile, args)
    assert settings["BMC_AGENT_C_BACKEND"] == "esbmc"
    assert "PAPER_REPRO_CBMC_REAL" not in env
    assert "BMC_AGENT_CBMC_PATH" not in env
    assert settings["SVCOMP_ARCH"] == profile["arch"]
    if profile_name == "rq1-ldv-guarded":
        assert "BMC_UNWIND_CAP" not in settings
        assert settings["SVCOMP_UNWIND"] == settings["SVCOMP_TIMEOUT"] == "0"
        assert settings["BMC_AGENT_PRESERVE_LDV_PROPERTY"] == "true"
        assert settings["BMC_AGENT_ENABLE_BMC_CONFIG_AGENT"] == "true"
    else:
        assert settings["BMC_UNWIND_CAP"] == str(profile["unwind"])
    _, jobs = run.build_jobs(profile, args, env)
    assert jobs[0]["command"][jobs[0]["command"].index("--c-backend") + 1] == "esbmc"
    assert jobs[0]["command"][jobs[0]["command"].index("--esbmc-path") + 1] == settings["BMC_AGENT_ESBMC_PATH"]
    probes = []
    def probe(command, **kwargs):
        probes.append(command)
        return SimpleNamespace(returncode=0, stdout="ESBMC version 8.3.0", stderr="")
    monkeypatch.setattr(run.subprocess, "run", probe)
    assert set(run.tool_versions(profile, args, env)) == {"esbmc"}
    assert probes == [[str(tmp_path / "bin/esbmc"), "--version"]]
    (tmp_path / "run_manifest.json").write_text(json.dumps({
        "profile": profile_name, "pilot": True, "execution_kind": "fresh_generation",
        "source": {"commit": "test"}, "trials": 1, "settings": profile, "cases": [],
        "c_backend": "esbmc", "environment": settings}))
    # An unrelated CBMC log must not determine the selected backend's configuration.
    (tmp_path / "cbmc_invocations.jsonl").write_text(json.dumps({"command": ["cbmc", "--unwind", "999"]}) + "\n")
    (tmp_path / "esbmc_invocations.jsonl").write_text(json.dumps({
        "command": ["esbmc", str(source), "--unwind", str(profile["unwind"]),
                    "--32" if profile["arch"] == "ILP32" else "--64"]}) + "\n")
    assert load("collect").collect(tmp_path)["solver_configuration"]["matches_profile"]


@pytest.mark.parametrize("profile,options", [
    ("rq3-acsl", ["--c-backend", "esbmc"]),
    ("rq3-jml-specgen-claude", ["--esbmc-path", "/pinned/esbmc"]),
    ("rq1-ldv", ["--esbmc-path", "/pinned/esbmc"]),
])
def test_runner_rejects_inapplicable_backend_options(tmp_path, monkeypatch, capsys, profile, options):
    run = load("run")
    monkeypatch.setattr(sys, "argv", ["paper-run", profile, "--output", str(tmp_path), *options])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 2
    expected = "ESBMC selection is supported only for RQ1" if profile.startswith("rq3") else "--esbmc-path requires --c-backend esbmc"
    assert expected in capsys.readouterr().err
    assert not (tmp_path / "run_manifest.json").exists()


def test_replay_cache_miss_invalidates_compatibility_evidence(tmp_path):
    collect = load("collect").collect
    replay_log = tmp_path / "replay.jsonl"
    replay_log.write_text(json.dumps({"path": "/openrouter/test/api/v1/chat/completions", "status": 409}) + "\n")
    manifest = {"profile": "rq3-jml-specgen-claude", "pilot": True,
                "execution_kind": "saved_response_replay", "source": {"commit": "test"},
                "trials": 1, "settings": {"kind": "jml"}, "cases": [], "replay_log": str(replay_log),
                "environment": {"BMC_AGENT_LLM_DEFAULT_BASE_URL": "http://127.0.0.1:8796/openrouter/test/api/v1"}}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    result = collect(tmp_path)
    assert not result["replay_fidelity"]["verified"]
    assert result["replay_fidelity"]["cache_misses"] == 1


def test_unwinding_failure_is_not_a_safe_proof_even_with_no_reports(tmp_path):
    collect = load("collect").collect
    manifest = {"profile": "rq1-aws", "pilot": True, "execution_kind": "fresh_generation",
                "source": {"commit": "test"}, "trials": 1,
                "settings": {"kind": "rq1", "strategy": "faithful_main", "unwind": 64, "arch": "LP64"},
                "cases": [{"name": "memset", "expected_verdict": "true"}]}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "execution.json").write_text(json.dumps([{"name": "memset", "returncode": 0, "wall_s": 1}]))
    case = tmp_path / "cases/memset"
    path = case / "artifacts/agdrv/main/cbmc_result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"result": {"verified": False, "counterexamples": [
        {"failing_property": "memset_impl.unwind.0"}]}}))
    (case / "run.log").write_text("Pipeline END: 0 real bug(s), 1 latent, 0 unresolved\n")
    row = collect(tmp_path)["rows"][0]
    assert row["verdict"] == "unknown"
    assert row["bounded_property_status"] == "no_reach_error_within_bound_incomplete_unwinding"
    assert not row["unwinding_complete"]


def test_rq3_historical_filter_policy_preserves_recorded_clause_translation(monkeypatch):
    from bmc_agent.dsl_to_cbmc import postcond_to_assert
    condition = r"arr[n1] == \old(arr[n2]) && arr[n2] == \old(arr[n1]) && forall i, (0 <= i && i < n && i != n1 && i != n2) ==> arr[i] == \old(arr[i])"
    monkeypatch.setenv("BMC_AGENT_DSL_FILTER_POLICY", "2026-06-22")
    original = postcond_to_assert(condition, ["arr", "n1", "n2", "n"])
    assert original[:2] == ["assert(arr[n1] == arr[n2]);", "assert(arr[n2] == arr[n1]);"]
    monkeypatch.delenv("BMC_AGENT_DSL_FILTER_POLICY")
    modern = postcond_to_assert(condition, ["arr", "n1", "n2", "n"])
    assert modern != original


def test_historical_acsl_harness_keeps_compositional_assembly(monkeypatch, tmp_path):
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.source_parser import parse_source_file
    from bmc_agent.spec import Spec
    source = "void sibling(void) {}\nvoid main(void) {}\n"
    path = tmp_path / "example.c"
    path.write_text(source)
    parsed = parse_source_file(str(path), source_text=source)
    func = parsed.get_function_info("main")
    monkeypatch.delenv("BMC_FAITHFUL_MAIN", raising=False)
    modern = HarnessGenerator(Config()).generate_harness(func, Spec("main", "true", "true"), parsed)
    assert modern == parsed.preprocessed_source
    monkeypatch.setenv("BMC_AGENT_C_HARNESS_POLICY", "2026-06-22")
    historical = HarnessGenerator(Config.from_env()).generate_harness(func, Spec("main", "true", "true"), parsed)
    assert "Sibling placeholder (referenced by TU-scope dispatch table): sibling" in historical
    assert "/* --- Harness entry point --- */" in historical
    assert historical != modern


def test_ldv_guarded_profile_environment():
    """The validated LDV entry must not apply a cap/floor or disable the agents."""
    import importlib.util
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "experiments/paper_reproduction"
    spec = importlib.util.spec_from_file_location("paper_run", here / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    profile = run.PROFILES["rq1-ldv-guarded"]
    assert profile["strategy"] == "ldv_property_guard"
    assert profile["unwind_policy"] == "agent_may_override"
    assert profile["input_lock"] == "rq1-ldv"

    class Args:
        base_url = "https://openrouter.ai/api/v1"
        model = ""
        api_key_env = ""
        cbmc_path = None
        frama_c_path = None
        openjml_path = None
        autospec_root = None
        output = Path("/tmp/ldv-guarded-plan")

    env, settings = run.child_environment(profile, Args())
    assert settings["BMC_AGENT_ENABLE_BMC_CONFIG_AGENT"] == "true"
    assert settings["BMC_AGENT_RESPECT_INLINE_DECISIONS"] == "true"
    assert settings["BMC_AGENT_PRESERVE_LDV_PROPERTY"] == "true"
    assert settings["BMC_AGENT_STRICT_EVIDENCE_GRADING"] == "true"
    assert settings["BMC_AGENT_CBMC_UNWIND"] == "1"
    assert settings["SVCOMP_UNWIND"] == "0"
    assert settings["SVCOMP_TIMEOUT"] == "0"
    assert "BMC_UNWIND_CAP" not in settings
    assert "BMC_TRANSITIVE_INLINE" not in settings
    assert settings["unwind_policy"] == "agent_may_override"


@pytest.mark.parametrize("backend", ["cbmc", "esbmc"])
def test_collect_accepts_agent_unwind_override_only_for_guarded_profile(tmp_path, backend):
    """An agent-chosen unwind=2 is valid in the guarded profile, not elsewhere."""
    import importlib.util
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "experiments/paper_reproduction"
    spec = importlib.util.spec_from_file_location("paper_collect", here / "collect.py")
    collect_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collect_mod)

    def make(directory, unwinds, policy, profile, default=1):
        directory.mkdir(parents=True)
        # A clean guarded environment: the acceptance rule also inspects it, so
        # the synthetic manifest must describe a cap/floor-free run.
        environment = ({"BMC_AGENT_CBMC_UNWIND": str(default),
                        "SVCOMP_UNWIND": "0", "SVCOMP_TIMEOUT": "0"}
                       if policy == "agent_may_override" else {})
        (directory / "run_manifest.json").write_text(json.dumps({
            "profile": profile, "pilot": False, "execution_kind": "fresh_generation", "c_backend": backend,
            "source": {"commit": "x"}, "cases": [], "trials": 1,
            "settings": {"kind": "rq1", "unwind": default, "arch": "ILP32",
                         "unwind_policy": policy,
                         "strategy": ("ldv_property_guard" if policy == "agent_may_override"
                                      else "public_ldv_fallback")},
            "environment": environment}))
        (directory / "execution.json").write_text("[]")
        with (directory / f"{backend}_invocations.jsonl").open("w") as fh:
            for unwind in unwinds:
                fh.write(json.dumps({"command": [backend, "h.c", "--unwind",
                                                 str(unwind), "--32"]}) + "\n")

    guarded = tmp_path / "guarded"
    make(guarded, [1, 2], "agent_may_override", "rq1-ldv-guarded")
    metrics = collect_mod.collect(guarded)
    assert metrics["solver_configuration"]["matches_profile"] is True
    assert metrics["solver_configuration"]["unwinds"] == [1, 2]

    historical = tmp_path / "historical"
    make(historical, [2], "fixed", "rq1-ldv")
    assert collect_mod.collect(historical)["solver_configuration"]["matches_profile"] is False

    capped = tmp_path / "capped"
    make(capped, [1], "agent_may_override", "rq1-ldv-guarded", default=2)
    assert collect_mod.collect(capped)["solver_configuration"]["matches_profile"] is False


def test_guarded_collect_rejects_environment_cap_or_floor_leak(tmp_path):
    """A clean observed set must not hide a cap/floor recorded in the environment."""
    import importlib.util
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "experiments/paper_reproduction"
    spec = importlib.util.spec_from_file_location("paper_collect_leak", here / "collect.py")
    collect_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collect_mod)

    def make(directory, unwinds, environment):
        directory.mkdir(parents=True)
        (directory / "run_manifest.json").write_text(json.dumps({
            "profile": "rq1-ldv-guarded", "pilot": False,
            "execution_kind": "fresh_generation", "source": {"commit": "x"},
            "cases": [], "trials": 1,
            "settings": {"kind": "rq1", "unwind": 1, "arch": "ILP32",
                         "unwind_policy": "agent_may_override",
                         "strategy": "ldv_property_guard"},
            "environment": environment}))
        (directory / "execution.json").write_text("[]")
        with (directory / "cbmc_invocations.jsonl").open("w") as fh:
            for unwind in unwinds:
                fh.write(json.dumps({"command": ["cbmc", "h.c", "--unwind",
                                                 str(unwind), "--32"]}) + "\n")

    clean = {"BMC_AGENT_CBMC_UNWIND": "1", "SVCOMP_UNWIND": "0", "SVCOMP_TIMEOUT": "0"}
    ok = tmp_path / "clean"
    make(ok, [1, 2], clean)
    metrics = collect_mod.collect(ok)
    assert metrics["solver_configuration"]["matches_profile"] is True

    for name, leaked in (
        ("cap", {"BMC_AGENT_CBMC_UNWIND": "1", "SVCOMP_UNWIND": "0",
                 "SVCOMP_TIMEOUT": "0", "BMC_UNWIND_CAP": "1"}),
        ("floor", {"BMC_AGENT_CBMC_UNWIND": "1", "SVCOMP_UNWIND": "64",
                   "SVCOMP_TIMEOUT": "0"}),
        ("inline", {"BMC_AGENT_CBMC_UNWIND": "1", "SVCOMP_UNWIND": "0",
                    "SVCOMP_TIMEOUT": "0", "BMC_TRANSITIVE_INLINE": "0"}),
        ("default", {"BMC_AGENT_CBMC_UNWIND": "2", "SVCOMP_UNWIND": "0",
                     "SVCOMP_TIMEOUT": "0"}),
    ):
        directory = tmp_path / name
        make(directory, [1, 2], leaked)
        result = collect_mod.collect(directory)["solver_configuration"]
        assert result["matches_profile"] is False, name
        assert result["environment_leaks"], name


def test_config_agent_honours_recorded_token_cap(monkeypatch):
    """The formal profile advertises 4096; the agent must not silently use 2048."""
    from bmc_agent.agents.bmc_config_tools import BmcConfigAgent

    monkeypatch.delenv("CONFIG_AUDIT_CONFIG_MAX_TOKENS", raising=False)
    assert BmcConfigAgent.max_tokens_per_turn_param == 2048

    class Cfg:
        pass

    class LLM:
        pass

    class Parsed:
        function_bodies = {}
        functions = {}

    monkeypatch.setenv("CONFIG_AUDIT_CONFIG_MAX_TOKENS", "4096")
    agent = BmcConfigAgent(Cfg(), LLM(), parsed_file=Parsed(), corpus_paths=[])
    assert agent.max_tokens_per_turn_param == 4096

    monkeypatch.setenv("CONFIG_AUDIT_CONFIG_MAX_TOKENS", "not-a-number")
    agent = BmcConfigAgent(Cfg(), LLM(), parsed_file=Parsed(), corpus_paths=[])
    assert agent.max_tokens_per_turn_param == 2048


def test_guarded_collect_rejects_absent_svcomp_floors(tmp_path):
    """Absent SVCOMP_UNWIND/SVCOMP_TIMEOUT is a floor leak, not a pass.

    With SVCOMP_PROP set, production falls back to 64/800 when these are missing,
    so only an explicit "0" disables the floor.
    """
    import importlib.util
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "experiments/paper_reproduction"
    spec = importlib.util.spec_from_file_location("paper_collect_absent", here / "collect.py")
    collect_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collect_mod)

    def collect_with(environment):
        directory = tmp_path / ("case_" + str(abs(hash(json.dumps(environment, sort_keys=True)))))
        directory.mkdir(parents=True)
        (directory / "run_manifest.json").write_text(json.dumps({
            "profile": "rq1-ldv-guarded", "pilot": False,
            "execution_kind": "fresh_generation", "source": {"commit": "x"},
            "cases": [], "trials": 1,
            "settings": {"kind": "rq1", "unwind": 1, "arch": "ILP32",
                         "unwind_policy": "agent_may_override",
                         "strategy": "ldv_property_guard"},
            "environment": environment}))
        (directory / "execution.json").write_text("[]")
        with (directory / "cbmc_invocations.jsonl").open("w") as fh:
            fh.write(json.dumps({"command": ["cbmc", "h.c", "--unwind", "1", "--32"]}) + "\n")
        return collect_mod.collect(directory)["solver_configuration"]

    for environment in (
        {"BMC_AGENT_CBMC_UNWIND": "1", "SVCOMP_TIMEOUT": "0"},          # unwind absent
        {"BMC_AGENT_CBMC_UNWIND": "1", "SVCOMP_UNWIND": "0"},           # timeout absent
        {"BMC_AGENT_CBMC_UNWIND": "1"},                                  # both absent
        {"BMC_AGENT_CBMC_UNWIND": "1", "SVCOMP_UNWIND": "", "SVCOMP_TIMEOUT": "0"},  # empty
        {"BMC_AGENT_CBMC_UNWIND": "1", "SVCOMP_UNWIND": "0", "SVCOMP_TIMEOUT": "800"},
    ):
        result = collect_with(environment)
        assert result["matches_profile"] is False, environment
        assert result["environment_leaks"], environment
