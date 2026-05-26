"""
Tests for ``bmc_agent.agents.adjacent_bug.AdjacentBugAgent``.

The agent runs the second LLM call in the realism flow — when the
primary check rejected CBMC's CEx (UNREALISTIC), AdjacentBugAgent
hunts for OTHER exploitable defects in the same function.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _make_agent(llm, *, system_prompt="SP"):
    from bmc_agent.agents.adjacent_bug import AdjacentBugAgent
    from bmc_agent.config import Config
    return AdjacentBugAgent(
        config=Config(llm_api_key="t"), llm=llm,
        system_prompt=system_prompt,
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_agent_routes_via_realism_role():
    """The role is intentionally 'realism' (not 'adjacent_bug') so the
    adjacent-bug call rides the same model as the primary realism
    check. Upgrading BMC_AGENT_LLM_REALISM_MODEL upgrades both."""
    from bmc_agent.agents.adjacent_bug import AdjacentBugAgent
    assert AdjacentBugAgent.name == "realism"


def test_agent_accepts_per_instance_system_prompt():
    a1 = _make_agent(MagicMock(), system_prompt="SP1")
    a2 = _make_agent(MagicMock(), system_prompt="SP2")
    assert a1.system_prompt == "SP1"
    assert a2.system_prompt == "SP2"


# ---------------------------------------------------------------------------
# LLM-call kwargs
# ---------------------------------------------------------------------------

def test_llm_call_kwargs_4096_thinking_off():
    agent = _make_agent(MagicMock())
    kw = agent._llm_call_kwargs()
    assert kw["max_tokens"] == 4096
    assert kw["thinking"] is False


def test_run_passes_role_and_kwargs():
    llm = MagicMock()
    llm.complete.return_value = json.dumps({"adjacent_bugs": []})
    _make_agent(llm).run(user_prompt="P", func_name="fn")
    kwargs = llm.complete.call_args.kwargs
    assert kwargs.get("role") == "realism"
    assert kwargs.get("max_tokens") == 4096
    assert kwargs.get("thinking") is False


# ---------------------------------------------------------------------------
# build_prompt is a pass-through
# ---------------------------------------------------------------------------

def test_build_prompt_passes_user_prompt_through():
    agent = _make_agent(MagicMock())
    out = agent.build_prompt(user_prompt="rendered-by-orchestrator", func_name="fn")
    assert out == "rendered-by-orchestrator"


def test_build_prompt_captures_func_name():
    agent = _make_agent(MagicMock())
    agent.build_prompt(user_prompt="P", func_name="my_fn")
    assert agent._func_name == "my_fn"


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def test_parse_returns_empty_list_when_no_adjacent_bugs():
    """LLM saying "no adjacent bugs" is a valid answer — surface as
    empty list, NOT None (None would be reported as parse error)."""
    agent = _make_agent(MagicMock())
    agent._func_name = "fn"
    out = agent.parse(json.dumps({"adjacent_bugs": []}))
    assert out == []


def test_parse_returns_candidates_when_present():
    """The _parse_adjacent_bugs schema is
    ``{location, bug_type, attacker_scenario, confidence}``. Entries
    without ``attacker_scenario`` are dropped (it's the load-bearing
    field — without it the candidate is hand-waving)."""
    agent = _make_agent(MagicMock())
    agent._func_name = "fn"
    out = agent.parse(json.dumps({
        "adjacent_bugs": [
            {
                "location": "line 42",
                "bug_type": "missing bounds check",
                "attacker_scenario": "user-controlled offset exceeds buf size",
                "confidence": "medium",
            },
        ],
    }))
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["attacker_scenario"].startswith("user-controlled offset")
    assert out[0]["bug_type"] == "missing bounds check"


def test_parse_drops_entries_without_attacker_scenario():
    """An entry missing ``attacker_scenario`` is filtered out — the
    LLM frequently emits half-formed candidates with location set
    but no actual exploit narrative."""
    agent = _make_agent(MagicMock())
    agent._func_name = "fn"
    out = agent.parse(json.dumps({
        "adjacent_bugs": [
            {"location": "line 42", "attacker_scenario": "real bug",
             "confidence": "high"},
            {"location": "line 99"},  # missing attacker_scenario → drop
        ],
    }))
    assert len(out) == 1
    assert out[0]["attacker_scenario"] == "real bug"


def test_parse_empty_response_returns_none():
    """Empty response → None (BaseAgent.run() reports as parse error
    so the orchestrator falls back to its except-handler — no adjacent
    bugs attached, primary verdict preserved)."""
    assert _make_agent(MagicMock()).parse("") is None


# ---------------------------------------------------------------------------
# Full run()
# ---------------------------------------------------------------------------

def test_run_returns_list_on_success():
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "adjacent_bugs": [
            {"function": "fn", "location": "line 1",
             "attacker_scenario": "x", "severity": "low"},
        ],
    })
    result = _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert result.ok is True
    assert len(result.output) == 1


def test_run_empty_list_still_counts_as_success():
    """An empty adjacent-bug list is a VALID outcome (LLM said "no
    other bugs found"). result.ok must be True so the orchestrator
    treats it as a clean "nothing to attach" rather than retrying."""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({"adjacent_bugs": []})
    result = _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert result.ok is True
    assert result.output == []


def test_run_handles_llm_error():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("timeout")
    result = _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert result.ok is False
    assert "LLMError" in result.error
