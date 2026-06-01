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


def test_provider_default_anthropic(monkeypatch):
    # Ensure an API key is visible so resolved_provider() doesn't fall back
    # to the claude-code CLI path (which kicks in only when no key is set
    # anywhere). With a key present, the default fallback is "anthropic".
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from bmc_agent.config import Config

    c = Config(llm_api_key="sk-test")
    assert c.resolved_provider() == "anthropic"


def test_provider_openrouter_stays_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from bmc_agent.config import Config

    c = Config(llm_api_key="sk-test", llm_base_url="https://openrouter.ai/api")
    assert c.resolved_provider() == "anthropic"


def test_provider_no_api_key_falls_back_to_claude_code(monkeypatch):
    # New behaviour: when no API key is set anywhere, resolved_provider()
    # returns "claude-code" so the local CLI subscription is used.
    for key in ("ANTHROPIC_API_KEY", "BMC_AGENT_LLM_API_KEY",
                "K2THINK_API_KEY", "BMC_AGENT_HYBRID_SPEC_GEN_KEY"):
        monkeypatch.delenv(key, raising=False)
    from bmc_agent.config import Config

    c = Config()
    assert c.resolved_provider() == "claude-code"


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


def test_bmc_agent_llm_api_key_env(monkeypatch):
    """BMC_AGENT_LLM_API_KEY is the canonical hybrid env name, alongside
    BMC_AGENT_LLM_BASE_URL / _MODEL / _PROVIDER. Must be honoured by both
    resolved_api_key() (used by realism/classifier role-routing) and
    from_env() (used by the CLI verify path)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("K2THINK_API_KEY", raising=False)
    monkeypatch.setenv("BMC_AGENT_LLM_API_KEY", "bmc-agent-test-key")
    from bmc_agent.config import Config

    # resolved_api_key honours it regardless of provider
    c = Config(llm_provider="openai")
    assert c.resolved_api_key() == "bmc-agent-test-key"
    c_anth = Config()
    assert c_anth.resolved_api_key() == "bmc-agent-test-key"

    # from_env picks it up too
    c_env = Config.from_env()
    assert c_env.llm_api_key == "bmc-agent-test-key"


def test_bmc_agent_llm_api_key_beats_k2think(monkeypatch):
    """When both BMC_AGENT_LLM_API_KEY and K2THINK_API_KEY are set,
    BMC_AGENT_LLM_API_KEY wins (it is the explicit-intent variable)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("K2THINK_API_KEY", "k2-key")
    monkeypatch.setenv("BMC_AGENT_LLM_API_KEY", "bmc-key")
    from bmc_agent.config import Config

    c = Config(llm_provider="openai")
    assert c.resolved_api_key() == "bmc-key"


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
    # K2/reasoning-model floor: small caller values are padded up to 24576 so
    # the model has comfortable room for a long <think> trace plus the answer.
    # (Initially 16384 -- bumped after K2 was observed exhausting that floor
    # on CCC spec-gen prompts.)
    assert body["max_tokens"] == 24576
    assert body["temperature"] == 0.1
    assert body["stream"] is False


