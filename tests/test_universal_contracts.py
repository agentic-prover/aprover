"""Tests for lite-mode universal-contract precondition synthesis."""

from __future__ import annotations

from bmc_agent.universal_contracts import (
    derive_universal_precondition,
    derive_contract_summary,
)
from bmc_agent.parser import FunctionInfo, FunctionSignature
from bmc_agent.spec_generator import _permissive_spec


def _fn(params: list[tuple[str, str]], name: str = "f", ret: str = "int") -> FunctionInfo:
    sig = FunctionSignature(name=name, return_type=ret, parameters=params)
    return FunctionInfo(
        name=name, signature=sig, body="", callees=set(), source_file="x.c",
    )


def test_paired_pointers_start_end_emits_ordering():
    fn = _fn([("const char *", "start"), ("const char *", "end"), ("int *", "result")])
    assert derive_universal_precondition(fn) == "start <= end"


def test_paired_pointers_src_dst():
    fn = _fn([("const char *", "src"), ("char *", "dst")])
    assert derive_universal_precondition(fn) == "src <= dst"


def test_paired_pointers_first_last():
    fn = _fn([("uint8_t *", "first"), ("uint8_t *", "last")])
    assert derive_universal_precondition(fn) == "first <= last"


def test_only_pointer_types_are_paired():
    """Non-pointer params named ``start`` / ``end`` don't trigger the pair —
    the pattern is specifically about pointer-pair caller-contract slips."""
    fn = _fn([("size_t", "start"), ("size_t", "end")])
    assert derive_universal_precondition(fn) == "true"


def test_no_paired_pattern_returns_true():
    fn = _fn([("int", "x"), ("int", "y")])
    assert derive_universal_precondition(fn) == "true"


def test_multiple_pairs_join_with_and():
    """A function with both start/end AND src/dst gets both clauses."""
    fn = _fn([
        ("const char *", "start"),
        ("const char *", "end"),
        ("const char *", "src"),
        ("char *", "dst"),
    ])
    out = derive_universal_precondition(fn)
    assert "start <= end" in out
    assert "src <= dst" in out
    assert " && " in out


def test_each_pair_emitted_once_even_if_multiple_aliases_match():
    """``end`` is in two pair tables (start/end AND begin/end). Both
    matches shouldn't double-emit if start and begin aren't both present."""
    fn = _fn([("const char *", "start"), ("const char *", "end")])
    out = derive_universal_precondition(fn)
    # One clause only.
    assert out.count("<=") == 1


def test_empty_signature_returns_true():
    fn = _fn([], ret="void")
    assert derive_universal_precondition(fn) == "true"


def test_void_param_doesnt_crash():
    """``void`` lone-param functions (C's ``f(void)`` idiom) should be
    handled gracefully."""
    fn = _fn([("void", "")])
    assert derive_universal_precondition(fn) == "true"


def test_summary_returns_paired_clauses():
    fn = _fn([("const char *", "start"), ("const char *", "end")])
    summary = derive_contract_summary(fn)
    assert "start <= end" in summary["paired_pointers"]


# ---------------------------------------------------------------------------
# Integration with _permissive_spec
# ---------------------------------------------------------------------------


def test_permissive_spec_without_contracts_returns_true():
    """Default (backwards-compat) behaviour: no contracts, pre=true."""
    spec = _permissive_spec("f")
    assert spec.precondition == "true"
    assert spec.postcondition == "true"


def test_permissive_spec_with_contracts_injects_paired_clause():
    """When ``with_contracts=True`` and func_info has a paired-pointer
    signature, the spec's precondition reflects it."""
    fn = _fn([("const char *", "start"), ("const char *", "end"), ("int *", "result")])
    spec = _permissive_spec("f", func_info=fn, with_contracts=True)
    assert spec.precondition == "start <= end"


def test_permissive_spec_with_contracts_but_no_pair_stays_true():
    fn = _fn([("int", "x")])
    spec = _permissive_spec("f", func_info=fn, with_contracts=True)
    assert spec.precondition == "true"


def test_permissive_spec_with_contracts_no_func_info_stays_true():
    """``with_contracts=True`` but ``func_info=None`` — graceful fallback."""
    spec = _permissive_spec("f", with_contracts=True)
    assert spec.precondition == "true"


# ---------------------------------------------------------------------------
# Pattern 2 — length-bound for (buf, len) pairs
# ---------------------------------------------------------------------------


def test_buf_len_pair_emits_length_bound():
    fn = _fn([("const char *", "buf"), ("size_t", "len")])
    out = derive_universal_precondition(fn, cbmc_unwind=4)
    assert "len <= 4" in out


def test_data_size_pair_emits_length_bound():
    fn = _fn([("const uint8_t *", "data"), ("size_t", "size")])
    out = derive_universal_precondition(fn, cbmc_unwind=8)
    assert "size <= 8" in out


