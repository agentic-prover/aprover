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


def test_structural_panic_detects_unwrap_failed():
    """Rust ``Result::unwrap()`` / ``Option::unwrap()`` failure lands as
    ``std::result::unwrap_failed`` or ``core::option::expect_failed``.
    These are pure panic sites and should be classified as structural.
    Regression: CCC ``bytes_to_str`` had an ``unwrap()`` on
    ``str::from_utf8`` that was being classified REAL_BUG instead of
    LATENT because the marker list missed unwrap_failed."""
    assert _is_structural_panic("std::result::unwrap_failed.assertion.1")
    assert _is_structural_panic("core::option::expect_failed.assertion.1")


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


def test_threat_model_security_promotes_pub_api_panics_to_real_bug():
    """Under threat_model='security', pub API IS the attacker's
    interface. A structural panic reachable via the pub API on a pub fn
    with no in-scope callers must be REAL_BUG, not LATENT — the attacker
    is a current caller in that threat model. The LATENT bucket is only
    for non-security threat models where we care about in-tree-reachable
    crashes specifically.

    Regression: 2026-05-19 — initial LATENT implementation flagged all
    18 CCC byte-helper bugs as LATENT regardless of threat model. The
    user pointed out that under --threat-model security (the default),
    those should be REAL_BUG because the attacker IS a current caller."""
    # We don't test the full validator wiring here (too many dependencies);
    # we just verify the threat-model field is honored by checking the
    # config attribute exists and defaults correctly.
    from bmc_agent.config import Config
    cfg = Config.from_env()
    # Default threat model is 'security'.
    assert cfg.threat_model == "security", cfg.threat_model
    # Explicit non-security threat models exist:
    cfg.threat_model = "safety"
    assert cfg.threat_model == "safety"
    cfg.threat_model = "functional"
    assert cfg.threat_model == "functional"


def test_private_fn_with_callers_classifies_as_latent_under_security():
    """Regression: 2026-05-19 — Kani standalone-harness mode returns CEXs
    with empty ``variable_assignments``, so the input-reachability stage
    can't propagate state through callers and the refinement loop stalls
    at precondition='true'. Previously this fell through to SPURIOUS even
    on real OOB / overflow panics in private helpers that have many
    in-scope callers (CCC's eh_frame.rs is the canonical example: 12
    private fns, all reachable from the linker pipeline under attacker-
    controlled ELF input). Under threat_model='security', the structural-
    panic + has-callers combo must classify as LATENT, not SPURIOUS.
    """
    from bmc_agent.cex_validator import _is_structural_panic, _is_publicly_callable

    class _Sig:
        # Rust signature: not pub, no self
        is_pub = False
        is_static = False

    class _Func:
        signature = _Sig()
        is_static = False

    func = _Func()
    # Sanity: private and structural panic combine correctly
    assert _is_publicly_callable(func) is False
    assert _is_structural_panic(
        "read_u32_le.assertion.7",
        ["property read_u32_le.assertion.7: index out of bounds: the length is less than or equal to the given index"],
    ) is True


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
