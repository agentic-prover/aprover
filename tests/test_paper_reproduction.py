"""Guards for scientific configuration isolation and honest result collection."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_cbmc_recorder_preserves_solver_pid_arguments_and_exit(tmp_path):
    wrapper = ROOT / "experiments/paper_reproduction/tools/cbmc-record"
    fake = tmp_path / "fake cbmc"
    fake.write_text(f"#!{sys.executable}\nimport os,sys\nprint(os.getpid())\nprint(repr(sys.argv[1:]))\nsys.exit(7)\n")
    fake.chmod(0o755)
    source = tmp_path / "input with spaces.c"
    source.write_text("int main() { return 0; }")
    log = tmp_path / "calls.jsonl"
    env = {**os.environ, "PAPER_REPRO_CBMC_REAL": str(fake), "PAPER_REPRO_CBMC_LOG": str(log)}
    proc = subprocess.run([str(wrapper), str(source), "--unwind", "1"], env=env, text=True, capture_output=True)
    recorded = json.loads(log.read_text())
    assert proc.returncode == 7
    assert int(proc.stdout.splitlines()[0]) == recorded["pid"]
    assert recorded["command"] == [str(fake), str(source), "--unwind", "1"]


def test_result_audit_detects_effective_unwind_drift(tmp_path):
    collect = load("collect").collect
    manifest = {"profile": "rq1-ldv", "pilot": True, "execution_kind": "fresh_generation",
                "source": {"commit": "test"}, "trials": 1,
                "settings": {"kind": "rq1", "strategy": "public_ldv_fallback", "unwind": 1, "arch": "ILP32"},
                "cases": []}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "cbmc_invocations.jsonl").write_text(json.dumps({"command": ["cbmc", "harness.c", "--unwind", "4", "--32"]}) + "\n")
    result = collect(tmp_path)
    assert not result["solver_configuration"]["matches_profile"]


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
