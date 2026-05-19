"""Tests for the spec-evaluation-overflow false-positive filter.

Phase 1 functional specs can overflow during evaluation when the LLM
uses plain ``+``/``-``/``*`` on inputs Kani sets to ``usize::MAX``,
``i64::MIN``, etc. The arithmetic overflow is in the SPEC, not the
function body — these CEXs should be classified as model artifacts,
not real bugs.

The filter lives in ``cex_validator._witness_obvious_artifact`` and
fires when the failing property starts with ``check_`` (the harness
wrapper) AND the trace mentions arithmetic overflow.
"""

from __future__ import annotations

from bmc_agent.cex_validator import _witness_obvious_artifact
from bmc_agent.cbmc import Counterexample


def _cex(prop: str, trace: list[str] | None = None) -> Counterexample:
    return Counterexample(
        failing_property=prop,
        variable_assignments={},
        trace=trace or [],
    )


def test_spec_overflow_add_filtered():
    """``check_align_up.assertion.5`` with "attempt to add with overflow"
    in trace must be flagged as spec-overflow artifact, not a real bug.

    Regression: CCC long_double.rs Phase 1 sweep 2026-05-19 produced
    4 false positives (f64_decompose, make_x87_infinity,
    shift_right_256_with_grs, shifted_limb) — all from the LLM's
    functional spec evaluating arithmetic on Kani's nondet inputs."""
    cex = _cex(
        "check_align_up.assertion.5",
        trace=["property check_align_up.assertion.5: attempt to add with overflow"],
    )
    assert _witness_obvious_artifact(cex) is not None
    assert "spec-evaluation overflow" in _witness_obvious_artifact(cex)


def test_spec_overflow_sub_filtered():
    cex = _cex(
        "check_shifted_limb.assertion.1",
        trace=["attempt to subtract with overflow"],
    )
    assert _witness_obvious_artifact(cex) is not None


def test_spec_overflow_mul_filtered():
    cex = _cex(
        "check_hash.assertion.3",
        trace=["attempt to multiply with overflow"],
    )
    assert _witness_obvious_artifact(cex) is not None


def test_spec_overflow_shift_filtered():
    cex = _cex(
        "check_foo.assertion.8",
        trace=["attempt to shift left with overflow"],
    )
    assert _witness_obvious_artifact(cex) is not None


def test_body_overflow_NOT_filtered():
    """Overflow in the function body (no ``check_`` prefix) is a REAL
    bug, must NOT be filtered. ``<fn>.assertion.N`` (no harness wrapper)
    is the body's own assertion line."""
    cex = _cex(
        "align_up_64.assertion.1",
        trace=["property align_up_64.assertion.1: attempt to add with overflow"],
    )
    # Without ``check_`` prefix, this is body arithmetic — real bug.
    assert _witness_obvious_artifact(cex) is None


def test_spec_postcondition_violation_NOT_filtered():
    """A genuine postcondition violation (functional spec says result
    should be X but body produces Y) lands as ``check_<fn>.assertion.N``
    with "postcondition violated" trace, not arithmetic overflow.
    These ARE real bugs and must NOT be filtered."""
    cex = _cex(
        "check_align_up.assertion.5",
        trace=["property check_align_up.assertion.5: postcondition violated"],
    )
    assert _witness_obvious_artifact(cex) is None


def test_body_overflow_with_check_prefix_NOT_filtered():
    """Edge case: if a body fn happens to be named ``check_*``, its
    body assertions also start with ``check_``. The filter requires
    BOTH ``check_`` prefix AND arithmetic-overflow trace; postcondition
    violations on check_*-named fns are still real."""
    cex = _cex("check_csum.assertion.2", trace=["postcondition violated"])
    assert _witness_obvious_artifact(cex) is None
