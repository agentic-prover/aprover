"""Tests for the CEx dedup window in pipeline._dedup_counterexamples.

The function decides how many CBMC counterexamples per property type are
forwarded to classification + realism check. Earlier behaviour kept exactly
one representative per type — that discarded deeper indices (e.g.
pointer_dereference.43) when an artifact-flavoured CEx (e.g.
pointer_dereference.7) happened to come first. Real bugs behind the
artifact were never inspected. The fix is to keep up to N (default 3).
"""

from bmc_agent.cbmc import Counterexample
from bmc_agent.pipeline import _dedup_counterexamples, DEFAULT_DEDUP_PER_TYPE


def _cex(prop: str) -> Counterexample:
    return Counterexample(failing_property=prop)


def test_default_keeps_up_to_three_per_type():
    cexs = [
        _cex("F.pointer_dereference.7"),
        _cex("F.pointer_dereference.11"),
        _cex("F.pointer_dereference.19"),
        _cex("F.pointer_dereference.43"),  # 4th — should be dropped
    ]
    out = _dedup_counterexamples(cexs)
    assert len(out) == DEFAULT_DEDUP_PER_TYPE == 3
    assert [c.failing_property for c in out] == [
        "F.pointer_dereference.7",
        "F.pointer_dereference.11",
        "F.pointer_dereference.19",
    ]


def test_assertion_kept_in_full():
    # Each assertion index is a distinct spec postcondition; never collapse.
    cexs = [
        _cex("main.assertion.1"),
        _cex("main.assertion.2"),
        _cex("main.assertion.3"),
        _cex("main.assertion.4"),
        _cex("main.assertion.5"),
    ]
    out = _dedup_counterexamples(cexs)
    assert len(out) == 5


def test_max_per_type_one_recovers_old_behaviour():
    cexs = [
        _cex("F.pointer_dereference.7"),
        _cex("F.pointer_dereference.43"),
        _cex("F.overflow.2"),
        _cex("F.overflow.5"),
        _cex("F.unwind.0"),
    ]
    out = _dedup_counterexamples(cexs, max_per_type=1)
    assert [c.failing_property for c in out] == [
        "F.pointer_dereference.7",
        "F.overflow.2",
        "F.unwind.0",
    ]


def test_multiple_property_types_each_get_window():
    # Each property TYPE has its own window of up to N reps.
    cexs = [
        _cex("F.pointer_dereference.7"),
        _cex("F.pointer_dereference.19"),
        _cex("F.pointer_dereference.43"),
        _cex("F.pointer_dereference.50"),  # 4th of type — dropped
        _cex("F.overflow.2"),
        _cex("F.overflow.5"),
    ]
    out = _dedup_counterexamples(cexs, max_per_type=3)
    types = [c.failing_property for c in out]
    assert types == [
        "F.pointer_dereference.7",
        "F.pointer_dereference.19",
        "F.pointer_dereference.43",
        "F.overflow.2",
        "F.overflow.5",
    ]


def test_empty_input():
    assert _dedup_counterexamples([]) == []


def test_property_without_index_treated_as_own_type():
    # Defensive: a property string without a dotted suffix shouldn't crash.
    cexs = [
        _cex("simple_property"),
        _cex("simple_property"),
        _cex("simple_property"),
        _cex("simple_property"),
    ]
    out = _dedup_counterexamples(cexs, max_per_type=2)
    assert len(out) == 2
