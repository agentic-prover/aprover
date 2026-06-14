"""Tests for bmc_agent.agent_telemetry and its BaseAgent.run() hook."""

from __future__ import annotations

import json

from bmc_agent import agent_telemetry as tel
from bmc_agent.agents.base import AgentResult, BaseAgent
from bmc_agent.config import Config


def test_record_and_summary_aggregates_per_role():
    tel.reset()
    tel.record("realism", 0.10, outcome="ok", iterations=1)
    tel.record("realism", 0.30, outcome="error", iterations=1, error="boom")
    tel.record("reproducer", 1.0, outcome="ok", iterations=4, tool_calls=3)
    s = tel.summary()
    assert s["realism"]["calls"] == 2
    assert s["realism"]["ok"] == 1 and s["realism"]["error"] == 1
    assert s["realism"]["total_duration_s"] == 0.4
    assert s["realism"]["avg_duration_s"] == 0.2
    assert s["reproducer"]["tool_calls"] == 3
    assert s["reproducer"]["iterations"] == 4


def test_reset_clears():
    tel.reset()
    tel.record("x", 0.1, outcome="ok")
    assert tel.summary()
    tel.reset()
    assert tel.summary() == {}


def test_record_agent_result_outcomes():
    tel.reset()
    tel.record_agent_result("a", 0.1, AgentResult(output={"k": 1}))
    tel.record_agent_result("a", 0.1, AgentResult(error="bad"))
    tel.record_agent_result("a", 0.1, AgentResult(output=None))  # empty
    tel.record_agent_result("a", 0.1, None)                       # raised
    s = tel.summary()["a"]
    assert s["calls"] == 4
    assert s["ok"] == 1
    assert s["empty"] == 1
    assert s["error"] == 2  # explicit error + None(raised)


def test_record_never_raises_on_bad_input():
    tel.reset()
    # Bad types must be swallowed, not propagated.
    tel.record("x", "not-a-float", outcome="weird")  # type: ignore[arg-type]
    tel.record_agent_result("y", 0.1, object())
    # Still callable / summarizable.
    assert isinstance(tel.summary(), dict)


def test_dump_writes_json(tmp_path):
    tel.reset()
    tel.record("realism", 0.2, outcome="ok")
    p = tmp_path / "agent_telemetry.json"
    summ = tel.dump(str(p))
    assert summ["realism"]["calls"] == 1
    payload = json.loads(p.read_text())
    assert "records" in payload and "summary" in payload
    assert payload["summary"]["realism"]["calls"] == 1
    assert payload["records"][0]["role"] == "realism"


class _DummyOK(BaseAgent):
    name = "dummy_ok"
    system_prompt = "x"

    def build_prompt(self, **kwargs):
        return "p"

    def _call_llm(self, prompt):
        return ("{}", None)

    def parse(self, response):
        return {"ok": 1}


class _DummyParseNone(BaseAgent):
    name = "dummy_none"
    system_prompt = "x"

    def build_prompt(self, **kwargs):
        return "p"

    def _call_llm(self, prompt):
        return ("garbage", None)

    def parse(self, response):
        return None


def test_baseagent_run_records_outcome():
    tel.reset()
    cfg = Config(llm_api_key="t")
    _DummyOK(cfg, None).run()
    _DummyParseNone(cfg, None).run()
    s = tel.summary()
    assert s["dummy_ok"]["ok"] == 1
    # parse()->None exhausts retries and run() returns an AgentResult with
    # error set, so it is recorded as "error" (not "empty").
    assert s["dummy_none"]["error"] == 1
