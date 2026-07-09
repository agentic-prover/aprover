"""Tests for the LLM tool-use foundation (LLMClient.complete_with_tools)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from bmc_agent.llm import (
    LLMClient,
    LLMError,
    ToolCall,
    ToolDef,
    ToolUseResult,
    _tools_to_openai_schema,
)


# ---------- schema rendering -----------------------------------------------


def test_tools_to_openai_schema_basic():
    tools = [ToolDef(
        name="add", description="Add two ints",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )]
    out = _tools_to_openai_schema(tools)
    assert out == [{
        "type": "function",
        "function": {
            "name": "add",
            "description": "Add two ints",
            "parameters": tools[0].parameters,
        },
    }]


def test_tools_to_openai_schema_multi():
    tools = [
        ToolDef(name="t1", description="d1", parameters={}),
        ToolDef(name="t2", description="d2", parameters={"type": "object"}),
    ]
    out = _tools_to_openai_schema(tools)
    assert len(out) == 2
    assert out[0]["function"]["name"] == "t1"
    assert out[1]["function"]["name"] == "t2"


# ---------- complete_with_tools loop (mocked httpx) ------------------------


def _mock_config_openai():
    """Build a Config that routes to the openai provider."""
    from bmc_agent.config import Config
    cfg = Config(artifact_dir="/tmp/_llm_tu_test")
    cfg.llm_provider = "openai"
    cfg.llm_base_url = "https://api.example.com/v1"
    cfg.llm_api_key = "test-key-not-real"
    return cfg


def _make_response(status_code=200, *, choices_message=None, json_obj=None):
    """Construct a httpx-like mock response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason_phrase = "OK"
    resp.text = json.dumps(json_obj) if json_obj is not None else ""
    if json_obj is not None:
        resp.json.return_value = json_obj
    elif choices_message is not None:
        resp.json.return_value = {"choices": [{"message": choices_message}],
                                  "usage": {}}
    return resp


def test_complete_with_tools_no_tool_call_returns_text():
    """LLM emits content directly with no tool_calls → terminate first turn."""
    cfg = _mock_config_openai()
    client = LLMClient(cfg)
    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.return_value = _make_response(
            choices_message={"role": "assistant", "content": "HELLO"}
        )
        result = client.complete_with_tools(
            "system", "user",
            tools=[ToolDef(name="t", description="d", parameters={})],
            tool_handlers={},
        )
    assert result.text == "HELLO"
    assert result.iterations == 1
    assert result.tool_calls_made == 0
    assert result.error == ""


def test_complete_with_tools_dispatches_handler():
    """LLM emits one tool_call → handler runs → LLM emits final text."""
    cfg = _mock_config_openai()
    client = LLMClient(cfg)
    handler_calls = []

    def my_handler(args):
        handler_calls.append(args)
        return "tool-output-data"

    # First turn: LLM requests tool. Second turn: LLM returns final text.
    turn1 = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "my_tool", "arguments": '{"x": 42}'},
        }],
    }
    turn2 = {"role": "assistant", "content": "FINAL", "tool_calls": []}

    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.side_effect = [
            _make_response(choices_message=turn1),
            _make_response(choices_message=turn2),
        ]
        result = client.complete_with_tools(
            "sys", "usr",
            tools=[ToolDef(name="my_tool", description="d",
                           parameters={"type": "object"})],
            tool_handlers={"my_tool": my_handler},
        )
    assert result.text == "FINAL"
    assert result.iterations == 2
    assert result.tool_calls_made == 1
    assert handler_calls == [{"x": 42}]


def test_complete_with_tools_caps_max_tool_calls():
    """Tool calls beyond max_tool_calls get rejected with error message."""
    cfg = _mock_config_openai()
    client = LLMClient(cfg)
    call_count = {"n": 0}

    def handler(args):
        call_count["n"] += 1
        return "ok"

    # First turn: 3 tool_calls in one batch. We cap at 2.
    turn1 = {
        "role": "assistant",
        "tool_calls": [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": "t", "arguments": "{}"}}
            for i in range(3)
        ],
    }
    turn2 = {"role": "assistant", "content": "DONE", "tool_calls": []}

    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.side_effect = [
            _make_response(choices_message=turn1),
            _make_response(choices_message=turn2),
        ]
        result = client.complete_with_tools(
            "sys", "usr",
            tools=[ToolDef(name="t", description="d", parameters={})],
            tool_handlers={"t": handler},
            max_tool_calls=2,
        )
    assert result.tool_calls_made == 2  # capped
    assert call_count["n"] == 2          # third never executed


