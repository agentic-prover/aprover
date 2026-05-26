"""
Tests for the three-oracle disagreement detector (Phase 3d).

When BMC fail + realism REALISTIC + dyn-val NOT_TRIGGERED disagree on
the same counterexample, that's a structured signal something in the
pipeline is wrong — most often the harness admits a state real
callers can't produce. The detector + LLM diagnoser route findings
through a single targeted LLM call that classifies the disagreement
as spec_refine / harness_encoding / property_fp / inconclusive and,
for property_fp, auto-downgrades confidence.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# detect_disagreement
# ---------------------------------------------------------------------------

def _make_report(*, realism: str | None, dyn: str | None, **extra) -> dict:
    rep = {
        "function_name": "foo",
        "violated_property": "foo.pointer_dereference.5",
        "confidence": "confirmed_dynamic",
        "realism_check": {"verdict": realism} if realism else None,
        "dynamic_outcome": dyn,
        "dynamic_signal": "SIGSEGV" if dyn == "confirmed" else None,
        "reproducer": "#include <archive.h>\nint main(void){return 0;}",
    }
    rep.update(extra)
    return rep


def test_detect_fires_on_bmc_fail_real_dyn_not_triggered():
    from bmc_agent.oracle_disagreement import (
        detect_disagreement, DisagreementKind,
    )
    rep = _make_report(realism="realistic", dyn="not_triggered")
    case = detect_disagreement(rep)
    assert case is not None
    assert case.kind == DisagreementKind.BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED
    assert case.function_name == "foo"
    assert case.violated_property == "foo.pointer_dereference.5"


def test_detect_no_fire_when_all_three_agree_real_bug():
    """BMC fail + realism REAL + dyn CONFIRMED → all three agree.
    No diagnosis needed."""
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(realism="realistic", dyn="confirmed")
    assert detect_disagreement(rep) is None


def test_detect_no_fire_when_all_three_agree_fp():
    """BMC fail + realism UNREAL + dyn NOT_TRIGGERED → all three agree
    this is an FP (handled by other downgrade paths, not this one)."""
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(realism="unrealistic", dyn="not_triggered")
    assert detect_disagreement(rep) is None


def test_detect_no_fire_on_inconclusive_dyn():
    """Dyn-val inconclusive isn't a strong signal — no disagreement to
    diagnose (it's actually an absence of one of the oracles)."""
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(realism="realistic", dyn="inconclusive")
    assert detect_disagreement(rep) is None


def test_detect_no_fire_when_dyn_skipped():
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(realism="realistic", dyn="skipped")
    assert detect_disagreement(rep) is None


def test_detect_no_fire_when_dyn_missing():
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(realism="realistic", dyn=None)
    assert detect_disagreement(rep) is None


def test_detect_no_fire_when_realism_missing():
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(realism=None, dyn="not_triggered")
    assert detect_disagreement(rep) is None


def test_detect_handles_string_case_variations():
    """Verdict / outcome strings may have whitespace or different casing
    — the detector should normalize before comparing."""
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(realism="REALISTIC", dyn="  Not_Triggered  ")
    assert detect_disagreement(rep) is not None


def test_detect_returns_none_on_non_dict():
    from bmc_agent.oracle_disagreement import detect_disagreement
    assert detect_disagreement("nope") is None  # type: ignore[arg-type]
    assert detect_disagreement(None) is None  # type: ignore[arg-type]


def test_detect_captures_reproducer_and_reasoning():
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(
        realism="realistic",
        dyn="not_triggered",
        realism_check={
            "verdict": "realistic",
            "reasoning": "the function lacks NULL check on pattern",
        },
        reproducer="#include <archive.h>\n/* full reproducer */\n",
    )
    case = detect_disagreement(rep)
    assert case is not None
    assert "NULL check" in case.realism_reasoning
    assert "archive.h" in case.reproducer_source


# ---------------------------------------------------------------------------
# diagnose (LLM call)
# ---------------------------------------------------------------------------

def _make_case():
    from bmc_agent.oracle_disagreement import (
        DisagreementCase, DisagreementKind,
    )
    return DisagreementCase(
        kind=DisagreementKind.BMC_FAIL_REALISM_REAL_DYN_NOT_TRIGGERED,
        function_name="foo",
        violated_property="foo.pointer_dereference.5",
        bmc_verdict="fail",
        realism_verdict="realistic",
        dyn_outcome="not_triggered",
        realism_reasoning="caller could pass NULL",
        reproducer_source="#include <archive.h>\nint main(){return 0;}",
    )


def test_diagnose_spec_refine_returns_clause():
    from bmc_agent.oracle_disagreement import diagnose, DiagnosisVerdict
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "spec_refine",
        "rationale": "PRE is too loose — real callers obey magic check.",
        "suggested_clause": "_a != NULL && _a->magic == ARCHIVE_MATCH_MAGIC",
        "confidence": "high",
    })
    result = diagnose(_make_case(), llm)
    assert result is not None
    assert result.verdict == DiagnosisVerdict.SPEC_REFINE
    assert "_a->magic" in result.suggested_clause
    assert result.confidence == "high"


