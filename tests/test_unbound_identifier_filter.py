"""Tests for the unbound-identifier filter in dsl_to_cbmc.

When the LLM emits a spec atom that references a name not in the
harness's lexical scope (function-body local, loop-quantifier
variable, undefined kernel constant), the translator drops the atom
to a comment so the harness still compiles.
"""

from __future__ import annotations

from bmc_agent.dsl_to_cbmc import precond_to_assume


def _bodies(stmts: list[str]) -> str:
    """Concatenate the assume/assert bodies for substring checks."""
    return "\n".join(stmts)


def test_unbound_ident_dropped_to_comment():
    """LLM-emitted clause referencing a function-body local that isn't
    a parameter — must be dropped, not asserted."""
    out = precond_to_assume("valid(buffer)", params=["nd", "param"])
    body = _bodies(out)
    assert "dropped" in body
    assert "buffer" in body
    # No live assume on buffer.
    assert "__CPROVER_assume(buffer" not in body


def test_parameter_identifiers_pass_through():
    """Identifiers that ARE in the parameter list translate normally."""
    out = precond_to_assume("valid(nd)", params=["nd", "param"])
    body = _bodies(out)
    assert "__CPROVER_assume(nd != NULL)" in body
    assert "dropped" not in body


def test_struct_field_access_does_not_trip_filter():
    """``nd->bar0_size`` should NOT be flagged — ``nd`` is a parameter
    and ``bar0_size`` is a field name after ``->`` (not a top-level
    binding). Field name chosen to avoid the pre-existing
    invented-field detector which fires on ``->\\w*_index`` and similar
    'snapshot'-shaped suffixes."""
    out = precond_to_assume("nd->bar0_size > 0", params=["nd"])
    body = _bodies(out)
    assert "__CPROVER_assume(nd->bar0_size > 0)" in body
    assert "dropped" not in body


def test_chained_struct_field_access_passes():
    """Deeper chains like ``nd->npdev.bar0_size``."""
    out = precond_to_assume("nd->npdev.bar0_size > 0", params=["nd"])
    body = _bodies(out)
    assert "__CPROVER_assume(nd->npdev.bar0_size > 0)" in body
    assert "dropped" not in body


def test_kernel_macro_passes_via_always_bound():
    """``PAGE_SIZE``, ``EFAULT``, etc. are defined in the harness
    preamble and always considered in-scope."""
    out = precond_to_assume("n <= PAGE_SIZE", params=["n"])
    body = _bodies(out)
    assert "n <= PAGE_SIZE" in body
    assert "dropped" not in body


def test_loop_quantifier_leak_dropped():
    """``forall i: ...`` should be sanitized away earlier, but if a bare
    ``i`` leaks (LLM emits ``i >= 0`` without the forall wrapper), the
    filter catches it."""
    out = precond_to_assume("i >= 0", params=["nd", "param"])
    body = _bodies(out)
    assert "dropped" in body


# test_assert_wrapper_does_not_trip_filter: removed alongside
# precond_to_assert (which the bug-hunt-mode deletion took with it).
# The equivalent property for postcond_to_assert is covered by the
# main path's own integration tests; no separate filter test needed.


def test_filter_preserves_postcondition_result_var():
    """``result`` is always bound (the harness's local return-value
    variable)."""
    from bmc_agent.dsl_to_cbmc import postcond_to_assert
    out = postcond_to_assert("result == 0 || result < 0", params=["nd"])
    body = _bodies(out)
    assert "result == 0" in body
    assert "result < 0" in body
    assert "dropped" not in body


def test_filter_handles_cast_argument():
    """A C cast like ``(struct ncdev*)p`` inside ``valid()`` should
    still match — the cast token shouldn't be flagged."""
    out = precond_to_assume(
        "valid((struct ncdev*)filep->private_data)", params=["filep"]
    )
    body = _bodies(out)
    # The translated atom keeps the cast intact.
    assert "(struct ncdev*)filep->private_data != NULL" in body
    assert "dropped" not in body
