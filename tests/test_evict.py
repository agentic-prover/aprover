"""Shared TTL + LRU evictor (``web._evict.evict``).

Covers the two subtle invariants that back both ``SessionStore`` and
``JobStore``: a non-evictable (still-running) entry survives both the TTL stage
and the LRU trim, and the LRU stage otherwise drops the oldest entries.
"""
from __future__ import annotations

from web._evict import evict


def _run(entries, *, now, ttl, cap, evictable=None):
    """Run evict over a dict of {key: {last_used, evictable}} and return drops."""
    dropped = []
    evict(
        entries, now, ttl, cap,
        last_used=lambda v: v["last_used"],
        on_drop=lambda k: (dropped.append(k), entries.pop(k, None)),
        ttl_evictable=(evictable or (lambda v: v.get("evictable", True))),
    )
    return dropped


def test_ttl_drops_idle_evictable_entry():
    entries = {"a": {"last_used": 0.0}, "b": {"last_used": 95.0}}
    dropped = _run(entries, now=100.0, ttl=10.0, cap=100)
    assert dropped == ["a"]          # a is 100s idle (> ttl); b is only 5s idle
    assert set(entries) == {"b"}


def test_ttl_keeps_idle_but_running_entry():
    # 'run' is idle past the TTL but still running -> must NOT be evicted.
    entries = {
        "run": {"last_used": 0.0, "evictable": False},
        "done": {"last_used": 0.0, "evictable": True},
    }
    dropped = _run(entries, now=100.0, ttl=10.0, cap=100)
    assert dropped == ["done"]
    assert set(entries) == {"run"}


def test_lru_skips_running_entry_even_when_oldest():
    # Over cap, both within TTL. The oldest entry is running (non-evictable), so
    # the LRU trim must spare it and drop the next-oldest evictable one instead.
    entries = {
        "old_running": {"last_used": 1.0, "evictable": False},
        "mid_done": {"last_used": 2.0, "evictable": True},
        "new_done": {"last_used": 3.0, "evictable": True},
    }
    dropped = _run(entries, now=5.0, ttl=1000.0, cap=2)
    assert dropped == ["mid_done"]   # oldest *evictable*, not oldest overall
    assert set(entries) == {"old_running", "new_done"}


def test_lru_trims_oldest_to_cap():
    entries = {n: {"last_used": float(i)} for i, n in enumerate(["a", "b", "c", "d"])}
    dropped = _run(entries, now=10.0, ttl=1000.0, cap=2)
    assert dropped == ["a", "b"]     # two oldest dropped, newest two kept
    assert set(entries) == {"c", "d"}


def test_lru_cannot_go_below_cap_when_all_running():
    # If every over-cap survivor is non-evictable, leave them — a hard cap must
    # never orphan a live run.
    entries = {
        "r1": {"last_used": 1.0, "evictable": False},
        "r2": {"last_used": 2.0, "evictable": False},
        "r3": {"last_used": 3.0, "evictable": False},
    }
    dropped = _run(entries, now=5.0, ttl=1000.0, cap=1)
    assert dropped == []
    assert set(entries) == {"r1", "r2", "r3"}
