"""
Tests for ``bmc_agent.agents.spec_gen_tools.SpecGenWithToolsAgent``.

The v2.2 tool-use spec-gen refinement (C2 step 8). The agent
owns: SpecToolContext build + complete_with_tools dispatch + parse
into Spec. Orchestrator (SpecGeneratorV2) keeps the trigger gating
(when to invoke tool-use vs base) and the prompt augmentation
(appending TOOL_USE_PROMPT_ADDENDUM).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _fake_tool_use_result(text: str, *, iterations=2, tool_calls=1, messages=None, error=""):
    return SimpleNamespace(
        text=text,
        iterations=iterations,
        tool_calls_made=tool_calls,
        messages=messages or [],
        error=error,
    )


def _make_agent(llm, *, system_prompt="SP"):
    from bmc_agent.agents.spec_gen_tools import SpecGenWithToolsAgent
    from bmc_agent.config import Config
    parsed = SimpleNamespace(
        functions={"fn": SimpleNamespace(parameters=[("int", "x")], return_type="int")},
        call_graph={}, function_bodies={},
        function_definitions={}, struct_definitions={},
    )
    return SpecGenWithToolsAgent(
        config=Config(llm_api_key="t"), llm=llm,
        system_prompt=system_prompt,
        parsed=parsed,
        corpus_paths=[Path("/tmp/fake.c")],
        all_specs_so_far={},
        boundary_detector=None,
    )


def _valid_spec_payload():
    return {
        "pre_validity": [
            {"clause": "x > 0", "evidence": ["body:L3"]},
        ],
        "pre_protocol": [],
        "postcondition": [
            {"clause": "result >= 0", "evidence": ["body:L7"]},
        ],
        "spec_disagreement": False,
    }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_agent_routes_via_spec_gen_role():
    """Same role as the base SpecGenAgent so upgrades follow one
    BMC_AGENT_LLM_SPEC_GEN_MODEL setting."""
    from bmc_agent.agents.spec_gen_tools import SpecGenWithToolsAgent
    assert SpecGenWithToolsAgent.name == "spec_gen"


def test_bounded_loop_matches_call_site():
    """max_iterations=8 / max_tool_calls=5 mirror the pre-existing
    SpecGeneratorV2._generate_with_tools."""
    from bmc_agent.agents.spec_gen_tools import SpecGenWithToolsAgent
    assert SpecGenWithToolsAgent.max_iterations_param == 8
    assert SpecGenWithToolsAgent.max_tool_calls_param == 5


# ---------------------------------------------------------------------------
# _call_llm
# ---------------------------------------------------------------------------

def test_call_llm_uses_complete_with_tools_not_complete():
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        json.dumps(_valid_spec_payload())
    )
    _make_agent(llm).run(prompt="P", fn_name="fn")
    llm.complete_with_tools.assert_called_once()
    llm.complete.assert_not_called()


def test_call_llm_passes_spec_gen_role():
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        json.dumps(_valid_spec_payload())
    )
    _make_agent(llm).run(prompt="P", fn_name="fn")
    kwargs = llm.complete_with_tools.call_args.kwargs
    assert kwargs.get("role") == "spec_gen"
    assert kwargs.get("max_iterations") == 8
    assert kwargs.get("max_tool_calls") == 5


# ---------------------------------------------------------------------------
# run() — happy path
# ---------------------------------------------------------------------------

def test_run_returns_spec_on_valid_response():
    from bmc_agent.spec import SpecStatus
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        json.dumps(_valid_spec_payload())
    )
    result = _make_agent(llm).run(prompt="P", fn_name="fn")
    assert result.ok is True
    assert result.output.function_name == "fn"
    assert result.output.status == SpecStatus.GENERATED
    assert result.output.precondition == "x > 0"


def test_run_surfaces_tool_use_result():
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        json.dumps(_valid_spec_payload()),
        iterations=4, tool_calls=3,
    )
    result = _make_agent(llm).run(prompt="P", fn_name="fn")
    assert result.tool_use_result is not None
    assert result.tool_use_result.iterations == 4
    assert result.tool_calls_made == 3


# ---------------------------------------------------------------------------
# run() — error / parse paths
# ---------------------------------------------------------------------------

def test_run_failure_on_terminated_loop():
    """When complete_with_tools caps the loop (max_iterations /
    max_tool_calls), ToolUseResult.error is non-empty — agent
    surfaces with the ``tool_use_terminated:`` prefix so the
    orchestrator can route to the right log line."""
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        "", iterations=8, tool_calls=5, error="max_iterations reached",
    )
    result = _make_agent(llm).run(prompt="P", fn_name="fn")
    assert result.ok is False
    assert "tool_use_terminated" in result.error


def test_run_failure_on_llm_error():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete_with_tools.side_effect = LLMError("backend down")
    result = _make_agent(llm).run(prompt="P", fn_name="fn")
    assert result.ok is False
    assert "LLMError" in result.error


def test_run_failure_on_unparseable_response():
    """LLM returned text but it isn't valid JSON / doesn't validate.
    parse() returns None → BaseAgent.run reports parse error."""
    llm = MagicMock()
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        "not a spec"
    )
    result = _make_agent(llm).run(prompt="P", fn_name="fn")
    assert result.ok is False


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def test_parse_returns_none_on_empty():
    assert _make_agent(MagicMock()).parse("") is None


def test_parse_returns_none_on_no_json():
    agent = _make_agent(MagicMock())
    agent._fn_name = "fn"
    assert agent.parse("just prose") is None


def test_parse_fenced_markdown():
    agent = _make_agent(MagicMock())
    agent._fn_name = "fn"
    out = agent.parse(
        "```json\n" + json.dumps(_valid_spec_payload()) + "\n```"
    )
    assert out is not None
    assert out.precondition == "x > 0"


# ---------------------------------------------------------------------------
# build_prompt is pass-through (orchestrator pre-appends the addendum)
# ---------------------------------------------------------------------------

def test_build_prompt_passes_through():
    agent = _make_agent(MagicMock())
    out = agent.build_prompt(prompt="rendered+addendum", fn_name="fn")
    assert out == "rendered+addendum"
    assert agent._fn_name == "fn"
