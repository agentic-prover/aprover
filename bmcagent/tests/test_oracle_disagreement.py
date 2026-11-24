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
    fields _diagnose_oracle_disagreements touches: self.store, self.llm,
    self.config (artifact_dir routes the learned-constraints store)."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline
    art = str(tmp_path / "artifacts")
    p = object.__new__(AMCPipeline)
    p.store = ArtifactStore(art)
    p.llm = llm
    p.config = Config(llm_api_key="test", artifact_dir=art)
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


# ---------------------------------------------------------------------------
# Auto-application: persist diagnosis as learned constraint
# ---------------------------------------------------------------------------

def test_strip_cprover_assume_wrap_strips_outer_call():
    from bmc_agent.oracle_disagreement import _strip_cprover_assume_wrap
    assert _strip_cprover_assume_wrap("__CPROVER_assume(p != NULL);") == "p != NULL"
    assert _strip_cprover_assume_wrap("__CPROVER_assume(p != NULL)") == "p != NULL"
    assert _strip_cprover_assume_wrap("  __CPROVER_assume( p->magic == 0xCAD );  ") == "p->magic == 0xCAD"


def test_strip_cprover_assume_wrap_passes_through_bare_clause():
    from bmc_agent.oracle_disagreement import _strip_cprover_assume_wrap
    assert _strip_cprover_assume_wrap("p != NULL") == "p != NULL"
    assert _strip_cprover_assume_wrap("_a->magic == 0xCAD11C9") == "_a->magic == 0xCAD11C9"


def test_strip_cprover_assume_wrap_empty_returns_empty():
    from bmc_agent.oracle_disagreement import _strip_cprover_assume_wrap
    assert _strip_cprover_assume_wrap("") == ""


def test_persist_spec_refine_records_function_clause(tmp_path):
    """SPEC_REFINE diagnosis → persists clause via LearnedConstraintsStore
    in the function_clauses slot for the given function."""
    from bmc_agent.config import Config
    from bmc_agent.feedback_loop import LearnedConstraintsStore
    from bmc_agent.oracle_disagreement import (
        DiagnosisResult, DiagnosisVerdict,
        persist_diagnosis_to_learned_constraints,
    )
    art = tmp_path / "art"
    art.mkdir()
    cfg = Config(llm_api_key="x", artifact_dir=str(art))
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.SPEC_REFINE,
        suggested_clause="_a != NULL && _a->magic == 0xCAD11C9",
        rationale="caller-contract slip",
        confidence="high",
    )
    persisted = persist_diagnosis_to_learned_constraints(
        cfg, "archive_match_include_uid", diag, source_property="p.5",
    )
    assert persisted is True
    store = LearnedConstraintsStore(str(art))
    clauses = store.function_clauses("archive_match_include_uid")
    assert any("_a->magic" in c for c in clauses)


def test_persist_harness_encoding_strips_wrap_and_records(tmp_path):
    """HARNESS_ENCODING diagnosis often returns an already-wrapped
    ``__CPROVER_assume(...)`` clause; persistence strips the wrap so the
    harness emitter doesn't double-wrap on the next BMC run."""
    from bmc_agent.config import Config
    from bmc_agent.feedback_loop import LearnedConstraintsStore
    from bmc_agent.oracle_disagreement import (
        DiagnosisResult, DiagnosisVerdict,
        persist_diagnosis_to_learned_constraints,
    )
    art = tmp_path / "art"
    art.mkdir()
    cfg = Config(llm_api_key="x", artifact_dir=str(art))
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.HARNESS_ENCODING,
        suggested_encoding="__CPROVER_assume(buf->len < buf->cap);",
        rationale="harness over-permits buf->len > cap",
        confidence="medium",
    )
    persisted = persist_diagnosis_to_learned_constraints(cfg, "fn", diag)
    assert persisted is True
    clauses = LearnedConstraintsStore(str(art)).function_clauses("fn")
    # Bare clause stored, no double __CPROVER_assume wrap
    assert "buf->len < buf->cap" in clauses[0]
    assert not clauses[0].startswith("__CPROVER_assume")


def test_persist_property_fp_does_not_record():
    """PROPERTY_FP diagnoses don't produce a learnable clause — they
    just downgrade the finding. Persist call should be a no-op."""
    from bmc_agent.config import Config
    from bmc_agent.oracle_disagreement import (
        DiagnosisResult, DiagnosisVerdict,
        persist_diagnosis_to_learned_constraints,
    )
    cfg = Config(llm_api_key="x", artifact_dir="/tmp/_unused_should_not_create")
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.PROPERTY_FP,
        rationale="over-cautious BMC check",
        confidence="high",
    )
    assert persist_diagnosis_to_learned_constraints(cfg, "fn", diag) is False


def test_persist_inconclusive_does_not_record():
    from bmc_agent.config import Config
    from bmc_agent.oracle_disagreement import (
        DiagnosisResult, DiagnosisVerdict,
        persist_diagnosis_to_learned_constraints,
    )
    cfg = Config(llm_api_key="x", artifact_dir="/tmp/_unused_inconclusive")
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.INCONCLUSIVE, rationale="?", confidence="low",
    )
    assert persist_diagnosis_to_learned_constraints(cfg, "fn", diag) is False


def test_persist_empty_clause_does_not_record(tmp_path):
    """A SPEC_REFINE verdict that didn't actually emit a clause (LLM
    bug or malformed response) must not record an empty clause."""
    from bmc_agent.config import Config
    from bmc_agent.oracle_disagreement import (
        DiagnosisResult, DiagnosisVerdict,
        persist_diagnosis_to_learned_constraints,
    )
    cfg = Config(llm_api_key="x", artifact_dir=str(tmp_path))
    diag = DiagnosisResult(
        verdict=DiagnosisVerdict.SPEC_REFINE,
        suggested_clause="", rationale="x", confidence="low",
    )
    assert persist_diagnosis_to_learned_constraints(cfg, "fn", diag) is False


