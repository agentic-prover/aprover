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
