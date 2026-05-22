"""Tests for the unsigned-return kernel-API contract.

Regression: ``_kernel_api_return_contract`` originally emitted
``__CPROVER_assume(result <= 0 && result >= -4095);`` for every matched
name, which is UNSATISFIABLE for unsigned return types (literal -4095
wraps to ULONG_MAX-4094 in unsigned, then ``result <= 0`` is only
``result == 0``, but ``result >= ULONG_MAX-4094`` is the disjoint upper
band). CBMC sees the assumption as ``false`` and silently prunes every
caller path through the stub — masking real downstream bugs.

The fix:
1. Suffix-match for project-local wrappers like ``neuron_copy_from_user``.
2. For unsigned returns (``unsigned long`` / ``size_t``), emit a
   signed-cast form so the negative-ERRNO range is interpretable
   regardless of declared signedness.
"""

from __future__ import annotations

from bmc_agent.harness_generator import _kernel_api_return_contract


def test_signed_int_constraint_unchanged():
    """The historical form ``result <= 0 && result >= -4095`` must
    remain for plain int returns — used by the entire kernel-API set."""
    out = _kernel_api_return_contract("copy_from_user", "int")
    assert out == ["__CPROVER_assume(result <= 0 && result >= -4095);"]


def test_unsigned_long_constraint_avoids_unsat_form():
    """For ``unsigned long`` returns the emitted constraint must NOT be
    the bare ``result <= 0 && result >= -4095`` (unsatisfiable). Use the
    signed-cast form."""
    out = _kernel_api_return_contract("copy_from_user", "unsigned long")
    assert len(out) == 1
    body = out[0]
    assert "result == 0" in body
    assert "(signed long)result" in body
    # Sanity: doesn't accidentally include the buggy literal pattern.
    assert "result <= 0 &&" not in body or "(signed long)" in body


def test_size_t_constraint_uses_signed_cast_form():
    out = _kernel_api_return_contract("copy_to_user", "size_t")
    assert len(out) == 1
    assert "(signed long)result" in out[0]


# ---------------------------------------------------------------------------
# Suffix-matching for project-local wrappers
# ---------------------------------------------------------------------------


def test_suffix_match_catches_project_wrapper():
    """``neuron_copy_from_user`` is a thin wrapper around
    ``copy_from_user``; suffix-matching should give it the same
    contract."""
    out = _kernel_api_return_contract("neuron_copy_from_user", "unsigned long")
    assert out, "wrapper must inherit contract"
    assert "(signed long)result" in out[0]


def test_suffix_match_requires_underscore_boundary():
    """``mycopy_to_user`` (no preceding underscore on the wrapper) must
    NOT match — its semantics are unknown."""
    out = _kernel_api_return_contract("mycopy_to_user", "unsigned long")
    assert out == []


def test_unknown_name_returns_empty():
    out = _kernel_api_return_contract("totally_unrelated_fn", "int")
    assert out == []


def test_non_int_non_unsigned_return_returns_empty():
    """Pointer-returning callees aren't governed by this contract;
    they have their own allocator-family contracts elsewhere."""
    out = _kernel_api_return_contract("copy_from_user", "void *")
    assert out == []
