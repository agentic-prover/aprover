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
    assert "spec-evaluation" in _witness_obvious_artifact(cex)


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


def test_spec_slice_oob_filtered():
    """Slice OOB inside a ``check_<fn>.assertion`` is also a spec-evaluation
    artifact (the functional spec indexes into a slice with an unguarded
    nondet pos). Regression: CCC encoding.rs decode_pua_byte 2026-05-19 —
    K2's functional spec did ``input[pos]`` without a bounds clause and
    Kani picked ``pos > input.len()``. The function itself was fine.
    """
    cex = _cex(
        "check_decode_pua_byte.assertion.5",
        trace=["property check_decode_pua_byte.assertion.5: index out of bounds: the length is less than or equal to the given index"],
    )
    assert _witness_obvious_artifact(cex) is not None
    assert "spec-evaluation" in _witness_obvious_artifact(cex)


def test_spec_divide_by_zero_filtered():
    cex = _cex(
        "check_calc.assertion.2",
        trace=["attempt to divide by zero"],
    )
    assert _witness_obvious_artifact(cex) is not None


def test_body_slice_oob_NOT_filtered():
    """Slice OOB in the body (no ``check_`` prefix) is the canonical
    structural panic and MUST remain a real bug under security.
    """
    cex = _cex(
        "read_u32.assertion.7",
        trace=["index out of bounds: the length is less than or equal to the given index"],
    )
    assert _witness_obvious_artifact(cex) is None


def test_kani_unsupported_construct_filtered():
    """``unsupported_construct.N`` is Kani's marker for FFI / syscall calls
    it can't symbolically execute (getpid, getenv, file I/O). These are
    verifier limitations, not function bugs. Regression: CCC temp_files
    2026-05-19 — make_temp_path and temp_dir got REAL_BUG verdicts that
    were really 'Kani can't model the syscall'.
    """
    cex = _cex(
        "std::sys::pal::unix::os::getpid.unsupported_construct.1",
        trace=["property std::sys::pal::unix::os::getpid.unsupported_construct.1: call to foreign function"],
    )
    assert _witness_obvious_artifact(cex) is not None
    assert "kani modelling artifact" in _witness_obvious_artifact(cex)


def test_kani_unsupported_construct_in_trace_filtered():
    """The marker may appear in the trace text instead of the property
    name on some Kani versions. Match either."""
    cex = _cex(
        "some.weird.property",
        trace=["unsupported_construct: external symbol call"],
    )
    assert _witness_obvious_artifact(cex) is not None


def test_spec_eval_filter_skipped_when_body_has_same_shape():
    """Regression: 2026-05-20 — 9 elf/io.rs byte-reader bugs and
    write::write_elf64_phdr_at were classified SPURIOUS by the
    spec-eval filter even though their function bodies have the same
    unguarded ``offset + N`` arithmetic that triggers the panic. The
    spec is a symptom; the function is genuinely buggy. When the body
    shows the same overflow shape, the filter must NOT fire and let
    downstream classification take over."""

    class _F:
        body = "fn read_u32(data: &[u8], offset: usize) -> u32 { u32::from_le_bytes([data[offset], data[offset+1], data[offset+2], data[offset+3]]) }"

    cex = _cex(
        "check_read_u32.assertion.1",
        trace=["property check_read_u32.assertion.1: attempt to add with overflow"],
    )
    # With func context (body has the same overflow shape): filter must NOT fire.
    assert _witness_obvious_artifact(cex, _F()) is None
    # Without func context: filter fires as before (backward compatibility).
    assert _witness_obvious_artifact(cex) is not None


def test_spec_eval_filter_still_fires_when_body_has_no_pattern():
    """Negative regression: filter must still fire when the body
    genuinely has no arithmetic / indexing shape -- e.g. a one-line
    wrapper or trivial getter. Otherwise we re-introduce false positives
    on functions where the spec is the only source of the panic."""

    class _F:
        body = "fn id_u32(x: u32) -> u32 { x }"  # no arithmetic, no indexing

    cex = _cex(
        "check_id_u32.assertion.1",
        trace=["property check_id_u32.assertion.1: attempt to add with overflow"],
    )
    # Body has no `+` `-` `*` `[` patterns; filter SHOULD fire (spec is the only source).
    assert _witness_obvious_artifact(cex, _F()) is not None


def test_body_shape_detects_slice_indexing():
    from bmc_agent.cex_validator import _body_has_same_panic_shape
    # Body indexes with arithmetic
    assert _body_has_same_panic_shape(
        "data[offset + 1]", "index out of bounds: ..."
    ) is True
    # Body uses .windows() (also panics on 0)
    assert _body_has_same_panic_shape(
        "bytes.windows(word_len)", "slice_index_fail"
    ) is True
    # Trivial body
    assert _body_has_same_panic_shape(
        "x", "index out of bounds: ..."
    ) is False


def test_body_shape_detects_unguarded_arithmetic():
    from bmc_agent.cex_validator import _body_has_same_panic_shape
    assert _body_has_same_panic_shape(
        "let r = a + b;", "attempt to add with overflow"
    ) is True
    # Wrapping ops only (no bare `+`/`-`/`*`): filter sees no unsafe pattern,
    # so the spec-eval filter SHOULD fire on a body using only safe ops.
    assert _body_has_same_panic_shape(
        "let r = a.wrapping_add(b);", "attempt to add with overflow"
    ) is False
