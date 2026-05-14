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
