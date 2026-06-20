"""Tests for the SpecGen Java/JML experiment adapter."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "experiments"
    / "specgen_compare"
    / "run_bmc_jml_specgen.py"
)


def load_runner():
    spec = importlib.util.spec_from_file_location("run_bmc_jml_specgen", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_discover_cases_prefers_case_named_java_file(tmp_path: Path):
    mod = load_runner()
    common = tmp_path / "common"
    oracle = tmp_path / "oracle"
    (common / "Return100").mkdir(parents=True)
    (oracle / "Return100").mkdir(parents=True)
    (common / "Return100" / "Return100.java").write_text("class Return100 {}\n")
    (common / "Return100" / "Return100Driver.java").write_text("class Return100Driver {}\n")
    (oracle / "Return100" / "Return100.java").write_text("class Return100 {}\n")

    cases = mod.discover_cases(common, oracle)

    assert [c.name for c in cases] == ["Return100"]
    assert cases[0].source.endswith("Return100.java")
    assert cases[0].oracle.endswith("Return100.java")


def test_select_cases_reports_missing_case(tmp_path: Path):
    mod = load_runner()
    cases = [mod.SpecGenCase("A", "/a/A.java", "")]
    try:
        mod.select_cases(cases, ["B"], None)
    except SystemExit as exc:
        assert "unknown SpecGen case" in str(exc)
    else:
        raise AssertionError("expected SystemExit for missing case")


def test_summarize_counts_statuses():
    mod = load_runner()
    rows = [
        mod.CaseRow("A", "A.java", "", "passed", True, 1, 1.0, "a", "r", "o"),
        mod.CaseRow("B", "B.java", "", "verification_failed", False, 2, 2.0, "b", "r", "o"),
    ]
    summary = mod.summarize(rows)
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["by_status"] == {"passed": 1, "verification_failed": 1}
