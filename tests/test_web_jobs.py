"""Thread-safety regression tests for ``web.jobs.Job`` and the log sink.

``Job.ingest`` is called concurrently from two threads (the pipeline's progress
callback and the runner's event loop) while the SSE endpoint calls
``snapshot``/``events_since``. These tests hammer those paths from multiple
threads and assert no exception escapes and the derived state stays consistent —
the race that motivated guarding ``ingest`` under the job lock.

The log-sink tests cover ``bmc_agent.logger.set_log_sink``: that it is
context-local (one run's logs never leak into another's) and a no-op when unset.
"""
from __future__ import annotations

import logging
import threading
import time

from bmc_agent.logger import get_logger, reset_log_sink, set_log_sink
from web.jobs import Job


def _make_job() -> Job:
    return Job(run_id="r1", session_id="s1", scope={}, llm={"model": "m"})


def test_snapshot_omits_api_key_and_carries_scope_repo():
    """Contract the refresh-resume frontend relies on: the snapshot exposes the
    scope (incl. repo) but never the API key — only the model."""
    job = Job(
        run_id="r2", session_id="s1",
        scope={"mode": "whole", "repo": "acme", "path": ""},
        llm={"model": "claude-sonnet-4-6", "backend": "anthropic",
             "base_url": "", "key": "sk-secret-DO-NOT-LEAK"},
    )
    snap = job.snapshot()
    assert snap["scope"]["repo"] == "acme"
    assert snap["model"] == "claude-sonnet-4-6"
    assert {"status", "eta", "cost", "phases", "functions", "findings"} <= snap.keys()
    # The key must not appear anywhere in the serialised snapshot.
    assert "sk-secret-DO-NOT-LEAK" not in repr(snap)
    assert "key" not in snap


def test_cost_reliability_folds_into_snapshot():
    """The reliability sub-dict nested in an event's cost must survive ingest and
    appear in the snapshot, so a refresh restores the reliability badge."""
    job = _make_job()
    job.ingest({
        "type": "function", "name": "f", "status": "verified",
        "cost": {
            "total_tokens": 1200, "prompt_tokens": 1000, "completion_tokens": 200,
            "reliability": {"total": 4, "success": 3, "timeout": 1,
                            "recent_fail": 1, "recent_total": 4,
                            "latency_ms_avg": 1800},
        },
    })
    rel = job.snapshot()["cost"]["reliability"]
    assert rel["total"] == 4 and rel["success"] == 3 and rel["timeout"] == 1
    assert rel["recent_fail"] == 1 and rel["latency_ms_avg"] == 1800


def test_cost_usd_folds_into_streamed_event():
    """The computed ``usd`` figure must be folded into the event's own cost dict,
    not just the recovery snapshot — the live SSE stream sends the raw event and
    the workbench reads ``cost.usd``, so without this the live meter shows "—"
    until a reconnect. ``usd_exact`` priced by the provider is preferred."""
    job = _make_job()
    ev = {
        "type": "function", "name": "f", "status": "verified",
        "cost": {
            "total_tokens": 1200, "prompt_tokens": 1000, "completion_tokens": 200,
            "model": "anthropic/claude-sonnet-4.6", "usd_exact": 0.0123,
        },
    }
    job.ingest(ev)
    # The event object that gets streamed carries the figure (exact provider cost).
    assert ev["cost"]["usd"] == 0.0123
    assert job.events[-1]["cost"]["usd"] == 0.0123
    # And the recovery snapshot agrees.
    assert job.snapshot()["cost"]["usd"] == 0.0123