def test_diagnose_harness_encoding_returns_encoding_suggestion():
    from bmc_agent.oracle_disagreement import diagnose, DiagnosisVerdict
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "harness_encoding",
        "rationale": "harness over-allocates without null terminator",
        "suggested_encoding": "buf[N] = '\\0';",
        "confidence": "medium",
    })
    result = diagnose(_make_case(), llm)
    assert result is not None
    assert result.verdict == DiagnosisVerdict.HARNESS_ENCODING
    assert result.suggested_encoding == "buf[N] = '\\0';"


def test_diagnose_property_fp_returns_no_clause():
    from bmc_agent.oracle_disagreement import diagnose, DiagnosisVerdict
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "property_fp",
        "rationale": "BMC overflow check fires on saturated arithmetic.",
        "confidence": "high",
    })
    result = diagnose(_make_case(), llm)
    assert result is not None
    assert result.verdict == DiagnosisVerdict.PROPERTY_FP


def test_diagnose_inconclusive_defaults():
    from bmc_agent.oracle_disagreement import diagnose, DiagnosisVerdict
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "inconclusive",
        "rationale": "Not enough info.",
        "confidence": "low",
    })
    result = diagnose(_make_case(), llm)
    assert result is not None
    assert result.verdict == DiagnosisVerdict.INCONCLUSIVE


def test_diagnose_unknown_verdict_defaults_to_inconclusive():
    """An LLM that returns ``verdict='something_weird'`` shouldn't crash
    the pipeline — defensive normalization to INCONCLUSIVE."""
    from bmc_agent.oracle_disagreement import diagnose, DiagnosisVerdict
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "wat",
        "rationale": "x",
        "confidence": "low",
    })
    result = diagnose(_make_case(), llm)
    assert result is not None
    assert result.verdict == DiagnosisVerdict.INCONCLUSIVE


def test_diagnose_parses_fenced_markdown():
    from bmc_agent.oracle_disagreement import diagnose
    llm = MagicMock()
    llm.complete.return_value = (
        '```json\n'
        '{"verdict": "spec_refine", "rationale": "...", '
        '"suggested_clause": "x != NULL", "confidence": "high"}\n'
        '```'
    )
    result = diagnose(_make_case(), llm)
    assert result is not None
    assert result.suggested_clause == "x != NULL"


def test_diagnose_parses_prose_embedded_json():
    from bmc_agent.oracle_disagreement import diagnose
    llm = MagicMock()
    llm.complete.return_value = (
        'Here is my analysis:\n'
        '{"verdict": "property_fp", "rationale": "...", "confidence": "medium"}\n'
        'That is all.'
    )
    result = diagnose(_make_case(), llm)
    assert result is not None
    assert result.verdict.value == "property_fp"


def test_diagnose_returns_none_on_unparseable_response():
    from bmc_agent.oracle_disagreement import diagnose
    llm = MagicMock()
    llm.complete.return_value = "not json at all"
    assert diagnose(_make_case(), llm) is None


def test_diagnose_returns_none_on_llm_error():
    from bmc_agent.llm import LLMError
    from bmc_agent.oracle_disagreement import diagnose
    llm = MagicMock()
    llm.complete.side_effect = LLMError("timeout")
    assert diagnose(_make_case(), llm) is None


# ---------------------------------------------------------------------------
# apply_diagnosis
# ---------------------------------------------------------------------------

