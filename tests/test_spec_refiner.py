"""Tests for bmc_agent.spec_refiner — realism-feedback-driven refinement."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bmc_agent.spec import Spec, SpecStatus
from bmc_agent.spec_refiner import (
    AcceptanceResult,
    RefinementProposal,
    SpecRefiner,
    _is_actionable_key_concern,
    _parse_refinement_response,
    check_refinement_acceptance,
)


# ---------- actionable-key-concern gate -------------------------------------


@pytest.mark.parametrize("kc,expected", [
    ("", False),
    ("   ", False),
    ("looks artificial", False),
    ("LOOKS ARTIFICIAL", False),
    ("seems impossible", False),
    ("Not plausible", False),
    ("(cannot determine)", False),
    # actionable: names a specific identifier / field / constraint
    ("the witness sets entry->aes_set=0 but constructor initializes it", True),
    ("buffer length parameter exceeds the documented maximum of 4096", True),
    ("p->next is NULL but list_init guarantees non-NULL after creation", True),
])
def test_is_actionable_key_concern(kc, expected):
    assert _is_actionable_key_concern(kc) is expected


# ---------- response parsing ------------------------------------------------


def test_parse_clean_refinement_response():
    raw = '{"scope": "refine", "added_clause": "!null(a->next)", "evidence_tag": "realism:foo:pointer_dereference", "rationale": "list_init sets next non-NULL"}'
    p = _parse_refinement_response(raw, "foo")
    assert p is not None
    assert p.scope == "refine"
    assert p.added_clause == "!null(a->next)"
    assert p.evidence_tag == "realism:foo:pointer_dereference"
    assert p.is_actionable


def test_parse_response_with_code_fence():
    raw = '```json\n{"scope": "refine", "added_clause": "len <= 4096"}\n```'
    p = _parse_refinement_response(raw, "foo")
    assert p is not None
    assert p.added_clause == "len <= 4096"


def test_parse_cannot_refine_response():
    raw = '{"scope": "cannot-refine", "discrepancy": "field is genuinely nullable"}'
    p = _parse_refinement_response(raw, "foo")
    assert p is not None
    assert p.scope == "cannot-refine"
    assert p.is_actionable is False


def test_parse_invalid_returns_none():
    assert _parse_refinement_response("not json", "foo") is None
    assert _parse_refinement_response("", "foo") is None


def test_parse_unknown_scope_falls_back_to_cannot_refine():
    raw = '{"scope": "wat", "added_clause": "x"}'
    p = _parse_refinement_response(raw, "foo")
    assert p is not None
    assert p.scope == "cannot-refine"


# ---------- acceptance check (the methodology-trap defense) -----------------


def test_acceptance_targeted_cex_gone_no_realistic_dropped():
    """The success case: targeted CEx removed; nothing else dropped."""
    result = check_refinement_acceptance(
        targeted_failing_property="foo.pointer_dereference.3",
        previously_realistic_properties={"foo.array_bounds.1"},
        new_failing_properties={"foo.array_bounds.1"},
    )
    assert result.accepted is True
    assert result.targeted_cex_gone is True
    assert result.realistic_preserved is True


def test_acceptance_rejects_when_targeted_cex_still_present():
    result = check_refinement_acceptance(
        targeted_failing_property="foo.pointer_dereference.3",
        previously_realistic_properties=set(),
        new_failing_properties={"foo.pointer_dereference.3", "foo.array_bounds.1"},
    )
    assert result.accepted is False
    assert result.targeted_cex_gone is False
    assert "still present" in result.reason


def test_acceptance_rejects_when_realistic_cex_silently_dropped():
    """The methodology-trap guard: refinement that masks a real bug must be REJECTED."""
    result = check_refinement_acceptance(
        targeted_failing_property="foo.pointer_dereference.3",
        previously_realistic_properties={
            "foo.array_bounds.1",
            "foo.pointer_dereference.7",
        },
        # The targeted CEx is gone, but so is the previously-realistic
        # array_bounds.1 — that's the over-tightening case we must catch.
        new_failing_properties={"foo.pointer_dereference.7"},
    )
    assert result.accepted is False
    assert result.targeted_cex_gone is True
    assert result.realistic_preserved is False
    assert "foo.array_bounds.1" in result.dropped_realistic
    assert "masked" in result.reason


def test_acceptance_empty_previously_realistic_is_safe():
    """No realistic CExs to preserve → only the targeted-gone check matters."""
    result = check_refinement_acceptance(
        targeted_failing_property="foo.x.3",
        previously_realistic_properties=set(),
        new_failing_properties=set(),
    )
    assert result.accepted is True


# ---------- SpecRefiner.propose_refinement ----------------------------------


def _mock_realism_unrealistic(key_concern="entry->aes_set=0 but constructor sets AES_SET_MBS"):
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    return RealismCheckResult(
        verdict=RealismVerdict.UNREALISTIC,
        reasoning="The CBMC witness shows " + key_concern,
        key_concern=key_concern,
    )


def _mock_func_info(name="foo", params=None, body="{ return 0; }"):
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name=name, return_type="int",
        parameters=params or [("struct foo *", "p")],
    )
    return FunctionInfo(
        name=name, signature=sig, body=body,
        callees=set(), source_file="",
    )


def _mock_cex(failing_property="foo.pointer_dereference.3", vars=None):
    cex = MagicMock()
    cex.failing_property = failing_property
    cex.variable_assignments = vars or {"p->field": "NULL"}
    return cex


def _refiner_with_mocked_llm(response_text):
    from bmc_agent.config import Config
    cfg = Config(artifact_dir="/tmp/_refiner_test")
    llm = MagicMock()
    llm.complete.return_value = response_text
    return SpecRefiner(cfg, llm)


def test_propose_gates_on_realism_verdict():
    """Only UNREALISTIC verdicts trigger refinement."""
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    refiner = _refiner_with_mocked_llm("")
    spec = Spec(function_name="foo", precondition="!null(p)", postcondition="true")
    for verdict in (RealismVerdict.REALISTIC, RealismVerdict.UNCERTAIN):
        r = RealismCheckResult(verdict=verdict, reasoning="", key_concern="x")
        p = refiner.propose_refinement(
            func_info=_mock_func_info(),
            current_spec=spec,
            rejected_cex=_mock_cex(),
            realism=r,
        )
        assert p is None
        # No LLM call should have been made.
        assert refiner.llm.complete.call_count == 0


def test_propose_gates_on_vague_key_concern():
    """Empty or hand-wave key_concerns don't fire a refinement."""
    refiner = _refiner_with_mocked_llm("")
    spec = Spec(function_name="foo", precondition="!null(p)", postcondition="true")
    for kc in ("", "looks artificial", "seems impossible"):
        from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
        r = RealismCheckResult(verdict=RealismVerdict.UNREALISTIC,
                               reasoning="", key_concern=kc)
        p = refiner.propose_refinement(
            func_info=_mock_func_info(),
            current_spec=spec,
            rejected_cex=_mock_cex(),
            realism=r,
        )
        assert p is None
    assert refiner.llm.complete.call_count == 0


