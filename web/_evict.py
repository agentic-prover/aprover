"""
Shared TTL + LRU eviction for the web app's in-memory stores.

Both ``web.sessions.SessionStore`` and ``web.jobs.JobStore`` keep a dict of
entries that must be bounded two ways: drop anything idle past a TTL, then
LRU-trim the remainder down to a hard cap. The only per-store differences are
how an entry's last-used time is read, what cleanup a drop needs (a session
removes its workspace; a job just disappears), and whether an entry is even
eligible for TTL eviction (a still-running job is kept even while idle). This
helper captures the shared shape; callers pass those three hooks.
"""
from __future__ import annotations

from typing import Any, Callable


def evict(
    entries: dict,
    now: float,
    ttl: float,
    cap: int,
    *,
    last_used: Callable[[Any], float],
    on_drop: Callable[[str], None],
    ttl_evictable: Callable[[Any], bool] = lambda _v: True,
) -> None:
    """Drop entries idle past ``ttl``, then LRU-trim to ``cap``. Caller holds the lock.

    ``last_used(value)`` yields an entry's last-touched epoch seconds.
    ``on_drop(key)`` removes the entry from ``entries`` and does any cleanup.
    ``ttl_evictable(value)`` gates the TTL stage (jobs keep a running entry even
    when idle); the LRU stage always applies once over ``cap``.
    """
    expired = [
        key for key, value in entries.items()
        if now - last_used(value) > ttl and ttl_evictable(value)
    ]
    for key in expired:
        on_drop(key)

    # LRU trim to the cap, but never drop a non-evictable (e.g. still-running)
    # entry — orphaning its worker thread would leak budget and 404 the client.
    # Only evictable entries are candidates; if the survivors over the cap are
    # all non-evictable, leave them (the wall-timeout still bounds a run).
    if len(entries) > cap:
        candidates = sorted(
            (kv for kv in entries.items() if ttl_evictable(kv[1])),
            key=lambda kv: last_used(kv[1]),
        )
        for key, _ in candidates[: len(entries) - cap]:
            on_drop(key)
