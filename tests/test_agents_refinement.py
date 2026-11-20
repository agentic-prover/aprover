"""
Tests for ``bmc_agent.agents.refinement.RefinementAgent``.

The agent is the LLM-call boundary of spec_refiner — given a function,
its current spec, a CEx, and a realism result, it proposes one tight
PRE clause that would exclude the rejected witness state. The
SpecRefiner orchestrator wraps it (gating on verdict +
actionable key_concern, then re-running BMC under the new spec).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_agent(llm):
    from bmc_agent.agents.refinement import RefinementAgent
    from bmc_agent.config import Config
    return RefinementAgent(config=Config(llm_api_key="t"), llm=llm)


def _make_inputs(*, prop: str = "fn.pointer_dereference.5"):
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.spec import Spec, SpecStatus

    sig = SimpleNamespace(
        return_type="int", name="fn",
        parameters=[("int *", "p"), ("int", "n")],
    )
    func = SimpleNamespace(name="fn", body="{ return *p; }", signature=sig)
    cex = SimpleNamespace(
        failing_property=prop,
        variable_assignments={"p": "NULL", "n": "5"},
    )
    realism = RealismCheckResult(
        verdict=RealismVerdict.UNREALISTIC,
        reasoning="caller never passes NULL p",
        key_concern="missing PRE for p != NULL",
    )
    spec = Spec(
        function_name="fn", precondition="true", postcondition="true",
        status=SpecStatus.GENERATED,
        pre_validity="", pre_protocol="",
    )
    return dict(
        func_info=func, current_spec=spec,
        rejected_cex=cex, realism=realism,
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_agent_name_is_refinement():
    from bmc_agent.agents.refinement import RefinementAgent
    assert RefinementAgent.name == "refinement"


def test_agent_system_prompt_is_spec_system_prompt():
    from bmc_agent.agents.refinement import RefinementAgent
    from bmc_agent.prompts import SPEC_SYSTEM_PROMPT
    from bmc_agent.config import Config
    agent = RefinementAgent(config=Config(llm_api_key="t"), llm=MagicMock())
    assert agent.system_prompt == SPEC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# LLM-call kwargs
# ---------------------------------------------------------------------------

def test_llm_call_kwargs_4096_thinking_off():
    agent = _make_agent(MagicMock())
    kw = agent._llm_call_kwargs()
    assert kw.get("max_tokens") == 4096
    assert kw.get("thinking") is False


def test_run_passes_kwargs_and_role_to_llm():
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "scope": "refine", "added_clause": "p != NULL",
        "evidence_tag": "x", "rationale": "y",
    })
    _make_agent(llm).run(**_make_inputs())
    kwargs = llm.complete.call_args.kwargs
    assert kwargs.get("max_tokens") == 4096
    assert kwargs.get("thinking") is False
    assert kwargs.get("role") == "refinement"


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_substitutes_all_fields():
    p = _make_agent(MagicMock()).build_prompt(**_make_inputs())
    assert "fn" in p
    assert "int *" in p or "int*" in p
    assert "{ return *p; }" in p
    assert "NULL" in p  # witness state
    assert "missing PRE for p != NULL" in p
    assert "fn.pointer_dereference.5" in p
    # property_class extracted: pointer_dereference (last non-numeric segment)
    assert "pointer_dereference" in p


def test_build_prompt_property_class_extraction_skips_trailing_digits():
    """``fn.pointer_dereference.5`` → property_class = pointer_dereference
    (the last non-numeric segment). Used by the evidence_tag suggestion."""
    p = _make_agent(MagicMock()).build_prompt(
        **_make_inputs(prop="add_owner_id.array_bounds.42")
    )
    assert "array_bounds" in p


def test_build_prompt_handles_empty_witness():
    inputs = _make_inputs()
    inputs["rejected_cex"].variable_assignments = {}
    p = _make_agent(MagicMock()).build_prompt(**inputs)
    assert "(no witness state)" in p


def test_build_prompt_handles_void_signature():
    """A function with no parameters renders ``void`` in the signature
    line (matching C's empty-arglist convention)."""
    inputs = _make_inputs()
    inputs["func_info"].signature.parameters = []
    p = _make_agent(MagicMock()).build_prompt(**inputs)
    assert "fn(void)" in p


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def test_parse_refine_scope_returns_proposal():
    out = _make_agent(MagicMock()).parse(json.dumps({
        "scope": "refine",
        "added_clause": "p != NULL",
        "evidence_tag": "realism:fn:pointer_dereference",
        "rationale": "no real caller passes NULL p",
    }))
    assert out is not None
    assert out.scope == "refine"
    assert out.added_clause == "p != NULL"
    assert out.evidence_tag.startswith("realism:fn:")
    assert out.is_actionable is True


def test_parse_cannot_refine_scope_returns_proposal():
    out = _make_agent(MagicMock()).parse(json.dumps({
        "scope": "cannot-refine",
        "discrepancy": "realism over-claimed; the body itself handles NULL",
        "rationale": "no safe clause exists",
    }))
    assert out is not None
    assert out.scope == "cannot-refine"
    assert out.is_actionable is False


def test_parse_fenced_markdown():
    out = _make_agent(MagicMock()).parse(
        "```json\n"
        + json.dumps({"scope": "refine", "added_clause": "x > 0",
                      "rationale": "x"})
        + "\n```"
    )
    assert out is not None
    assert out.added_clause == "x > 0"


def test_parse_prose_embedded_json():
    out = _make_agent(MagicMock()).parse(
        "Here's my proposal:\n"
        + json.dumps({"scope": "refine", "added_clause": "y < 10",
                      "rationale": "x"})
    )
    assert out is not None
    assert out.scope == "refine"


def test_parse_unknown_scope_defaults_cannot_refine():
    """The pre-existing _parse_refinement_response normalises unknown
    scope values to ``cannot-refine``."""
    out = _make_agent(MagicMock()).parse(json.dumps({
        "scope": "weird", "rationale": "x",
    }))
    assert out is not None
    assert out.scope == "cannot-refine"


def test_parse_empty_returns_none():
    assert _make_agent(MagicMock()).parse("") is None


def test_parse_no_json_returns_none():
    assert _make_agent(MagicMock()).parse("just prose, no braces") is None


# ---------------------------------------------------------------------------
# Full run()
# ---------------------------------------------------------------------------

def test_run_returns_proposal_on_success():
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "scope": "refine",
        "added_clause": "p != NULL",
        "rationale": "missing PRE",
    })
    result = _make_agent(llm).run(**_make_inputs())
    assert result.ok is True
    assert result.output.scope == "refine"
    assert result.output.added_clause == "p != NULL"


