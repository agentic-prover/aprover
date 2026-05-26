"""
Tests for ``bmc_agent.agents.realism.RealismAgent``.

The agent owns Pass 1 (the primary verdict) of the realism check —
the LLM call + parse boundary. The orchestrating ``RealismChecker``
keeps the witness-pattern pre-checks, hint injection, Pass 2 /
adjacent-bug / tool-use augmentation, and grounding audit.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _make_agent(llm, *, system_prompt="SP"):
    from bmc_agent.agents.realism import RealismAgent
    from bmc_agent.config import Config
    return RealismAgent(
        config=Config(llm_api_key="t"), llm=llm,
        system_prompt=system_prompt,
    )


def _valid_realistic_payload():
    """A realism response payload — REALISTIC requires source-line-guard
    and public-api-call-chain fields per the schema."""
    return {
        "verdict": "REALISTIC",
        "reasoning": "deref happens, no guard, public caller reaches it",
        "key_concern": "NULL deref at line 14",
        "confidence": "high",
        "source_line_guard": (
            "Line 14: rb->buf[rb->pos] = byte; no NULL check before this "
            "assignment. The function trusts its caller."
        ),
        "public_api_call_chain": (
            "external_input → public_api_entry(buf, len) → rb_write(rb, buf, len)"
        ),
    }


def _valid_unrealistic_payload():
    return {
        "verdict": "UNREALISTIC",
        "reasoning": "caller-contract slip — real callers obey magic check",
        "key_concern": "missing PRE for handle validation",
        "confidence": "high",
    }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_agent_name_is_realism():
    """Routes via BMC_AGENT_LLM_REALISM_* env vars — same role as
    augmentation calls so users upgrade the realism backbone in one
    place."""
    from bmc_agent.agents.realism import RealismAgent
    assert RealismAgent.name == "realism"


def test_agent_accepts_per_instance_system_prompt():
    from bmc_agent.agents.realism import RealismAgent
    from bmc_agent.config import Config
    a1 = _make_agent(MagicMock(), system_prompt="SP1")
    a2 = _make_agent(MagicMock(), system_prompt="SP2")
    assert a1.system_prompt == "SP1"
    assert a2.system_prompt == "SP2"


# ---------------------------------------------------------------------------
# LLM-call kwargs (thinking mode)
# ---------------------------------------------------------------------------

def test_llm_call_kwargs_thinking_off_default():
    agent = _make_agent(MagicMock())
    agent._use_thinking = False
    kw = agent._llm_call_kwargs()
    assert kw["max_tokens"] == 4096
    assert kw["thinking"] is False
    assert "thinking_budget" not in kw


def test_llm_call_kwargs_thinking_on_extends_budget():
    """When extended thinking is on, max_tokens budget extends by 4000
    (mirrors the pre-existing realism_checker call site)."""
    agent = _make_agent(MagicMock())
    agent._use_thinking = True
    kw = agent._llm_call_kwargs()
    assert kw["max_tokens"] == 4096 + 4000
    assert kw["thinking"] is True
    assert kw["thinking_budget"] == 4000


def test_run_threads_thinking_through_to_llm():
    """The use_thinking flag must reach LLMClient.complete via
    _llm_call_kwargs — without this, the thinking budget is wrong on
    the second/third call after one with the flag set."""
    llm = MagicMock()
    llm.complete.return_value = json.dumps(_valid_unrealistic_payload())
    _make_agent(llm).run(
        user_prompt="P", func_name="fn", use_thinking=True,
    )
    kwargs = llm.complete.call_args.kwargs
    assert kwargs["thinking"] is True
    assert kwargs["max_tokens"] == 4096 + 4000


def test_run_threads_thinking_false_default():
    llm = MagicMock()
    llm.complete.return_value = json.dumps(_valid_unrealistic_payload())
    _make_agent(llm).run(user_prompt="P", func_name="fn")  # use_thinking=False default
    kwargs = llm.complete.call_args.kwargs
    assert kwargs["thinking"] is False


def test_run_uses_realism_role():
    llm = MagicMock()
    llm.complete.return_value = json.dumps(_valid_unrealistic_payload())
    _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert llm.complete.call_args.kwargs.get("role") == "realism"


def test_run_consecutive_calls_dont_leak_thinking_state():
    """If call 1 sets use_thinking=True and call 2 doesn't, call 2's
    kwargs should NOT carry over thinking=True. Stateful instance
    fields must be set per call."""
    llm = MagicMock()
    llm.complete.return_value = json.dumps(_valid_unrealistic_payload())
    agent = _make_agent(llm)
    agent.run(user_prompt="P1", func_name="fn", use_thinking=True)
    agent.run(user_prompt="P2", func_name="fn")  # default = False
    second_call_kwargs = llm.complete.call_args_list[-1].kwargs
    assert second_call_kwargs["thinking"] is False


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_passes_user_prompt_through():
    """The orchestrator pre-renders the prompt — agent is transparent."""
    agent = _make_agent(MagicMock())
    out = agent.build_prompt(
        user_prompt="my-rendered-prompt", func_name="fn",
    )
    assert out == "my-rendered-prompt"


def test_build_prompt_captures_func_name_and_thinking_flag():
    agent = _make_agent(MagicMock())
    agent.build_prompt(user_prompt="P", func_name="my_fn", use_thinking=True)
    assert agent._func_name == "my_fn"
    assert agent._use_thinking is True


# ---------------------------------------------------------------------------
# parse — delegates to _parse_result, which has its own coverage
# ---------------------------------------------------------------------------

def test_parse_unrealistic_response():
    from bmc_agent.realism_checker import RealismVerdict
    agent = _make_agent(MagicMock())
    agent._func_name = "fn"
    out = agent.parse(json.dumps(_valid_unrealistic_payload()))
    assert out is not None
    assert out.verdict == RealismVerdict.UNREALISTIC


def test_parse_realistic_response():
    from bmc_agent.realism_checker import RealismVerdict
    agent = _make_agent(MagicMock())
    agent._func_name = "fn"
    out = agent.parse(json.dumps(_valid_realistic_payload()))
    assert out is not None
    assert out.verdict == RealismVerdict.REALISTIC


def test_parse_empty_returns_none():
    """Empty response → None (BaseAgent.run reports as parse error)."""
    assert _make_agent(MagicMock()).parse("") is None


# ---------------------------------------------------------------------------
# Full run()
# ---------------------------------------------------------------------------

def test_run_returns_realism_result_on_success():
    from bmc_agent.realism_checker import RealismVerdict
    llm = MagicMock()
    llm.complete.return_value = json.dumps(_valid_unrealistic_payload())
    result = _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert result.ok is True
    assert result.output.verdict == RealismVerdict.UNREALISTIC


def test_run_propagates_llm_error():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("timeout")
    result = _make_agent(llm).run(user_prompt="P", func_name="fn")
    assert result.ok is False
    assert "LLMError" in result.error
