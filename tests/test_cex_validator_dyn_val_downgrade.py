"""
Tests for the REAL_BUG → UNRESOLVED downgrade applied INSIDE
``_try_dynamic_validation`` when dyn-val explicitly comes up clean
on a crash-class property.

Why this exists: the cex_validator's caller-chain walk classifies as
REAL_BUG whenever ``system_entry_reached=True``, BEFORE dyn-val runs.
The earlier pipeline-level realism-skip short-circuit only marks the
realism field, not the classifier outcome — so without this downgrade,
the persisted ``outcome=real_bug`` stands even when dyn-val ran the
same input under real libc and finished cleanly (no crash). That's the
``append_id_w.pointer_dereference.11`` FP surfaced in the postfix7 sweep
on archive_acl.c, 2026-05-27.

The downgrade is sound: a CBMC crash-class property (pointer-deref,
bounds, double-free, recursion / unwind, etc.) describes a fault that
WOULD SIGFAULT at runtime. When dyn-val executes the public-API
reproducer and finishes cleanly in bounded time, the CBMC witness is
a verification-model artifact (unconstrained allocator returns, only-
symbolic aliasing, …) — not a real bug.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_validator(dynamic_validator):
    from bmc_agent.cex_validator import CExValidator
    v = object.__new__(CExValidator)
    v._dynamic_validator = dynamic_validator
    v._reach_errored = False
    v._feas_errored = False
    return v


def _make_validation_result(*, outcome, failing_property, reproducer="real source"):
    from bmc_agent.cex_validator import CExOutcome
    cex = SimpleNamespace(failing_property=failing_property)
    vr = SimpleNamespace(
        system_entry_input=reproducer,
        caller_path=["entry_fn", "fn"],
        counterexample=cex,
        dynamic_result=None,
        outcome=outcome,
        reasoning="initial classifier reasoning",
    )
    return vr


def _make_func(name):
    return SimpleNamespace(name=name)


def _make_parsed(entry):
    pf = SimpleNamespace()
    pf.get_function_info = MagicMock(return_value=entry)
    return pf


def _make_dyn_result(outcome_str):
    """Build a DynamicValidationResult stand-in. The downgrade only
    reads ``.outcome``."""
    from bmc_agent.dynamic_validator import DynamicOutcome
    return SimpleNamespace(outcome=DynamicOutcome(outcome_str))


# ---------------------------------------------------------------------------
# Positive cases — downgrade SHOULD fire
# ---------------------------------------------------------------------------

def test_downgrades_real_bug_on_pointer_dereference_not_triggered():
    """The canonical case: pointer_dereference (crash-class), dyn-val
    NOT_TRIGGERED. The classifier walked the caller chain to system
    entry and stamped REAL_BUG; dyn-val then says "ran clean."
    Outcome must be downgraded to UNRESOLVED."""
    from bmc_agent.cex_validator import CExOutcome
    dyn = MagicMock()
    dyn.validate.return_value = _make_dyn_result("not_triggered")
    v = _make_validator(dyn)
    entry = _make_func("entry_fn")
    vr = _make_validation_result(
        outcome=CExOutcome.REAL_BUG,
        failing_property="append_id_w.pointer_dereference.11",
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed(entry))
    assert vr.outcome == CExOutcome.UNRESOLVED
    # Reasoning is amended to record why
    assert "Downgraded" in vr.reasoning
    assert "NOT_TRIGGERED" in vr.reasoning


def test_downgrades_real_bug_on_bounds_not_triggered():
    """``bounds`` is crash-class — same downgrade applies."""
    from bmc_agent.cex_validator import CExOutcome
    dyn = MagicMock()
    dyn.validate.return_value = _make_dyn_result("not_triggered")
    v = _make_validator(dyn)
    entry = _make_func("entry_fn")
    vr = _make_validation_result(
        outcome=CExOutcome.REAL_BUG,
        failing_property="parse.bounds.4",
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed(entry))
    assert vr.outcome == CExOutcome.UNRESOLVED


def test_downgrades_real_bug_on_recursion_not_triggered():
    """``recursion`` was added to crash-class in 0b4e4a8 — bound-class
    failures also get downgraded when dyn-val comes up clean."""
    from bmc_agent.cex_validator import CExOutcome
    dyn = MagicMock()
    dyn.validate.return_value = _make_dyn_result("not_triggered")
    v = _make_validator(dyn)
    entry = _make_func("entry_fn")
    vr = _make_validation_result(
        outcome=CExOutcome.REAL_BUG,
        failing_property="append_id_w.recursion",
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed(entry))
    assert vr.outcome == CExOutcome.UNRESOLVED


# ---------------------------------------------------------------------------
# Negative cases — downgrade must NOT fire
# ---------------------------------------------------------------------------

def test_no_downgrade_when_outcome_is_not_real_bug():
    """The downgrade only fires when starting from REAL_BUG. An
    already-UNRESOLVED outcome stays UNRESOLVED (the test should pass
    irrespective of dyn-val result)."""
    from bmc_agent.cex_validator import CExOutcome
    dyn = MagicMock()
    dyn.validate.return_value = _make_dyn_result("not_triggered")
    v = _make_validator(dyn)
    entry = _make_func("entry_fn")
    vr = _make_validation_result(
        outcome=CExOutcome.UNRESOLVED,
        failing_property="f.pointer_dereference.0",
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed(entry))
    assert vr.outcome == CExOutcome.UNRESOLVED  # no surprise change


def test_no_downgrade_when_dyn_val_confirmed():
    """``CONFIRMED`` is the positive evidence — REAL_BUG stands."""
    from bmc_agent.cex_validator import CExOutcome
    dyn = MagicMock()
    dyn.validate.return_value = _make_dyn_result("confirmed")
    v = _make_validator(dyn)
    entry = _make_func("entry_fn")
    vr = _make_validation_result(
        outcome=CExOutcome.REAL_BUG,
        failing_property="f.pointer_dereference.1",
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed(entry))
    assert vr.outcome == CExOutcome.REAL_BUG


def test_no_downgrade_when_dyn_val_inconclusive():
    """``INCONCLUSIVE`` means the harness didn't compile / didn't run
    / didn't produce a usable signal. The classifier's prior verdict
    stands (no evidence to overturn it)."""
    from bmc_agent.cex_validator import CExOutcome
    dyn = MagicMock()
    dyn.validate.return_value = _make_dyn_result("inconclusive")
    v = _make_validator(dyn)
    entry = _make_func("entry_fn")
    vr = _make_validation_result(
        outcome=CExOutcome.REAL_BUG,
        failing_property="f.pointer_dereference.1",
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed(entry))
    assert vr.outcome == CExOutcome.REAL_BUG


def test_no_downgrade_on_silent_ub_property_even_when_not_triggered():
    """Silent-UB classes (overflow, conversion, pointer_arithmetic) do
    NOT manifest as runtime crashes without instrumentation. A
    NOT_TRIGGERED on these is uninformative — the runtime wraps
    silently. Must NOT downgrade or we erase real bugs (the May-7
    VibeOS malloc.overflow.1 regression)."""
    from bmc_agent.cex_validator import CExOutcome
    dyn = MagicMock()
    dyn.validate.return_value = _make_dyn_result("not_triggered")
    v = _make_validator(dyn)
    entry = _make_func("entry_fn")
    vr = _make_validation_result(
        outcome=CExOutcome.REAL_BUG,
        failing_property="malloc.overflow.1",
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed(entry))
    assert vr.outcome == CExOutcome.REAL_BUG


def test_no_downgrade_when_dyn_val_not_run_at_all():
    """When dyn-val isn't enabled or the configuration disabled it,
    ``dynamic_result`` ends up as a SKIPPED outcome. Must not
    downgrade — there's no contradictory evidence."""
    from bmc_agent.cex_validator import CExOutcome
    dyn = MagicMock()
    dyn.validate.return_value = _make_dyn_result("skipped")
    v = _make_validator(dyn)
    entry = _make_func("entry_fn")
    vr = _make_validation_result(
        outcome=CExOutcome.REAL_BUG,
        failing_property="f.pointer_dereference.1",
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed(entry))
    assert vr.outcome == CExOutcome.REAL_BUG
