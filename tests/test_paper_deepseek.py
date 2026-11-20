"""Offline checks for the validated DS request settings; no API calls."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bmc_agent.config import Config
from bmc_agent.llm import LLMClient


def _runner():
    path = Path(__file__).resolve().parents[1] / "experiments/paper_reproduction/run.py"
    spec = importlib.util.spec_from_file_location("paper_deepseek_run", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(tmp_path, **overrides):
    return SimpleNamespace(**dict(base_url=None, model="", cbmc_path=None,
                                  frama_c_path=None, autospec_root=None,
                                  output=tmp_path, **overrides))


def _requests(config):
    client = LLMClient(config)
    with patch("httpx.Client") as http:
        post = http.return_value.__enter__.return_value.post
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "done"}}]}
        assert client.complete("system", "user") == "done"
        assert client.complete_with_tools(system_prompt="system", user_prompt="user",
                                          tools=[], tool_handlers={}).text == "done"
        return post.call_args_list


def test_guarded_ds_defaults_reach_both_request_paths(tmp_path):
    run = _runner()
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline-placeholder"}, clear=True):
        env, recorded = run.child_environment(run.PROFILES["rq1-ldv-guarded"], _args(tmp_path))
    assert recorded["BMC_AGENT_LLM_DEFAULT_MODEL"] == "deepseek-flash"
    assert recorded["BMC_AGENT_LLM_DEFAULT_BASE_URL"] == "https://api.deepseek.com/v1"
    assert recorded["BMC_AGENT_DEEPSEEK_THINKING"] == "disabled"
    assert "offline-placeholder" not in json.dumps(recorded)
    with patch.dict(os.environ, env, clear=True):
        calls = _requests(Config.from_env())
    for call in calls:
        assert call.args[0] == "https://api.deepseek.com/v1/chat/completions"
        assert call.kwargs["json"]["thinking"] == {"type": "disabled"}
        assert call.kwargs["json"]["model"] == "deepseek-flash"
        assert call.kwargs["headers"]["Authorization"] == "Bearer offline-placeholder"


@pytest.mark.parametrize("model,mode", [("deepseek-flash", ""), ("anthropic/claude-sonnet-4.5", "disabled")])
def test_other_requests_do_not_gain_deepseek_option(model, mode):
    with patch.dict(os.environ, {"BMC_AGENT_DEEPSEEK_THINKING": mode}, clear=True):
        calls = _requests(Config(llm_model=model, llm_provider="openai",
                                 llm_base_url="https://openrouter.ai/api/v1", llm_api_key="offline"))
    assert all("thinking" not in call.kwargs["json"] for call in calls)


def test_historical_profiles_keep_endpoint_and_clear_ds_setting(tmp_path):
    run = _runner()
    with patch.dict(os.environ, {"BMC_AGENT_DEEPSEEK_THINKING": "disabled",
                                 "OPENROUTER_API_KEY": "offline-router"}, clear=True):
        for name, profile in run.PROFILES.items():
            if name == "rq1-ldv-guarded":
                continue
            env, recorded = run.child_environment(profile, _args(tmp_path))
            assert recorded["BMC_AGENT_LLM_DEFAULT_BASE_URL"] == "https://openrouter.ai/api/v1"
            assert recorded["BMC_AGENT_LLM_DEFAULT_MODEL"] == profile["model"]
            assert env["BMC_AGENT_LLM_DEFAULT_API_KEY"] == "offline-router"
            assert "BMC_AGENT_DEEPSEEK_THINKING" not in env
