"""
Verification-run jobs for the workbench.

A *job* is one verification run (a scope: whole repo / subdir / single file)
executed in a background thread, with:

- **live event log** — phase / function / finding / log / cost / result events,
  retained so a late or reconnecting SSE client can replay from the start;
- **derived state** — current phase per stage, per-function status, findings,
  and cumulative spend (tokens + estimated $) — for the recovery snapshot;
- **cooperative pause / cancel** — a paused job blocks the pipeline at the next
  phase/function boundary; cancel raises out of it. A budget cap that is
  exceeded cancels the job and marks it ``halted`` with reason ``budget``.

Jobs are in-memory and owned by the session that created them (eviction mirrors
``web.sessions``: idle TTL + LRU cap). The pipeline plumbing lives in
``web.runner``; this module owns orchestration, state, and the control flags.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Iterator, Optional

from bmc_agent.eta import EtaEstimator
from web._evict import evict
from web.limits import JOB_TTL as _TTL_SECONDS
from web.limits import MAX_JOBS as _MAX_JOBS
from web.pricing import estimate_usd


class RunCancelled(Exception):
    """Raised inside the progress callback to unwind a cancelled/halted run."""


# Stages shown in the workbench pipeline tracker, in order.
PHASES = ("spec", "bmc", "classify", "report")


class Job:
    """One verification run + its live state. Thread-safe."""

    def __init__(self, run_id: str, session_id: str, scope: dict, llm: dict,
                 budget_cap: Optional[float] = None) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.scope = scope          # {mode, repo, path, only_functions, domain_knowledge, max_files, options}
        self.llm = llm              # {backend, model, base_url, key}
        self.budget_cap = budget_cap

        self.status = "running"     # running | paused | halted | done | error
        self.error = ""
        self.halt_reason = ""       # "budget" | "cancelled" | "timeout" | ""

        self.events: list[dict] = []
        self.phases: dict[str, str] = {}      # phase -> "start"|"complete"
        self.functions: dict[str, dict] = {}  # name -> last function event
        self.spec_fn: dict[str, str] = {}      # name -> "active"|"done" (spec chip)
        self.findings: list[dict] = []
        self.cost: dict = {"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0, "usd": None, "model": llm.get("model", "")}

        self._budget_warned = False   # one-shot: cap-inactive notice for unpriced models

        self.pause = threading.Event()
        self.cancel = threading.Event()

        self.created = time.time()
        self.last_used = time.time()
        self._lock = threading.Lock()
        self._done = False

        # Estimated time remaining, folded from the same progress events.
        # ``eta_start`` rebases on each "run" reset (autonomous re-rounds) so the
        # estimate is timed per-run, not from job creation. See Job.ingest.
        self.eta_est = EtaEstimator()
        self.eta_start = self.created
        self.eta: dict = {"progress": 0.0, "elapsed_s": 0.0,
                          "remaining_s": None, "total_s": None}

    # -- event ingestion ------------------------------------------------
    def ingest(self, ev: dict) -> None:
        """Fold a raw runner/pipeline event into derived state + the log.

        Called concurrently from the pipeline thread (the progress callback) and
        the runner worker thread (log/result events), so the whole body — derived
        state, ETA, and the event-log append — runs under ``self._lock`` to stay
        consistent with ``snapshot()``."""
        with self._lock:
            t = ev.get("type")
            if t == "phase":
                self.phases[ev.get("phase", "")] = ev.get("status", "")
            elif t == "function":
                name = ev.get("name", "")
                if name:
                    self.functions[name] = ev
            elif t == "spec_fn":
                name = ev.get("name", "")
                if name:
                    self.spec_fn[name] = ev.get("status", "")
            elif t == "finding":
                bug = ev.get("bug") or {}
                if bug:
                    self.findings.append(bug)
            cost = ev.get("cost")
            if isinstance(cost, dict) and cost.get("total_tokens") is not None:
                # Prefer the provider's exact figure (claude-code's total_cost_usd);
                # fall back to the token-based estimate for API backends.
                usd = cost.get("usd_exact")
                if usd is None:
                    usd = estimate_usd(cost)
                # Fold the figure into the event's own cost dict so the live SSE
                # stream carries it (the workbench reads cost.usd). ``cost`` is the
                # same object as ``ev["cost"]``, so this also updates the streamed
                # event — without it the live meter shows "—" until a reconnect.
                cost["usd"] = usd
                self.cost = dict(cost)
            # Fold progress into the ETA estimate and ride it out on the event so
            # the live SSE stream + recovery snapshot both carry a current ETA.
            # A "run" reset (autonomous re-round) rebases the elapsed clock so the
            # ETA reflects the current run, not the whole job's lifetime.
            if self.eta_est.update(ev):
                self.eta_start = time.time()
            self.eta = self.eta_est.eta_payload(time.time() - self.eta_start)
            ev["eta"] = self.eta
            self.events.append(ev)
            self.last_used = time.time()

    def set_status(self, status: str) -> None:
        """Thread-safe status write (the pause loop flips this off-lock)."""
        with self._lock:
            self.status = status

    def events_since(self, idx: int) -> tuple[list[dict], bool, int]:
        """Return (new events from ``idx``, done flag, total event count) atomically.

        The total lets the SSE loop decide completion without a second, unlocked
        ``len(self.events)`` read that could miss a late append."""
        with self._lock:
            return self.events[idx:], self._done, len(self.events)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "run_id": self.run_id,
                "status": self.status,
                "error": self.error,
                "halt_reason": self.halt_reason,
                "scope": self.scope,
                "model": self.llm.get("model", ""),
                "budget_cap": self.budget_cap,
                "phases": dict(self.phases),
                "functions": list(self.functions.values()),
                "spec_fn": dict(self.spec_fn),
                "findings": list(self.findings),
                "cost": dict(self.cost),
                "eta": dict(self.eta),
                "n_events": len(self.events),
            }

    def finish(self, status: str, error: str = "", halt_reason: str = "") -> None:
        with self._lock:
            self.status = status
            if error:
                self.error = error
            if halt_reason:
                self.halt_reason = halt_reason
            self._done = True
            # The BYOK key is only needed while the run is live; drop it so it
            # isn't retained for the job's post-completion TTL. Retry re-reads
            # the key from its request (the client still sends X-LLM-Key).
            if isinstance(self.llm, dict):
                self.llm["key"] = ""
            self.events.append({
                "type": "done", "status": self.status,
                "error": self.error, "halt_reason": self.halt_reason,
            })


def make_progress(job: Job) -> Callable[[dict], None]:
    """Build the ``config.progress`` callback for ``job``: record the event,
    enforce the budget cap, then block while paused / raise while cancelled."""

    def progress(ev: dict) -> None:
        job.ingest(ev)

        # Budget cap: once estimated spend exceeds the cap, halt cleanly.
        if job.budget_cap is not None:
            usd = job.cost.get("usd")
            if usd is not None and usd > job.budget_cap:
                job.halt_reason = "budget"
                job.cancel.set()
            elif usd is None and job.cost.get("total_tokens") and not job._budget_warned:
                # Unpriced model: estimate_usd() can't compute spend, so the USD
                # cap can never trip. Say so once instead of silently ignoring it;
                # the wall-timeout + per-function budget still bound the run.
                job._budget_warned = True
                job.ingest({
                    "type": "log", "level": "warning",
                    "message": (
                        "Budget cap (${:.2f}) is inactive: the selected model is "
                        "unpriced, so spend can't be computed. The wall-clock "
                        "timeout and per-function budget still apply.".format(job.budget_cap)
                    ),
                })

        if job.cancel.is_set():
            raise RunCancelled()

        # Cooperative pause: hold the pipeline here until resumed/cancelled.
        if job.pause.is_set():
            job.set_status("paused")
            while job.pause.is_set() and not job.cancel.is_set():
                time.sleep(0.1)
            if not job.cancel.is_set():
                job.set_status("running")

        if job.cancel.is_set():
            raise RunCancelled()

    return progress


def run_job(job: Job, gen_factory: Callable[..., Iterator[dict]]) -> None:
    """Drive a job to completion in a background thread.

    ``gen_factory(progress=..., pause_check=...)`` returns a ``web.runner``
    streaming generator. All yielded events are folded into the job; the
    pipeline's own structured progress arrives via the progress callback.
    """
    progress = make_progress(job)

    def pause_check() -> bool:
        return job.pause.is_set() and not job.cancel.is_set()

    def worker() -> None:
        try:
            for ev in gen_factory(progress=progress, pause_check=pause_check):
                t = ev.get("type")
                if t == "result":
                    res = ev.get("result") or {}
                    # Backfill findings if the pipeline produced no per-finding
                    # progress events (e.g. an error/timeout result).
                    if not job.findings and res.get("bugs"):
                        for b in res["bugs"]:
                            job.findings.append(b)
                    job.ingest({"type": "result", **res})
                    if not res.get("ok") and not job.cancel.is_set():
                        # A pipeline-level failure (timeout / no key / exception).
                        job.finish("error", error=res.get("error", "verification failed"))
                        return
                else:
                    job.ingest(ev)
        except RunCancelled:
            job.finish("halted", halt_reason=job.halt_reason or "cancelled")
            return
        except Exception as exc:  # pragma: no cover - surfaced to the client
            job.finish("error", error=f"{type(exc).__name__}: {exc}")
            return

        if job.cancel.is_set():
            job.finish("halted", halt_reason=job.halt_reason or "cancelled")
        else:
            job.finish("done")

    threading.Thread(target=worker, daemon=True).start()


class JobStore:
    """In-memory registry of run_id -> Job, owned per session."""

    def __init__(self, ttl: int = _TTL_SECONDS, cap: int = _MAX_JOBS) -> None:
        self._ttl = ttl
        self._cap = cap
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def create(self, session_id: str, scope: dict, llm: dict,
               budget_cap: Optional[float] = None) -> Job:
        now = time.time()
        with self._lock:
            self._evict(now)
            run_id = uuid.uuid4().hex
            job = Job(run_id, session_id, scope, llm, budget_cap)
            self._jobs[run_id] = job
            self._evict(now)
            return job

    def get(self, run_id: str, session_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None or job.session_id != session_id:
                return None
            job.last_used = time.time()
            return job

    def _evict(self, now: float) -> None:
        # Only finished jobs are TTL-evictable (a long run must survive idle gaps
        # between events); the LRU cap can still drop any job once over capacity.
        evict(
            self._jobs, now, self._ttl, self._cap,
            last_used=lambda j: j.last_used,
            on_drop=lambda rid: self._jobs.pop(rid, None),
            ttl_evictable=lambda j: j._done,
        )


# Module-level singleton shared by the web app.
STORE = JobStore()
