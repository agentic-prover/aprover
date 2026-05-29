from __future__ import annotations

import json


def test_progress_event_appends_jsonl(tmp_path):
    from bmc_agent.config import Config
    from bmc_agent.progress import append_progress_event

    cfg = Config(artifact_dir=str(tmp_path))
    path = append_progress_event(cfg, "unit_event", driver="d", function="f")

    assert path == tmp_path / "progress.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "unit_event"
    assert row["driver"] == "d"
    assert row["function"] == "f"
    assert "ts" in row


def test_progress_summary_is_latest_snapshot(tmp_path):
    from bmc_agent.config import Config
    from bmc_agent.progress import write_progress_summary

    cfg = Config(artifact_dir=str(tmp_path))
    path = write_progress_summary(cfg, {"driver": "d", "files_completed": 2})

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["driver"] == "d"
    assert data["files_completed"] == 2
    assert "updated_at" in data


def test_cbmc_progress_summary_extracts_undefined_symbols():
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.progress import summarize_cbmc_result

    raw = (
        "[{\"messageText\":\"failed to find symbol 'num_rows'\"},"
        "{\"messageText\":\"failed to find symbol 'num_rows'\"},"
        "{\"messageText\":\"CONVERSION ERROR\"}]"
    )
    summary = summarize_cbmc_result(
        CBMCResult(
            verified=False,
            raw_output=raw,
            error="cbmc exited with code 6",
        )
    )

    assert summary["status"] == "error"
    assert summary["error_kind"] == "undefined_symbol"
    assert summary["undefined_symbols"] == {"num_rows": 2}


def test_bmc_engine_writes_function_progress_events(tmp_path):
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.config import Config
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    class FakeBackend:
        language = "rust"

        def generate_harness(self, *args, **kwargs):
            return "fn main() {}"

        def check(self, *args, **kwargs):
            return CBMCResult(verified=True)

    source = tmp_path / "m.c"
    source.write_text("int f(int x) { return x + 1; }\n", encoding="utf-8")
    parsed = parse_c_file(source)
    func = parsed.get_function_info("f")
    cfg = Config(artifact_dir=str(tmp_path / "artifacts"))
    engine = BMCEngine(cfg, ArtifactStore(cfg.artifact_dir), backend=FakeBackend())
    spec = Spec("f", "true", "true", status=SpecStatus.GENERATED)

    verdict = engine.check_function(func, spec, parsed, "driver")

    assert verdict.verified is True
    events = [
        json.loads(line)
        for line in (tmp_path / "artifacts" / "progress.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [e["event"] for e in events] == ["function_start", "function_end"]
    assert events[-1]["status"] == "verified"
    assert events[-1]["cbmc_result_path"].endswith("driver/f/cbmc_result.json")
