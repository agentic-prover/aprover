"""Tests for the realism-rejection feedback loop (bmc_agent/feedback_loop.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Remediation parsing
# ---------------------------------------------------------------------------


def test_parse_remediation_code_change():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    raw = json.dumps({
        "scope": "code-change",
        "code_change": "Add a self-ref struct field NULL-init in harness_generator.",
        "rationale": "The CEx walks comp->next which is nondet — this is a model artifact class.",
        "confidence": "high",
    })
    r = _parse_remediation(raw, "xmlPatternStreamable")
    assert r.scope == RemediationScope.CODE_CHANGE
    assert "self-ref" in r.code_change
    assert r.confidence == "high"


def test_parse_remediation_function_spec():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    raw = json.dumps({
        "scope": "function-spec",
        "clause": "comp != NULL && comp->next == NULL",
        "rationale": "All real callers pass a freshly-allocated comp with no chain attached.",
        "confidence": "medium",
    })
    r = _parse_remediation(raw, "xmlPatternStreamable")
    assert r.scope == RemediationScope.FUNCTION_SPEC
    assert r.clause == "comp != NULL && comp->next == NULL"


def test_parse_remediation_project_invariant():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    raw = json.dumps({
        "scope": "project-invariant",
        "clause": "xmlMalloc != NULL",
        "rationale": "xmlMalloc is set by library init before any public API.",
        "confidence": "high",
    })
    r = _parse_remediation(raw, "any_function")
    assert r.scope == RemediationScope.PROJECT_INVARIANT
    assert r.clause == "xmlMalloc != NULL"


def test_parse_remediation_handles_markdown_fence():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    raw = (
        "```json\n"
        + json.dumps({
            "scope": "none",
            "rationale": "Cannot safely propose anything.",
            "confidence": "low",
        })
        + "\n```\n"
    )
    r = _parse_remediation(raw, "f")
    assert r.scope == RemediationScope.NONE


def test_parse_remediation_handles_garbage():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    r = _parse_remediation("not json at all", "f")
    assert r.scope == RemediationScope.NONE


# ---------------------------------------------------------------------------
# LearnedConstraintsStore
# ---------------------------------------------------------------------------


def test_store_starts_empty(tmp_path: Path):
    from bmc_agent.feedback_loop import LearnedConstraintsStore
    store = LearnedConstraintsStore(tmp_path)
    assert store.project_clauses() == []
    assert store.function_clauses("anything") == []
    assert store.summary()["project_clauses"] == 0


def test_store_records_project_invariant(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.PROJECT_INVARIANT,
        clause="xmlMalloc != NULL",
        rationale="lib init",
        confidence="high",
    )
    assert store.record("any_fn", r) is True
    assert "xmlMalloc != NULL" in store.project_clauses()
    # Re-recording is a no-op (idempotent)
    assert store.record("any_fn", r) is False


def test_store_records_function_clause(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.FUNCTION_SPEC,
        clause="comp != NULL",
        rationale="all callers check",
        confidence="medium",
    )
    store.record("xmlPatternStreamable", r)
    assert "comp != NULL" in store.function_clauses("xmlPatternStreamable")
    # Different function: not contaminated
    assert store.function_clauses("xmlOther") == []


def test_store_records_code_change_todo(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.CODE_CHANGE,
        code_change="Add an artifact pattern for chrooted-host filesystem stubs.",
        rationale="not encodable as an invariant",
        confidence="high",
    )
    store.record("xmlSomeFn", r, source_property="xmlSomeFn.pointer.1")
    summary = store.summary()
    assert summary["code_change_todos"] == 1


def test_store_persists_across_instances(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    s1 = LearnedConstraintsStore(tmp_path)
    s1.record("f", Remediation(
        scope=RemediationScope.PROJECT_INVARIANT,
        clause="xmlFree != NULL",
        confidence="high",
    ))
    # Reload from disk
    s2 = LearnedConstraintsStore(tmp_path)
    assert "xmlFree != NULL" in s2.project_clauses()


def test_store_auto_promotes_when_threshold_reached(tmp_path: Path):
    """When ≥PROMOTION_THRESHOLD functions independently learn the same
    clause, it auto-migrates from function_clauses to project_clauses."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    clause = "ctxt != NULL"
    r = Remediation(scope=RemediationScope.FUNCTION_SPEC, clause=clause)
    store.record("fnA", r)
    store.record("fnB", r)
    # Not yet promoted (only 2 functions)
    assert clause not in store.project_clauses()
    assert clause in store.function_clauses("fnA")
    # 3rd function triggers promotion
    store.record("fnC", r)
    assert clause in store.project_clauses()
    # Per-function copies are retired
    assert clause not in store.function_clauses("fnA")
    assert clause not in store.function_clauses("fnB")
    assert clause not in store.function_clauses("fnC")


