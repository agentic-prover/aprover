"""
Tests for ``bmc_agent.agents.disagreement.DisagreementDiagnoseAgent``.

Covers the agent's contract end-to-end: build_prompt produces the
expected shape, parse handles all the JSON envelope variants the LLM
emits, and run() returns AgentResult with the right success / error
states. The standalone ``oracle_disagreement.diagnose`` function tests
exercise the wrapper path; this file tests the agent itself.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _make_agent(llm):
    from bmc_agent.agents.disagreement import DisagreementDiagnoseAgent
    from bmc_agent.config import Config
    return DisagreementDiagnoseAgent(
        config=Config(llm_api_key="t"), llm=llm,
    )


def _make_case():
    from bmc_agent.oracle_disagreement import (
        DisagreementCase, DisagreementKind,
    )
    return DisagreementCase(
        kind=DisagreementKind.BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED,
        function_name="archive_match_include_uid",
        violated_property="add_owner_id_stub.pointer_dereference.5",
        bmc_verdict="fail",
        realism_verdict="realistic",
        dyn_outcome="not_triggered",
        realism_reasoning="caller could pass NULL handle",
        reproducer_source="#include <archive.h>\nint main(){return 0;}",
    )


# ---------------------------------------------------------------------------
# Identity (the bits that make this an agent, not a function)
# ---------------------------------------------------------------------------

def test_agent_name_is_disagreement_diagnose():
    """The role identifier must match the env-var routing key
    (BMC_AGENT_LLM_DISAGREEMENT_DIAGNOSE_*) so per-role model selection
    actually applies to this agent."""
    from bmc_agent.agents.disagreement import DisagreementDiagnoseAgent
    assert DisagreementDiagnoseAgent.name == "disagreement_diagnose"


def test_agent_system_prompt_mentions_verification_expert():
    from bmc_agent.agents.disagreement import DisagreementDiagnoseAgent
    sp = DisagreementDiagnoseAgent.system_prompt
    assert "verification" in sp.lower()
    assert "JSON" in sp


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_includes_case_fields():
    agent = _make_agent(MagicMock())
    p = agent.build_prompt(case=_make_case())
    assert "archive_match_include_uid" in p
    assert "add_owner_id_stub.pointer_dereference.5" in p
    assert "caller could pass NULL handle" in p
    assert "archive.h" in p


def test_build_prompt_truncates_long_inputs():
    """A 10kB realism_reasoning shouldn't blow the prompt budget — the
    template caps each field to a sensible length."""
    from bmc_agent.oracle_disagreement import (
        DisagreementCase, DisagreementKind,
    )
    huge = "x" * 50000
    case = DisagreementCase(
        kind=DisagreementKind.BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED,
        function_name="fn", violated_property="p",
        bmc_verdict="fail", realism_verdict="realistic", dyn_outcome="not_triggered",
        realism_reasoning=huge,
        reproducer_source=huge,
    )
    p = _make_agent(MagicMock()).build_prompt(case=case)
    assert len(p) < 10000   # well under the 50k+50k worst case


def test_build_prompt_substitutes_unknown_for_missing_fields():
    from bmc_agent.oracle_disagreement import (
        DisagreementCase, DisagreementKind,
    )
    case = DisagreementCase(
        kind=DisagreementKind.BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED,
        function_name="",
        violated_property="",
        bmc_verdict="fail", realism_verdict="realistic", dyn_outcome="not_triggered",
    )
    p = _make_agent(MagicMock()).build_prompt(case=case)
    assert "(unknown)" in p
    assert "(none recorded)" in p
    assert "(no reproducer)" in p


# ---------------------------------------------------------------------------
# parse — all the JSON envelope variants
# ---------------------------------------------------------------------------

def test_parse_bare_json_returns_diagnosis():
    from bmc_agent.oracle_disagreement import DiagnosisVerdict
    agent = _make_agent(MagicMock())
    out = agent.parse(json.dumps({
        "verdict": "spec_refine",
        "rationale": "PRE is too loose",
        "suggested_clause": "_a != NULL && _a->magic == 0xCAD11C9",
        "confidence": "high",
    }))
    assert out is not None
    assert out.verdict == DiagnosisVerdict.SPEC_REFINE
    assert "0xCAD11C9" in out.suggested_clause


def test_parse_fenced_markdown():
    from bmc_agent.oracle_disagreement import DiagnosisVerdict
    agent = _make_agent(MagicMock())
    out = agent.parse(
        "```json\n"
        + json.dumps({"verdict": "property_fp", "rationale": "x", "confidence": "high"})
        + "\n```"
    )
    assert out is not None
    assert out.verdict == DiagnosisVerdict.PROPERTY_FP


def test_parse_prose_embedded_json():
    from bmc_agent.oracle_disagreement import DiagnosisVerdict
    agent = _make_agent(MagicMock())
    out = agent.parse(
        "Here's my diagnosis:\n"
        + json.dumps({"verdict": "harness_encoding", "suggested_encoding": "x[N]=0;",
                      "rationale": "y", "confidence": "medium"})
        + "\nThat's all."
    )
    assert out is not None
    assert out.verdict == DiagnosisVerdict.HARNESS_ENCODING


def test_parse_unknown_verdict_defaults_inconclusive():
    from bmc_agent.oracle_disagreement import DiagnosisVerdict
    agent = _make_agent(MagicMock())
    out = agent.parse(json.dumps({
        "verdict": "wat", "rationale": "x", "confidence": "low",
    }))
    assert out is not None
    assert out.verdict == DiagnosisVerdict.INCONCLUSIVE


def test_parse_unparseable_returns_none():
    """parse() returning None lets BaseAgent.run() report a clean
    parse-error — the agent never crashes the pipeline."""
    agent = _make_agent(MagicMock())
    assert agent.parse("not json") is None
    assert agent.parse("") is None


def test_parse_truncates_long_string_fields():
    """Defensive: the LLM occasionally emits multi-thousand-char
    rationales / clauses. parse() truncates so a single broken
    response can't blow downstream JSON storage."""
    agent = _make_agent(MagicMock())
    out = agent.parse(json.dumps({
        "verdict": "spec_refine",
        "rationale": "x" * 5000,
        "suggested_clause": "y" * 1000,
        "suggested_encoding": "z" * 1000,
        "confidence": "high",
    }))
    assert out is not None
    assert len(out.rationale) <= 2000
    assert len(out.suggested_clause) <= 400
    assert len(out.suggested_encoding) <= 400