def test_complete_with_tools_unknown_tool_returns_error_to_llm():
    """If LLM calls a tool we didn't register, feed the error back."""
    cfg = _mock_config_openai()
    client = LLMClient(cfg)
    turn1 = {
        "role": "assistant",
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "bogus", "arguments": "{}"},
        }],
    }
    turn2 = {"role": "assistant", "content": "GIVING UP", "tool_calls": []}
    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.side_effect = [
            _make_response(choices_message=turn1),
            _make_response(choices_message=turn2),
        ]
        result = client.complete_with_tools(
            "sys", "usr",
            tools=[ToolDef(name="bogus", description="d", parameters={})],
            tool_handlers={},  # bogus not registered
        )
    # Tool was NOT counted as a successful call.
    assert result.tool_calls_made == 0
    assert result.text == "GIVING UP"
    # Error message should have appeared in messages as a tool result.
    tool_results = [m for m in result.messages if m.get("role") == "tool"]
    assert any("not registered" in m["content"] for m in tool_results)


def test_complete_with_tools_handler_exception_fed_back():
    """If a handler raises, the error becomes a tool result the LLM sees."""
    cfg = _mock_config_openai()
    client = LLMClient(cfg)

    def crashing(args):
        raise RuntimeError("boom")

    turn1 = {
        "role": "assistant",
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "t", "arguments": "{}"},
        }],
    }
    turn2 = {"role": "assistant", "content": "OK", "tool_calls": []}
    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.side_effect = [
            _make_response(choices_message=turn1),
            _make_response(choices_message=turn2),
        ]
        result = client.complete_with_tools(
            "sys", "usr",
            tools=[ToolDef(name="t", description="d", parameters={})],
            tool_handlers={"t": crashing},
        )
    tool_results = [m for m in result.messages if m.get("role") == "tool"]
    assert any("RuntimeError: boom" in m["content"] for m in tool_results)


def test_complete_with_tools_caps_max_iterations():
    """If LLM keeps asking for tools forever, terminate at max_iterations."""
    cfg = _mock_config_openai()
    client = LLMClient(cfg)
    # Every turn returns a tool call. Never a final text.
    tool_turn = {
        "role": "assistant",
        "tool_calls": [{
            "id": "c", "type": "function",
            "function": {"name": "t", "arguments": "{}"},
        }],
    }
    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.side_effect = [
            _make_response(choices_message=tool_turn) for _ in range(10)
        ]
        result = client.complete_with_tools(
            "sys", "usr",
            tools=[ToolDef(name="t", description="d", parameters={})],
            tool_handlers={"t": lambda a: "ok"},
            max_iterations=3,
            max_tool_calls=10,
        )
    assert result.iterations == 3
    assert "max_iterations" in result.error


def test_complete_with_tools_truncates_large_results():
    """Tool result content > result_truncate gets cut + marker appended."""
    cfg = _mock_config_openai()
    client = LLMClient(cfg)
    turn1 = {
        "role": "assistant",
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "t", "arguments": "{}"},
        }],
    }
    turn2 = {"role": "assistant", "content": "OK", "tool_calls": []}
    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.side_effect = [
            _make_response(choices_message=turn1),
            _make_response(choices_message=turn2),
        ]
        result = client.complete_with_tools(
            "sys", "usr",
            tools=[ToolDef(name="t", description="d", parameters={})],
            tool_handlers={"t": lambda a: "X" * 10000},
            result_truncate=100,
        )
    tool_results = [m for m in result.messages if m.get("role") == "tool"]
    assert tool_results
    assert len(tool_results[0]["content"]) <= 100 + len("\n…[truncated]")
    assert "[truncated]" in tool_results[0]["content"]


def test_complete_with_tools_anthropic_uses_native_loop():
    """Anthropic provider now routes to the native tool-use loop (previously
    raised NotImplementedError; see _anthropic_tool_use_loop)."""
    from unittest.mock import patch
    from bmc_agent.config import Config
    cfg = Config(artifact_dir="/tmp/_x")
    cfg.llm_provider = "anthropic"
    cfg.llm_api_key = "x"
    client = LLMClient(cfg)
    with patch.object(LLMClient, "_anthropic_tool_use_loop", return_value="SENTINEL") as m:
        out = client.complete_with_tools(
            "s", "u",
            tools=[ToolDef(name="t", description="d", parameters={})],
            tool_handlers={},
        )
    assert out == "SENTINEL"
    assert m.called