def test_store_does_not_double_promote(tmp_path: Path):
    """Re-recording the same clause for additional functions after
    promotion shouldn't duplicate the project entry."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    r = Remediation(scope=RemediationScope.FUNCTION_SPEC, clause="x > 0")
    for fn in ("a", "b", "c", "d", "e"):
        store.record(fn, r)
    assert store.project_clauses().count("x > 0") == 1


def test_store_ignores_unknown_schema_version(tmp_path: Path):
    from bmc_agent.feedback_loop import LearnedConstraintsStore
    f = tmp_path / "learned_constraints.json"
    f.write_text(json.dumps({"version": 99, "project_clauses": ["bogus"]}))
    store = LearnedConstraintsStore(tmp_path)
    assert store.project_clauses() == []


# ---------------------------------------------------------------------------
# Harness applies learned clauses when feedback-loop enabled
# ---------------------------------------------------------------------------


def test_harness_emit_learned_clauses_disabled_returns_empty(tmp_path: Path):
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config
    config = Config()
    config.artifact_dir = str(tmp_path)
    # feedback loop OFF — nothing emitted even if store has entries
    assert _emit_learned_clauses(config, "any_fn", "project") == []


def test_harness_emit_learned_clauses_project(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    # Seed the store
    store = LearnedConstraintsStore(tmp_path)
    store.record("any", Remediation(
        scope=RemediationScope.PROJECT_INVARIANT,
        clause="xmlMalloc != NULL",
        confidence="high",
    ))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    out = _emit_learned_clauses(config, "x", "project")
    assert "__CPROVER_assume(xmlMalloc != NULL);" in out


def test_harness_emit_learned_clauses_function_scoped(tmp_path: Path):
    """function clauses must be returned ONLY for the matching function."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    store = LearnedConstraintsStore(tmp_path)
    store.record("xmlFoo", Remediation(
        scope=RemediationScope.FUNCTION_SPEC,
        clause="x != NULL",
        confidence="medium",
    ))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    assert _emit_learned_clauses(config, "xmlFoo", "function") == [
        "__CPROVER_assume(x != NULL);"
    ]
    assert _emit_learned_clauses(config, "xmlOther", "function") == []


# ---------------------------------------------------------------------------
# Feedback loop: flag_selection threading (bug 2 regression)
# ---------------------------------------------------------------------------


