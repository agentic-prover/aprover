"""Tests for the pre-run cost/token estimate (``web.estimate``).

The estimator makes no LLM calls — it parses the scope, tokenizes representative
prompts locally, and models the pipeline as tokens-per-request × #requests. These
tests pin the contract (shape, ordering of the range, free/custom pricing) rather
than exact token counts, which are calibration knobs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from web import estimate


@pytest.fixture
def c_file(tmp_path: Path) -> Path:
    src = tmp_path / "demo.c"
    src.write_text(
        "int add(int a, int b) { return a + b; }\n"
        "int mul(int a, int b) { int r = 0; for (int i = 0; i < b; i++) r += a; return r; }\n"
    )
    return src


def _llm(model: str) -> dict:
    return {"backend": "openai" if "/" in model else "anthropic",
            "model": model}


def test_estimate_shape_and_range_ordering(c_file: Path):
    r = estimate.estimate_scope(c_file, False, _llm("claude-sonnet-4-6"))
    assert r["n_functions"] == 2
    assert r["n_files"] == 1
    for field in ("requests", "prompt_tokens", "completion_tokens", "total_tokens", "usd"):
        assert {"low", "expected", "high"} == set(r[field].keys())
    # The range is monotonic: low <= expected <= high.
    for field in ("requests", "total_tokens"):
        assert r[field]["low"] <= r[field]["expected"] <= r[field]["high"]
    # A priced preset yields a non-null, ordered USD range.
    assert r["priced"] and not r["free"]
    assert r["usd"]["low"] <= r["usd"]["expected"] <= r["usd"]["high"]
    assert r["usd"]["expected"] > 0


def test_estimate_zero_priced_model_is_free(c_file: Path, monkeypatch):
    # A preset priced at (0, 0) per Mtok is reported as free.
    monkeypatch.setattr(estimate.pricing, "preset_price", lambda m: (0.0, 0.0))
    r = estimate.estimate_scope(c_file, False, _llm("some-free-model"))
    assert r["free"] is True
    assert r["usd"] == {"low": 0.0, "expected": 0.0, "high": 0.0}
    # Tokens are still estimated even though it's free.
    assert r["total_tokens"]["expected"] > 0


def test_estimate_custom_model_tokens_only(c_file: Path):
    r = estimate.estimate_scope(c_file, False, _llm("my/custom-model"))
    assert r["free"] is False
    assert r["priced"] is False
    assert r["usd"] == {"low": None, "expected": None, "high": None}
    assert r["total_tokens"]["expected"] > 0


def test_estimate_directory_scope(tmp_path: Path):
    (tmp_path / "a.c").write_text("int f(int x){return x;}\n")
    (tmp_path / "b.c").write_text("int g(int y){return y+1;}\nint h(void){return 0;}\n")
    r = estimate.estimate_scope(tmp_path, True, _llm("claude-sonnet-4-6"))
    assert r["n_files"] == 2
    assert r["n_functions"] == 3


# --- the estimate tracks the chosen run options ----------------------------

def test_estimate_realism_option_lowers_cost(c_file: Path):
    # Realism is on by default (CLI parity); turning it off drops the per-CEx call.
    base = estimate.estimate_scope(c_file, False, _llm("claude-sonnet-4-6"))
    without = estimate.estimate_scope(c_file, False, _llm("claude-sonnet-4-6"),
                                      options={"ai_layers": {"enable_realism_check": False}})
    assert base["requests"]["expected"] > without["requests"]["expected"]
    assert base["total_tokens"]["expected"] > without["total_tokens"]["expected"]


def test_estimate_lite_mode_drops_specgen_cost(c_file: Path):
    base = estimate.estimate_scope(c_file, False, _llm("claude-sonnet-4-6"))
    lite = estimate.estimate_scope(c_file, False, _llm("claude-sonnet-4-6"),
                                   options={"harness": {"lite_mode": True}})
    # Lite mode skips per-function spec-gen → fewer deterministic requests.
    assert lite["requests"]["low"] < base["requests"]["low"]


def test_estimate_default_assumes_realism_on(c_file: Path):
    # The web run inherits the CLI default (realism on); the estimate must match.
    # Passing the equivalent option explicitly is a no-op.
    a = estimate.estimate_scope(c_file, False, _llm("claude-sonnet-4-6"))
    b = estimate.estimate_scope(c_file, False, _llm("claude-sonnet-4-6"),
                                options={"ai_layers": {"enable_realism_check": True}})
    assert a["requests"]["expected"] == b["requests"]["expected"]


def test_estimate_reports_languages(c_file: Path):
    r = estimate.estimate_scope(c_file, False, _llm("claude-sonnet-4-6"))
    assert r["languages"] == ["c"]