def test_cost_usd_falls_back_to_token_estimate_in_event():
    """When the provider gives no exact figure (usd_exact absent), the event still
    carries a token-based estimate so the live meter shows a price."""
    job = _make_job()
    ev = {
        "type": "function", "name": "f", "status": "verified",
        "cost": {
            "total_tokens": 1_000_000, "prompt_tokens": 1_000_000,
            "completion_tokens": 0, "model": "anthropic/claude-sonnet-4.6",
        },
    }
    job.ingest(ev)
    # claude-sonnet input price is $3/Mtok → 1M prompt tokens ≈ $3.00.
    assert ev["cost"]["usd"] == 3.0
    assert job.events[-1]["cost"]["usd"] == 3.0


def test_events_replay_includes_terminal_done():
    """A reconnecting client replays from index 0 and must see the terminal
    'done' event — that's what drives the finished-run view after a refresh."""
    job = _make_job()
    job.ingest({"type": "phase", "phase": "spec", "status": "complete"})
    job.ingest({"type": "finding", "bug": {"function": "f"}})
    job.finish("done")

    evs, done, total = job.events_since(0)
    assert done is True
    assert total == len(evs)
    assert evs[-1]["type"] == "done"
    assert evs[-1]["status"] == "done"


def test_spec_fn_events_fold_into_snapshot():
    """Per-function spec progress (type='spec_fn') drives the workbench's spec
    chips; the latest status per function must survive ingest into the snapshot
    so a reconnecting client recovers chip state. It must NOT leak into the BMC
    `functions` map (distinct event type, distinct semantics)."""
    job = _make_job()
    job.ingest({"type": "spec_fn", "name": "a", "status": "active"})
    job.ingest({"type": "spec_fn", "name": "b", "status": "active"})
    job.ingest({"type": "spec_fn", "name": "a", "status": "done"})

    snap = job.snapshot()
    assert snap["spec_fn"] == {"a": "done", "b": "active"}
    # spec progress is separate from BMC verdicts
    assert snap["functions"] == []


def test_prep_events_replay_without_disturbing_state():
    """Directory pre-pass `prep` events drive the setup checklist on the client
    and are replayed on reconnect (appended to the event log), but they carry no
    derived state — they must not land in functions/spec_fn/findings."""
    job = _make_job()
    job.ingest({"type": "prep", "detail": "analyzing API boundaries"})
    job.ingest({"type": "prep", "detail": "building call graph · 1/3"})

    evs, _done, total = job.events_since(0)
    assert [e for e in evs if e.get("type") == "prep"]  # replayed for reconnects
    assert total == 2
    snap = job.snapshot()
    assert snap["functions"] == [] and snap["spec_fn"] == {} and snap["findings"] == []


def test_verified_function_harness_survives_snapshot():
    """A verified function event carries its proof harness; that must survive
    ingest into Job.functions and the snapshot, so the workbench can show the
    proof (and the harness on click) after a refresh."""
    job = _make_job()
    job.ingest({
        "type": "function", "name": "parse", "status": "verified",
        "n_counterexamples": 0,
        "harness": "int main(){ return 0; }", "harness_lang": "c",
    })
    fns = job.snapshot()["functions"]
    assert len(fns) == 1
    proof = fns[0]
    assert proof["name"] == "parse" and proof["status"] == "verified"
    assert proof["harness"] == "int main(){ return 0; }"
    assert proof["harness_lang"] == "c"


