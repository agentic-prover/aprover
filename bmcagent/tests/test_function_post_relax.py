"""Tests for the FUNCTION_POST_RELAX remediation scope.

Drops over-tight POST clauses (analogous to the no-longer-extant CALLEE_SPEC_RELAX,
from a callee's spec) but for the FUT's own postcondition. Triggered
when CBMC reports ``main.assertion.<N>`` violations that trace to an
over-tight LLM-emitted POST (e.g., ``result == 0 || result < 0`` when
real Linux semantics allow positive returns from copy_from_user).
"""

from __future__ import annotations

from bmc_agent.feedback_loop import (
    LearnedConstraintsStore,
    Remediation,
    RemediationScope,
    _parse_remediation,
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_function_post_relax_scope():
    raw = (
        '{"scope": "function-post-relax", "clause": "result == 0 || result < 0", '
        '"rationale": "copy_from_user returns non-negative byte count"}'
    )
    r = _parse_remediation(raw, "ncdev_bar_rw")
    assert r.scope == RemediationScope.FUNCTION_POST_RELAX
    assert r.clause == "result == 0 || result < 0"


def test_parse_function_post_relax_basic():
    """FUT-POST relaxations are about the FUT's own spec — parser must
    accept this scope on its own without any callee-side argument."""
    raw = '{"scope": "function-post-relax", "clause": "result > 0"}'
    r = _parse_remediation(raw, "fut")
    assert r.scope == RemediationScope.FUNCTION_POST_RELAX


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_store_persists_function_post_relaxation(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.FUNCTION_POST_RELAX,
        clause="result == 0 || result < 0",
        rationale="kernel copy_from_user returns byte count",
        confidence="high",
    )
    assert s.record("ncdev_bar_rw", r) is True
    assert s.function_post_relaxations("ncdev_bar_rw") == [
        "result == 0 || result < 0",
    ]


def test_store_post_relaxations_survive_reload(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    s.record(
        "ncdev_bar_rw",
        Remediation(
            scope=RemediationScope.FUNCTION_POST_RELAX,
            clause="result > 0 ==> result is errno",
        ),
    )
    s2 = LearnedConstraintsStore(tmp_path)
    assert s2.function_post_relaxations("ncdev_bar_rw") == [
        "result > 0 ==> result is errno"
    ]


def test_store_post_relaxations_unknown_func_returns_empty(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    assert s.function_post_relaxations("never_seen") == []


def test_store_post_relaxation_dedup(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.FUNCTION_POST_RELAX,
        clause="result < 0",
    )
    assert s.record("f", r) is True
    # Same clause again is a no-op.
    assert s.record("f", r) is False
    assert s.function_post_relaxations("f") == ["result < 0"]


def test_store_no_op_when_clause_missing(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    assert s.record(
        "f",
        Remediation(scope=RemediationScope.FUNCTION_POST_RELAX, clause=""),
    ) is False
    assert s.function_post_relaxations("f") == []


# ---------------------------------------------------------------------------
# Consumption: the harness's FUT-POST assert is regenerated without
# the relaxed clauses on the next run.
# ---------------------------------------------------------------------------


def test_harness_drops_relaxed_post_clauses(tmp_path):
    """End-to-end through the persistence store: a relaxation on the
    FUT's POST is dropped from the assert(...) emitted by
    generate_harness."""
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import FunctionSignature, ParsedCFile
    from bmc_agent.spec import Spec

    # Seed the store before constructing the harness generator so the
    # generator reads the relaxation from disk.
    store = LearnedConstraintsStore(tmp_path)
    store.record(
        "f",
        Remediation(
            scope=RemediationScope.FUNCTION_POST_RELAX,
            clause="result == 0 || result < 0",
        ),
    )

    cfg = Config()
    cfg.artifact_dir = str(tmp_path)
    gen = HarnessGenerator(cfg)
    relax = gen._function_post_relaxations("f")
    assert "result == 0 || result < 0" in relax

    # Apply via drop_clauses to mirror what generate_harness does.
    from bmc_agent.spec import drop_clauses
    post = "result == 0 || result < 0"
    out = drop_clauses(post, relax)
    assert out == "" or "result" not in out
