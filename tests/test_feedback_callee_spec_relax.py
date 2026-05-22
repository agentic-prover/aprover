"""Tests for the callee-spec-relax remediation in the feedback loop.

When bug-hunt mode (validity/protocol split) emits ``assert(PRE)`` at
the top of a callee stub and CBMC reports
``<callee>_stub.assertion.<N>`` FAILURE, the over-tight clause belongs
to the callee's spec — not the FUT's. The new CALLEE_SPEC_RELAX scope
attaches the relaxation to the callee.

Covers:
- ``extract_stub_callee`` recognises the bug-hunt-mode property pattern.
- ``_parse_remediation`` accepts ``callee-spec-relax`` only when a
  stub_callee was provided; otherwise downgrades to NONE.
- ``LearnedConstraintsStore`` persists and recalls callee relaxations.
"""

from __future__ import annotations

from bmc_agent.feedback_loop import (
    LearnedConstraintsStore,
    Remediation,
    RemediationScope,
    _parse_remediation,
    extract_stub_callee,
)


# ---------------------------------------------------------------------------
# Property-name detector
# ---------------------------------------------------------------------------


def test_extract_stub_callee_recognises_bug_hunt_pattern():
    assert extract_stub_callee("ncdev_bar_read_stub.assertion.3") == "ncdev_bar_read"
    assert extract_stub_callee("ncdev_bar_write_stub.assertion.1") == "ncdev_bar_write"
    # Multiple underscores in the callee name are preserved.
    assert extract_stub_callee("neuron_copy_from_user_stub.assertion.2") == "neuron_copy_from_user"


def test_extract_stub_callee_returns_none_for_non_stub_properties():
    # FUT body assertions (the normal counterexample kind).
    assert extract_stub_callee("main.assertion.1") is None
    assert extract_stub_callee("main.pointer_dereference.5") is None
    # Other CBMC property classes inside the stub do NOT count — only
    # the "assertion" class is emitted by bug-hunt mode's stub-PRE
    # check.
    assert extract_stub_callee("ncdev_bar_read_stub.pointer_dereference.3") is None
    # Whitespace tolerated; empty / None return None.
    assert extract_stub_callee("") is None
    assert extract_stub_callee("   ") is None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_callee_spec_relax_when_stub_callee_present():
    raw = (
        '{"scope": "callee-spec-relax", "clause": "valid(user_va)", '
        '"rationale": "copy_to_user handles NULL gracefully", '
        '"confidence": "high"}'
    )
    r = _parse_remediation(raw, "ncdev_bar_rw", stub_callee="ncdev_bar_read")
    assert r.scope == RemediationScope.CALLEE_SPEC_RELAX
    assert r.clause == "valid(user_va)"
    assert r.callee == "ncdev_bar_read"
    assert r.confidence == "high"


def test_parse_callee_spec_relax_downgraded_when_no_stub_callee():
    """The model can only legitimately pick callee-spec-relax when the
    violation was inside a callee stub. If it picks the scope while
    the violation is in the FUT body, that's a model error — downgrade
    to NONE rather than persisting a bogus relaxation."""
    raw = '{"scope": "callee-spec-relax", "clause": "x > 0"}'
    r = _parse_remediation(raw, "fut", stub_callee="")
    assert r.scope == RemediationScope.NONE
    # Empty callee in the resulting Remediation.
    assert r.callee == ""


def test_parse_existing_scopes_unaffected_by_stub_callee_arg():
    raw = '{"scope": "function-spec", "clause": "p != NULL"}'
    r = _parse_remediation(raw, "fut", stub_callee="some_callee")
    assert r.scope == RemediationScope.FUNCTION_SPEC
    # callee field stays empty for non-callee-spec-relax scopes.
    assert r.callee == ""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_store_persists_callee_relaxation(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.CALLEE_SPEC_RELAX,
        clause="valid(user_va)",
        callee="ncdev_bar_read",
        rationale="copy_to_user handles NULL",
        confidence="high",
    )
    changed = s.record("ncdev_bar_rw", r)
    assert changed is True
    assert s.callee_relaxations("ncdev_bar_read") == ["valid(user_va)"]
    # A second, distinct clause for the same callee accumulates.
    r2 = Remediation(
        scope=RemediationScope.CALLEE_SPEC_RELAX,
        clause="data_count > 0",
        callee="ncdev_bar_read",
    )
    assert s.record("ncdev_bar_rw", r2) is True
    assert s.callee_relaxations("ncdev_bar_read") == [
        "valid(user_va)",
        "data_count > 0",
    ]
    # Dedup: re-recording the same clause is a no-op.
    assert s.record("ncdev_bar_rw", r2) is False


def test_store_relaxations_survive_reload(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    s.record(
        "fut",
        Remediation(
            scope=RemediationScope.CALLEE_SPEC_RELAX,
            clause="bar == 0 || bar == 2",
            callee="ncdev_bar_read",
        ),
    )
    # Fresh store instance — should load from disk.
    s2 = LearnedConstraintsStore(tmp_path)
    assert s2.callee_relaxations("ncdev_bar_read") == [
        "bar == 0 || bar == 2"
    ]


def test_store_callee_relaxations_unknown_callee_returns_empty(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    assert s.callee_relaxations("never_seen") == []


def test_store_no_op_when_clause_or_callee_missing(tmp_path):
    s = LearnedConstraintsStore(tmp_path)
    # No clause → no-op.
    assert s.record(
        "fut",
        Remediation(scope=RemediationScope.CALLEE_SPEC_RELAX, callee="cc"),
    ) is False
    # No callee → no-op.
    assert s.record(
        "fut",
        Remediation(scope=RemediationScope.CALLEE_SPEC_RELAX, clause="x"),
    ) is False
    assert s.callee_relaxations("cc") == []