def test_concurrent_ingest_and_snapshot_no_race():
    """Two writers + a reader on one Job must not raise or corrupt state."""
    job = _make_job()
    n = 400

    def write_findings() -> None:
        for i in range(n):
            job.ingest({"type": "finding", "bug": {"id": f"a{i}"}})

    def write_functions() -> None:
        for i in range(n):
            job.ingest({"type": "function", "name": f"fn{i}", "status": "verified"})

    errors: list[Exception] = []

    def read_snapshots() -> None:
        for _ in range(n * 2):
            try:
                job.snapshot()
                job.events_since(0)
            except Exception as exc:  # pragma: no cover - the bug we guard against
                errors.append(exc)

    threads = [
        threading.Thread(target=write_findings),
        threading.Thread(target=write_functions),
        threading.Thread(target=read_snapshots),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"snapshot/iteration raced with ingest: {errors[:3]}"
    assert len(job.findings) == n
    assert len(job.functions) == n
    # Every ingested event is in the log exactly once (writers only).
    assert len(job.events) == 2 * n


def test_events_since_returns_total():
    job = _make_job()
    job.ingest({"type": "log", "message": "a"})
    job.ingest({"type": "log", "message": "b"})
    evs, done, total = job.events_since(1)
    assert total == 2
    assert len(evs) == 1
    assert done is False


def test_set_status_threadsafe_and_visible_in_snapshot():
    job = _make_job()
    job.set_status("paused")
    assert job.snapshot()["status"] == "paused"


def test_log_sink_is_context_local(tmp_path):
    """A sink set in one thread must not capture another thread's logs."""
    log = get_logger("test_sink_component", artifact_dir=str(tmp_path))
    log.setLevel(logging.INFO)

    captured_a: list[str] = []
    captured_b: list[str] = []
    barrier = threading.Barrier(2)

    def run(captured: list[str], tag: str) -> None:
        token = set_log_sink(lambda level, msg: captured.append(msg))
        try:
            barrier.wait()  # ensure both sinks are live simultaneously
            log.info("from-%s", tag)
        finally:
            reset_log_sink(token)

    ta = threading.Thread(target=run, args=(captured_a, "a"))
    tb = threading.Thread(target=run, args=(captured_b, "b"))
    ta.start(); tb.start()
    ta.join(); tb.join()

    # Each thread's sink saw only its own line — no cross-attribution.
    assert captured_a == ["from-a"]
    assert captured_b == ["from-b"]


def test_log_sink_noop_when_unset(tmp_path):
    """With no sink set, logging must not raise (sink handler is inert)."""
    log = get_logger("test_sink_unset", artifact_dir=str(tmp_path))
    log.info("no sink here")  # must simply not raise


def test_eta_clock_rebases_on_run_reset():
    """An autonomous re-round emits a fresh "run" event. The Job must rebase its
    ETA clock to that event, not the job's creation — otherwise progress resets
    to 0 while elapsed still spans prior rounds, inflating remaining wildly."""
    job = _make_job()
    # Pretend an earlier round ran for ~1000s before this round begins.
    job.created = job.eta_start = time.time() - 1000.0

    # Round-2 "run" event: resets progress AND rebases the elapsed clock to ~now.
    job.ingest({"type": "run", "n_files": 1})
    assert job.eta_start > job.created  # clock moved forward to this run
    assert job.eta["progress"] == 0.0

    # A little progress in the new round: elapsed reflects the round (~0s), so the
    # ETA is sane — not the ~2333s a stale 1000s clock would have produced.
    job.ingest({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    assert abs(job.eta["progress"] - 0.30) < 1e-6
    assert job.eta["elapsed_s"] < 60
    assert job.eta["remaining_s"] is not None and job.eta["remaining_s"] < 200


def test_scope_options_survive_snapshot_and_retry_copy():
    """The validated run-settings live in scope['options']; the snapshot exposes
    them (recovery view) and a retry copies the scope verbatim (dict(prev.scope)),
    so a retried run re-applies the exact same clamped configuration."""
    opts = {"depth": {"cbmc_unwind": 8}, "ai_layers": {"enable_realism_check": True}}
    job = Job(run_id="r3", session_id="s1",
              scope={"mode": "whole", "repo": "acme", "options": opts},
              llm={"model": "m", "key": "sk-secret-DO-NOT-LEAK"})
    snap = job.snapshot()
    assert snap["scope"]["options"] == opts
    # Retry mechanism (server.py): scope = dict(prev.scope) → options ride along.
    assert dict(job.scope)["options"] == opts
    # The key still never leaks through the options-carrying snapshot.
    assert "sk-secret-DO-NOT-LEAK" not in repr(snap)