def test_run_handles_llm_error_cleanly():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("timeout")
    result = _make_agent(llm).run(**_make_inputs())
    assert result.ok is False
    assert "LLMError" in result.error


# ---------------------------------------------------------------------------
# Standalone wrapper: SpecRefiner.propose_refinement
# ---------------------------------------------------------------------------

def test_spec_refiner_short_circuits_on_realistic_verdict():
    """SpecRefiner.propose_refinement returns None without invoking the
    agent when realism is REALISTIC — that's the bug signal we want to
    preserve."""
    from bmc_agent.spec_refiner import SpecRefiner
    from bmc_agent.config import Config
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    llm = MagicMock()
    inputs = _make_inputs()
    inputs["realism"] = RealismCheckResult(
        verdict=RealismVerdict.REALISTIC,
        reasoning="real bug", key_concern="x",
    )
    refiner = SpecRefiner(Config(llm_api_key="t"), llm)
    assert refiner.propose_refinement(**inputs) is None
    llm.complete.assert_not_called()


def test_spec_refiner_delegates_to_agent_for_unrealistic():
    from bmc_agent.spec_refiner import SpecRefiner
    from bmc_agent.config import Config
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "scope": "refine", "added_clause": "p != NULL", "rationale": "x",
    })
    refiner = SpecRefiner(Config(llm_api_key="t"), llm)
    prop = refiner.propose_refinement(**_make_inputs())
    assert prop is not None
    assert prop.is_actionable
    assert prop.added_clause == "p != NULL"


def test_spec_refiner_agent_failure_returns_none():
    """LLM error in the agent → wrapper returns None (caller bails)."""
    from bmc_agent.llm import LLMError
    from bmc_agent.spec_refiner import SpecRefiner
    from bmc_agent.config import Config
    llm = MagicMock()
    llm.complete.side_effect = LLMError("network down")
    refiner = SpecRefiner(Config(llm_api_key="t"), llm)
    assert refiner.propose_refinement(**_make_inputs()) is None
