"""Guards for scientific configuration isolation and honest result collection."""
import importlib.util
import json
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


def test_aws_profile_cannot_inherit_ldv_strategy_or_role_routing(monkeypatch):
    run = load("run")
    monkeypatch.setenv("BMC_FRAME_HAVOC", "1")
    monkeypatch.setenv("SVCOMP_ARCH", "ILP32")
    monkeypatch.setenv("BMC_AGENT_LLM_SPEC_GEN_MODEL", "unrelated-model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    args = SimpleNamespace(base_url="http://127.0.0.1:8794/openrouter/aws/api/v1", model="",
                           cbmc_path=None, frama_c_path=None, autospec_root=None)
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
                "settings": {"kind": "rq1", "strategy": "frame_havoc"},
                "cases": [{"name": "task", "expected_verdict": "false"}]}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "execution.json").write_text(json.dumps([{"name": "task", "wall_s": 2, "returncode": 0}]))
    path = tmp_path / "cases/task/artifacts/agdrv/main/cbmc_result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"saved_at": "test", "result": {"verified": False, "counterexamples": [
        {"failing_property": "reach_error.assertion.1"}]}}))
    result = collect(tmp_path)
    assert result["rows"][0]["verdict"] == "abstract_bug_candidate"


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
    monkeypatch.setattr(sys, "argv", argv + ["--execute"])
    assert run.main() == 0
    executed = json.loads((tmp_path / "run_manifest.json").read_text())["fingerprint"]
    assert planned == executed
