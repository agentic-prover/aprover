"""Tests for the elapsed-time ETA estimator (``bmc_agent.eta``).

We feed :class:`EtaEstimator` synthetic progress event sequences (the same
``type=phase``/``function``/``run`` events the pipeline emits) and assert that
progress rises monotonically toward 1.0 and the extrapolated time-remaining
shrinks. No pipeline or LLM is exercised.
"""
from __future__ import annotations

from bmc_agent.eta import EtaEstimator, fmt_duration


def test_fmt_duration():
    assert fmt_duration(0) == "0s"
    assert fmt_duration(45) == "45s"
    assert fmt_duration(130) == "2m10s"
    assert fmt_duration(125) == "2m05s"
    assert fmt_duration(3700) == "1h01m"
    assert fmt_duration(-5) == "0s"  # never negative


def test_single_file_progress_monotonic():
    est = EtaEstimator()
    assert est.progress == 0.0
    assert est.n_files == 1  # no "run" event => single file

    seq = [
        {"type": "phase", "phase": "spec", "status": "start", "file": "a.c"},
        {"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "bmc", "status": "start", "n_functions": 4, "file": "a.c"},
        {"type": "function", "phase": "bmc", "name": "f1", "file": "a.c"},
        {"type": "function", "phase": "bmc", "name": "f2", "file": "a.c"},
        {"type": "function", "phase": "bmc", "name": "f3", "file": "a.c"},
        {"type": "function", "phase": "bmc", "name": "f4", "file": "a.c"},
        {"type": "phase", "phase": "bmc", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "classify", "status": "start", "file": "a.c"},
        {"type": "phase", "phase": "classify", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "report", "status": "complete", "file": "a.c"},
    ]
    last = -1.0
    for ev in seq:
        est.update(ev)
        assert est.progress >= last - 1e-9  # monotonic non-decreasing
        last = est.progress

    # spec(.30) done, after the 2nd function bmc sub-progress is partway
    # through .45 — overall well past the spec weight.
    assert est.progress >= 0.99  # all phases complete


def test_bmc_subprogress_and_remaining_shrinks():
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "start", "n_functions": 4, "file": "a.c"})

    # After spec only (0.30), 100s elapsed => total ~333s, remaining ~233s.
    p_spec = est.progress
    assert abs(p_spec - 0.30) < 1e-6
    rem_spec = est.remaining(100.0)
    assert rem_spec is not None and rem_spec > 200

    # Each function adds 0.45/4 = 0.1125 of progress.
    for i, name in enumerate(("f1", "f2", "f3", "f4"), start=1):
        est.update({"type": "function", "phase": "bmc", "name": name, "file": "a.c"})
        assert abs(est.progress - (0.30 + 0.1125 * i)) < 1e-6

    # More progress at the same elapsed => less time remaining.
    assert est.remaining(100.0) < rem_spec

    # Duplicate function events must not double-count.
    before = est.progress
    est.update({"type": "function", "phase": "bmc", "name": "f4", "file": "a.c"})
    assert est.progress == before


def test_warmup_returns_none():
    est = EtaEstimator()
    # Below the floor (no progress yet) we can't extrapolate.
    assert est.remaining(10.0) is None
    assert est.total(10.0) is None
    payload = est.eta_payload(10.0)
    assert payload["remaining_s"] is None and payload["progress"] == 0.0


def test_prep_events_are_ignored_and_do_not_advance_files():
    """Directory pre-pass `prep` events (call-graph build, domain analysis) carry
    no progress and must not be mistaken for a file boundary or reset — the ETA
    estimator ignores them entirely."""
    est = EtaEstimator()
    est.update({"type": "run", "n_files": 2})
    progressed = est.update({"type": "prep", "detail": "building call graph · 1/2", "file": ""})
    assert progressed is False
    assert est.progress == 0.0
    assert est._files_done == 0  # the empty-file prep event did not advance a file


