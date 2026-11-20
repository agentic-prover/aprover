"""Tests for Phase 3e — in-pipeline TriageToolsAgent oracle.

The pipeline-time wiring in ``AMCPipeline._run_phase_3e_triage`` runs
the agent on every unresolved counterexample, writes a triage.json
sidecar matching ``scripts/triage_unresolved.py``'s layout, and
promotes REAL_BUG/high verdicts into bug reports. These tests stub
out the agent and ``_make_report`` so we can validate the wiring
without LLM calls or a full corpus.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bmc_agent.agents.base import AgentResult
from bmc_agent.agents.triage import TriageResult, TriageVerdict
from bmc_agent.cbmc import Counterexample
from bmc_agent.cex_validator import CExOutcome, ValidationResult


def _make_pipeline(tmp_path, *, enable_3e: bool = True):
    """Minimal AMCPipeline with just the fields _run_phase_3e_triage uses."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline

    art = str(tmp_path / "artifacts")
    p = object.__new__(AMCPipeline)
    p.store = ArtifactStore(art)
    p.llm = MagicMock()
    p.config = Config(llm_api_key="test", artifact_dir=art)
    p.config.enable_phase_3e_triage = enable_3e
    p.reporter = BugReporter(p.store)
    # spec_gen normally holds corpus_paths; for the test, expose an
    # empty list so the agent's grep tool just operates on the parsed
    # file passed in.
    p.spec_gen = MagicMock()
    p.spec_gen.corpus_paths = []
    return p


def _make_validation(
    fn_name: str,
    prop: str,
    *,
    reasoning: str = "ambiguous CEx",
    caller_path: list | None = None,
) -> ValidationResult:
    cex = Counterexample(
        failing_property=prop,
        variable_assignments={"x": "10", "__CPROVER_internal": "ignored"},
    )
    return ValidationResult(
        function_name=fn_name,
        counterexample=cex,
        caller_path=caller_path or [],
        system_entry_input=None,
        refinement_history=[],
        final_precondition=None,
        reasoning=reasoning,
        outcome=CExOutcome.UNRESOLVED,
    )


def _make_func_info(name: str, body: str = "void f() {}"):
    """Build a minimal FunctionInfo duck-typed for the helper's use."""
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name=name,
        return_type="void",
        parameters=[],
        is_static=False,
    )
    return FunctionInfo(
        name=name,
        signature=sig,
        body=body,
        callees=set(),
        source_file="test.c",
    )


def _stub_agent_result(
    verdict: TriageVerdict,
    confidence: str = "high",
    fp_class: str | None = None,
    reasoning: str = "stubbed",
) -> AgentResult[TriageResult]:
    return AgentResult(
        output=TriageResult(
            verdict=verdict,
            confidence=confidence,
            reasoning=reasoning,
            fp_class=fp_class,
        ),
        raw_response="",
    )


def test_phase_3e_noop_when_unresolved_empty(tmp_path):
    """No CExes → method exits without instantiating the agent."""
    p = _make_pipeline(tmp_path)
    parsed = MagicMock()
    parsed.path = tmp_path / "src.c"
    p._run_phase_3e_triage(
        driver_name="drv",
        parsed=parsed,
        all_funcs={},
        current_specs={},
        bug_reports=[],
        confirmed_real_bugs=set(),
    )
    # Nothing crashed; _unresolved still empty.
    assert p.reporter._unresolved == []