# ---------------------------------------------------------------------------
# Full run() — agent contract honoured end-to-end
# ---------------------------------------------------------------------------

def test_run_returns_diagnosis_on_success():
    from bmc_agent.oracle_disagreement import DiagnosisVerdict
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "spec_refine",
        "rationale": "loose PRE",
        "suggested_clause": "_a != NULL",
        "confidence": "high",
    })
    result = _make_agent(llm).run(case=_make_case())
    assert result.ok is True
    assert result.output.verdict == DiagnosisVerdict.SPEC_REFINE
    assert result.output.suggested_clause == "_a != NULL"


def test_run_propagates_llm_error_via_agent_result():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("timeout")
    result = _make_agent(llm).run(case=_make_case())
    assert result.ok is False
    assert "LLMError" in result.error


def test_run_routes_via_disagreement_diagnose_role():
    """Smoke test: confirm the agent calls LLMClient with the right role
    string — without this, per-role env-var routing wouldn't fire."""
    llm = MagicMock()
    llm.complete.return_value = '{"verdict":"inconclusive","rationale":"x","confidence":"low"}'
    _make_agent(llm).run(case=_make_case())
    assert llm.complete.call_args.kwargs.get("role") == "disagreement_diagnose"


# ---------------------------------------------------------------------------
# Back-compat: standalone diagnose() wrapper still works
# ---------------------------------------------------------------------------

def test_standalone_diagnose_function_delegates_to_agent():
    """The pre-existing ``oracle_disagreement.diagnose(case, llm)`` API
    is preserved for callers (pipeline + integration tests) — under
    the hood it now constructs the agent."""
    from bmc_agent.oracle_disagreement import diagnose, DiagnosisVerdict
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "spec_refine",
        "rationale": "ok",
        "suggested_clause": "x != NULL",
        "confidence": "high",
    })
    result = diagnose(_make_case(), llm)
    assert result is not None
    assert result.verdict == DiagnosisVerdict.SPEC_REFINE
    assert result.suggested_clause == "x != NULL"
