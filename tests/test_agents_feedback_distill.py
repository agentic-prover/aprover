"""
Tests for ``bmc_agent.agents.feedback_distill.FeedbackDistillAgent``.

Covers:
  * Agent name routes via BMC_AGENT_LLM_FEEDBACK_DISTILL_* env vars
  * build_prompt substitutes verdict / function / witness / reasoning /
    existing clauses into the DISTILL_PROMPT template
  * _llm_call_kwargs sets max_tokens + thinking=False (reasoning-model budget /
    distill-doesn't-need-thinking concerns)
  * parse returns Remediation for each scope value
  * Full run() success / LLM-error
  * Standalone learn_from_rejection wrapper still works (gates on
    realism verdict + delegates to agent)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _make_agent(llm):
    from bmc_agent.agents.feedback_distill import FeedbackDistillAgent
    from bmc_agent.config import Config
    return FeedbackDistillAgent(config=Config(llm_api_key="t"), llm=llm)


def _make_inputs(*, verdict_str: str = "unrealistic"):
    """Build the kwargs expected by build_prompt(). Uses simple stand-ins
    so we don't need to construct a full FunctionInfo / Counterexample
    from the parser — only the attributes the prompt reads matter."""
    from types import SimpleNamespace
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    return dict(
        func=SimpleNamespace(name="fn", body="{ return 0; }"),
        counterexample=SimpleNamespace(
            failing_property="fn.assertion.1",
            variable_assignments={"x": "0", "y": "NULL"},
        ),
        realism=RealismCheckResult(
            verdict=RealismVerdict(verdict_str),
            reasoning="caller-contract slip",
            key_concern="missing NULL guard",
        ),
        existing_project_clauses=["g_init != 0"],
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_agent_name_is_feedback_distill():
    """Role identifier must match the env-var routing key."""
    from bmc_agent.agents.feedback_distill import FeedbackDistillAgent
    assert FeedbackDistillAgent.name == "feedback_distill"


def test_agent_system_prompt_is_spec_system_prompt():
    """Distill uses the same system prompt as spec-gen (both reason
    about DSL clauses + function semantics)."""
    from bmc_agent.agents.feedback_distill import FeedbackDistillAgent
    from bmc_agent.prompts import SPEC_SYSTEM_PROMPT
    from bmc_agent.config import Config
    agent = FeedbackDistillAgent(
        config=Config(llm_api_key="t"), llm=MagicMock(),
    )
    assert agent.system_prompt == SPEC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# LLM-call kwargs
# ---------------------------------------------------------------------------

def test_llm_call_kwargs_sets_max_tokens_and_thinking():
    """Reasoning models exhaust a 2k budget on their <think> trace before the
    JSON; bump max_tokens. Also turn extended-thinking off (distill
    response has its own structured reasoning)."""
    agent = _make_agent(MagicMock())
    kw = agent._llm_call_kwargs()
    assert kw.get("max_tokens") == 16384
    assert kw.get("thinking") is False


def test_run_passes_max_tokens_to_llm_complete():
    """Smoke: the kwargs actually reach LLMClient.complete (the
    base class wires them via _llm_call_kwargs)."""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "scope": "none", "rationale": "x", "confidence": "low",
    })
    _make_agent(llm).run(**_make_inputs())
    kwargs = llm.complete.call_args.kwargs
    assert kwargs.get("max_tokens") == 16384
    assert kwargs.get("thinking") is False
    assert kwargs.get("role") == "feedback_distill"


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_substitutes_all_fields():
    p = _make_agent(MagicMock()).build_prompt(**_make_inputs())
    assert "UNREALISTIC" in p
    assert "fn" in p
    assert "fn.assertion.1" in p
    assert "caller-contract slip" in p
    assert "missing NULL guard" in p
    assert "g_init != 0" in p


def test_build_prompt_handles_uncertain_verdict():
    p = _make_agent(MagicMock()).build_prompt(
        **_make_inputs(verdict_str="uncertain")
    )
    assert "UNCERTAIN" in p


def test_build_prompt_handles_empty_witness_state():
    """When the CEx has no variable_assignments, the prompt still renders
    with a clear placeholder instead of a blank section."""
    inputs = _make_inputs()
    inputs["counterexample"].variable_assignments = {}
    p = _make_agent(MagicMock()).build_prompt(**inputs)
    assert "(no witness variables)" in p


def test_build_prompt_truncates_long_body():
    """A 50k function body must not blow the prompt budget."""
    from types import SimpleNamespace
    inputs = _make_inputs()
    inputs["func"] = SimpleNamespace(name="fn", body="x" * 50000)
    p = _make_agent(MagicMock()).build_prompt(**inputs)
    # The body slot is capped at 6000; total prompt under 12k
    assert len(p) < 12000


def test_build_prompt_handles_empty_project_clauses():
    """Empty list of existing project invariants renders as ``(none)``
    rather than an empty section."""
    inputs = _make_inputs()
    inputs["existing_project_clauses"] = []
    p = _make_agent(MagicMock()).build_prompt(**inputs)
    assert "(none)" in p


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def test_parse_function_spec_scope():
    from bmc_agent.feedback_loop import RemediationScope
    agent = _make_agent(MagicMock())
    out = agent.parse(json.dumps({
        "scope": "function-spec",
        "clause": "ids->count == 0 || ids->ids != NULL",
        "rationale": "constructor invariant",
        "confidence": "high",
    }))
    assert out.scope == RemediationScope.FUNCTION_SPEC
    assert "ids->ids" in out.clause


def test_parse_project_invariant_scope():
    from bmc_agent.feedback_loop import RemediationScope
    out = _make_agent(MagicMock()).parse(json.dumps({
        "scope": "project-invariant",
        "clause": "g_init != 0",
        "rationale": "every TU initialises g_init first",
        "confidence": "medium",
    }))
    assert out.scope == RemediationScope.PROJECT_INVARIANT


def test_parse_function_post_relax_scope():
    from bmc_agent.feedback_loop import RemediationScope
    out = _make_agent(MagicMock()).parse(json.dumps({
        "scope": "function-post-relax",
        "clause": "result < 0",
        "rationale": "real callers can return positive",
        "confidence": "high",
    }))
    assert out.scope == RemediationScope.FUNCTION_POST_RELAX


def test_parse_code_change_scope():
    from bmc_agent.feedback_loop import RemediationScope
    out = _make_agent(MagicMock()).parse(json.dumps({
        "scope": "code-change",
        "code_change": "add struct-invariant inference for (count, array) pairs",
        "rationale": "missing structural capability",
        "confidence": "high",
    }))
    assert out.scope == RemediationScope.CODE_CHANGE
    assert "count, array" in out.code_change


def test_parse_none_scope():
    from bmc_agent.feedback_loop import RemediationScope
    out = _make_agent(MagicMock()).parse(json.dumps({
        "scope": "none",
        "rationale": "can't safely propose any fix",
        "confidence": "low",
    }))
    assert out.scope == RemediationScope.NONE


def test_parse_fenced_markdown():
    from bmc_agent.feedback_loop import RemediationScope
    out = _make_agent(MagicMock()).parse(
        "```json\n"
        + json.dumps({"scope": "function-spec", "clause": "x != NULL",
                      "rationale": "x", "confidence": "high"})
        + "\n```"
    )
    assert out.scope == RemediationScope.FUNCTION_SPEC


def test_parse_unparseable_returns_none_scope():
    """An LLM that emits garbage isn't None — it's
    Remediation(scope=NONE) per the existing _parse_remediation
    convention. The agent's parse() returns this verbatim, which is
    a VALID parsed answer."""
    from bmc_agent.feedback_loop import RemediationScope
    out = _make_agent(MagicMock()).parse("totally not json")
    assert out is not None
    assert out.scope == RemediationScope.NONE


def test_parse_empty_returns_none():
    """Truly empty input → None (BaseAgent.run reports as parse error)."""
    assert _make_agent(MagicMock()).parse("") is None


# ---------------------------------------------------------------------------
# Full run()
# ---------------------------------------------------------------------------

def test_run_returns_remediation_on_success():
    from bmc_agent.feedback_loop import RemediationScope
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "scope": "function-spec",
        "clause": "p != NULL",
        "rationale": "missing PRE",
        "confidence": "high",
    })
    result = _make_agent(llm).run(**_make_inputs())
    assert result.ok is True
    assert result.output.scope == RemediationScope.FUNCTION_SPEC
    assert result.output.clause == "p != NULL"


def test_run_handles_llm_error():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("timeout")
    result = _make_agent(llm).run(**_make_inputs())
    assert result.ok is False
    assert "LLMError" in result.error


# ---------------------------------------------------------------------------
# Standalone wrapper: learn_from_rejection
# ---------------------------------------------------------------------------

def test_learn_from_rejection_gates_on_verdict():
    """REALISTIC verdicts must NEVER trigger distillation (they're the
    real-bug signal we want to preserve). The wrapper short-circuits
    before constructing the agent."""
    from bmc_agent.feedback_loop import (
        RemediationScope, learn_from_rejection,
    )
    from bmc_agent.config import Config
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from types import SimpleNamespace
    llm = MagicMock()
    rem = learn_from_rejection(
        config=Config(llm_api_key="t"), llm=llm,
        func=SimpleNamespace(name="fn", body="{}"),
        counterexample=SimpleNamespace(
            failing_property="p", variable_assignments={},
        ),
        realism=RealismCheckResult(
            verdict=RealismVerdict.REALISTIC,
            reasoning="real bug", key_concern="",
        ),
        existing_project_clauses=[],
    )
    assert rem.scope == RemediationScope.NONE
    # Agent never invoked (LLM never called)
    llm.complete.assert_not_called()


def test_learn_from_rejection_delegates_to_agent_for_unrealistic():
    """UNREALISTIC verdicts go through the agent."""
    from bmc_agent.feedback_loop import (
        RemediationScope, learn_from_rejection,
    )
    from bmc_agent.config import Config
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "scope": "function-spec",
        "clause": "p != NULL",
        "rationale": "x",
        "confidence": "high",
    })
    inputs = _make_inputs(verdict_str="unrealistic")
    rem = learn_from_rejection(
        config=Config(llm_api_key="t"), llm=llm,
        **inputs,
    )
    assert rem.scope == RemediationScope.FUNCTION_SPEC
    assert rem.clause == "p != NULL"
    llm.complete.assert_called_once()


def test_learn_from_rejection_converts_agent_failure_to_none():
    """Agent LLM error → wrapper returns Remediation(scope=NONE) with
    rationale describing the failure. Pipeline never crashes."""
    from bmc_agent.llm import LLMError
    from bmc_agent.feedback_loop import (
        RemediationScope, learn_from_rejection,
    )
    from bmc_agent.config import Config
    llm = MagicMock()
    llm.complete.side_effect = LLMError("network down")
    rem = learn_from_rejection(
        config=Config(llm_api_key="t"), llm=llm,
        **_make_inputs(verdict_str="unrealistic"),
    )
    assert rem.scope == RemediationScope.NONE
    assert "network down" in rem.rationale