def test_multi_file_scaling():
    est = EtaEstimator()
    est.update({"type": "run", "n_files": 2})
    assert est.n_files == 2

    # Finish file a.c entirely.
    for ev in [
        {"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "bmc", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "classify", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "report", "status": "complete", "file": "a.c"},
    ]:
        est.update(ev)
    # One of two files fully done, current (a.c) fraction ~1.0, but it has not
    # been finalised yet (no next-file event) => overall ~0.5.
    assert abs(est.progress - 0.5) < 1e-6

    # Starting b.c finalises a.c (files_done -> 1) and resets the per-file state.
    est.update({"type": "phase", "phase": "spec", "status": "start", "file": "b.c"})
    assert est._files_done == 1
    # files_done(1) + b.c fraction(0, spec only started) = 0.5 of 2 files.
    assert abs(est.progress - 0.5) < 1e-6

    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "b.c"})
    # 1 + 0.30, over 2 => 0.65
    assert abs(est.progress - 0.65) < 1e-6


def test_run_event_resets():
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    assert est.progress > 0
    est.update({"type": "run", "n_files": 3})
    assert est.progress == 0.0
    assert est.n_files == 3


# -- edge cases / hardening --------------------------------------------------

def test_update_returns_run_reset_flag():
    """update() signals a "run" reset so front-ends can rebase their clock."""
    est = EtaEstimator()
    assert est.update({"type": "run", "n_files": 2}) is True
    assert est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"}) is False
    assert est.update({"type": "function", "phase": "bmc", "name": "f", "file": "a.c"}) is False
    assert est.update({"type": "log", "msg": "x"}) is False


def test_update_ignores_non_dict():
    """A malformed event must be ignored, never raise into the run."""
    est = EtaEstimator()
    for bad in (None, "phase", 123, ["type", "run"]):
        assert est.update(bad) is False
    assert est.progress == 0.0