# ---------------------------------------------------------------------------
# Pipeline integration: return-value drives self_recheck_queue
# ---------------------------------------------------------------------------

def test_pipeline_returns_true_when_spec_refine_persists(tmp_path):
    """When the LLM emits SPEC_REFINE, the pipeline method should
    persist the clause AND return True so the caller adds the function
    to the re-verification queue."""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "spec_refine",
        "rationale": "tight pre missing",
        "suggested_clause": "_a != NULL && _a->magic == 0xCAD11C9",
        "confidence": "high",
    })

    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.artifacts import ArtifactStore
    art = tmp_path / "art"
    cfg = Config(llm_api_key="x", artifact_dir=str(art))
    p = object.__new__(AMCPipeline)
    p.store = ArtifactStore(str(art))
    p.llm = llm
    p.config = cfg
    fn_dir = p.store._fn_dir("drv", "fn")
    _write_bug_report(
        fn_dir, function_name="fn",
        violated_property="fn.pointer_dereference.5",
        confidence="confirmed_dynamic",
        realism_check={"verdict": "realistic", "reasoning": "x"},
        dynamic_outcome="not_triggered",
    )
    result = p._diagnose_oracle_disagreements("drv", "fn")
    assert result is True  # signals "add to recheck queue"

    # Persisted clause is in the store
    from bmc_agent.feedback_loop import LearnedConstraintsStore
    clauses = LearnedConstraintsStore(str(art)).function_clauses("fn")
    assert any("_a->magic" in c for c in clauses)


def test_pipeline_returns_false_when_property_fp(tmp_path):
    """PROPERTY_FP downgrades the finding but does NOT need re-verify."""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "verdict": "property_fp",
        "rationale": "over-cautious",
        "confidence": "high",
    })
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.artifacts import ArtifactStore
    art = tmp_path / "art"
    cfg = Config(llm_api_key="x", artifact_dir=str(art))
    p = object.__new__(AMCPipeline)
    p.store = ArtifactStore(str(art))
    p.llm = llm
    p.config = cfg
    fn_dir = p.store._fn_dir("drv", "fn")
    _write_bug_report(
        fn_dir, function_name="fn", confidence="confirmed_dynamic",
        realism_check={"verdict": "realistic"},
        dynamic_outcome="not_triggered",
    )
    result = p._diagnose_oracle_disagreements("drv", "fn")
    assert result is False


# ---------------------------------------------------------------------------
# Phase 3d shape 2: realism REALISTIC + dyn INCONCLUSIVE + UNREPRODUCIBLE
# ---------------------------------------------------------------------------

def _make_unreproducible_report(**extra):
    """Build a bug_report with the UNREPRODUCIBLE shape — surfaced by
    the archive_match_owner_excluded triage."""
    rep = _make_report(realism="realistic", dyn="inconclusive")
    rep["reproducer"] = (
        "// UNREPRODUCIBLE: The bug requires match_owner_id to return "
        "134217728 (0x8000000), but there is no public API to configure "
        "archive_match internal state to produce this specific non-boolean "
        "return value from the internal matching function."
    )
    rep.update(extra)
    return rep


def test_detect_fires_on_realism_real_dyn_inconclusive_unreproducible():
    """Phase 3d shape 2: when dyn-val is inconclusive specifically
    because the LLM emitted ``// UNREPRODUCIBLE``, that's the same
    signal as not_triggered (the LLM looked and couldn't construct
    a public-API call sequence) — fire the diagnoser."""
    from bmc_agent.oracle_disagreement import (
        detect_disagreement, DisagreementKind,
    )
    rep = _make_unreproducible_report()
    case = detect_disagreement(rep)
    assert case is not None
    assert case.kind == DisagreementKind.BMC_FAIL_REALISM_REAL_REPRODUCER_UNREACHABLE
    assert case.dyn_outcome == "inconclusive"


def test_detect_no_fire_on_inconclusive_without_unreproducible_marker():
    """A regular ``inconclusive`` (compile failure, timeout, etc.) is
    NOT the same as an UNREPRODUCIBLE marker — the former is a tool
    issue, the latter is structural unreachability. Only the latter
    fires shape 2."""
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_report(realism="realistic", dyn="inconclusive")
    rep["reproducer"] = "#include <archive.h>\nint main(){return 0;}"
    assert detect_disagreement(rep) is None


def test_detect_no_fire_on_unrealistic_with_unreproducible_marker():
    """Realism UNREAL + UNREPRODUCIBLE → all sources agree no real
    bug; no disagreement to diagnose."""
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_unreproducible_report(realism_check={"verdict": "unrealistic"})
    assert detect_disagreement(rep) is None


def test_detect_unreproducible_marker_recognised_after_whitespace():
    """The detector strips leading whitespace before checking the
    marker prefix — matches the dyn_validator's gate."""
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_unreproducible_report()
    rep["reproducer"] = "\n\n   // UNREPRODUCIBLE: stuff"
    assert detect_disagreement(rep) is not None


def test_detect_unreproducible_carries_reproducer_to_diagnose():
    """The unreproducible marker + the LLM's explanation reach the
    diagnoser as the reproducer_source — the marker text is the
    LLM's own admission of unreachability, which is the diagnostic
    signal."""
    from bmc_agent.oracle_disagreement import detect_disagreement
    rep = _make_unreproducible_report()
    case = detect_disagreement(rep)
    assert case is not None
    assert "UNREPRODUCIBLE" in case.reproducer_source
