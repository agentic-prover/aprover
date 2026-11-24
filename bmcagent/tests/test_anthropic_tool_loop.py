"""Anthropic-native tool-use loop in LLMClient.complete_with_tools.

Previously complete_with_tools raised NotImplementedError for the anthropic
provider (openai-compatible only), so the *_tools.py agents silently fell back
to flat on an anthropic-only deployment. These tests pin the native loop with a
mocked anthropic client (no network)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from bmc_agent.config import Config
from bmc_agent.llm import LLMClient, ToolDef, ToolUseResult


def _mk_client(responses):
    msgs = MagicMock()
    msgs.create.side_effect = responses
    client = MagicMock()
    client.with_options.return_value = client
    client.messages = msgs
    return client


def _tooluse(tid, name, inp):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _resp(blocks, inp=10, out=5):
    return SimpleNamespace(content=blocks,
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


def _client(model="claude-sonnet-4-6"):
    c = Config(); c.llm_provider = "anthropic"; c.llm_model = model
    return LLMClient(c)


TOOLS = [ToolDef(name="get_secret", description="d",
                 parameters={"type": "object", "properties": {}})]


def test_tool_call_then_final_text():
    cl = _client()
    cl._client = _mk_client([
        _resp([_tooluse("t1", "get_secret", {})], 10, 5),
        _resp([_text("answer is 42")], 8, 4),
    ])
    calls = []
    res = cl.complete_with_tools(
        system_prompt="s", user_prompt="u", tools=TOOLS,
        tool_handlers={"get_secret": lambda a: (calls.append(a) or "42")},
        max_iterations=4, max_tool_calls=3)
    assert isinstance(res, ToolUseResult)
    assert res.tool_calls_made == 1
    assert res.iterations == 2
    assert calls == [{}]
    assert "42" in res.text
    assert cl.usage_total_tokens == 27   # 10+5+8+4 -> telemetry plumbed


def test_unknown_tool_reports_error_to_model_not_crash():
    cl = _client()
    cl._client = _mk_client([
        _resp([_tooluse("t1", "nope", {})]),
        _resp([_text("done")]),
    ])
    res = cl.complete_with_tools(
        system_prompt="s", user_prompt="u", tools=TOOLS,
        tool_handlers={}, max_iterations=4, max_tool_calls=3)
    assert res.text == "done"


def test_max_iterations_exceeded_sets_error():
    cl = _client()
    # always asks for a tool -> never terminates -> hits the cap
    cl._client = _mk_client([_resp([_tooluse("t%d" % i, "get_secret", {})]) for i in range(3)])
    res = cl.complete_with_tools(
        system_prompt="s", user_prompt="u", tools=TOOLS,
        tool_handlers={"get_secret": lambda a: "x"},
        max_iterations=3, max_tool_calls=99)
    assert "max_iterations" in res.error


def test_opus_omits_temperature_in_tool_loop():
    cl = _client(model="claude-opus-4-8")
    cl._client = _mk_client([_resp([_text("hi")])])
    cl.complete_with_tools(system_prompt="s", user_prompt="u", tools=TOOLS,
                           tool_handlers={}, max_iterations=2, max_tool_calls=2)
    _, kwargs = cl._client.messages.create.call_args
    assert "temperature" not in kwargs   # opus-4-8 rejects it