def test_phase_3e_promotes_real_bug_high(tmp_path, monkeypatch):
    p = _make_pipeline(tmp_path)
    val = _make_validation("fn", "fn.overflow.1")
    p.reporter._unresolved.append(val)

    func = _make_func_info("fn", "void fn() { /* buggy */ }")
    spec = MagicMock()
    all_funcs = {"fn": func}
    current_specs = {"fn": spec}

    # Stub the agent to vote REAL_BUG/high.
    from bmc_agent.agents import triage_tools

    class StubAgent:
        def __init__(self, *a, **kw): pass
        def run(self, **kw):
            return _stub_agent_result(
                TriageVerdict.REAL_BUG, "high",
                reasoning="size calculator under-budgets path",
            )

    monkeypatch.setattr(triage_tools, "TriageToolsAgent", StubAgent)

    # Stub _make_report so we don't drag the full report pipeline in.
    made_reports: list = []

    def fake_make_report(self, validation, *a, **kw):
        made_reports.append(validation)
        rep = MagicMock()
        rep.function_name = validation.function_name
        rep.confidence = "confirmed_bmc"
        return rep

    from bmc_agent.pipeline import AMCPipeline
    monkeypatch.setattr(AMCPipeline, "_make_report", fake_make_report)

    bug_reports: list = []
    confirmed: set = set()
    parsed = MagicMock()
    parsed.path = tmp_path / "src.c"

    p._run_phase_3e_triage(
        driver_name="drv",
        parsed=parsed,
        all_funcs=all_funcs,
        current_specs=current_specs,
        bug_reports=bug_reports,
        confirmed_real_bugs=confirmed,
    )

    assert len(made_reports) == 1
    assert len(bug_reports) == 1
    assert ("fn", "overflow") in confirmed
    # Promoted out of unresolved
    assert p.reporter._unresolved == []
    # Sidecar written next to per-CEx classification dir
    sidecar = (
        p.store._fn_dir("drv", "fn") / "classifications" / "fn.overflow.1.triage.json"
    )
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["verdict"] == "real_bug"
    assert data["confidence"] == "high"
    assert data["phase"] == "3e"
    # Validation got its outcome flipped + reasoning extended
    assert val.outcome == CExOutcome.REAL_BUG
    assert "[Phase 3e triage:" in val.reasoning


def test_phase_3e_keeps_likely_fp_in_unresolved(tmp_path, monkeypatch):
    p = _make_pipeline(tmp_path)
    val = _make_validation("fn", "fn.pointer_dereference.2")
    p.reporter._unresolved.append(val)

    func = _make_func_info("fn")
    all_funcs = {"fn": func}
    current_specs = {"fn": MagicMock()}

    from bmc_agent.agents import triage_tools

    class StubAgent:
        def __init__(self, *a, **kw): pass
        def run(self, **kw):
            return _stub_agent_result(
                TriageVerdict.LIKELY_FP, "high",
                fp_class="harness-uninitialized-opaque-struct",
                reasoning="caller invariants exclude the CEx state",
            )

    monkeypatch.setattr(triage_tools, "TriageToolsAgent", StubAgent)

    bug_reports: list = []
    confirmed: set = set()
    parsed = MagicMock()

    p._run_phase_3e_triage(
        driver_name="drv",
        parsed=parsed,
        all_funcs=all_funcs,
        current_specs=current_specs,
        bug_reports=bug_reports,
        confirmed_real_bugs=confirmed,
    )

    # No promotion.
    assert bug_reports == []
    assert confirmed == set()
    # Still in unresolved bucket — not auto-dismissed.
    assert len(p.reporter._unresolved) == 1
    # Sidecar with fp_class.
    sidecar = (
        p.store._fn_dir("drv", "fn") / "classifications"
        / "fn.pointer_dereference.2.triage.json"
    )
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["verdict"] == "likely_fp"
    assert data["fp_class"] == "harness-uninitialized-opaque-struct"
    # ValidationResult was NOT flipped.
    assert val.outcome == CExOutcome.UNRESOLVED


