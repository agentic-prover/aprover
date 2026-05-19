"""Tests for K2 Think (OpenAI-compatible) provider support in LLMClient.

We test:
* Auto-detect of provider from base URL.
* Reasoning-block stripping (K2 emits `<think>...</think>` traces inline).
* Routing: openai provider does NOT initialise the anthropic SDK.
* Config env loading picks up K2THINK_API_KEY.

We do NOT make real HTTP calls in tests; the live smoke test happens in the
CCC re-run separately.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest


def test_strip_reasoning_balanced():
    from bmc_agent.llm import _strip_reasoning_blocks

    assert _strip_reasoning_blocks("PREAMBLE<think>secret</think>final") == "final"


def test_strip_reasoning_closing_only():
    from bmc_agent.llm import _strip_reasoning_blocks

    # K2 has been observed emitting only the closing tag.
    assert _strip_reasoning_blocks("noise</think>\n\nactual") == "actual"


def test_strip_reasoning_no_markers():
    from bmc_agent.llm import _strip_reasoning_blocks

    assert _strip_reasoning_blocks("plain text") == "plain text"


def test_strip_reasoning_empty():
    from bmc_agent.llm import _strip_reasoning_blocks

    assert _strip_reasoning_blocks("") == ""


def test_provider_auto_k2think():
    from bmc_agent.config import Config

    c = Config(llm_base_url="https://api.k2think.ai/v1")
    assert c.resolved_provider() == "openai"


def test_provider_auto_v1_suffix():
    from bmc_agent.config import Config

    c = Config(llm_base_url="https://example.com/v1")
    assert c.resolved_provider() == "openai"


def test_provider_default_anthropic():
    from bmc_agent.config import Config

    c = Config()
    assert c.resolved_provider() == "anthropic"


def test_provider_openrouter_stays_anthropic():
    from bmc_agent.config import Config

    c = Config(llm_base_url="https://openrouter.ai/api")
    assert c.resolved_provider() == "anthropic"


def test_provider_explicit_override():
    from bmc_agent.config import Config

    c = Config(llm_provider="openai", llm_base_url="https://elsewhere.test")
    assert c.resolved_provider() == "openai"


def test_k2_api_key_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("K2THINK_API_KEY", "k2-test-key")
    from bmc_agent.config import Config

    c = Config(llm_provider="openai")
    assert c.resolved_api_key() == "k2-test-key"


def test_anthropic_key_preferred_when_provider_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("K2THINK_API_KEY", "k2-key")
    from bmc_agent.config import Config

    c = Config()
    assert c.resolved_provider() == "anthropic"
    assert c.resolved_api_key() == "anthropic-key"


def test_openai_request_payload_shape():
    """Verify _complete_openai builds the right OpenAI-compatible POST."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    captured = {}

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = ""

        def json(self):
            return {
                "choices": [{"message": {"content": "noise</think>{\"ok\": 1}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    class _FakeHttpx:
        Client = _FakeClient

        @staticmethod
        def Timeout(*a, **k):  # noqa: N802
            return None

    config = Config(
        llm_model="MBZUAI-IFM/K2-Think-v2",
        llm_api_key="IFM-abc",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)

    with patch.dict("sys.modules", {"httpx": _FakeHttpx}):
        out = client.complete("sys", "user", max_tokens=64, temperature=0.1)

    assert out == '{"ok": 1}'  # reasoning prefix stripped, content unchanged after
    assert captured["url"] == "https://api.k2think.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer IFM-abc"
    body = captured["json"]
    assert body["model"] == "MBZUAI-IFM/K2-Think-v2"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "user"}
    assert body["max_tokens"] == 64
    assert body["temperature"] == 0.1
    assert body["stream"] is False


def test_openai_path_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("K2THINK_API_KEY", raising=False)
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient, LLMError

    config = Config(llm_provider="openai")
    client = LLMClient(config)
    with pytest.raises(LLMError):
        client.complete("s", "u")


def test_openai_http_error_propagates():
    """HTTP 4xx/5xx should raise LLMError with status info."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient, LLMError

    class _Resp:
        status_code = 401
        reason_phrase = "Unauthorized"
        text = '{"error":"invalid_key"}'

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    class _FakeHttpx:
        Client = _FakeClient

        @staticmethod
        def Timeout(*a, **k):  # noqa: N802
            return None

    config = Config(
        llm_model="x",
        llm_api_key="bad",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    config.max_spec_retries = 1  # don't waste cycles
    client = LLMClient(config)

    with patch.dict("sys.modules", {"httpx": _FakeHttpx}):
        with pytest.raises(LLMError) as exc_info:
            client.complete("s", "u")

    assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)
