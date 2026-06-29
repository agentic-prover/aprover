"""
Elapsed-time-based ETA estimation for verification runs.

A single :class:`EtaEstimator` turns the pipeline's structured progress events
(the ``config.progress`` channel fed by ``AMCPipeline._emit``) into a progress
fraction in ``[0, 1]``, from which an ETA is extrapolated linearly from elapsed
time: ``remaining = elapsed * (1 - p) / p``. Both front-ends share this logic —
the web computes it server-side per :class:`web.jobs.Job`; the CLI attaches
``make_cli_callback`` when ``--eta`` is passed.

Progress model (per file): the run's phases carry static weights and the long
BMC phase is sub-divided continuously, so the estimate refines smoothly rather
than jumping only at phase boundaries. Three signals refine it beyond raw
event-counting, each optional with a graceful fallback so single-file ``run()``,
the Rust/Java paths, and synthetic test sequences are unaffected:

- **File size** — ``run`` events may carry ``file_locs`` (filename → LOC). When
  present, each file's share of overall progress is weighted by its LOC instead
  of a flat ``1/n_files``, so finishing a 50-line file barely moves the bar while
  a 2000-line file moves it a lot. Absent it, falls back to equal weighting.
- **Function size** — the ``bmc`` start event may carry ``function_locs`` (name →
  LOC). When present, BMC sub-progress is the LOC-fraction of settled functions
  rather than their count, so one large function advances the bar proportionally.
  Absent it, falls back to ``len(seen) / n_functions``.
- **Counterexample count** — BMC ``function`` events carry ``n_counterexamples``;
  their sum is the classify phase's real workload. When that signal is present, a
  clean file (zero CEx) credits the classify weight the moment BMC completes
  (classify is ~free), and a file with CEx sub-divides the classify weight by
  validated/total. Absent it, classify is credited only on ``complete`` as before.
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Optional

# Per-file phase weights (sum to 1.0). BMC dominates wall time on most runs, so
# it carries the largest share and is the only phase sub-divided per-function.
PHASE_WEIGHTS: dict[str, float] = {
    "spec": 0.30,
    "bmc": 0.45,
    "classify": 0.20,
    "report": 0.05,
}

# Below this progress fraction the extrapolation is too noisy to show — the
# front-ends render "estimating…" until it clears.
_MIN_PROGRESS = 0.03


def _coerce_int(value: object) -> int:
    """Best-effort non-negative int from an event field; 0 on anything odd."""
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _coerce_loc_map(value: object) -> dict[str, float]:
    """Sanitise a ``{name: loc}`` mapping from an event into ``{str: float>=0}``,
    dropping any malformed entries (never raises into the run)."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in value.items():
        try:
            loc = float(v)
        except (TypeError, ValueError):
            continue
        if loc > 0:
            out[str(k)] = loc
    return out