def test_openai_path_preserves_high_max_tokens():
    """A caller asking for >= 16384 should not be clipped."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    captured = {}

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = ""

        def json(self):
            return {
                "choices": [{
                    "message": {"content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {},
            }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["json"] = json
            return _Resp()

    class _FakeHttpx:
        Client = _FakeClient

        @staticmethod
        def Timeout(*a, **k):  # noqa: N802
            return None

    config = Config(
        llm_model="K",
        llm_api_key="key",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": _FakeHttpx}):
        client.complete("s", "u", max_tokens=32_000, temperature=0.0)
    assert captured["json"]["max_tokens"] == 32_000


def test_openai_finish_reason_length_raises():
    """No </think> closing tag + finish_reason=length must be loud, not silent."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient, LLMError

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = ""

        def json(self):
            return {
                "choices": [{
                    "message": {"content": "Let me think about the spec..."},
                    "finish_reason": "length",
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 16384},
            }

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
        llm_model="K",
        llm_api_key="key",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    config.max_spec_retries = 1
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": _FakeHttpx}):
        with pytest.raises(LLMError) as exc_info:
            client.complete("s", "u")
    assert "max_tokens" in str(exc_info.value)


def test_openai_finish_reason_length_with_think_returns_answer():
    """finish_reason=length but </think> emitted -> still return the answer after the tag."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = ""

        def json(self):
            return {
                "choices": [{
                    "message": {"content": "thinking...</think>final_answer_truncated"},
                    "finish_reason": "length",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

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
        llm_model="K",
        llm_api_key="key",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": _FakeHttpx}):
        out = client.complete("s", "u")
    assert out == "final_answer_truncated"


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


def test_http_4xx_does_not_burn_retries():
    """HTTP 4xx is a permanent client error (e.g. OpenRouter's 8MB
    request-size 400, auth 401, etc.). The retry classifier must NOT
    treat it as transient. Observed: OpenRouter rejected oversized
    realism prompts and bmc-agent burned 3×backoff before giving up."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient, LLMError

    attempts = {"n": 0}

    class _Resp:
        status_code = 400
        reason_phrase = "Bad Request"
        # Mimic OpenRouter's 8MB-exceeded payload.
        text = '{"error":{"message":"The total text input size exceeds 8 MB","code":400}}'

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
            attempts["n"] += 1
            return _Resp()

    class _FakeHttpx:
        Client = _FakeClient

        @staticmethod
        def Timeout(*a, **k):  # noqa: N802
            return None

    config = Config(
        llm_model="x",
        llm_api_key="key",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_provider="openai",
    )
    # If 4xx were treated as transient, max_spec_retries=3 would cause
    # 3 HTTP calls. We expect exactly 1.
    config.max_spec_retries = 3
    client = LLMClient(config)

    with patch.dict("sys.modules", {"httpx": _FakeHttpx}):
        with pytest.raises(LLMError):
            client.complete("s", "u")

    assert attempts["n"] == 1, (
        f"HTTP 4xx should not retry; saw {attempts['n']} attempts"
    )


# ---------------------------------------------------------------------------
# Hybrid per-role LLM routing
# ---------------------------------------------------------------------------


def test_role_settings_returns_global_defaults_when_no_override():
    from bmc_agent.config import Config

    c = Config(
        llm_model="default-model",
        llm_api_key="default-key",
        llm_base_url="https://default.example/v1",
        llm_provider="openai",
    )
    s = c.role_settings("spec_gen")
    assert s["model"] == "default-model"
    assert s["api_key"] == "default-key"
    assert s["base_url"] == "https://default.example/v1"
    assert s["provider"] == "openai"


def test_role_settings_uses_override_when_present():
    from bmc_agent.config import Config

    c = Config(
        llm_model="default-model",
        llm_api_key="default-key",
        llm_base_url="https://default.example/v1",
        llm_provider="openai",
        llm_role_overrides={
            "spec_gen": {
                "model": "anthropic/claude-sonnet-4.5",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "or-key",
                "provider": "openai",
            },
        },
    )
    s_spec = c.role_settings("spec_gen")
    assert s_spec["model"] == "anthropic/claude-sonnet-4.5"
    assert s_spec["api_key"] == "or-key"
    assert s_spec["base_url"] == "https://openrouter.ai/api/v1"
    # Other roles still see defaults.
    s_other = c.role_settings("refinement")
    assert s_other["model"] == "default-model"


def test_role_settings_partial_override_falls_back_to_defaults():
    """An override that sets only `model` keeps the default base_url/api_key."""
    from bmc_agent.config import Config

    c = Config(
        llm_model="default-model",
        llm_api_key="default-key",
        llm_base_url="https://default.example/v1",
        llm_role_overrides={"spec_gen": {"model": "override-model"}},
    )
    s = c.role_settings("spec_gen")
    assert s["model"] == "override-model"
    assert s["api_key"] == "default-key"
    assert s["base_url"] == "https://default.example/v1"


def test_hybrid_env_var_sets_up_spec_gen_and_feedback_routes(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("K2THINK_API_KEY", raising=False)
    monkeypatch.setenv("BMC_AGENT_HYBRID_SPEC_GEN_KEY", "or-test-key")
    from bmc_agent.config import Config

    c = Config.from_env()
    assert "spec_gen" in c.llm_role_overrides
    assert "feedback_distill" in c.llm_role_overrides
    sg = c.llm_role_overrides["spec_gen"]
    assert sg["model"] == "anthropic/claude-sonnet-4.5"
    assert sg["base_url"] == "https://openrouter.ai/api/v1"
    assert sg["api_key"] == "or-test-key"
    assert sg["provider"] == "openai"


def test_explicit_role_env_overrides_pick_up_one_role(monkeypatch):
    """Setting only BMC_AGENT_LLM_REFINEMENT_* picks up just refinement."""
    monkeypatch.delenv("BMC_AGENT_HYBRID_SPEC_GEN_KEY", raising=False)
    monkeypatch.setenv("BMC_AGENT_LLM_REFINEMENT_MODEL", "ref-model")
    monkeypatch.setenv("BMC_AGENT_LLM_REFINEMENT_API_KEY", "ref-key")
    from bmc_agent.config import Config

    c = Config.from_env()
    assert "refinement" in c.llm_role_overrides
    assert c.llm_role_overrides["refinement"]["model"] == "ref-model"
    assert c.llm_role_overrides["refinement"]["api_key"] == "ref-key"
    assert "spec_gen" not in c.llm_role_overrides


def test_llm_client_routes_spec_gen_through_override(monkeypatch):
    """End-to-end: complete(..., role='spec_gen') hits the override settings."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    captured_urls = []

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = ""
        def json(self):
            return {
                "choices": [{"message": {"content": '{"x":1}'}, "finish_reason": "stop"}],
                "usage": {},
            }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, json=None, headers=None):
            captured_urls.append((url, headers.get("Authorization", "")))
            return _Resp()

    class _FakeHttpx:
        Client = _FakeClient
        @staticmethod
        def Timeout(*a, **k):  # noqa: N802
            return None

    config = Config(
        llm_model="default-k2",
        llm_api_key="k2-key",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
        llm_role_overrides={
            "spec_gen": {
                "model": "anthropic/claude-sonnet-4.5",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "or-key",
                "provider": "openai",
            },
        },
    )
    client = LLMClient(config)

    from unittest.mock import patch
    with patch.dict("sys.modules", {"httpx": _FakeHttpx}):
        client.complete("s", "u", role="spec_gen")
        client.complete("s", "u", role=None)  # default
        client.complete("s", "u", role="refinement")  # no override -> default

    assert len(captured_urls) == 3
    # spec_gen routed through OpenRouter with or-key
    assert "openrouter.ai" in captured_urls[0][0]
    assert "or-key" in captured_urls[0][1]
    # default + refinement routed through K2 with k2-key
    assert "k2think.ai" in captured_urls[1][0]
    assert "k2-key" in captured_urls[1][1]
    assert "k2think.ai" in captured_urls[2][0]
    assert "k2-key" in captured_urls[2][1]


def test_llm_client_restores_settings_after_role_call(monkeypatch):
    """Config state must be unchanged after a role-overridden call returns."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}], "usage": {}}

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
        llm_model="k2",
        llm_api_key="k2-key",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
        llm_role_overrides={
            "spec_gen": {"model": "claude", "api_key": "or-key",
                         "base_url": "https://openrouter.ai/api/v1",
                         "provider": "openai"},
        },
    )
    client = LLMClient(config)

    from unittest.mock import patch
    with patch.dict("sys.modules", {"httpx": _FakeHttpx}):
        client.complete("s", "u", role="spec_gen")

    # Original config must be restored.
    assert config.llm_model == "k2"
    assert config.llm_api_key == "k2-key"
    assert config.llm_base_url == "https://api.k2think.ai/v1"
    assert config.llm_provider == "openai"


# ---------------------------------------------------------------------------
# Additional per-role routing: disagreement_diagnose + DEFAULT alias
# ---------------------------------------------------------------------------

def test_disagreement_diagnose_role_env_var_picked_up(monkeypatch):
    """The Phase 3d ``disagreement_diagnose`` role is routable via the
    same BMC_AGENT_LLM_<ROLE>_* env-var convention as the other roles."""
    for k in ("ANTHROPIC_API_KEY", "K2THINK_API_KEY",
              "BMC_AGENT_HYBRID_SPEC_GEN_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BMC_AGENT_LLM_DISAGREEMENT_DIAGNOSE_MODEL", "anthropic/claude-opus-4")
    monkeypatch.setenv("BMC_AGENT_LLM_DISAGREEMENT_DIAGNOSE_API_KEY", "diag-key")
    monkeypatch.setenv("BMC_AGENT_LLM_DISAGREEMENT_DIAGNOSE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("BMC_AGENT_LLM_DISAGREEMENT_DIAGNOSE_PROVIDER", "openai")

    from bmc_agent.config import Config
    c = Config.from_env()
    assert "disagreement_diagnose" in c.llm_role_overrides
    s = c.role_settings("disagreement_diagnose")
    assert s["model"] == "anthropic/claude-opus-4"
    assert s["api_key"] == "diag-key"
    assert s["base_url"] == "https://openrouter.ai/api/v1"
    assert s["provider"] == "openai"
    # Other roles unaffected
    assert "realism" not in c.llm_role_overrides


def test_disagreement_diagnose_falls_back_to_default_when_unset(monkeypatch):
    """Without per-role override, disagreement_diagnose uses the global
    default."""
    for k in ("ANTHROPIC_API_KEY", "K2THINK_API_KEY",
              "BMC_AGENT_HYBRID_SPEC_GEN_KEY",
              "BMC_AGENT_LLM_DISAGREEMENT_DIAGNOSE_MODEL",
              "BMC_AGENT_LLM_DISAGREEMENT_DIAGNOSE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BMC_AGENT_LLM_DEFAULT_MODEL", "global-model")
    monkeypatch.setenv("BMC_AGENT_LLM_DEFAULT_API_KEY", "global-key")

    from bmc_agent.config import Config
    c = Config.from_env()
    s = c.role_settings("disagreement_diagnose")
    assert s["model"] == "global-model"
    assert s["api_key"] == "global-key"


def test_default_env_var_alias_preferred_over_legacy(monkeypatch):
    """When both BMC_AGENT_LLM_DEFAULT_* and the legacy BMC_AGENT_LLM_*
    are set, DEFAULT wins (it's the clearer, newer name)."""
    for k in ("ANTHROPIC_API_KEY", "K2THINK_API_KEY",
              "BMC_AGENT_HYBRID_SPEC_GEN_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BMC_AGENT_LLM_MODEL", "legacy-model")
    monkeypatch.setenv("BMC_AGENT_LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("BMC_AGENT_LLM_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setenv("BMC_AGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("BMC_AGENT_LLM_DEFAULT_MODEL", "new-model")
    monkeypatch.setenv("BMC_AGENT_LLM_DEFAULT_API_KEY", "new-key")
    monkeypatch.setenv("BMC_AGENT_LLM_DEFAULT_BASE_URL", "https://new.example/v1")
    monkeypatch.setenv("BMC_AGENT_LLM_DEFAULT_PROVIDER", "anthropic")

    from bmc_agent.config import Config
    c = Config.from_env()
    assert c.llm_model == "new-model"
    assert c.llm_api_key == "new-key"
    assert c.llm_base_url == "https://new.example/v1"
    assert c.llm_provider == "anthropic"


def test_legacy_env_var_still_works_when_default_unset(monkeypatch):
    """Back-compat: BMC_AGENT_LLM_MODEL etc. still set the global
    defaults when BMC_AGENT_LLM_DEFAULT_* isn't present."""
    for k in ("ANTHROPIC_API_KEY", "K2THINK_API_KEY",
              "BMC_AGENT_HYBRID_SPEC_GEN_KEY",
              "BMC_AGENT_LLM_DEFAULT_MODEL",
              "BMC_AGENT_LLM_DEFAULT_API_KEY",
              "BMC_AGENT_LLM_DEFAULT_BASE_URL",
              "BMC_AGENT_LLM_DEFAULT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BMC_AGENT_LLM_MODEL", "legacy-model")
    monkeypatch.setenv("BMC_AGENT_LLM_API_KEY", "legacy-key")

    from bmc_agent.config import Config
    c = Config.from_env()
    assert c.llm_model == "legacy-model"
    assert c.llm_api_key == "legacy-key"


def test_oracle_disagreement_diagnose_uses_disagreement_role(monkeypatch):
    """Phase 3d's diagnose() calls llm.complete with
    role='disagreement_diagnose' (was previously role='realism' — that
    locked it to the realism model when users wanted a stronger one
    for the more subtle diagnosis task)."""
    from unittest.mock import MagicMock
    from bmc_agent.oracle_disagreement import (
        diagnose, DisagreementCase, DisagreementKind,
    )
    llm = MagicMock()
    llm.complete.return_value = (
        '{"verdict": "inconclusive", "rationale": "x", "confidence": "low"}'
    )
    case = DisagreementCase(
        kind=DisagreementKind.BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED,
        function_name="fn", violated_property="p.5",
        bmc_verdict="fail", realism_verdict="realistic",
        dyn_outcome="not_triggered",
        realism_reasoning="x", reproducer_source="int main(){}",
    )
    diagnose(case, llm)
    # The role kwarg passed to LLMClient.complete must be the new role
    call = llm.complete.call_args
    role = call.kwargs.get("role") if call.kwargs else None
    assert role == "disagreement_diagnose", (
        f"diagnose() should pass role='disagreement_diagnose', got {role!r}"
    )


# --- CLI provider-routing flags (--provider / --specs-via-claude-code /
#     --claude-code-agentic), Step 1 + Step 2 ----------------------------------

def test_specs_via_claude_code_routes_only_spec_roles(monkeypatch):
    """--specs-via-claude-code routes spec_gen + refinement to claude-code and
    leaves every other role on the global default."""
    for key in ("BMC_AGENT_LLM_SPEC_GEN_PROVIDER", "BMC_AGENT_LLM_REFINEMENT_PROVIDER",
                "BMC_AGENT_LLM_PROVIDER", "BMC_AGENT_LLM_DEFAULT_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args, _apply_model_arg
    from bmc_agent.config import Config

    args = build_parser().parse_args(
        ["verify", "--source", "x.c", "--driver", "d", "--specs-via-claude-code"]
    )
    cfg = Config.from_env()
    _apply_model_arg(cfg, args)
    _apply_provider_args(cfg, args)

    assert cfg.role_settings("spec_gen")["provider"] == "claude-code"
    assert cfg.role_settings("refinement")["provider"] == "claude-code"
    # A non-spec role keeps the global default (not forced to claude-code).
    assert cfg.role_settings("realism")["provider"] == cfg.llm_provider
    assert cfg.claude_code_agentic is False


def test_provider_flag_sets_global(monkeypatch):
    monkeypatch.delenv("BMC_AGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("BMC_AGENT_LLM_DEFAULT_PROVIDER", raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args
    from bmc_agent.config import Config

    args = build_parser().parse_args(
        ["verify", "--source", "x.c", "--driver", "d", "--provider", "claude-code"]
    )
    cfg = Config.from_env()
    _apply_provider_args(cfg, args)
    assert cfg.llm_provider == "claude-code"


def test_no_provider_flags_is_noop(monkeypatch):
    monkeypatch.delenv("BMC_AGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("BMC_AGENT_LLM_DEFAULT_PROVIDER", raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args
    from bmc_agent.config import Config

    args = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d"])
    cfg = Config.from_env()
    before_provider, before_overrides = cfg.llm_provider, dict(cfg.llm_role_overrides)
    _apply_provider_args(cfg, args)
    assert cfg.llm_provider == before_provider
    assert cfg.llm_role_overrides == before_overrides
    assert cfg.claude_code_agentic is False


def test_claude_code_agentic_flag_and_add_dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("BMC_AGENT_CLAUDE_CODE_AGENTIC", raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args
    from bmc_agent.config import Config

    src = tmp_path / "mod.c"
    src.write_text("int f(void){return 0;}\n")
    inc = tmp_path / "inc"
    inc.mkdir()
    args = build_parser().parse_args(
        ["verify", "--source", str(src), "--driver", "d",
         "--specs-via-claude-code", "--claude-code-agentic",
         "--include-dir", str(inc)]
    )
    cfg = Config.from_env()
    _apply_provider_args(cfg, args)
    assert cfg.claude_code_agentic is True
    # source dir + include dir are granted, de-duped, no cwd surprises.
    assert str(src.resolve().parent) in cfg.claude_code_add_dirs
    assert str(inc.resolve()) in cfg.claude_code_add_dirs


def test_claude_code_agentic_env(monkeypatch):
    monkeypatch.setenv("BMC_AGENT_CLAUDE_CODE_AGENTIC", "1")
    from bmc_agent.config import Config
    assert Config.from_env().claude_code_agentic is True


def test_agentic_umbrella_flag(monkeypatch):
    """--agentic turns on the whole stack: specs+refinement -> claude-code,
    tools-on, soundness gate, and harness-repair fallback."""
    for k in ("BMC_AGENT_LLM_PROVIDER", "BMC_AGENT_LLM_DEFAULT_PROVIDER",
              "BMC_AGENT_ENABLE_SOUNDNESS_GATE", "BMC_AGENT_ENABLE_AGENTIC_HARNESS_REPAIR",
              "BMC_AGENT_CLAUDE_CODE_AGENTIC"):
        monkeypatch.delenv(k, raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args
    from bmc_agent.config import Config
    a = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d", "--agentic"])
    cfg = Config.from_env()
    _apply_provider_args(cfg, a)
    assert cfg.role_settings("spec_gen")["provider"] == "claude-code"
    assert cfg.role_settings("refinement")["provider"] == "claude-code"
    assert cfg.claude_code_agentic is True
    assert cfg.enable_soundness_gate is True
    assert cfg.enable_agentic_harness_repair is True
    # non-spec roles stay on the default
    assert cfg.role_settings("realism")["provider"] == cfg.llm_provider