def test_apply_diagnosis_property_fp_downgrades_confirmed_dynamic():
    from bmc_agent.oracle_disagreement import (
        apply_diagnosis, DiagnosisResult, DiagnosisVerdict,
    )
    rep = _make_report(
        realism="realistic", dyn="not_triggered",
        confidence="confirmed_dynamic",
        reasoning_trail="base",
    )
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.PROPERTY_FP,
        rationale="BMC overflow check over-cautious",
        confidence="high",
    )
    out = apply_diagnosis(rep, diag)
    assert out["confidence"] == "unlikely"
    assert "[ORACLE-DISAGREEMENT]" in out["reasoning_trail"]
    assert "base" in out["reasoning_trail"]  # original preserved
    assert "downgraded from 'confirmed_dynamic'" in out["reasoning_trail"]
    assert out["oracle_disagreement_diagnosis"]["verdict"] == "property_fp"


def test_apply_diagnosis_property_fp_downgrades_confirmed_system_entry():
    from bmc_agent.oracle_disagreement import (
        apply_diagnosis, DiagnosisResult, DiagnosisVerdict,
    )
    rep = _make_report(
        realism="realistic", dyn="not_triggered",
        confidence="confirmed_system_entry",
    )
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.PROPERTY_FP, rationale="x", confidence="high",
    )
    out = apply_diagnosis(rep, diag)
    assert out["confidence"] == "unlikely"


def test_apply_diagnosis_property_fp_does_not_downgrade_unlikely():
    """If confidence is already 'unlikely', no further downgrade."""
    from bmc_agent.oracle_disagreement import (
        apply_diagnosis, DiagnosisResult, DiagnosisVerdict,
    )
    rep = _make_report(
        realism="realistic", dyn="not_triggered",
        confidence="unlikely",
    )
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.PROPERTY_FP, rationale="x", confidence="high",
    )
    out = apply_diagnosis(rep, diag)
    assert out["confidence"] == "unlikely"
    # No annotation added since no real change
    assert "[ORACLE-DISAGREEMENT]" not in (out.get("reasoning_trail") or "")


def test_apply_diagnosis_spec_refine_attaches_but_does_not_downgrade():
    """SPEC_REFINE diagnosis is attached for review; confidence is
    preserved because auto-application isn't wired yet."""
    from bmc_agent.oracle_disagreement import (
        apply_diagnosis, DiagnosisResult, DiagnosisVerdict,
    )
    rep = _make_report(
        realism="realistic", dyn="not_triggered",
        confidence="confirmed_dynamic",
    )
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.SPEC_REFINE,
        suggested_clause="_a != NULL && _a->magic == 0xCAD11C9",
        rationale="caller-contract slip",
        confidence="high",
    )
    out = apply_diagnosis(rep, diag)
    assert out["confidence"] == "confirmed_dynamic"  # preserved
    assert out["oracle_disagreement_diagnosis"]["verdict"] == "spec_refine"
    assert out["oracle_disagreement_diagnosis"]["suggested_clause"] == (
        "_a != NULL && _a->magic == 0xCAD11C9"
    )


def test_apply_diagnosis_harness_encoding_attaches_only():
    from bmc_agent.oracle_disagreement import (
        apply_diagnosis, DiagnosisResult, DiagnosisVerdict,
    )
    rep = _make_report(
        realism="realistic", dyn="not_triggered",
        confidence="confirmed_dynamic",
    )
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.HARNESS_ENCODING,
        suggested_encoding="buf[N] = 0;",
        rationale="missing terminator",
        confidence="medium",
    )
    out = apply_diagnosis(rep, diag)
    assert out["confidence"] == "confirmed_dynamic"
    assert out["oracle_disagreement_diagnosis"]["suggested_encoding"] == "buf[N] = 0;"


def test_apply_diagnosis_inconclusive_attaches_no_change():
    from bmc_agent.oracle_disagreement import (
        apply_diagnosis, DiagnosisResult, DiagnosisVerdict,
    )
    rep = _make_report(
        realism="realistic", dyn="not_triggered",
        confidence="confirmed_dynamic",
    )
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.INCONCLUSIVE, rationale="?", confidence="low",
    )
    out = apply_diagnosis(rep, diag)
    assert out["confidence"] == "confirmed_dynamic"
    assert out["oracle_disagreement_diagnosis"]["verdict"] == "inconclusive"