def test_propose_actionable_response_returns_proposal():
    refiner = _refiner_with_mocked_llm(
        '{"scope": "refine", "added_clause": "!null(p->next)", "evidence_tag": "realism:foo:pointer_dereference", "rationale": "list_init guarantees non-NULL"}'
    )
    spec = Spec(function_name="foo", precondition="!null(p)", postcondition="true",
                pre_validity="!null(p)")
    p = refiner.propose_refinement(
        func_info=_mock_func_info(),
        current_spec=spec,
        rejected_cex=_mock_cex(),
        realism=_mock_realism_unrealistic(),
    )
    assert p is not None
    assert p.scope == "refine"
    assert p.added_clause == "!null(p->next)"
    assert p.is_actionable
    assert refiner.llm.complete.call_count == 1


def test_propose_llm_failure_returns_none():
    refiner = _refiner_with_mocked_llm("")
    refiner.llm.complete.side_effect = RuntimeError("LLM crashed")
    spec = Spec(function_name="foo", precondition="!null(p)", postcondition="true")
    p = refiner.propose_refinement(
        func_info=_mock_func_info(),
        current_spec=spec,
        rejected_cex=_mock_cex(),
        realism=_mock_realism_unrealistic(),
    )
    assert p is None


# ---------- SpecRefiner.apply_refinement_to_spec ---------------------------


def test_apply_to_empty_pre_validity():
    refiner = _refiner_with_mocked_llm("")
    spec = Spec(function_name="foo", precondition="true", postcondition="true",
                pre_validity="", pre_protocol="")
    proposal = RefinementProposal(scope="refine", added_clause="!null(p)",
                                  evidence_tag="realism:foo:x")
    new_spec = refiner.apply_refinement_to_spec(spec=spec, proposal=proposal)
    assert new_spec.pre_validity == "!null(p)"
    assert new_spec.precondition == "!null(p)"
    assert new_spec.status == SpecStatus.REFINED
    assert new_spec.evidence["!null(p)"] == ["realism:foo:x"]


def test_apply_to_existing_pre_validity_ands_clause():
    refiner = _refiner_with_mocked_llm("")
    spec = Spec(function_name="foo", precondition="!null(p)",
                postcondition="true",
                pre_validity="!null(p)", pre_protocol="")
    proposal = RefinementProposal(scope="refine", added_clause="!null(p->next)",
                                  evidence_tag="realism:foo:y")
    new_spec = refiner.apply_refinement_to_spec(spec=spec, proposal=proposal)
    assert "!null(p)" in new_spec.pre_validity
    assert "!null(p->next)" in new_spec.pre_validity
    assert "&&" in new_spec.pre_validity


def test_apply_preserves_existing_evidence():
    """Original evidence tags carry through."""
    refiner = _refiner_with_mocked_llm("")
    spec = Spec(function_name="foo", precondition="!null(p)",
                postcondition="true",
                pre_validity="!null(p)",
                evidence={"!null(p)": ["caller_site_1"]})
    proposal = RefinementProposal(scope="refine", added_clause="!null(p->x)",
                                  evidence_tag="realism:foo:z")
    new_spec = refiner.apply_refinement_to_spec(spec=spec, proposal=proposal)
    assert new_spec.evidence["!null(p)"] == ["caller_site_1"]
    assert new_spec.evidence["!null(p->x)"] == ["realism:foo:z"]


def test_apply_with_non_actionable_proposal_returns_spec_unchanged():
    refiner = _refiner_with_mocked_llm("")
    spec = Spec(function_name="foo", precondition="!null(p)",
                postcondition="true", pre_validity="!null(p)")
    proposal = RefinementProposal(scope="cannot-refine", discrepancy="x")
    new_spec = refiner.apply_refinement_to_spec(spec=spec, proposal=proposal)
    # Same object reference (not modified).
    assert new_spec is spec
