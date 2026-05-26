"""
Tests for _synthesize_sibling_cex_summary (commit f4080a9).

The method runs after Phase 3 completes for a function. It walks the
per-CEx classification records, computes a summary of sibling outcomes,
and decorates bug_report.json. When sibling instability is detected and
the saved confidence is high, it downgrades to 'unlikely'.

Tests exercise the summary computation, the instability gate, the
confidence-downgrade rules, and defensive paths (missing report,
empty classifications, unparseable JSON).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_pipeline(tmp_path: Path):
    """Instantiate AMCPipeline without running __init__ — the synthesis
    method only touches self.store, so bypassing LLMClient / harness gen
    setup keeps the test hermetic.
    """
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.pipeline import AMCPipeline

    pipeline = object.__new__(AMCPipeline)
    pipeline.store = ArtifactStore(str(tmp_path / "artifacts"))
    return pipeline


def _write_bug_report(fn_dir: Path, confidence: str, reasoning_trail: str = "base"):
    payload = {
        "saved_at": "2026-05-26T00:00:00+00:00",
        "report": {
            "function_name": fn_dir.name,
            "confidence": confidence,
            "reasoning_trail": reasoning_trail,
        },
    }
    (fn_dir / "bug_report.json").write_text(json.dumps(payload, indent=2))


def _write_classification(fn_dir: Path, name: str, outcome: str):
    cls_dir = fn_dir / "classifications"
    cls_dir.mkdir(parents=True, exist_ok=True)
    (cls_dir / f"{name}.json").write_text(json.dumps({
        "classification": {"outcome": outcome},
    }))


def _read_report(fn_dir: Path) -> dict:
    return json.loads((fn_dir / "bug_report.json").read_text())["report"]


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------

def test_summary_records_all_outcome_buckets(tmp_path):
    """Mix of real_bug + unresolved + spurious + latent → all four counts
    appear in the summary, and total matches."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_a")
    _write_bug_report(fn_dir, "confirmed_dynamic")
    _write_classification(fn_dir, "p1", "real_bug")
    _write_classification(fn_dir, "p2", "spurious")
    _write_classification(fn_dir, "p3", "unresolved")
    _write_classification(fn_dir, "p4", "latent")

    p._synthesize_sibling_cex_summary("drv", "fn_a")

    summary = _read_report(fn_dir)["sibling_cex_summary"]
    assert summary["total_cexes_validated"] == 4
    assert summary["real_bug_count"] == 1
    assert summary["spurious_count"] == 1
    assert summary["unresolved_count"] == 1
    assert summary["latent_count"] == 1


def test_summary_outcome_case_insensitive(tmp_path):
    """Outcome strings are lowercased before bucketing."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_b")
    _write_bug_report(fn_dir, "confirmed_dynamic")
    _write_classification(fn_dir, "p1", "REAL_BUG")
    _write_classification(fn_dir, "p2", "Spurious")
    _write_classification(fn_dir, "p3", "  UNRESOLVED  ")

    p._synthesize_sibling_cex_summary("drv", "fn_b")
    summary = _read_report(fn_dir)["sibling_cex_summary"]
    assert summary["real_bug_count"] == 1
    assert summary["spurious_count"] == 1
    assert summary["unresolved_count"] == 1


# ---------------------------------------------------------------------------
# Instability gate
# ---------------------------------------------------------------------------

def test_instability_signal_false_for_single_cex(tmp_path):
    """N=1 cannot be 'unstable' — definition requires N > 1."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_solo")
    _write_bug_report(fn_dir, "confirmed_system_entry")
    _write_classification(fn_dir, "p1", "unresolved")

    p._synthesize_sibling_cex_summary("drv", "fn_solo")
    report = _read_report(fn_dir)
    assert report["sibling_cex_summary"]["instability_signal"] is False
    # No downgrade either — confidence unchanged
    assert report["confidence"] == "confirmed_system_entry"


def test_instability_signal_true_when_majority_unconfirmed(tmp_path):
    """2 unresolved + 1 real out of 3 → (unres+spur)=2 >= ceil(3/2)=1 → unstable."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_unstable")
    _write_bug_report(fn_dir, "confirmed_system_entry")
    _write_classification(fn_dir, "p1", "real_bug")
    _write_classification(fn_dir, "p2", "unresolved")
    _write_classification(fn_dir, "p3", "spurious")

    p._synthesize_sibling_cex_summary("drv", "fn_unstable")
    assert _read_report(fn_dir)["sibling_cex_summary"]["instability_signal"] is True


def test_instability_signal_false_when_majority_real(tmp_path):
    """3 real + 1 unresolved out of 4 → (unres+spur)=1 < n_total//2=2 → stable."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_stable")
    _write_bug_report(fn_dir, "confirmed_dynamic")
    for i in range(3):
        _write_classification(fn_dir, f"p{i}", "real_bug")
    _write_classification(fn_dir, "p3", "unresolved")

    p._synthesize_sibling_cex_summary("drv", "fn_stable")
    assert _read_report(fn_dir)["sibling_cex_summary"]["instability_signal"] is False