# ---------------------------------------------------------------------------
# Pipeline integration: _diagnose_oracle_disagreements
# ---------------------------------------------------------------------------

def _make_pipeline(tmp_path, llm):
    """Build a minimal AMCPipeline whose only initialised pieces are the
    fields _diagnose_oracle_disagreements touches: self.store, self.llm."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.pipeline import AMCPipeline
    p = object.__new__(AMCPipeline)
    p.store = ArtifactStore(str(tmp_path / "artifacts"))
    p.llm = llm
    return p


def _write_bug_report(fn_dir, **fields):
    payload = {"saved_at": "2026-05-26T00:00:00Z", "report": fields}
    (fn_dir / "bug_report.json").write_text(json.dumps(payload, indent=2))


def test_pipeline_skips_when_no_bug_report(tmp_path):
    p = _make_pipeline(tmp_path, MagicMock())
    p._diagnose_oracle_disagreements("drv", "no_such_fn")  # must not raise


def test_pipeline_skips_when_oracles_agree(tmp_path):
    llm = MagicMock()
    p = _make_pipeline(tmp_path, llm)
    fn_dir = p.store._fn_dir("drv", "fn")
    _write_bug_report(
        fn_dir, function_name="fn", confidence="confirmed_dynamic",
        realism_check={"verdict": "realistic"},
        dynamic_outcome="confirmed",
    )
    p._diagnose_oracle_disagreements("drv", "fn")
    llm.complete.assert_not_called()
    # No diagnosis field added
    out = json.loads((fn_dir / "bug_report.json").read_text())["report"]
    assert "oracle_disagreement_diagnosis" not in out


def test_pipeline_fires_diagnosis_on_disagreement(tmp_path):
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "property_fp",
        "rationale": "saturated arithmetic — BMC over-cautious",
        "confidence": "high",
    })
    p = _make_pipeline(tmp_path, llm)
    fn_dir = p.store._fn_dir("drv", "fn")
    _write_bug_report(
        fn_dir,
        function_name="fn",
        violated_property="fn.overflow.1",
        confidence="confirmed_dynamic",
        realism_check={"verdict": "realistic", "reasoning": "caller could pass overflow input"},
        dynamic_outcome="not_triggered",
        reproducer="#include <archive.h>\nint main(){return 0;}",
        reasoning_trail="base",
    )
    p._diagnose_oracle_disagreements("drv", "fn")
    # Diagnosis attached
    out = json.loads((fn_dir / "bug_report.json").read_text())["report"]
    assert out["oracle_disagreement_diagnosis"]["verdict"] == "property_fp"
    # property_fp + high-confidence start → downgraded
    assert out["confidence"] == "unlikely"
    assert "[ORACLE-DISAGREEMENT]" in out["reasoning_trail"]


def test_pipeline_spec_refine_attaches_without_downgrade(tmp_path):
    """When LLM says SPEC_REFINE, the diagnosis is attached and the
    confidence is preserved (auto-application is a follow-up)."""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "spec_refine",
        "rationale": "loose PRE",
        "suggested_clause": "_a != NULL",
        "confidence": "high",
    })
    p = _make_pipeline(tmp_path, llm)
    fn_dir = p.store._fn_dir("drv", "fn")
    _write_bug_report(
        fn_dir, function_name="fn",
        confidence="confirmed_dynamic",
        realism_check={"verdict": "realistic"},
        dynamic_outcome="not_triggered",
    )
    p._diagnose_oracle_disagreements("drv", "fn")
    out = json.loads((fn_dir / "bug_report.json").read_text())["report"]
    assert out["confidence"] == "confirmed_dynamic"
    assert out["oracle_disagreement_diagnosis"]["suggested_clause"] == "_a != NULL"


def test_pipeline_tolerates_unparseable_bug_report(tmp_path):
    """A malformed bug_report.json must not crash the pipeline."""
    p = _make_pipeline(tmp_path, MagicMock())
    fn_dir = p.store._fn_dir("drv", "fn")
    (fn_dir / "bug_report.json").write_text("{not valid json")
    p._diagnose_oracle_disagreements("drv", "fn")  # no exception