def test_autonomous_multi_round_reset():
    """Each autonomous round emits its own "run" event; the estimate resets to 0
    and reports the reset both times so front-ends time each round separately."""
    est = EtaEstimator()
    # Round 1: one file, fully verified.
    assert est.update({"type": "run", "n_files": 1}) is True
    for ev in [
        {"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "bmc", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "classify", "status": "complete", "file": "a.c"},
        {"type": "phase", "phase": "report", "status": "complete", "file": "a.c"},
    ]:
        est.update(ev)
    assert est.progress >= 0.99

    # Round 2 begins: the run event resets progress to 0 and signals the reset.
    assert est.update({"type": "run", "n_files": 1}) is True
    assert est.progress == 0.0
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    assert abs(est.progress - 0.30) < 1e-6


def test_bmc_zero_functions_no_crash():
    """A bmc phase with n_functions=0 must not divide by zero; sub-progress holds
    flat until the phase completes."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "start", "n_functions": 0, "file": "a.c"})
    assert abs(est.progress - 0.30) < 1e-6  # bmc start contributes nothing
    est.update({"type": "function", "phase": "bmc", "name": "f", "file": "a.c"})
    assert abs(est.progress - 0.30) < 1e-6  # stray function can't lift it
    est.update({"type": "phase", "phase": "bmc", "status": "complete", "file": "a.c"})
    assert abs(est.progress - 0.75) < 1e-6  # spec + full bmc weight on completion


def test_bmc_missing_n_functions_holds_until_complete():
    """Without n_functions, per-function events can't be scaled, so bmc holds at
    0 sub-progress until the phase completes."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "start", "file": "a.c"})  # no n_functions
    for name in ("f1", "f2"):
        est.update({"type": "function", "phase": "bmc", "name": name, "file": "a.c"})
    assert abs(est.progress - 0.30) < 1e-6
    est.update({"type": "phase", "phase": "bmc", "status": "complete", "file": "a.c"})
    assert abs(est.progress - 0.75) < 1e-6


def test_bmc_seen_exceeds_total_caps_at_weight():
    """More function events than n_functions must not push bmc above its weight."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "start", "n_functions": 2, "file": "a.c"})
    for name in ("f1", "f2", "f3", "f4"):  # twice the declared count
        est.update({"type": "function", "phase": "bmc", "name": name, "file": "a.c"})
    assert abs(est.progress - 0.75) < 1e-6  # spec(.30) + bmc capped at .45


def test_function_event_non_bmc_phase_ignored():
    """function events for a non-bmc phase carry no sub-progress."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "bmc", "status": "start", "n_functions": 4, "file": "a.c"})
    est.update({"type": "function", "phase": "spec", "name": "x", "file": "a.c"})
    assert est.progress == 0.0  # non-bmc function ignored
    est.update({"type": "function", "phase": "bmc", "name": "f1", "file": "a.c"})
    assert abs(est.progress - 0.45 / 4) < 1e-6  # bmc function counts


def test_eta_payload_populated_after_progress():
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    payload = est.eta_payload(100.0)
    assert abs(payload["progress"] - 0.30) < 1e-6
    assert payload["elapsed_s"] == 100.0
    # p=0.30 over 100s => total ~333s, remaining ~233s.
    assert payload["remaining_s"] is not None and 220 < payload["remaining_s"] < 250
    assert payload["total_s"] is not None and 320 < payload["total_s"] < 350


# -- spec sub-progress (keeps the ETA from stalling through a long spec phase) --

def test_spec_subprogress_clears_warmup_before_complete():
    """A long spec phase (one LLM call per function) must advance the bar — and
    thus the ETA — instead of sitting at 0 until it completes. After the first
    function's spec the estimate clears the warm-up floor."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "start", "file": "a.c"})
    # Still 0 until the first structured progress note arrives.
    assert est.progress == 0.0
    assert est.remaining(100.0) is None  # warming up

    est.update({"type": "phase", "phase": "spec", "status": "start",
                "detail": "generating specs · 1/10",
                "spec_done": 1, "spec_total": 10, "file": "a.c"})
    # 1/10 of the 0.30 spec weight => 0.03, exactly at the floor — ETA appears.
    assert abs(est.progress - 0.03) < 1e-6
    assert est.remaining(100.0) is not None

    est.update({"type": "phase", "phase": "spec", "status": "start",
                "spec_done": 5, "spec_total": 10, "file": "a.c"})
    assert abs(est.progress - 0.30 * 0.5) < 1e-6  # half the spec weight

    # Completion credits the full weight regardless of the last note.
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    assert abs(est.progress - 0.30) < 1e-6


def test_spec_subprogress_caps_and_no_telemetry_fallback():
    """spec_done > spec_total can't exceed the weight; and a spec phase with no
    telemetry holds at 0 until completion (the pre-existing behaviour)."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "start",
                "spec_done": 12, "spec_total": 10, "file": "a.c"})
    assert abs(est.progress - 0.30) < 1e-6  # capped at the spec weight

    # Without spec_total, the phase contributes nothing until complete.
    est2 = EtaEstimator()
    est2.update({"type": "phase", "phase": "spec", "status": "start", "file": "a.c"})
    est2.update({"type": "phase", "phase": "spec", "status": "start",
                 "detail": "preprocessing source", "file": "a.c"})
    assert est2.progress == 0.0
    est2.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    assert abs(est2.progress - 0.30) < 1e-6


# -- LOC / counterexample weighting (the accuracy rework) --------------------

def _finish_file(est, name):
    """Drive every phase of one file to completion (clean, no CEx)."""
    for ev in [
        {"type": "phase", "phase": "spec", "status": "complete", "file": name},
        {"type": "phase", "phase": "bmc", "status": "complete", "file": name},
        {"type": "phase", "phase": "classify", "status": "complete", "file": name},
        {"type": "phase", "phase": "report", "status": "complete", "file": name},
    ]:
        est.update(ev)


def test_file_size_weighting():
    """With per-file LOC, finishing a tiny file barely moves the bar; finishing
    the big one moves it a lot — unlike flat 1/n_files weighting."""
    est = EtaEstimator()
    est.update({"type": "run", "n_files": 2,
                "file_locs": {"big.c": 1000, "small.c": 10}})

    # Finish small.c entirely. It is 10/1010 of the directory, so even fully
    # done (and not yet finalised) overall progress is ~1%.
    _finish_file(est, "small.c")
    assert est.progress < 0.02  # flat weighting would have said ~0.5

    # Starting big.c finalises small.c (10 LOC credited) — still ~1%.
    est.update({"type": "phase", "phase": "spec", "status": "start", "file": "big.c"})
    assert abs(est.progress - 10 / 1010) < 1e-6

    # Finishing big.c reaches ~100%.
    _finish_file(est, "big.c")
    assert est.progress > 0.99


