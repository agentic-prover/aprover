"""
Tests for ``bmc_agent.agents.realism_tools.RealismToolsAgent``.

First migration of a ``complete_with_tools`` call site (C2 step 7).
The agent owns: tool-use loop dispatch, ToolUseResult stashing,
parse to ``RealismCheckResult``. The orchestrator (RealismChecker)
keeps the reconsider-prompt construction and the downstream
grounding audit (which reads tool_use_result.messages).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _fake_tool_use_result(text: str, *, iterations=2, tool_calls=1, messages=None, error=""):
    """Build a ToolUseResult-like SimpleNamespace for mocking the LLM."""
    return SimpleNamespace(
        text=text,
        iterations=iterations,
        tool_calls_made=tool_calls,
        messages=messages or [],
        error=error,
    )


def _make_agent(llm, *, system_prompt="SP"):
    from bmc_agent.agents.realism_tools import RealismToolsAgent
    from bmc_agent.config import Config
    parsed = SimpleNamespace(
        functions={}, call_graph={}, function_bodies={},
        function_definitions={}, struct_definitions={},
    )
    return RealismToolsAgent(
        config=Config(llm_api_key="t"), llm=llm,
        system_prompt=system_prompt,
        parsed_file=parsed,
        all_specs={},
    )


def _verdict_payload(verdict: str = "UNREALISTIC"):
    return {
        "verdict": verdict,
        "reasoning": "tool-grounded reasoning",
        "key_concern": "...",
        "confidence": "high",
    }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_agent_routes_via_realism_role():
    """Same role as the primary realism check + adjacent bug —
    one BMC_AGENT_LLM_REALISM_MODEL setting upgrades all three."""
    from bmc_agent.agents.realism_tools import RealismToolsAgent
    assert RealismToolsAgent.name == "realism"


def test_agent_carries_parsed_file_and_all_specs():
    """The realism tools need parsed_file + all_specs to look up
    function bodies / callee POST clauses mid-reasoning. Verify the
    agent holds them — the defensive dict() copy happens at tool-
    context-build time inside _call_llm, not at construction."""
    parsed = SimpleNamespace(functions={"fn": "sig"})
    specs = {"fn": "spec"}
    from bmc_agent.agents.realism_tools import RealismToolsAgent
    from bmc_agent.config import Config
    agent = RealismToolsAgent(
        config=Config(llm_api_key="t"), llm=MagicMock(),
        system_prompt="SP", parsed_file=parsed, all_specs=specs,
    )
    assert agent.parsed_file is parsed
    assert agent.all_specs == specs


def test_agent_uses_tool_use_loop_bounds_from_call_site():
    """The bounded-loop limits (6 iterations / 3 tool calls) match
    the pre-existing realism_checker._augment_with_tools call."""
    from bmc_agent.agents.realism_tools import RealismToolsAgent
    assert RealismToolsAgent.max_iterations_param == 6
    assert RealismToolsAgent.max_tool_calls_param == 3


# ---------------------------------------------------------------------------
# _call_llm dispatches to complete_with_tools
# ---------------------------------------------------------------------------

def test_call_llm_invokes_complete_with_tools_not_complete():
    """Confirm the tool-use path is taken (not the single-call path)."""
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        json.dumps(_verdict_payload())
    )
    _make_agent(llm).run(user_prompt="P", func_name="fn")
    llm.complete_with_tools.assert_called_once()
    llm.complete.assert_not_called()


def test_call_llm_passes_realism_role():
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        json.dumps(_verdict_payload())
    )
    _make_agent(llm).run(user_prompt="P", func_name="fn")
    kwargs = llm.complete_with_tools.call_args.kwargs
    assert kwargs.get("role") == "realism"


def test_call_llm_passes_bounded_loop_kwargs():
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        json.dumps(_verdict_payload())
    )
    _make_agent(llm).run(user_prompt="P", func_name="fn")
    kwargs = llm.complete_with_tools.call_args.kwargs
    assert kwargs.get("max_iterations") == 6
    assert kwargs.get("max_tool_calls") == 3
    assert kwargs.get("max_tokens_per_turn") == 4096


# ---------------------------------------------------------------------------
# Tool-use result surfaces through AgentResult
# ---------------------------------------------------------------------------

def test_run_surfaces_tool_use_result_on_success():
    """The orchestrator needs ToolUseResult.messages for the grounding
    audit. AgentResult.tool_use_result carries it."""
    llm = MagicMock()
    fake_messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "lookup_function",
                          "arguments": '{"name":"fn"}'}},
        ]},
    ]
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        json.dumps(_verdict_payload()),
        iterations=3, tool_calls=2, messages=fake_messages,
    )
    result = _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert result.ok is True
    assert result.tool_use_result is not None
    assert result.tool_use_result.iterations == 3
    assert result.tool_use_result.tool_calls_made == 2
    assert len(result.tool_use_result.messages) == 1
    assert result.tool_calls_made == 2   # also surfaced at top level


def test_run_returns_failure_when_complete_with_tools_caps_loop():
    """When the LLM exceeds max_iterations or max_tool_calls,
    ToolUseResult.error is non-empty. Agent treats this as a soft
    error — surfaces in AgentResult.error so the orchestrator can
    log the termination reason."""
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        "",  # no text (capped before final response)
        iterations=6, tool_calls=3,
        error="max_iterations reached",
    )
    result = _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert result.ok is False
    assert "tool_use_terminated" in result.error
    assert "max_iterations" in result.error


def test_run_returns_failure_on_llm_error():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete_with_tools.side_effect = LLMError("network down")
    result = _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert result.ok is False
    assert "LLMError" in result.error


# ---------------------------------------------------------------------------
# parse delegates to _parse_result
# ---------------------------------------------------------------------------

def test_parse_returns_realism_check_result():
    from bmc_agent.realism_checker import RealismVerdict
    agent = _make_agent(MagicMock())
    agent._func_name = "fn"
    out = agent.parse(json.dumps(_verdict_payload("UNREALISTIC")))
    assert out is not None
    assert out.verdict == RealismVerdict.UNREALISTIC


def test_parse_empty_returns_none():
    assert _make_agent(MagicMock()).parse("") is None


# ---------------------------------------------------------------------------
# Build_prompt is pass-through
# ---------------------------------------------------------------------------

def test_build_prompt_is_passthrough():
    agent = _make_agent(MagicMock())
    out = agent.build_prompt(user_prompt="prerendered-reconsider", func_name="fn")
    assert out == "prerendered-reconsider"
    assert agent._func_name == "fn"