def test_phase_3e_skips_real_bug_low_confidence(tmp_path, monkeypatch):
    """REAL_BUG with low/medium confidence does NOT promote — we only
    promote ``high`` so the in-pipeline path can't inflate counts."""
    p = _make_pipeline(tmp_path)
    val = _make_validation("fn", "fn.overflow.3")
    p.reporter._unresolved.append(val)

    func = _make_func_info("fn")
    all_funcs = {"fn": func}
    current_specs = {"fn": MagicMock()}

    from bmc_agent.agents import triage_tools

    class StubAgent:
        def __init__(self, *a, **kw): pass
        def run(self, **kw):
            return _stub_agent_result(TriageVerdict.REAL_BUG, "medium")

    monkeypatch.setattr(triage_tools, "TriageToolsAgent", StubAgent)

    bug_reports: list = []
    p._run_phase_3e_triage(
        driver_name="drv",
        parsed=MagicMock(),
        all_funcs=all_funcs,
        current_specs=current_specs,
        bug_reports=bug_reports,
        confirmed_real_bugs=set(),
    )

    assert bug_reports == []
    assert len(p.reporter._unresolved) == 1
    # Sidecar still written so the verdict isn't silently lost
    sidecar = (
        p.store._fn_dir("drv", "fn") / "classifications"
        / "fn.overflow.3.triage.json"
    )
    assert sidecar.exists()
    assert json.loads(sidecar.read_text())["confidence"] == "medium"


def test_phase_3e_tolerates_agent_errors(tmp_path, monkeypatch):
    """An agent returning no parsed output is logged + skipped, not fatal."""
    p = _make_pipeline(tmp_path)
    p.reporter._unresolved.append(_make_validation("fn", "fn.unwind.0"))

    func = _make_func_info("fn")
    all_funcs = {"fn": func}
    current_specs = {"fn": MagicMock()}

    from bmc_agent.agents import triage_tools

    class StubAgent:
        def __init__(self, *a, **kw): pass
        def run(self, **kw):
            return AgentResult(output=None, error="parse failure")

    monkeypatch.setattr(triage_tools, "TriageToolsAgent", StubAgent)

    p._run_phase_3e_triage(
        driver_name="drv",
        parsed=MagicMock(),
        all_funcs=all_funcs,
        current_specs=current_specs,
        bug_reports=[],
        confirmed_real_bugs=set(),
    )

    # Still in unresolved, no sidecar
    assert len(p.reporter._unresolved) == 1
    sidecar = (
        p.store._fn_dir("drv", "fn") / "classifications" / "fn.unwind.0.triage.json"
    )
    assert not sidecar.exists()


def test_phase_3e_skips_duplicate_confirmed_real_bug(tmp_path, monkeypatch):
    """If the prop-type was already confirmed elsewhere, triage doesn't
    double-count but DOES drop the duplicate from unresolved."""
    p = _make_pipeline(tmp_path)
    val = _make_validation("fn", "fn.overflow.1")
    p.reporter._unresolved.append(val)

    func = _make_func_info("fn")
    all_funcs = {"fn": func}
    current_specs = {"fn": MagicMock()}

    from bmc_agent.agents import triage_tools

    class StubAgent:
        def __init__(self, *a, **kw): pass
        def run(self, **kw):
            return _stub_agent_result(TriageVerdict.REAL_BUG, "high")

    monkeypatch.setattr(triage_tools, "TriageToolsAgent", StubAgent)

    from bmc_agent.pipeline import AMCPipeline
    made_reports: list = []

    def fake_make_report(self, validation, *a, **kw):
        made_reports.append(validation)
        return MagicMock(confidence="confirmed_bmc")

    monkeypatch.setattr(AMCPipeline, "_make_report", fake_make_report)

    bug_reports: list = []
    confirmed: set = {("fn", "overflow")}  # already confirmed in Phase 3

    p._run_phase_3e_triage(
        driver_name="drv",
        parsed=MagicMock(),
        all_funcs=all_funcs,
        current_specs=current_specs,
        bug_reports=bug_reports,
        confirmed_real_bugs=confirmed,
    )

    # _make_report NOT called — no duplicate report
    assert made_reports == []
    assert bug_reports == []
    # But the duplicate is dropped from unresolved
    assert p.reporter._unresolved == []