def test_bmc_loc_weighting():
    """BMC sub-progress weights by function LOC: settling the big function first
    advances the BMC phase far more than settling the small one would."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "start", "n_functions": 2,
                "function_locs": {"big": 900, "small": 100}, "file": "a.c"})
    assert abs(est.progress - 0.30) < 1e-6  # spec only so far

    # Settling "big" (900/1000 of the LOC) advances BMC by ~0.9 of its 0.45.
    est.update({"type": "function", "phase": "bmc", "name": "big",
                "n_counterexamples": 0, "file": "a.c"})
    assert abs(est.progress - (0.30 + 0.45 * 0.9)) < 1e-6

    est.update({"type": "function", "phase": "bmc", "name": "small",
                "n_counterexamples": 0, "file": "a.c"})
    assert abs(est.progress - (0.30 + 0.45)) < 1e-6  # full BMC weight


def test_clean_file_fast_path():
    """A file BMC proves clean (all n_counterexamples=0) credits the classify
    weight the instant BMC completes — no stall through a no-op phase."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "start", "n_functions": 2,
                "function_locs": {"f1": 5, "f2": 5}, "file": "a.c"})
    for n in ("f1", "f2"):
        est.update({"type": "function", "phase": "bmc", "name": n,
                    "n_counterexamples": 0, "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "complete", "file": "a.c"})
    # spec(.30) + bmc(.45) + classify(.20) credited as free => only report left.
    assert abs(est.progress - 0.95) < 1e-6
    est.update({"type": "phase", "phase": "report", "status": "complete", "file": "a.c"})
    assert abs(est.progress - 1.0) < 1e-6


def test_classify_subdivides_by_counterexamples():
    """When BMC reports CEx, the classify weight sub-divides by validated/total
    rather than jumping only at completion."""
    est = EtaEstimator()
    est.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "start", "n_functions": 2,
                "function_locs": {"f1": 5, "f2": 5}, "file": "a.c"})
    # f1 yields 3 CEx, f2 yields 1 => 4 total to classify.
    est.update({"type": "function", "phase": "bmc", "name": "f1",
                "n_counterexamples": 3, "file": "a.c"})
    est.update({"type": "function", "phase": "bmc", "name": "f2",
                "n_counterexamples": 1, "file": "a.c"})
    est.update({"type": "phase", "phase": "bmc", "status": "complete", "file": "a.c"})
    base = 0.30 + 0.45  # spec + bmc; classify not yet credited (CEx present)
    assert abs(est.progress - base) < 1e-6

    est.update({"type": "phase", "phase": "classify", "status": "start", "file": "a.c"})
    est.update({"type": "function", "phase": "classify", "name": "f1",
                "n_counterexamples": 3, "file": "a.c"})
    # 3 of 4 CEx validated => 3/4 of the 0.20 classify weight.
    assert abs(est.progress - (base + 0.20 * 0.75)) < 1e-6

    est.update({"type": "function", "phase": "classify", "name": "f2",
                "n_counterexamples": 1, "file": "a.c"})
    assert abs(est.progress - (base + 0.20)) < 1e-6  # all CEx => full weight


def test_malformed_loc_maps_ignored():
    """Garbage in file_locs/function_locs is dropped, not raised — falls back to
    count-based weighting."""
    est = EtaEstimator()
    est.update({"type": "run", "n_files": 2, "file_locs": "not-a-dict"})
    # No usable LOC => equal 1/n_files weighting.
    _finish_file(est, "a.c")
    est.update({"type": "phase", "phase": "spec", "status": "start", "file": "b.c"})
    assert abs(est.progress - 0.5) < 1e-6  # one of two files done

    est2 = EtaEstimator()
    est2.update({"type": "phase", "phase": "spec", "status": "complete", "file": "a.c"})
    est2.update({"type": "phase", "phase": "bmc", "status": "start", "n_functions": 2,
                 "function_locs": {"f1": "x", "f2": None}, "file": "a.c"})
    est2.update({"type": "function", "phase": "bmc", "name": "f1", "file": "a.c"})
    # Bad locs dropped => count-based: 1 of 2 functions => half of 0.45.
    assert abs(est2.progress - (0.30 + 0.45 / 2)) < 1e-6
