"""Tests for LLMClient reliability + latency telemetry (the workbench badge).

Covers the per-attempt recorder/snapshot math directly, plus the categorisation
wired through ``complete()`` (success / timeout / decode). No real HTTP calls.
"""
from __future__ import annotations

import pytest

from bmc_agent.config import Config
from bmc_agent.llm import LLMClient


def _client() -> LLMClient:
    # Explicit provider bypasses key-based auto-detect; the provider call itself
    # is monkeypatched in the integration tests below.
    return LLMClient(Config(llm_provider="anthropic"))


def test_record_call_counts_and_window():
    c = _client()
    c._record_call(True, None, 1.0)
    c._record_call(False, "timeout", 2.0)
    c._record_call(False, "decode", 0.5)
    c._record_call(False, "other", 0.5)
    c._record_call(True, None, 1.0)
    snap = c.reliability_snapshot()
    assert snap["total"] == 5
    assert snap["success"] == 2
    assert snap["timeout"] == 1
    assert snap["decode"] == 1
    assert snap["other"] == 1
    # recent window (maxlen 5): 3 of the last 5 failed
    assert snap["recent_total"] == 5
    assert snap["recent_fail"] == 3
    # avg latency = (1+2+0.5+0.5+1)/5 s = 1.0s = 1000ms
    assert snap["latency_ms_avg"] == 1000


def test_recent_window_caps_at_five():
    c = _client()
    for _ in range(7):
        c._record_call(True, None, 0.1)
    c._record_call(False, "timeout", 0.1)  # 8th call: only this one is a fail
    snap = c.reliability_snapshot()
    assert snap["total"] == 8
    assert snap["recent_total"] == 5          # window never exceeds 5
    assert snap["recent_fail"] == 1           # only the last call failed


def test_empty_snapshot_has_null_latency():
    snap = _client().reliability_snapshot()
    assert snap["total"] == 0
    assert snap["latency_ms_avg"] is None
    assert snap["latency_ms_recent"] is None


def test_classify_failure_categories():
    import httpx  # any exception type works; we test the name/msg heuristics
    from bmc_agent.llm import LLMTruncatedError, LLMRetryableError

    classify = LLMClient._classify_failure
    assert classify(TimeoutError("x"), "ReadTimeout", "read timeout") == "timeout"
    assert classify(Exception("x"), "Foo", "connection reset") == "other"
    assert classify(LLMTruncatedError("t"), "LLMTruncatedError", "truncated") == "decode"
    assert classify(LLMRetryableError("r"), "LLMRetryableError", "bad") == "decode"


def test_complete_records_success(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_complete_anthropic", lambda *a, **k: "the answer")
    out = c.complete("sys", "user")
    assert out == "the answer"
    snap = c.reliability_snapshot()
    assert snap["total"] == 1 and snap["success"] == 1
    assert snap["latency_ms_avg"] is not None  # latency was recorded


def test_complete_records_timeout(monkeypatch):
    c = _client()
    c.config.max_spec_retries = 1  # one attempt, then give up

    def boom(*a, **k):
        raise TimeoutError("read timeout")

    monkeypatch.setattr(c, "_complete_anthropic", boom)
    with pytest.raises(Exception):
        c.complete("sys", "user")
    snap = c.reliability_snapshot()
    assert snap["total"] == 1 and snap["timeout"] == 1 and snap["success"] == 0
    assert snap["recent_fail"] == 1


def test_complete_records_decode_on_validate_failure(monkeypatch):
    c = _client()
    c.config.max_spec_retries = 1
    monkeypatch.setattr(c, "_complete_anthropic", lambda *a, **k: "not json")
    # validate always fails -> decode failure, but the best-effort result is
    # still returned (the caller does its own parse handling).
    out = c.complete("sys", "user", validate=lambda r: False)
    assert out == "not json"
    snap = c.reliability_snapshot()
    assert snap["total"] == 1 and snap["decode"] == 1 and snap["success"] == 0
