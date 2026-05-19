"""Tests for the LATENT bug-classification bucket.

LATENT findings are panics reachable via the public API (cargo-fuzz /
future-caller) but with no in-tree call site that produces the CEx state.
Distinct from REAL_BUG (in-tree-reachable) and SPURIOUS (modelling
artifact). See CExOutcome.LATENT in bmc_agent/cex_validator.py.
"""

from __future__ import annotations

from bmc_agent.cex_validator import (
    CExOutcome,
    ValidationResult,
    _is_publicly_callable,
    _is_structural_panic,
)
from bmc_agent.cbmc import Counterexample


def _cex(prop: str, trace: list[str] | None = None) -> Counterexample:
    """Build a minimal Counterexample for tests."""
    return Counterexample(
        failing_property=prop,
        variable_assignments={},
        trace=trace or [],
    )


class _FakeSig:
    """Minimal duck-typed signature stand-in."""
    def __init__(self, *, is_pub=False, is_static=False, has_is_pub=True):
        if has_is_pub:
            self.is_pub = is_pub
        self.is_static = is_static


class _FakeFunc:
    def __init__(self, sig):
        self.signature = sig
        self.name = "fake"


# ------------------------------------------------------------------
# _is_structural_panic
# ------------------------------------------------------------------


def test_structural_panic_detects_slice_oob():
    assert _is_structural_panic("slice_index_fail.assertion.1")
    assert _is_structural_panic("foo", trace=["property foo: index out of bounds"])


def test_structural_panic_detects_arithmetic_overflow():
    assert _is_structural_panic("fn.assertion.1", trace=["attempt to add with overflow"])
    assert _is_structural_panic("x.assertion.1", trace=["attempt to subtract with overflow"])
    assert _is_structural_panic("y.assertion.1", trace=["attempt to multiply with overflow"])


def test_structural_panic_detects_divide_zero():
    assert _is_structural_panic("foo.assertion.1", trace=["attempt to divide by zero"])
    assert _is_structural_panic(
        "foo.assertion.1",
        trace=["attempt to calculate the remainder with a divisor of zero"],
    )


def test_structural_panic_detects_capacity_overflow():
    """raw_vec capacity overflow from Vec::resize / extend etc."""
    assert _is_structural_panic("alloc::raw_vec::capacity_overflow.assertion.1")


def test_structural_panic_rejects_custom_postconditions():
    """User-defined postcondition violations should NOT count as structural
    panics — they're modelling-level (LLM-generated postcond) not pure Rust
    panic sites. Otherwise every assertion failure would be reported as
    latent."""
    assert not _is_structural_panic("main.assertion.1")
    assert not _is_structural_panic("postcondition_violation")
    assert not _is_structural_panic("foo.assertion.5", trace=["assertion failed: x > 0"])


# ------------------------------------------------------------------
# _is_publicly_callable
# ------------------------------------------------------------------


def test_publicly_callable_rust_pub_fn():
    """Rust `pub fn` is publicly callable."""
    fn = _FakeFunc(_FakeSig(is_pub=True))
    assert _is_publicly_callable(fn)


def test_publicly_callable_rust_private_fn():
    """Rust private fn (no `pub`) is not on the public API."""
    fn = _FakeFunc(_FakeSig(is_pub=False))
    assert not _is_publicly_callable(fn)


def test_publicly_callable_c_extern_fn():
    """C non-static fn (external linkage) is publicly callable. The
    signature lacks the `is_pub` attribute, so the helper falls back to
    the C path (not is_static)."""
    fn = _FakeFunc(_FakeSig(is_static=False, has_is_pub=False))
    assert _is_publicly_callable(fn)


def test_publicly_callable_c_static_fn():
    """C static fn has file-scope; not on the public API."""
    fn = _FakeFunc(_FakeSig(is_static=True, has_is_pub=False))
    assert not _is_publicly_callable(fn)


# ------------------------------------------------------------------
# ValidationResult — is_latent_bug property
# ------------------------------------------------------------------


def test_validation_result_latent_outcome_flag():
    cex = _cex("slice_index_fail.assertion.1")
    vr = ValidationResult(
        function_name="bytes_to_str",
        counterexample=cex,
        caller_path=[],
        system_entry_input=None,
        refinement_history=[],
        final_precondition="start <= end && end <= b.len()",
        reasoning="latent",
        outcome=CExOutcome.LATENT,
    )
    assert vr.is_latent_bug is True
    assert vr.is_real_bug is False


def test_validation_result_real_bug_is_not_latent():
    cex = _cex("foo.assertion.1")
    vr = ValidationResult(
        function_name="foo",
        counterexample=cex,
        caller_path=["caller"],
        system_entry_input=None,
        refinement_history=[],
        final_precondition=None,
        reasoning="real",
        outcome=CExOutcome.REAL_BUG,
    )
    assert vr.is_real_bug is True
    assert vr.is_latent_bug is False


def test_validation_result_to_dict_includes_latent_flag():
    cex = _cex("slice_index_fail.assertion.1")
    vr = ValidationResult(
        function_name="f",
        counterexample=cex,
        caller_path=[],
        system_entry_input=None,
        refinement_history=[],
        final_precondition=None,
        reasoning="",
        outcome=CExOutcome.LATENT,
    )
    d = vr.to_dict()
    assert d["outcome"] == "latent"
    assert d["is_latent_bug"] is True
    assert d["is_real_bug"] is False
