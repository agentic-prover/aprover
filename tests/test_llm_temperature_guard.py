"""Newer Anthropic models (e.g. claude-opus-4-8) reject the ``temperature``
parameter with HTTP 400. LLMClient must omit it for those models while still
sending it for models that accept it (sonnet-4-6, haiku-4-5). Regression guard
for the per-role model-routing work (routing a role to Opus must not 400)."""

from bmc_agent.llm import _model_rejects_temperature


def test_opus_4_8_rejects_temperature():
    assert _model_rejects_temperature("claude-opus-4-8") is True
    assert _model_rejects_temperature("CLAUDE-OPUS-4-8") is True


def test_sonnet_and_haiku_accept_temperature():
    assert _model_rejects_temperature("claude-sonnet-4-6") is False
    assert _model_rejects_temperature("claude-haiku-4-5") is False


def test_empty_or_none_model_is_safe():
    assert _model_rejects_temperature("") is False
    assert _model_rejects_temperature(None) is False
