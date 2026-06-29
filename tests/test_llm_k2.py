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
    fake = _make_fake_httpx(
        [{
            "choices": [{"message": {"content": "noise</think>{\"ok\": 1}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }],
        captured,
    )

    config = Config(
        llm_model="MBZUAI-IFM/K2-Think-v2",
        llm_api_key="IFM-abc",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)

    with patch.dict("sys.modules", {"httpx": fake}):
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
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_openai_extracts_openrouter_cost():
    """OpenRouter returns the authoritative per-request USD spend as
    ``usage.cost`` in the trailing chunk. _complete_openai must accumulate it
    into usage_total_cost_usd (so the web layer reports exact spend), and it
    must accumulate across calls."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    fake = _make_fake_httpx([{
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                  "total_tokens": 150, "cost": 0.0123},
    }])

    config = Config(
        llm_model="anthropic/claude-sonnet-4.5",
        llm_api_key="or-key",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", max_tokens=64, temperature=0.0)
        assert client.usage_total_cost_usd == 0.0123
        assert client.usage_total_tokens == 150
        # A second call accumulates (the fake repeats the last response).
        client.complete("s", "u", max_tokens=64, temperature=0.0)
    assert client.usage_total_cost_usd == 0.0246


def test_openai_no_cost_field_leaves_cost_zero():
    """Plain OpenAI / K2 endpoints omit usage.cost; _add_cost(None) must be a
    no-op so token telemetry still works and exact cost stays 0.0 (the web layer
    then falls back to its token-based estimate)."""
    from bmc_agent.llm import LLMClient

    fake = _make_fake_httpx([{
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }])
    client = LLMClient(_k2_config())
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", max_tokens=64, temperature=0.0)
    assert client.usage_total_cost_usd == 0.0
    assert client.usage_total_tokens == 15


def test_openrouter_request_asks_for_cost():
    """OpenRouter only populates usage.cost when usage accounting is requested
    via the ``usage: {include: true}`` body param. _complete_openai must send it
    on OpenRouter endpoints (alongside stream_options for token telemetry), or
    the spend meter never sees a cost and shows a dash."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    fake = _make_fake_httpx([{
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }], captured)

    config = Config(
        llm_model="anthropic/claude-sonnet-4.5",
        llm_api_key="or-key",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", max_tokens=64, temperature=0.0)
    body = captured["json"]
    assert body["usage"] == {"include": True}
    assert body["stream_options"] == {"include_usage": True}


def test_non_openrouter_omits_usage_include():
    """A stray ``usage`` body param can 400 plain OpenAI / K2 endpoints, so the
    accounting flag is gated to OpenRouter only."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    for base in ("https://api.openai.com/v1", "https://api.k2think.ai/v1"):
        captured: dict = {}
        fake = _make_fake_httpx([{
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }], captured)
        config = Config(
            llm_model="gpt-4o",
            llm_api_key="k",
            llm_base_url=base,
            llm_provider="openai",
        )
        client = LLMClient(config)
        with patch.dict("sys.modules", {"httpx": fake}):
            client.complete("s", "u", max_tokens=64, temperature=0.0)
        assert "usage" not in captured["json"], base


# ---------------------------------------------------------------------------
# K2 Think inference-backend routing (cerebras / nvidia / auto)
# ---------------------------------------------------------------------------

def _k2_ok_response():
    return [{"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}]


def test_k2_nvidia_injects_metadata():
    """backend='nvidia' on the K2 endpoint adds metadata.use_nvidia=true; the
    model id is unchanged (selection is body-only)."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    fake = _make_fake_httpx(_k2_ok_response(), captured)
    client = LLMClient(_k2_config(llm_k2_backend="nvidia"))
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", max_tokens=64)
    body = captured["json"]
    assert body["metadata"] == {"use_nvidia": True}
    assert body["model"] == "MBZUAI-IFM/K2-Think-v2"


def test_k2_cerebras_omits_metadata():
    """backend='cerebras' (and empty default) must NOT send metadata — Cerebras
    is the implicit default when the flag is absent."""
    from bmc_agent.llm import LLMClient

    for backend in ("cerebras", ""):
        captured: dict = {}
        fake = _make_fake_httpx(_k2_ok_response(), captured)
        client = LLMClient(_k2_config(llm_k2_backend=backend))
        with patch.dict("sys.modules", {"httpx": fake}):
            client.complete("s", "u", max_tokens=64)
        assert "metadata" not in captured["json"], backend


def test_k2_metadata_gated_to_k2_endpoint():
    """Even with backend='nvidia', a non-K2 endpoint (OpenRouter) must never get
    the metadata flag — it would be rejected/ignored there."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    fake = _make_fake_httpx(_k2_ok_response(), captured)
    config = Config(
        llm_model="anthropic/claude-sonnet-4.5",
        llm_api_key="or-key",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_provider="openai",
        llm_k2_backend="nvidia",
    )
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", max_tokens=64)
    assert "metadata" not in captured["json"]


def test_k2_auto_flips_backend_on_retryable_failure():
    """Under 'auto', a retryable failure cools down the tried backend so the next
    attempt flips to the other one (latency-aware fallback). Starting on Cerebras
    (no metadata), the retry must switch to NVIDIA (metadata.use_nvidia)."""
    from bmc_agent.llm import LLMClient, LLMError

    captured: dict = {}
    # 503 on every response (the fake applies one status to all) — both attempts
    # fail, but we only care that the two attempts targeted different backends.
    fake = _make_fake_httpx([{}], captured, status_code=503,
                            reason_phrase="Service Unavailable")
    cfg = _k2_config(llm_k2_backend="auto")
    cfg.max_spec_retries = 2
    client = LLMClient(cfg)
    with patch("bmc_agent.llm.time.sleep", lambda *_: None):
        with patch.dict("sys.modules", {"httpx": fake}):
            with pytest.raises(LLMError):
                client.complete("s", "u")
    payloads = captured["payloads"]
    assert len(payloads) == 2
    assert "metadata" not in payloads[0]                       # 1st: Cerebras
    assert payloads[1].get("metadata") == {"use_nvidia": True}  # 2nd: NVIDIA


def test_select_k2_backend_prefers_lower_latency():
    """The auto selector returns the lowest-EWMA backend once both are probed,
    honours explicit choices, and avoids a cooled-down backend."""
    from bmc_agent.llm import LLMClient

    client = LLMClient(_k2_config(llm_k2_backend="auto"))
    # No data yet => prefer Cerebras (documented-faster default).
    assert client._select_k2_backend() == "cerebras"
    # Record Cerebras slow, NVIDIA fast => NVIDIA wins.
    client._k2_latency = {"cerebras": 5.0, "nvidia": 1.0}
    assert client._select_k2_backend() == "nvidia"
    # A cooldown on NVIDIA forces the other backend.
    client._k2_cooldown["nvidia"] = 2
    assert client._select_k2_backend() == "cerebras"
    # Explicit choice ignores latency/cooldown entirely.
    client.config.llm_k2_backend = "nvidia"
    assert client._select_k2_backend() == "nvidia"


def test_config_from_env_k2_backend(monkeypatch):
    monkeypatch.setenv("BMC_AGENT_LLM_K2_BACKEND", "NVIDIA")
    from bmc_agent.config import Config

    assert Config.from_env().llm_k2_backend == "nvidia"  # normalised lower-case


def test_openai_path_preserves_high_max_tokens():
    """A caller asking for >= 16384 should not be clipped."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    captured = {}
    fake = _make_fake_httpx(
        [{"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
          "usage": {}}],
        captured,
    )

    config = Config(
        llm_model="K",
        llm_api_key="key",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", max_tokens=32_000, temperature=0.0)
    assert captured["json"]["max_tokens"] == 32_000


def test_openai_finish_reason_length_raises():
    """No </think> closing tag + finish_reason=length must be loud, not silent."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient, LLMError

    fake = _make_fake_httpx([{
        "choices": [{
            "message": {"content": "Let me think about the spec..."},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 16384},
    }])

    config = Config(
        llm_model="K",
        llm_api_key="key",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    config.max_spec_retries = 1
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": fake}):
        with pytest.raises(LLMError) as exc_info:
            client.complete("s", "u")
    assert "max_tokens" in str(exc_info.value)


def test_openai_finish_reason_length_with_think_returns_answer():
    """finish_reason=length but </think> emitted -> still return the answer after the tag."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    fake = _make_fake_httpx([{
        "choices": [{
            "message": {"content": "thinking...</think>final_answer_truncated"},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }])

    config = Config(
        llm_model="K",
        llm_api_key="key",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": fake}):
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

    fake = _make_fake_httpx(
        [{}], status_code=401, reason_phrase="Unauthorized",
        error_text='{"error":"invalid_key"}',
    )

    config = Config(
        llm_model="x",
        llm_api_key="bad",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    config.max_spec_retries = 1  # don't waste cycles
    client = LLMClient(config)

    with patch.dict("sys.modules", {"httpx": fake}):
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

    captured = {}
    # Mimic OpenRouter's 8MB-exceeded payload.
    fake = _make_fake_httpx(
        [{}], captured, status_code=400, reason_phrase="Bad Request",
        error_text='{"error":{"message":"The total text input size exceeds 8 MB","code":400}}',
    )

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

    with patch.dict("sys.modules", {"httpx": fake}):
        with pytest.raises(LLMError):
            client.complete("s", "u")

    attempts = len(captured["payloads"])
    assert attempts == 1, (
        f"HTTP 4xx should not retry; saw {attempts} attempts"
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

    captured = {}
    fake = _make_fake_httpx(
        [{"choices": [{"message": {"content": '{"x":1}'}, "finish_reason": "stop"}],
          "usage": {}}],
        captured,
    )

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
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", role="spec_gen")
        client.complete("s", "u", role=None)  # default
        client.complete("s", "u", role="refinement")  # no override -> default

    captured_urls = captured["urls"]
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

    fake = _make_fake_httpx(
        [{"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
          "usage": {}}],
    )

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
    with patch.dict("sys.modules", {"httpx": fake}):
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

    # --no-agentic isolates the role-routing under test from the now-default
    # agentic stack (which would otherwise turn on claude_code read-only tools).
    args = build_parser().parse_args(
        ["verify", "--source", "x.c", "--driver", "d",
         "--no-agentic", "--specs-via-claude-code"]
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

    # --no-agentic: with the agentic stack on by default a bare invocation would
    # turn on claude_code read-only tools; --no-agentic restores the plain core so
    # this stays a true provider-routing no-op.
    args = build_parser().parse_args(
        ["verify", "--source", "x.c", "--driver", "d", "--no-agentic"]
    )
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


def test_agentic_claude_code_forces_all_roles(monkeypatch):
    """--agentic-claude-code: EVERY agent role -> claude-code, tools-on,
    soundness gate, harness-repair."""
    for k in ("BMC_AGENT_LLM_PROVIDER", "BMC_AGENT_LLM_DEFAULT_PROVIDER",
              "BMC_AGENT_ENABLE_SOUNDNESS_GATE", "BMC_AGENT_ENABLE_AGENTIC_HARNESS_REPAIR",
              "BMC_AGENT_CLAUDE_CODE_AGENTIC",
              "BMC_AGENT_LLM_REALISM_PROVIDER", "BMC_AGENT_LLM_TRIAGE_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args
    from bmc_agent.config import Config
    a = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d", "--agentic-claude-code"])
    cfg = Config.from_env()
    _apply_provider_args(cfg, a)
    # EVERY agent role now defaults to claude-code
    for role in ("spec_gen", "refinement", "realism", "triage",
                 "disagreement_diagnose", "feedback_distill"):
        assert cfg.role_settings(role)["provider"] == "claude-code", role
    assert cfg.claude_code_agentic is True
    assert cfg.enable_soundness_gate is True
    assert cfg.enable_agentic_harness_repair is True


def test_agentic_general_does_not_force_a_backend(monkeypatch):
    """--agentic (general) enables the stack but FORCES no backend: with no
    per-role / default provider set, no role is pinned to claude-code (each keeps
    its own routing)."""
    for k in ("BMC_AGENT_LLM_PROVIDER", "BMC_AGENT_LLM_DEFAULT_PROVIDER",
              "BMC_AGENT_LLM_SPEC_GEN_PROVIDER", "BMC_AGENT_LLM_REFINEMENT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args
    from bmc_agent.config import Config
    a = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d", "--agentic"])
    cfg = Config.from_env()
    _apply_provider_args(cfg, a)
    overrides = getattr(cfg, "llm_role_overrides", {}) or {}
    assert not any(v.get("provider") == "claude-code" for v in overrides.values())
    # but the agentic stack is on
    assert cfg.claude_code_agentic is True
    assert cfg.enable_soundness_gate is True
    assert cfg.enable_split_spec_gen is True


def test_agentic_claude_code_per_agent_override_wins(monkeypatch):
    """--agentic-claude-code defaults every agent to claude-code, but an explicit
    per-agent provider override (env) WINS — any agent can be re-pointed."""
    for k in ("BMC_AGENT_LLM_PROVIDER", "BMC_AGENT_LLM_DEFAULT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    # Re-point ONLY realism to a fast OpenAI-compatible model.
    monkeypatch.setenv("BMC_AGENT_LLM_REALISM_PROVIDER", "openai")
    monkeypatch.setenv("BMC_AGENT_LLM_REALISM_MODEL", "gpt-4o-mini")
    from bmc_agent.cli import build_parser, _apply_provider_args
    from bmc_agent.config import Config
    a = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d", "--agentic-claude-code"])
    cfg = Config.from_env()
    _apply_provider_args(cfg, a)
    # realism keeps the explicit override; everything else defaults to claude-code
    assert cfg.role_settings("realism")["provider"] == "openai"
    assert cfg.role_settings("realism")["model"] == "gpt-4o-mini"
    assert cfg.role_settings("spec_gen")["provider"] == "claude-code"
    assert cfg.role_settings("refinement")["provider"] == "claude-code"


def test_agentic_refine_lean_flag(monkeypatch):
    """--agentic-refine routes ONLY refinement to claude-code (spec-gen stays on
    the fast default provider) but still enables the guards + split spec-gen."""
    for k in ("BMC_AGENT_LLM_SPEC_GEN_PROVIDER", "BMC_AGENT_LLM_REFINEMENT_PROVIDER",
              "BMC_AGENT_LLM_PROVIDER", "BMC_AGENT_LLM_DEFAULT_PROVIDER",
              "BMC_AGENT_SPEC_GEN_SPLIT", "BMC_AGENT_ENABLE_SOUNDNESS_GATE"):
        monkeypatch.delenv(k, raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args
    from bmc_agent.config import Config
    a = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d", "--agentic-refine"])
    cfg = Config.from_env()
    _apply_provider_args(cfg, a)
    # refinement -> claude-code; spec_gen stays default (the whole point)
    assert cfg.role_settings("refinement")["provider"] == "claude-code"
    assert cfg.role_settings("spec_gen")["provider"] == cfg.llm_provider  # NOT claude-code
    assert cfg.role_settings("spec_gen")["provider"] != "claude-code"
    assert cfg.claude_code_agentic is True
    assert cfg.enable_soundness_gate is True
    assert cfg.enable_agentic_harness_repair is True
    assert cfg.enable_split_spec_gen is True


# ---------------------------------------------------------------------------
# K2 reasoning-response hardening: cap fix, truncation retry, validator
# re-query, code-fence unwrap, complete_json
# ---------------------------------------------------------------------------


def _sse_from_body(body):
    """Render a non-streaming OpenAI response dict as the SSE ``data:`` lines a
    real ``/v1/chat/completions`` stream would deliver for the same content:
    one delta chunk carrying the full content + ``finish_reason``, an optional
    trailing usage-only chunk (empty ``choices``), then ``[DONE]``.
    """
    lines = []
    choices = body.get("choices") or []
    if choices:
        ch = choices[0]
        content = (ch.get("message") or {}).get("content", "")
        lines.append("data: " + json.dumps({
            "choices": [{"delta": {"content": content},
                         "finish_reason": ch.get("finish_reason")}],
        }))
    if body.get("usage"):
        lines.append("data: " + json.dumps({"choices": [], "usage": body["usage"]}))
    lines.append("data: [DONE]")
    return lines


def _make_fake_httpx(responses, captured=None, *, status_code=200,
                     reason_phrase="OK", error_text=""):
    """Return a fake ``httpx`` module whose ``Client.stream`` replays
    ``responses`` (non-streaming response dicts) as SSE, in order, recording
    each request in ``captured`` (``payloads`` list + last ``json``/``url``/
    ``headers`` + ``urls`` list of ``(url, auth)``).

    The last response repeats once the list is exhausted. Pass
    ``status_code`` >= 400 (+ ``error_text``) to exercise the HTTP-error path.
    """
    seq = list(responses) if responses else [{}]
    cap = captured if captured is not None else {}

    class _StreamResp:
        def __init__(self, body):
            self._body = body
            self.status_code = status_code
            self.reason_phrase = reason_phrase
            self.text = error_text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

        def iter_lines(self):
            for ln in _sse_from_body(self._body):
                yield ln

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, json=None, headers=None):
            cap.setdefault("payloads", []).append(json)
            cap["json"] = json
            cap["url"] = url
            cap["headers"] = headers
            cap.setdefault("urls", []).append(
                (url, (headers or {}).get("Authorization", "")))
            body = seq.pop(0) if len(seq) > 1 else seq[0]
            return _StreamResp(body)

    class _FakeHttpx:
        Client = _FakeClient

        @staticmethod
        def Timeout(*a, **k):  # noqa: N802
            return None

    return _FakeHttpx


def _k2_config(**over):
    from bmc_agent.config import Config

    base = dict(
        llm_model="MBZUAI-IFM/K2-Think-v2",
        llm_api_key="IFM-abc",
        llm_base_url="https://api.k2think.ai/v1",
        llm_provider="openai",
    )
    base.update(over)
    return Config(**base)


def test_default_cap_lets_k2_floor_apply():
    """The K2/reasoning default cap must sit at/above the 24576 floor so the
    floor isn't silently clamped back down (the original latent bug)."""
    from bmc_agent.llm import _default_openai_max_tokens_cap

    assert _default_openai_max_tokens_cap("MBZUAI-IFM/K2-Think-v2") >= 24576
    assert _default_openai_max_tokens_cap("some-unknown-model") >= 24576
    # Older OpenAI chat models keep their hard ceilings.
    assert _default_openai_max_tokens_cap("gpt-3.5-turbo-1106") == 4096
    assert _default_openai_max_tokens_cap("gpt-4o-mini") == 16384


def test_max_tokens_cap_env_override():
    from bmc_agent.llm import _resolved_openai_cap

    with patch.dict("os.environ", {"BMC_AGENT_LLM_MAX_TOKENS_CAP": "40000"}):
        assert _resolved_openai_cap("MBZUAI-IFM/K2-Think-v2") == 40000


def test_unwrap_code_fence():
    from bmc_agent.llm import _unwrap_code_fence, _strip_reasoning_blocks

    assert _unwrap_code_fence("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert _unwrap_code_fence("```\nplain\n```") == "plain"
    # Not wrapped -> untouched.
    assert _unwrap_code_fence("{\"a\": 1}") == '{"a": 1}'
    # Reasoning + fenced answer is fully cleaned.
    assert _strip_reasoning_blocks("reason</think>```json\n{\"a\": 1}\n```") == '{"a": 1}'


def test_openai_truncation_retries_and_escalates_max_tokens():
    """A truncated reasoning trace (finish_reason=length, no </think>) must
    retry with a bigger budget, then return the answer from the next draw."""
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    responses = [
        {  # 1st: truncated mid-think
            "choices": [{"message": {"content": "thinking and thinking"},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 24576},
        },
        {  # 2nd: clean answer
            "choices": [{"message": {"content": "done</think>FINAL"},
                         "finish_reason": "stop"}],
            "usage": {},
        },
    ]
    fake = _make_fake_httpx(responses, captured)
    client = LLMClient(_k2_config())
    with patch("bmc_agent.llm.time.sleep", lambda *_: None):
        with patch.dict("sys.modules", {"httpx": fake}):
            out = client.complete("s", "u", max_tokens=4096)
    assert out == "FINAL"
    payloads = captured["payloads"]
    assert len(payloads) == 2, "should have retried once"
    # 1st request used the 24576 floor; the retry escalated toward the cap.
    assert payloads[0]["max_tokens"] == 24576
    assert payloads[1]["max_tokens"] == 32768


def test_validator_requery_then_success():
    """A response failing the caller's validator is re-sampled; a later
    conforming draw is returned."""
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    responses = [
        {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}],
         "usage": {}},
        {"choices": [{"message": {"content": "{\"ok\": 1}"}, "finish_reason": "stop"}],
         "usage": {}},
    ]
    fake = _make_fake_httpx(responses, captured)
    client = LLMClient(_k2_config())
    from bmc_agent.json_utils import extract_json_object
    with patch("bmc_agent.llm.time.sleep", lambda *_: None):
        with patch.dict("sys.modules", {"httpx": fake}):
            out = client.complete(
                "s", "u",
                validate=lambda t: extract_json_object(t) is not None,
            )
    assert out == '{"ok": 1}'
    assert len(captured["payloads"]) == 2


def test_validator_exhaustion_returns_best_effort_without_raising():
    """When every draw fails validation, complete() returns the last response
    (best-effort) rather than raising — callers keep their own parse handling."""
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    responses = [
        {"choices": [{"message": {"content": "still not json"}, "finish_reason": "stop"}],
         "usage": {}},
    ]
    fake = _make_fake_httpx(responses, captured)
    cfg = _k2_config()
    cfg.max_spec_retries = 2
    client = LLMClient(cfg)
    with patch("bmc_agent.llm.time.sleep", lambda *_: None):
        with patch.dict("sys.modules", {"httpx": fake}):
            out = client.complete("s", "u", validate=lambda t: False)
    assert out == "still not json"
    assert len(captured["payloads"]) == 2  # tried the full budget


def test_complete_json_returns_parsed_object():
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    responses = [
        {"choices": [{"message": {"content": "reason</think>```json\n{\"a\": 2}\n```"},
                      "finish_reason": "stop"}], "usage": {}},
    ]
    fake = _make_fake_httpx(responses, captured)
    client = LLMClient(_k2_config())
    with patch.dict("sys.modules", {"httpx": fake}):
        obj = client.complete_json("s", "u")
    assert obj == {"a": 2}


def test_complete_json_raises_when_never_parseable():
    from bmc_agent.llm import LLMClient, LLMError

    captured: dict = {}
    responses = [
        {"choices": [{"message": {"content": "no json here"}, "finish_reason": "stop"}],
         "usage": {}},
    ]
    fake = _make_fake_httpx(responses, captured)
    cfg = _k2_config()
    cfg.max_spec_retries = 2
    client = LLMClient(cfg)
    with patch("bmc_agent.llm.time.sleep", lambda *_: None):
        with patch.dict("sys.modules", {"httpx": fake}):
            with pytest.raises(LLMError):
                client.complete_json("s", "u")


# ---------------------------------------------------------------------------
# OpenRouter app-attribution headers (https://openrouter.ai/docs/app-attribution)
# ---------------------------------------------------------------------------

def test_openrouter_attribution_headers_values():
    from bmc_agent.llm import openrouter_attribution_headers

    h = openrouter_attribution_headers()
    assert h["HTTP-Referer"] == "https://aprover.ai"
    assert h["X-OpenRouter-Title"] == "AProver"
    assert h["X-Title"] == "AProver"  # backwards-compat alias


def test_openai_request_carries_openrouter_attribution_headers():
    """Every OpenAI-compatible request must carry the OpenRouter attribution
    headers so AProver shows up in OpenRouter's app rankings / analytics."""
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    fake = _make_fake_httpx([{
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }], captured)
    config = Config(
        llm_model="anthropic/claude-sonnet-4.5",
        llm_api_key="or-key",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_provider="openai",
    )
    client = LLMClient(config)
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", max_tokens=64, temperature=0.0)
    headers = captured["headers"]
    assert headers["HTTP-Referer"] == "https://aprover.ai"
    assert headers["X-OpenRouter-Title"] == "AProver"
    assert headers["X-Title"] == "AProver"


def test_openai_request_carries_attribution_headers_on_k2_too():
    """Attribution headers are harmless to non-OpenRouter providers and are
    attached unconditionally — verify they reach the wire even on the K2
    endpoint."""
    from bmc_agent.llm import LLMClient

    captured: dict = {}
    fake = _make_fake_httpx([{
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }], captured)
    client = LLMClient(_k2_config())
    with patch.dict("sys.modules", {"httpx": fake}):
        client.complete("s", "u", max_tokens=64)
    headers = captured["headers"]
    assert headers["HTTP-Referer"] == "https://aprover.ai"
    assert headers["X-OpenRouter-Title"] == "AProver"
