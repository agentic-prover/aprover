"""Tests for FP pattern detector (Phase 4 of autonomous mode)."""

from __future__ import annotations

from bmc_agent.fp_pattern_detector import detect_pattern, FpPattern


def _bug(state: dict, call_chain: list[str] | None = None) -> dict:
    """Build a bug_report dict with the canonical on-disk layout."""
    return {
        "report": {
            "function_name": "f",
            "violated_property": "f.pointer_dereference.1",
            "call_chain": call_chain or [],
            "counterexample": {"variable_assignments": state},
        }
    }


def test_detect_uninit_vtable_with_fn_pointer_type_sig():
    """The canonical libarchive case: ``compare_key = ((signed int (*)(struct X *, const void *))NULL)``."""
    ev = detect_pattern(_bug({
        "rbt": "_rbt_obj!0@1",
        "compare_key": "((signed int (*)(struct archive_rb_node *, const void *))NULL)",
        "key": "NULL",
    }))
    assert ev.pattern == FpPattern.UNINIT_VTABLE
    assert "compare_key" in ev.cited_fields
    assert ev.confidence >= 0.7


def test_detect_uninit_vtable_with_two_fn_pointers_bumps_confidence():
    ev = detect_pattern(_bug({
        "compare_key": "((signed int (*)(struct X *, const void *))NULL)",
        "compare_nodes": "((signed int (*)(struct X *, struct X *))NULL)",
    }))
    assert ev.pattern == FpPattern.UNINIT_VTABLE
    assert ev.confidence >= 0.9


def test_detect_uninit_vtable_via_field_name_heuristic():
    """Bare NULL on a field whose name strongly suggests a fn pointer."""
    ev = detect_pattern(_bug({
        "release_fn": "NULL",
        "data": "_data_buf!0@1",
    }))
    assert ev.pattern == FpPattern.UNINIT_VTABLE
    assert "release_fn" in ev.cited_fields


def test_detect_uninit_container_all_nondet():
    """Every user field nondet/zero — fires UNINIT_CONTAINER."""
    ev = detect_pattern(_bug({
        "first": "NULL",
        "last": "NULL",
        "count": "0u",
        "size": "0ul",
        "flags": "0",
    }))
    assert ev.pattern in (FpPattern.UNINIT_CONTAINER, FpPattern.UNINIT_VTABLE)


def test_detect_no_pattern_when_state_looks_meaningful():
    """Mix of populated buffers + sentinel values shouldn't fire FP."""
    ev = detect_pattern(_bug({
        "_buf": "<array: 5 elements>",
        "_buf[0]": "0x41",
        "_buf[1]": "0x42",
        "len": "5u",
        "ptr": "_buf!0@1",
    }))
    assert ev.pattern == FpPattern.NO_PATTERN


def test_detect_handles_outer_inner_shape():
    """The bug_report.json on disk has shape {saved_at, report: {…}}.
    The detector should accept both that and the inner-only shape.
    """
    inner = {
        "function_name": "f",
        "counterexample": {
            "variable_assignments": {
                "compare_key": "((signed int (*)(struct X *, const void *))NULL)"
            },
        },
    }
    outer = {"saved_at": "2026-05-23T00:00:00", "report": inner}
    assert detect_pattern(inner).pattern == FpPattern.UNINIT_VTABLE
    assert detect_pattern(outer).pattern == FpPattern.UNINIT_VTABLE


def test_detect_uses_classification_fallback_when_bug_has_no_cex():
    """If bug_report.counterexample is empty, fall back to
    classification.classification.counterexample.variable_assignments."""
    bug = {"report": {"function_name": "f", "counterexample": {}}}
    classification = {
        "classification": {
            "counterexample": {
                "variable_assignments": {
                    "release_fn": "NULL",
                }
            }
        }
    }
    ev = detect_pattern(bug, classification)
    assert ev.pattern == FpPattern.UNINIT_VTABLE


def test_call_chain_propagates_to_evidence():
    ev = detect_pattern(_bug(
        {"compare_key": "((signed int (*)(...))NULL)"},
        call_chain=["pub_api", "internal_helper", "f"],
    ))
    assert ev.cited_functions == ["pub_api", "internal_helper", "f"]
