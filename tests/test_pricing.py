"""Tests for the workbench spend estimator (``web.pricing``).

The exact USD figure comes from the provider when available (OpenRouter's
``usage.cost``, claude-code's ``total_cost_usd``). This module is the *fallback*
token-based estimate for endpoints that don't price the call for us, so the
tests focus on model-name matching (incl. OpenRouter's ``provider/model``
namespacing) and the estimate arithmetic.
"""
from __future__ import annotations

import pytest

from web.pricing import MODEL_PRESETS, estimate_usd, preset_price, price_for_model


def test_model_presets_shape():
    # Every provider the workbench offers has presets, each with one default.
    for provider in ("anthropic", "openrouter", "openai", "k2think"):
        presets = MODEL_PRESETS[provider]
        assert presets, provider
        for p in presets:
            assert {"id", "label"} <= p.keys()


def test_preset_price_exact_only():
    # Priced preset → exact price; unpriced/custom → None (tokens-only estimate).
    assert preset_price("claude-opus-4-8") == (5.0, 25.0)
    assert preset_price("MBZUAI-IFM/K2-Think-v2") == (0.0, 0.0)  # free
    assert preset_price("moonshotai/kimi-k2") is None            # paid, no list price yet
    assert preset_price("totally/custom-model") is None


def test_preset_price_wins_over_substring():
    # Exact preset price is consulted before the coarse substring fallback.
    assert price_for_model("claude-opus-4-8") == (5.0, 25.0)


def test_price_for_model_matches_anthropic():
    assert price_for_model("claude-sonnet-4-6") == (3.0, 15.0)
    # Opus-tier list price (Opus 4.8): $5 / $25 per Mtok.
    assert price_for_model("claude-opus-4-1") == (5.0, 25.0)
    assert price_for_model("claude-haiku-4-5") == (1.0, 5.0)


def test_price_for_model_strips_openrouter_prefix():
    """OpenRouter ids are namespaced ``provider/model``; the bare model name
    after the prefix is what's matched."""
    assert price_for_model("anthropic/claude-sonnet-4.5") == (3.0, 15.0)
    assert price_for_model("anthropic/claude-opus-4.1") == (5.0, 25.0)


def test_price_for_model_non_anthropic_families():
    assert price_for_model("deepseek/deepseek-chat") is not None
    assert price_for_model("qwen/qwen-2.5-72b") is not None
    assert price_for_model("moonshotai/kimi-k2") is not None
    assert price_for_model("meta-llama/llama-3.1-70b") is not None
    assert price_for_model("google/gemini-2.5-pro") is not None


def test_price_for_model_unknown_returns_none():
    assert price_for_model("some/unheard-of-model") is None
    assert price_for_model("") is None


def test_price_for_model_env_override(monkeypatch):
    monkeypatch.setenv("BMC_AGENT_PRICE_IN", "2.0")
    monkeypatch.setenv("BMC_AGENT_PRICE_OUT", "8.0")
    # Override applies to every model, even unknown ones.
    assert price_for_model("anything/at-all") == (2.0, 8.0)


def test_estimate_usd_arithmetic():
    cost = {"model": "anthropic/claude-sonnet-4.5",
            "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    # 1M in * $3 + 1M out * $15 = $18.
    assert estimate_usd(cost) == 18.0


def test_estimate_usd_unknown_model_is_none():
    cost = {"model": "some/unknown", "prompt_tokens": 1000, "completion_tokens": 100}
    assert estimate_usd(cost) is None


def test_estimate_usd_handles_non_dict():
    assert estimate_usd(None) is None
    assert estimate_usd("nope") is None


# --- live OpenRouter prices -------------------------------------------------
# fetch_openrouter_prices() warms a module cache from OpenRouter's models API;
# the lookups then read that cache (no network). Tests monkeypatch httpx / the
# cache so they never touch the network.

import web.pricing as pricing  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _clear_or_cache():
    with pricing._or_lock:
        pricing._or_cache.clear()
        pricing._or_fetched_at = 0.0


@pytest.fixture(autouse=True)
def _isolate_or_cache():
    # Clear the module-global OpenRouter price cache around every test so a
    # warmed/failed test can't leak a live price into another (the live cache is
    # preferred over the static presets, which would silently flip results).
    _clear_or_cache()
    yield
    _clear_or_cache()


def test_fetch_openrouter_prices_parses_per_token_to_per_mtok(monkeypatch):
    payload = {"data": [
        # 0.000005 $/tok prompt → $5/Mtok; 0.000025 → $25/Mtok.
        {"id": "anthropic/claude-opus-4.8",
         "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
        {"id": "z-ai/glm-5.2",
         "pricing": {"prompt": "0.00000095", "completion": "0.000003"}},
        # malformed → skipped, not fatal.
        {"id": "broken/model", "pricing": {"prompt": "n/a", "completion": "1"}},
    ]}
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(payload))
    prices = pricing.fetch_openrouter_prices()
    assert prices["anthropic/claude-opus-4.8"] == (5.0, 25.0)
    assert prices["z-ai/glm-5.2"][0] == 0.95
    assert "broken/model" not in prices


def test_live_price_preferred_over_static_preset(monkeypatch):
    # Warm the cache directly (no network) with a price that differs from the
    # static fallback, and confirm preset_price / price_for_model use it.
    with pricing._or_lock:
        pricing._or_cache["anthropic/claude-opus-4.8"] = (1.0, 2.0)
        pricing._or_fetched_at = 1.0
    monkeypatch.setattr(pricing.time, "time", lambda: 1.0)  # keep cache fresh
    assert pricing.preset_price("anthropic/claude-opus-4.8") == (1.0, 2.0)
    assert pricing.price_for_model("anthropic/claude-opus-4.8") == (1.0, 2.0)


def test_presets_with_live_prices_overrides_openrouter(monkeypatch):
    with pricing._or_lock:
        pricing._or_cache["openai/gpt-5.4-mini"] = (0.25, 2.0)
        pricing._or_fetched_at = 1.0
    monkeypatch.setattr(pricing, "fetch_openrouter_prices",
                        lambda: dict(pricing._or_cache))
    rows = pricing.presets_with_live_prices()["openrouter"]
    mini = next(r for r in rows if r["id"] == "openai/gpt-5.4-mini")
    assert (mini["input"], mini["output"]) == (0.25, 2.0)
