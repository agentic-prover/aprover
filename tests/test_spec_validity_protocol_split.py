"""Tests for the validity / protocol PRE split in bmc_agent.spec.

Covers:
- Round-trip ``to_dict`` / ``from_dict`` with the new fields.
- Back-compat: old JSON without ``pre_validity`` / ``pre_protocol``.
- ``Spec.split_precondition`` falls back to the classifier when the
  structured fields are empty, and honours them when populated.
- ``classify_precondition`` correctly buckets the clause families
  inventoried from the 2026-05-22 Neuron P2 sweep.
"""

from bmc_agent.spec import (
    Spec,
    SpecStatus,
    classify_precondition,
)


def test_spec_roundtrip_with_split_fields():
    s = Spec(
        function_name="ncdev_bar_read",
        precondition="valid(nd) && data_count <= 4",
        postcondition="ensures result == 0",
        pre_validity="valid(nd)",
        pre_protocol="data_count <= 4",
    )
    d = s.to_dict()
    assert d["pre_validity"] == "valid(nd)"
    assert d["pre_protocol"] == "data_count <= 4"
    s2 = Spec.from_dict(d)
    assert s2.pre_validity == "valid(nd)"
    assert s2.pre_protocol == "data_count <= 4"
    assert s2.precondition == s.precondition


def test_spec_from_dict_backwards_compat_missing_split_fields():
    """Old serialised specs lack pre_validity / pre_protocol — must
    still load and default the fields to ``""``."""
    legacy = {
        "function_name": "f",
        "precondition": "valid(p) && p->state == READY",
        "postcondition": "true",
        "callee_specs": {},
        "loop_invariants": [],
        "status": "generated",
        "spec_disagreement": False,
    }
    s = Spec.from_dict(legacy)
    assert s.pre_validity == ""
    assert s.pre_protocol == ""
    assert s.precondition == "valid(p) && p->state == READY"
    assert s.status == SpecStatus.GENERATED


def test_split_precondition_uses_structured_fields_when_present():
    s = Spec(
        function_name="f",
        precondition="ignored when split is set",
        postcondition="true",
        pre_validity="valid(p)",
        pre_protocol="locked(&p->lock)",
    )
    v, p = s.split_precondition()
    assert v == "valid(p)"
    assert p == "locked(&p->lock)"


def test_split_precondition_falls_back_to_classifier():
    """No structured fields → classify the flat ``precondition``."""
    s = Spec(
        function_name="f",
        precondition="valid(p) && locked(&p->lock)",
        postcondition="true",
    )
    v, prot = s.split_precondition()
    assert "valid(p)" in v
    assert "locked(&p->lock)" in prot


def test_split_precondition_empty_flat():
    s = Spec(function_name="f", precondition="", postcondition="true")
    assert s.split_precondition() == ("", "")


def test_split_precondition_trivially_true():
    s = Spec(function_name="f", precondition="true", postcondition="true")
    assert s.split_precondition() == ("", "")


# ---------------------------------------------------------------------------
# Clause classifier — validity bucket
# ---------------------------------------------------------------------------


def test_classify_valid_predicate_is_validity():
    v, p = classify_precondition("valid(nd)")
    assert v == "valid(nd)"
    assert p == ""


def test_classify_valid_range_is_validity():
    v, p = classify_precondition("valid_range(reg_addresses, 0, data_count)")
    assert v == "valid_range(reg_addresses, 0, data_count)"
    assert p == ""


def test_classify_owns_is_validity():
    v, p = classify_precondition("owns(ctx, buf)")
    assert v == "owns(ctx, buf)"
    assert p == ""


def test_classify_in_bounds_is_validity():
    v, p = classify_precondition("in_bounds(arr, i)")
    assert v == "in_bounds(arr, i)"
    assert p == ""


def test_classify_not_null_is_validity():
    v, p = classify_precondition("!null(param)")
    assert "!null(param)" in v
    assert p == ""


def test_classify_no_overflow_is_validity():
    v, p = classify_precondition("no_overflow(start_addr + pool_size)")
    assert "no_overflow" in v
    assert p == ""


def test_classify_bare_numeric_compare_is_validity_default():
    v, p = classify_precondition("size <= 128")
    assert v == "size <= 128"
    assert p == ""


# ---------------------------------------------------------------------------
# Clause classifier — protocol bucket
# ---------------------------------------------------------------------------


def test_classify_locked_is_protocol():
    v, p = classify_precondition("locked(&mc->mpset->lock)")
    assert v == ""
    assert p == "locked(&mc->mpset->lock)"


def test_classify_npid_is_attached_is_protocol():
    v, p = classify_precondition("npid_is_attached(nd)")
    assert v == ""
    assert p == "npid_is_attached(nd)"


def test_classify_state_field_compare_is_protocol():
    v, p = classify_precondition("p->state == READY")
    assert v == ""
    assert "state" in p


def test_classify_refcount_compare_is_protocol():
    v, p = classify_precondition("obj->ref_count > 0")
    assert v == ""
    assert "ref_count" in p


# ---------------------------------------------------------------------------
# Clause classifier — mixed
# ---------------------------------------------------------------------------


def test_classify_mixed_clauses_split_correctly():
    pre = (
        "valid(nd) && valid_range(reg_addresses, 0, data_count) && "
        "locked(&mc->mpset->lock) && npid_is_attached(nd)"
    )
    v, p = classify_precondition(pre)
    # Validity clauses are present in v, NOT in p.
    assert "valid(nd)" in v
    assert "valid_range(reg_addresses, 0, data_count)" in v
    assert "locked(&mc->mpset->lock)" not in v
    # Protocol clauses are present in p, NOT in v.
    assert "locked(&mc->mpset->lock)" in p
    assert "npid_is_attached(nd)" in p
    assert "valid(nd)" not in p


def test_classify_strips_requires_keyword():
    v, p = classify_precondition("requires valid(p) && locked(l)")
    assert "valid(p)" in v
    assert "locked(l)" in p


def test_classify_ncdev_bar_read_caller_contract():
    """The motivating case: ``valid_range(reg_addresses, 0, data_count)``
    is a caller obligation. Classifier must put it in validity so the
    bug-hunt mode asserts it at every call site."""
    v, p = classify_precondition(
        "valid_range(reg_addresses, 0, data_count) && data_count <= 4"
    )
    assert "valid_range(reg_addresses, 0, data_count)" in v
    # ``data_count <= 4`` is a bare comparison — defaults to validity.
    assert "data_count <= 4" in v
    assert p == ""