def test_feedback_iterate_threads_flag_selection_through(tmp_path: Path):
    """The iter-1 CBMC re-run must receive the same Phase-1.5
    flag_selection that iter-0 used. Without this, --unsigned-overflow-check
    and friends get dropped on the re-run and CBMC silently 'verifies
    clean' the property the bug was on.

    Regression: VibeOS memory.c malloc.overflow.1 was being suppressed
    this way — iter-0 CBMC with --unsigned-overflow-check found the
    overflow, iter-1 dropped the flag and "verified clean".
    """
    from unittest.mock import MagicMock
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.feedback_loop import Remediation, RemediationScope
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.flag_selector import FlagSelection
    from bmc_agent.config import Config

    config = Config()
    config.enable_realism_check = True
    config.enable_feedback_loop = True
    config.feedback_max_iters = 1

    pipeline = AMCPipeline.__new__(AMCPipeline)
    pipeline.config = config
    pipeline.llm = MagicMock()
    pipeline.cex_validator = MagicMock()
    pipeline.realism_checker = MagicMock()
    pipeline.reporter = MagicMock()

    # The iter-0 flag selection stashed on self by Phase 1.5.
    pipeline._flag_selections = {
        "malloc": FlagSelection(unsigned_overflow_check=True, reasoning="size math"),
    }

    # Capture the flag_selection arg the feedback loop's re-run passes.
    captured = {}
    fake_verdict = MagicMock()
    fake_verdict.verified = True
    fake_verdict.counterexamples = []
    def fake_check(func, spec, parsed, driver_name, all_funcs=None, flag_selection=None):
        captured["flag_selection"] = flag_selection
        return fake_verdict
    pipeline.bmc_engine = MagicMock()
    pipeline.bmc_engine.check_function.side_effect = fake_check

    # _feedback_record's LLM distillation: skip the real LLM, return a clause.
    pipeline._feedback_record = MagicMock(return_value=Remediation(
        scope=RemediationScope.FUNCTION_SPEC,
        clause="size <= (SIZE_MAX / 2)",
        confidence="high",
    ))

    # Inputs for _feedback_iterate.
    validation = MagicMock()
    validation.counterexample = MagicMock()
    validation.counterexample.failing_property = "malloc.overflow.1"
    validation.counterexample.failure_location = {"line": "10"}
    realism = RealismCheckResult(
        verdict=RealismVerdict.UNREALISTIC,
        reasoning="bounded heap, never SIZE_MAX",
        key_concern="overflow on alignment math",
        llm_confidence="high",
    )
    func = MagicMock(); func.name = "malloc"; func.body = "..."
    from bmc_agent.spec import Spec
    spec = Spec(function_name="malloc", precondition="size >= 0", postcondition="true")

    pipeline._feedback_iterate(
        validation, realism, func, spec, MagicMock(),
        all_funcs={}, driver_name="d", all_specs={},
    )

    # CRITICAL: the flag_selection from Phase 1.5 must have been passed through.
    assert captured.get("flag_selection") is not None
    assert captured["flag_selection"].unsigned_overflow_check is True


def test_pipeline_clean_proof_helpers():
    """``_all_applied_clauses`` and ``_flag_summary`` give the log line
    explicit information about what was assumed and what was checked."""
    from bmc_agent.pipeline import _all_applied_clauses, _flag_summary
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.flag_selector import FlagSelection
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = LearnedConstraintsStore(td)
        # Project + function clauses
        store.record("malloc", Remediation(
            scope=RemediationScope.FUNCTION_SPEC,
            clause="size <= (SIZE_MAX / 2)",
            confidence="high",
        ))
        store.record("anyone", Remediation(
            scope=RemediationScope.PROJECT_INVARIANT,
            clause="heap_start != 0",
            confidence="high",
        ))
        cfg = Config()
        cfg.enable_feedback_loop = True
        cfg.artifact_dir = td
        spec = Spec(function_name="malloc", precondition="size >= 0", postcondition="true")
        clauses = _all_applied_clauses(cfg, "malloc", spec)
        # Order: project, function, spec-pre
        assert clauses == ["heap_start != 0", "size <= (SIZE_MAX / 2)", "size >= 0"]

    # Trivial preconditions ("true", "1") get filtered out so the log
    # doesn't say "verified clean under {true}".
    cfg2 = Config()
    cfg2.enable_feedback_loop = False
    spec2 = Spec(function_name="f", precondition="true", postcondition="true")
    assert _all_applied_clauses(cfg2, "f", spec2) == []

    # Flag summary
    assert _flag_summary(None) == "default (pointer-check, bounds-check)"
    assert _flag_summary(FlagSelection()) == "default (pointer-check, bounds-check)"
    s = _flag_summary(FlagSelection(unsigned_overflow_check=True, pointer_overflow_check=True))
    assert "unsigned-overflow-check" in s
    assert "pointer-overflow-check" in s