def test_complete_with_tools_codex_dispatches_handler():
    """Codex provider uses the bounded JSON protocol instead of Anthropic."""
    from bmc_agent.config import Config

    cfg = Config(artifact_dir="/tmp/_llm_tu_test")
    cfg.llm_provider = "codex"
    client = LLMClient(cfg)
    handler_calls = []

    def handler(args):
        handler_calls.append(args)
        return {"answer": 42}

    with patch.object(
        LLMClient,
        "_complete_codex",
        side_effect=[
            '{"tool":"lookup","arguments":{"name":"target"}}',
            '{"final":"DONE"}',
        ],
    ) as mock_codex:
        result = client.complete_with_tools(
            "sys",
            "usr",
            tools=[
                ToolDef(
                    name="lookup",
                    description="Look up a target",
                    parameters={"type": "object"},
                )
            ],
            tool_handlers={"lookup": handler},
            max_iterations=3,
            max_tool_calls=2,
        )

    assert result.text == "DONE"
    assert result.iterations == 2
    assert result.tool_calls_made == 1
    assert handler_calls == [{"name": "target"}]
    assert mock_codex.call_count == 2


def test_complete_with_tools_codex_accepts_domain_json_as_final():
    """Tool-using agents often request a final domain JSON object."""
    from bmc_agent.config import Config

    cfg = Config(artifact_dir="/tmp/_llm_tu_test")
    cfg.llm_provider = "codex"
    client = LLMClient(cfg)

    with patch.object(
        LLMClient,
        "_complete_codex",
        return_value='{"flags":{"signed_overflow":true},"reasoning":"needed"}',
    ) as mock_codex:
        result = client.complete_with_tools(
            "sys",
            "usr",
            tools=[ToolDef(name="lookup", description="d", parameters={})],
            tool_handlers={"lookup": lambda args: "unused"},
            max_iterations=3,
        )

    assert result.text == '{"flags":{"signed_overflow":true},"reasoning":"needed"}'
    assert result.iterations == 1
    assert result.tool_calls_made == 0
    assert mock_codex.call_count == 1


def test_complete_codex_uses_ephemeral_read_only_and_guard():
    """Codex CLI calls should be stateless and read-only."""
    from pathlib import Path
    from bmc_agent.config import Config

    cfg = Config(artifact_dir="/tmp/_llm_tu_test")
    cfg.llm_provider = "codex"
    cfg.codex_bin = "codex"
    cfg.codex_timeout_s = 77
    client = LLMClient(cfg)
    seen = {}

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *, input=None, capture_output=None, text=None, timeout=None):
        seen["cmd"] = cmd
        seen["input"] = input
        seen["timeout"] = timeout
        out_path = Path(cmd[cmd.index("--output-last-message") + 1])
        out_path.write_text("FINAL", encoding="utf-8")
        return Proc()

    with patch("subprocess.run", side_effect=fake_run):
        out = client._complete_codex("SYS", "USER", 128, 0.0)

    assert out == "FINAL"
    assert seen["cmd"][:2] == ["codex", "exec"]
    assert "--ephemeral" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--sandbox") + 1] == "read-only"
    assert seen["timeout"] == 77.0
    assert seen["input"].startswith(
        "You are executing a single stateless BMC-Agent backend request."
    )


def test_complete_with_tools_serializes_non_string_result():
    """Handler returning a dict/list gets json.dumps'd before being sent back."""
    cfg = _mock_config_openai()
    client = LLMClient(cfg)
    turn1 = {
        "role": "assistant",
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "t", "arguments": "{}"},
        }],
    }
    turn2 = {"role": "assistant", "content": "OK", "tool_calls": []}
    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.side_effect = [
            _make_response(choices_message=turn1),
            _make_response(choices_message=turn2),
        ]
        result = client.complete_with_tools(
            "sys", "usr",
            tools=[ToolDef(name="t", description="d", parameters={})],
            tool_handlers={"t": lambda a: {"x": 1, "y": [1, 2, 3]}},
        )
    tool_results = [m for m in result.messages if m.get("role") == "tool"]
    payload = json.loads(tool_results[0]["content"])
    assert payload == {"x": 1, "y": [1, 2, 3]}


def test_complete_with_tools_http_error_raises_llm_error():
    cfg = _mock_config_openai()
    client = LLMClient(cfg)
    err_resp = MagicMock()
    err_resp.status_code = 500
    err_resp.reason_phrase = "Internal Server Error"
    err_resp.text = "server exploded"
    with patch("httpx.Client") as mock_httpx:
        instance = mock_httpx.return_value.__enter__.return_value
        instance.post.return_value = err_resp
        with pytest.raises(LLMError, match="HTTP 500"):
            client.complete_with_tools(
                "s", "u",
                tools=[ToolDef(name="t", description="d", parameters={})],
                tool_handlers={"t": lambda a: "ok"},
            )