# ---------------------------------------------------------------------------
# Confidence downgrade
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("conf", ["confirmed_dynamic", "confirmed_system_entry", "realistic"])
def test_downgrade_high_confidence_when_unstable(tmp_path, conf):
    """High-confidence tier + instability_signal=True → confidence flipped
    to 'unlikely' and a SIBLING-CEX INSTABILITY note added."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", f"fn_{conf}")
    _write_bug_report(fn_dir, conf, reasoning_trail="initial trail")
    _write_classification(fn_dir, "p1", "real_bug")
    _write_classification(fn_dir, "p2", "unresolved")
    _write_classification(fn_dir, "p3", "spurious")

    p._synthesize_sibling_cex_summary("drv", f"fn_{conf}")

    report = _read_report(fn_dir)
    assert report["confidence"] == "unlikely"
    assert "[SIBLING-CEX INSTABILITY]" in report["reasoning_trail"]
    assert "initial trail" in report["reasoning_trail"]  # original preserved
    assert f"downgraded from '{conf}'" in report["reasoning_trail"]


def test_no_downgrade_when_already_unlikely(tmp_path):
    """If confidence is already 'unlikely', leave it alone — nothing to
    demote to and adding the note would be misleading."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_unlikely")
    _write_bug_report(fn_dir, "unlikely", reasoning_trail="already unlikely")
    _write_classification(fn_dir, "p1", "unresolved")
    _write_classification(fn_dir, "p2", "spurious")

    p._synthesize_sibling_cex_summary("drv", "fn_unlikely")
    report = _read_report(fn_dir)
    assert report["confidence"] == "unlikely"
    assert "[SIBLING-CEX INSTABILITY]" not in report.get("reasoning_trail", "")


def test_no_downgrade_when_stable_high_confidence(tmp_path):
    """Stable verdict + high confidence → confidence preserved."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_solid")
    _write_bug_report(fn_dir, "confirmed_dynamic", reasoning_trail="solid")
    for i in range(3):
        _write_classification(fn_dir, f"p{i}", "real_bug")

    p._synthesize_sibling_cex_summary("drv", "fn_solid")
    report = _read_report(fn_dir)
    assert report["confidence"] == "confirmed_dynamic"
    assert "[SIBLING-CEX INSTABILITY]" not in report.get("reasoning_trail", "")


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------

def test_no_op_when_bug_report_missing(tmp_path):
    """If bug_report.json doesn't exist, the synthesis returns cleanly
    without crashing or creating one."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_no_report")
    _write_classification(fn_dir, "p1", "real_bug")

    p._synthesize_sibling_cex_summary("drv", "fn_no_report")  # must not raise
    assert not (fn_dir / "bug_report.json").exists()


def test_no_op_when_no_classifications(tmp_path):
    """No per-CEx records → no summary added, no downgrade applied."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_no_cls")
    _write_bug_report(fn_dir, "confirmed_dynamic")

    p._synthesize_sibling_cex_summary("drv", "fn_no_cls")
    report = _read_report(fn_dir)
    assert "sibling_cex_summary" not in report
    assert report["confidence"] == "confirmed_dynamic"


def test_skips_unparseable_classification_files(tmp_path):
    """A malformed classification JSON must not blow up the synthesis;
    it should be skipped and the valid siblings still counted."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_partial")
    _write_bug_report(fn_dir, "confirmed_dynamic")
    _write_classification(fn_dir, "p1", "real_bug")
    _write_classification(fn_dir, "p2", "unresolved")
    (fn_dir / "classifications" / "p3.json").write_text("{not valid json")

    p._synthesize_sibling_cex_summary("drv", "fn_partial")
    summary = _read_report(fn_dir)["sibling_cex_summary"]
    assert summary["total_cexes_validated"] == 2  # the malformed one dropped


def test_classification_without_outcome_skipped(tmp_path):
    """A classification record lacking an outcome string is skipped, not
    counted in any bucket."""
    p = _make_pipeline(tmp_path)
    fn_dir = p.store._fn_dir("drv", "fn_outcomeless")
    _write_bug_report(fn_dir, "confirmed_dynamic")
    cls_dir = fn_dir / "classifications"
    cls_dir.mkdir(parents=True, exist_ok=True)
    (cls_dir / "p1.json").write_text(json.dumps({"classification": {}}))
    (cls_dir / "p2.json").write_text(json.dumps({"classification": {"outcome": "real_bug"}}))

    p._synthesize_sibling_cex_summary("drv", "fn_outcomeless")
    summary = _read_report(fn_dir)["sibling_cex_summary"]
    assert summary["total_cexes_validated"] == 1
    assert summary["real_bug_count"] == 1
