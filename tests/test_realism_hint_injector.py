"""Tests for the realism-prompt hint injector (Phase 4b)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bmc_agent.realism_hint_injector import (
    collect_hints,
    persist_hints,
    realism_extra_skepticism,
)


def _write_bug(root: Path, fn: str, variable_assignments: dict, call_chain: list[str] | None = None) -> None:
    """Write a synthetic bug_report.json matching the on-disk layout."""
    d = root / fn
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": "2026-05-23T00:00:00",
        "report": {
            "function_name": fn,
            "violated_property": f"{fn}.pointer_dereference.1",
            "call_chain": call_chain or [],
            "counterexample": {"variable_assignments": variable_assignments},
        },
    }
    (d / "bug_report.json").write_text(json.dumps(payload))


def test_collect_below_threshold_returns_empty_bundle(tmp_path):
    """A single UNINIT_VTABLE finding shouldn't trigger a hint at
    threshold=3 — too noisy."""
    _write_bug(tmp_path, "fn1", {
        "compare_key": "((signed int (*)(struct X *, const void *))NULL)",
    })
    bundle = collect_hints(tmp_path, threshold=3)
    assert bundle.text == ""
    # Counts still populated for telemetry.
    assert bundle.patterns_observed.get("uninit_vtable", 0) == 1


def test_collect_above_threshold_produces_hint(tmp_path):
    """3 UNINIT_VTABLE findings → hint paragraph rendered."""
    fnpat = "((signed int (*)(struct X *, const void *))NULL)"
    for i, fn in enumerate(["a", "b", "c"]):
        field = "compare_key" if i % 2 == 0 else "compare_nodes"
        _write_bug(tmp_path, fn, {field: fnpat}, call_chain=["pub_api", "init_wrap", fn])
    bundle = collect_hints(tmp_path, threshold=3)
    assert bundle.text
    assert "ADDITIONAL SKEPTICISM CONTEXT" in bundle.text
    assert "compare_key" in bundle.text or "compare_nodes" in bundle.text
    # Concrete count makes it audit-friendly.
    assert "3 prior finding" in bundle.text


def test_collect_renders_at_most_three_hints(tmp_path):
    """If both UNINIT_VTABLE and UNINIT_CONTAINER cross threshold,
    both render but the total is capped at 3 hint paragraphs.
    """
    # 3 vtable findings
    for fn in ["v1", "v2", "v3"]:
        _write_bug(tmp_path, fn, {
            "compare_key": "((signed int (*)(struct X *))NULL)",
        })
    # 3 container findings (all nondet/NULL/zero)
    for fn in ["c1", "c2", "c3"]:
        _write_bug(tmp_path, fn, {
            "first": "NULL", "last": "NULL",
            "count": "0u", "size": "0ul", "flags": "0",
        })
    bundle = collect_hints(tmp_path, threshold=3)
    # Each hint paragraph starts with "* " in markdown bullet style.
    assert bundle.text.count("\n* ") <= 3


def test_persist_hints_writes_to_disk(tmp_path):
    """persist_hints writes the expected file."""
    fnpat = "((signed int (*)(struct X *))NULL)"
    for fn in ["a", "b", "c"]:
        _write_bug(tmp_path, fn, {"compare_key": fnpat})
    bundle = collect_hints(tmp_path, threshold=3)
    out_path = persist_hints(bundle, tmp_path, round_idx=0)
    assert out_path.exists()
    text = out_path.read_text()
    assert "autonomous round 1" in text
    assert "ADDITIONAL SKEPTICISM CONTEXT" in text


def test_persist_hints_empty_bundle_writes_marker(tmp_path):
    """Even with no qualifying hints, the file is written with a marker
    so a downstream operator can see the round produced nothing."""
    bundle = collect_hints(tmp_path, threshold=3)  # empty corpus → empty bundle
    out_path = persist_hints(bundle, tmp_path, round_idx=2)
    text = out_path.read_text()
    assert "round 3" in text
    assert "no patterns crossed threshold" in text


def test_realism_extra_skepticism_returns_none_when_empty():
    from bmc_agent.realism_hint_injector import HintBundle
    assert realism_extra_skepticism(HintBundle()) is None


def test_realism_extra_skepticism_returns_text_when_nonempty():
    from bmc_agent.realism_hint_injector import HintBundle
    b = HintBundle(text="hello world")
    assert realism_extra_skepticism(b) == "hello world"