def fmt_duration(seconds: float) -> str:
    """Human-friendly ``"2m10s"`` / ``"1h05m"`` / ``"45s"`` for a duration."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


class EtaEstimator:
    """Folds progress events into a 0..1 fraction and extrapolates an ETA.

    Stateful and resettable: a ``type="run"`` event resets it (so an
    ``autonomous`` re-round restarts cleanly — the estimate is always for the
    current pipeline run). Feed every event via :meth:`update`.
    """

    def __init__(self) -> None:
        self._reset_all()

    def _reset_all(self) -> None:
        self.n_files = 1
        self._files_done = 0
        self._cur_file: Optional[str] = None
        # filename -> LOC, from the ``run`` event (empty => equal-weight files).
        self._file_locs: dict[str, float] = {}
        self._done_loc = 0.0   # summed LOC of finalised files
        self._reset_file()

    def _reset_file(self) -> None:
        self._phase_status: dict[str, str] = {}   # phase -> "start" | "complete"
        # Spec sub-progress: functions whose specs are generated so far / total
        # (empty/zero => no telemetry, credit spec only on completion).
        self._spec_done = 0
        self._spec_total = 0
        self._bmc_total = 0
        self._bmc_seen: set[str] = set()
        self._bmc_locs: dict[str, float] = {}     # fn -> LOC (empty => count-based)
        # Counterexample accounting for the classify phase. ``_bmc_cex_known``
        # gates the CEx-aware logic so synthetic/old events (no n_counterexamples)
        # fall back to crediting classify only on completion.
        self._bmc_cex_known = False
        self._bmc_cex_total = 0
        self._classify_cex_seen = 0

    # -- ingestion ------------------------------------------------------
    def update(self, ev: dict) -> bool:
        """Fold one progress event in. Returns ``True`` iff this was a ``run``
        event that reset the estimate — front-ends watch the return value to
        rebase their elapsed-time clock so each run (e.g. an autonomous round)
        is timed from its own start rather than the original job creation."""
        if not isinstance(ev, dict):
            return False  # malformed event — ignore rather than raise into the run
        t = ev.get("type")
        if t == "run":
            self._reset_all()
            try:
                self.n_files = max(1, int(ev.get("n_files", 1) or 1))
            except (TypeError, ValueError):
                self.n_files = 1
            self._file_locs = _coerce_loc_map(ev.get("file_locs"))
            return True

        if t not in ("phase", "function"):
            return False  # log / finding / cost / result / done carry no progress

        # File boundary: any structural event naming a new file finalises the
        # previous one (it ran all its phases). Single-file run() leaves file
        # constant, so this never trips.
        f = ev.get("file")
        if f and f != self._cur_file:
            if self._cur_file is not None:
                self._files_done += 1
                # Credit the finalised file's LOC (0 if sizes weren't supplied).
                self._done_loc += self._file_locs.get(self._cur_file, 0.0)
            self._cur_file = f
            self._reset_file()

        if t == "phase":
            phase = ev.get("phase", "")
            status = ev.get("status", "")
            if phase:
                self._phase_status[phase] = status
            if phase == "spec" and status == "start" and "spec_total" in ev:
                # Live spec sub-progress (one LLM round-trip per function).
                self._spec_total = _coerce_int(ev.get("spec_total"))
                self._spec_done = _coerce_int(ev.get("spec_done"))
            if phase == "bmc" and status == "start":
                try:
                    self._bmc_total = int(ev.get("n_functions", 0) or 0)
                except (TypeError, ValueError):
                    self._bmc_total = 0
                self._bmc_locs = _coerce_loc_map(ev.get("function_locs"))
        elif t == "function":
            phase = ev.get("phase")
            name = ev.get("name")
            if phase == "bmc" and name:
                self._bmc_seen.add(name)
                # Tally counterexamples so the classify phase knows its workload.
                if "n_counterexamples" in ev:
                    self._bmc_cex_known = True
                    self._bmc_cex_total += _coerce_int(ev.get("n_counterexamples"))
            elif phase == "classify":
                # Each function's counterexamples settle here; advance classify.
                self._classify_cex_seen += _coerce_int(ev.get("n_counterexamples"))
        return False

    # -- derived progress ----------------------------------------------
    def _spec_subfraction(self) -> float:
        """Fraction of the spec phase done, by settled-function count. Returns
        ``0.0`` when no spec telemetry was supplied (synthetic/old events, the
        Rust path) so spec is then credited only on completion as before."""
        if self._spec_total > 0:
            return min(1.0, self._spec_done / self._spec_total)
        return 0.0

    def _bmc_subfraction(self) -> float:
        """Fraction of the BMC phase done: LOC-weighted if function sizes were
        supplied, else by settled-function count."""
        total_loc = sum(self._bmc_locs.values())
        if total_loc > 0:
            seen_loc = sum(self._bmc_locs.get(n, 0.0) for n in self._bmc_seen)
            return min(1.0, seen_loc / total_loc)
        if self._bmc_total > 0:
            return min(1.0, len(self._bmc_seen) / self._bmc_total)
        return 0.0

    def _current_file_fraction(self) -> float:
        frac = 0.0
        # report: simple complete-credited weight.
        if self._phase_status.get("report") == "complete":
            frac += PHASE_WEIGHTS["report"]

        # spec: continuous sub-progress while running (one LLM call per
        # function can run for many minutes), full weight on complete. Without
        # this the bar — and thus the ETA — sits at 0 through the whole phase.
        spec_st = self._phase_status.get("spec")
        if spec_st == "complete":
            frac += PHASE_WEIGHTS["spec"]
        elif spec_st == "start":
            frac += PHASE_WEIGHTS["spec"] * self._spec_subfraction()

        # bmc: continuous sub-progress while running, full weight on complete.
        bmc_st = self._phase_status.get("bmc")
        if bmc_st == "complete":
            frac += PHASE_WEIGHTS["bmc"]
        elif bmc_st == "start":
            frac += PHASE_WEIGHTS["bmc"] * self._bmc_subfraction()

        frac += self._classify_fraction()
        return min(frac, 1.0)

    def _classify_fraction(self) -> float:
        """Classify-phase contribution. Counterexample-aware when BMC reported
        counts: a clean file (0 CEx) credits the full weight the moment BMC
        completes, an unclean one sub-divides by validated/total. Falls back to
        crediting only on ``complete`` when no CEx signal was seen."""
        weight = PHASE_WEIGHTS["classify"]
        cl_st = self._phase_status.get("classify")
        if cl_st == "complete":
            return weight
        if not self._bmc_cex_known:
            return 0.0  # no CEx telemetry — credit only on completion (old behaviour)
        bmc_complete = self._phase_status.get("bmc") == "complete"
        if self._bmc_cex_total == 0:
            # Clean file: nothing to classify, so it's effectively free once BMC
            # is done — don't make the bar stall through a no-op phase.
            return weight if bmc_complete else 0.0
        if cl_st == "start":
            return weight * min(1.0, self._classify_cex_seen / self._bmc_cex_total)
        return 0.0

    @property
    def progress(self) -> float:
        """Overall completion fraction in ``[0, 1]``."""
        cur = self._current_file_fraction()
        total_loc = sum(self._file_locs.values())
        if total_loc > 0:
            # LOC-weighted across files: finished files + the current file's
            # in-progress share, all scaled by their line counts.
            cur_loc = self._file_locs.get(self._cur_file, 0.0)
            p = (self._done_loc + cur_loc * cur) / total_loc
        else:
            p = (self._files_done + cur) / max(1, self.n_files)
        return max(0.0, min(1.0, p))

    def remaining(self, elapsed_s: float) -> Optional[float]:
        """Estimated seconds left, or ``None`` while still warming up."""
        p = self.progress
        if p < _MIN_PROGRESS:
            return None
        if p >= 1.0:
            return 0.0
        return elapsed_s * (1.0 - p) / p

    def total(self, elapsed_s: float) -> Optional[float]:
        """Estimated total run length, or ``None`` while still warming up."""
        p = self.progress
        if p < _MIN_PROGRESS:
            return None
        return elapsed_s / p

    def eta_payload(self, elapsed_s: float) -> dict:
        """Serialisable ETA snapshot for the web (attached to events/snapshot)."""
        rem = self.remaining(elapsed_s)
        tot = self.total(elapsed_s)
        return {
            "progress": round(self.progress, 4),
            "elapsed_s": round(elapsed_s, 1),
            "remaining_s": None if rem is None else round(rem, 1),
            "total_s": None if tot is None else round(tot, 1),
        }


class _CliReporter:
    """A ``config.progress`` callback that prints throttled ETA lines to stderr.

    Holds its own estimator + monotonic start. Prints on every phase transition
    and at most once every few seconds otherwise; stays silent until the
    estimate is meaningful. Never raises into the pipeline.
    """

    _THROTTLE_S = 5.0

    def __init__(self) -> None:
        self._est = EtaEstimator()
        self._start: Optional[float] = None
        self._last_print = 0.0

    def __call__(self, ev: dict) -> None:
        try:
            now = time.monotonic()
            if self._start is None:
                self._start = now
            # A "run" event restarts the estimate (e.g. a new autonomous round);
            # rebase the clock so the ETA is timed from this run, not the first.
            if self._est.update(ev):
                self._start = now

            is_phase = ev.get("type") == "phase"
            if not is_phase and (now - self._last_print) < self._THROTTLE_S:
                return

            payload = self._est.eta_payload(now - self._start)
            rem = payload["remaining_s"]
            if rem is None:
                return  # still estimating — nothing useful to show yet
            self._last_print = now
            pct = int(payload["progress"] * 100)
            print(f"[eta] {pct}% · ~{fmt_duration(rem)} remaining", file=sys.stderr)
        except Exception:
            # ETA is best-effort telemetry; never let it disturb a long run.
            pass


def make_cli_callback() -> Callable[[dict], None]:
    """Build a ``config.progress`` callback that prints ETA updates to stderr."""
    return _CliReporter()
