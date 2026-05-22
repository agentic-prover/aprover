"""Tests for the consumption side of callee-spec-relax remediation.

The feedback loop persists ``callee_relaxations`` to
``learned_constraints.json``. On the next run, ``_generate_stub``
should consult the store, drop the matching clauses from the callee's
PRE, and emit asserts/assumes WITHOUT the FP-producing clauses.

This closes the feedback loop for bug-hunt mode FPs.
"""

from __future__ import annotations

from bmc_agent.feedback_loop import LearnedConstraintsStore, Remediation, RemediationScope
from bmc_agent.harness_generator import _generate_stub
from bmc_agent.parser import FunctionSignature, ParsedCFile
from bmc_agent.spec import Spec, drop_clauses


def _ncdev_bar_read_pf() -> ParsedCFile:
    sig = FunctionSignature(
        name="ncdev_bar_read",
        return_type="int",
        parameters=[
            ("struct neuron_device *", "nd"),
            ("u64 *", "reg_addresses"),
            ("u32", "data_count"),
            ("void *", "user_va"),
        ],
    )
    return ParsedCFile(
        path="ncdev.c",
        functions={"ncdev_bar_read": sig},
        call_graph={},
        function_bodies={},
    )


def _spec_with_overtight_pre() -> Spec:
    return Spec(
        function_name="ncdev_bar_read",
        precondition=(
            "valid(nd) && valid_range(reg_addresses, 0, data_count) && "
            "valid(user_va) && data_count > 0"
        ),
        postcondition="result == 0 || result < 0",
    )


# ---------------------------------------------------------------------------
# drop_clauses helper
# ---------------------------------------------------------------------------


def test_drop_clauses_removes_matching_clauses_only():
    pre = (
        "valid(nd) && valid_range(reg_addresses, 0, data_count) && "
        "valid(user_va) && data_count > 0"
    )
    out = drop_clauses(pre, ["valid(user_va)", "data_count > 0"])
    assert "valid(user_va)" not in out
    assert "data_count > 0" not in out
    # Untouched clauses remain.
    assert "valid(nd)" in out
    assert "valid_range(reg_addresses, 0, data_count)" in out


def test_drop_clauses_whitespace_insensitive():
    pre = "valid(p)  &&   data_count >  0"
    out = drop_clauses(pre, ["data_count > 0"])
    assert "data_count" not in out
    assert "valid(p)" in out


def test_drop_clauses_handles_requires_prefix():
    pre = "requires valid(p) && x > 0"
    out = drop_clauses(pre, ["x > 0"])
    assert out.startswith("requires")
    assert "valid(p)" in out
    assert "x > 0" not in out


def test_drop_clauses_returns_empty_when_all_dropped():
    out = drop_clauses("valid(p)", ["valid(p)"])
    assert out == ""


def test_drop_clauses_unknown_clauses_are_no_op():
    """An LLM-regenerated spec may no longer contain a previously-dropped
    clause. Stale relaxations must be tolerated, not raised."""
    pre = "valid(p)"
    out = drop_clauses(pre, ["never_appeared(x)", "another > 0"])
    assert out == "valid(p)"


def test_drop_clauses_empty_inputs():
    assert drop_clauses("", ["x"]) == ""
    assert drop_clauses("valid(p)", []) == "valid(p)"


def test_drop_clauses_paren_insensitive_match():
    """A spec writes ``(bar == 0 || bar == 2)`` with outer parens;
    the split removes them so the in-clause form is ``bar == 0 ||
    bar == 2``. A relaxation persisted EITHER way must match.
    Regression: live ncdev_bar_rw demo had ``(bar == 0 || bar == 2)``
    in the seed but the splitter stripped parens before the
    whitespace-only comparator could match. Now both sides go through
    the same paren-strip normaliser."""
    pre = "valid(p) && (bar == 0 || bar == 2) && data_count > 0"
    out = drop_clauses(pre, ["(bar == 0 || bar == 2)"])
    assert "bar" not in out
    # Symmetric: a non-parenthesised drop entry against the same PRE.
    out2 = drop_clauses(pre, ["bar == 0 || bar == 2"])
    assert "bar" not in out2


# ---------------------------------------------------------------------------
# Stub emission honours relaxations
# ---------------------------------------------------------------------------


def test_stub_drops_relaxed_clauses_in_bug_hunt_mode():
    spec = _spec_with_overtight_pre()
    relaxations = ["valid(user_va)", "data_count > 0"]
    stub = _generate_stub(
        "ncdev_bar_read",
        spec,
        _ncdev_bar_read_pf(),
        spec_mode="bug-hunt",
        callee_relaxations=relaxations,
    )
    # No assert mentioning user_va or data_count > 0 (the dropped
    # clauses) should remain.
    for line in stub.splitlines():
        if line.lstrip().startswith("assert("):
            assert "user_va" not in line, f"user_va leak: {line}"
            # ``data_count`` may still appear inside valid_range's
            # R_OK term, but the bare ``data_count > 0`` assert
            # must NOT.
            assert "data_count > 0" not in line.replace(" ", ""), (
                f"data_count>0 leak: {line}"
            )
    # The retained validity clauses still fire.
    assert any(
        "assert(" in line and "reg_addresses" in line
        for line in stub.splitlines()
    ), stub


def test_stub_drops_relaxed_clauses_in_functional_mode_too():
    """The relaxation should apply regardless of spec_mode — the
    over-tight clause should never re-appear as an assume either,
    because that would constrain CBMC's exploration in a way that
    masks bugs."""
    spec = _spec_with_overtight_pre()
    stub = _generate_stub(
        "ncdev_bar_read",
        spec,
        _ncdev_bar_read_pf(),
        spec_mode="functional",
        callee_relaxations=["valid(user_va)"],
    )
    # No assume on user_va.
    for line in stub.splitlines():
        if "__CPROVER_assume(" in line:
            assert "user_va" not in line, f"user_va leak in assume: {line}"


def test_stub_no_relaxations_keeps_original_pre():
    """Empty/None relaxations is the first-run case — PRE intact."""
    spec = _spec_with_overtight_pre()
    stub = _generate_stub(
        "ncdev_bar_read",
        spec,
        _ncdev_bar_read_pf(),
        spec_mode="bug-hunt",
        callee_relaxations=[],
    )
    # All four clauses should appear in some assert.
    assert any("user_va" in line for line in stub.splitlines())


# ---------------------------------------------------------------------------
# End-to-end via the persisted store
# ---------------------------------------------------------------------------


def test_relaxation_round_trips_through_store(tmp_path):
    """Persist via Remediation → reload → confirm the stub gen path
    that consults the store would see the drop."""
    store = LearnedConstraintsStore(tmp_path)
    store.record(
        "ncdev_bar_rw",  # FUT that surfaced the FP
        Remediation(
            scope=RemediationScope.CALLEE_SPEC_RELAX,
            clause="valid(user_va)",
            callee="ncdev_bar_read",
        ),
    )
    # Fresh store instance, like a new sweep run would do.
    store2 = LearnedConstraintsStore(tmp_path)
    drops = store2.callee_relaxations("ncdev_bar_read")
    assert drops == ["valid(user_va)"]

    spec = _spec_with_overtight_pre()
    stub = _generate_stub(
        "ncdev_bar_read",
        spec,
        _ncdev_bar_read_pf(),
        spec_mode="bug-hunt",
        callee_relaxations=drops,
    )
    for line in stub.splitlines():
        if line.lstrip().startswith("assert("):
            assert "user_va" not in line