def test_p_n_pair_emits_length_bound():
    fn = _fn([("const char *", "p"), ("int", "n")])
    out = derive_universal_precondition(fn, cbmc_unwind=4)
    assert "n <= 4" in out


def test_buf_without_len_param_no_clause():
    """``buf`` without a partner len-param doesn't emit anything."""
    fn = _fn([("char *", "buf"), ("int", "flags")])
    out = derive_universal_precondition(fn, cbmc_unwind=4)
    assert "<=" not in out


def test_len_param_with_non_pointer_buf_no_clause():
    """A ``len`` param without a matching pointer-typed buf-param
    doesn't fire (we only emit when both names are present)."""
    fn = _fn([("int", "x"), ("size_t", "len")])
    out = derive_universal_precondition(fn)
    assert "<=" not in out


# ---------------------------------------------------------------------------
# Pattern 3 — ops/vtable non-null
# ---------------------------------------------------------------------------


def test_ops_field_non_null():
    """When a struct param has an ``ops`` pointer field, the precondition
    asserts the field is non-NULL."""
    fn = _fn([("struct rb_tree *", "rbt")])
    structs = {"rb_tree": [("struct rb_tree_ops *", "ops"), ("void *", "root")]}
    out = derive_universal_precondition(fn, struct_definitions=structs)
    assert "rbt->ops != NULL" in out


def test_ops_field_with_inner_function_pointers():
    """When the ops struct's body is also visible, every function-pointer
    field gets a non-NULL clause."""
    fn = _fn([("struct rb_tree *", "rbt")])
    structs = {
        "rb_tree": [("struct rb_tree_ops *", "ops"), ("void *", "root")],
        "rb_tree_ops": [
            ("int (*)(void *, void *)", "compare"),
            ("void (*)(void *)", "free"),
        ],
    }
    out = derive_universal_precondition(fn, struct_definitions=structs)
    assert "rbt->ops != NULL" in out
    assert "rbt->ops->compare != NULL" in out
    assert "rbt->ops->free != NULL" in out


def test_ops_field_typedef_function_pointer_detected():
    """Function-pointer fields declared via typedef (``rb_compare_fn``)
    are still detected by the ``_fn``/``_cb``/``_callback`` suffix."""
    fn = _fn([("struct rb_tree *", "rbt")])
    structs = {
        "rb_tree": [("struct rb_tree_ops *", "ops")],
        "rb_tree_ops": [("rb_compare_fn", "compare"), ("int", "version")],
    }
    out = derive_universal_precondition(fn, struct_definitions=structs)
    assert "rbt->ops->compare != NULL" in out


def test_no_ops_field_no_clause():
    """Structs without an ops/vtable field don't fire."""
    fn = _fn([("struct point *", "p")])
    structs = {"point": [("int", "x"), ("int", "y")]}
    out = derive_universal_precondition(fn, struct_definitions=structs)
    assert "ops" not in out
    assert out == "true"


# ---------------------------------------------------------------------------
# Pattern 4 — magic-field non-zero
# ---------------------------------------------------------------------------


def test_magic_field_non_zero():
    fn = _fn([("struct handle *", "h")])
    structs = {"handle": [("int", "magic"), ("void *", "data")]}
    out = derive_universal_precondition(fn, struct_definitions=structs)
    assert "h->magic != 0" in out


def test_sentinel_field_non_zero():
    fn = _fn([("struct handle *", "h")])
    structs = {"handle": [("uint32_t", "sentinel")]}
    out = derive_universal_precondition(fn, struct_definitions=structs)
    assert "h->sentinel != 0" in out


def test_magic_pointer_field_not_treated_as_int_magic():
    """A pointer-typed field named 'magic' is unusual; we only emit the
    non-zero clause for integer-typed magic fields to avoid false
    constraints."""
    fn = _fn([("struct handle *", "h")])
    structs = {"handle": [("void *", "magic")]}
    out = derive_universal_precondition(fn, struct_definitions=structs)
    assert "magic" not in out


# ---------------------------------------------------------------------------
# Multi-pattern: all four fire on a complex signature
# ---------------------------------------------------------------------------


def test_all_patterns_compose():
    """A function that hits all four pattern classes emits all clauses
    joined with ``&&``."""
    fn = _fn([
        ("const char *", "start"),
        ("const char *", "end"),
        ("const char *", "buf"),
        ("size_t", "len"),
        ("struct rb_tree *", "rbt"),
    ])
    structs = {
        "rb_tree": [
            ("struct rb_tree_ops *", "ops"),
            ("int", "magic"),
        ],
        "rb_tree_ops": [("int (*)(void *, void *)", "compare")],
    }
    out = derive_universal_precondition(fn, struct_definitions=structs, cbmc_unwind=4)
    assert "start <= end" in out
    assert "len <= 4" in out
    assert "rbt->ops != NULL" in out
    assert "rbt->ops->compare != NULL" in out
    assert "rbt->magic != 0" in out
    assert " && " in out
