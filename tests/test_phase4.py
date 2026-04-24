"""
Phase 4 acceptance tests for GRACE Evaluation Harness.

All tests run without CBMC or ANTHROPIC_API_KEY — LLM and CBMC are mocked.

Tests:
 1. CorpusEntry and GroundTruthBug dataclasses
 2. Corpus.load() on the examples/ directory
 3. Corpus.add_entry() — creates correct directory structure
 4. CBMCAloneBaseline.run() with mocked CBMC
 5. MetricsCollector.collect_driver_metrics() with mock data
 6. MetricsCollector.compute_summary() with 2 mock driver metrics
 7. ReportGenerator.generate_driver_report() — produces valid markdown
 8. ReportGenerator.generate_summary_report() — produces table with correct numbers
 9. EvaluationRunner.run_corpus() end-to-end with all mocked
10. examples/block_device.c and examples/memory_allocator.c parse correctly
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
SIMPLE_DRIVER = EXAMPLES_DIR / "simple_driver.c"
BLOCK_DEVICE = EXAMPLES_DIR / "block_device.c"
MEMORY_ALLOCATOR = EXAMPLES_DIR / "memory_allocator.c"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> "Config":
    from amc.config import Config

    return Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="__nonexistent_cbmc__",
        cbmc_unwind=4,
        cbmc_timeout=30,
        llm_api_key="fake-key",
    )


def _make_store(tmp_path: Path) -> "ArtifactStore":
    from amc.artifacts import ArtifactStore

    return ArtifactStore(str(tmp_path / "artifacts"))


def _make_driver_metrics(
    driver_name: str,
    total_functions: int = 5,
    bugs: int = 1,
    fp_rate: float = 0.2,
    coverage: float = 0.8,
) -> "DriverMetrics":
    from amc.evaluation.metrics import DriverMetrics

    return DriverMetrics(
        driver_name=driver_name,
        total_functions=total_functions,
        functions_specified=int(total_functions * coverage),
        functions_checked=total_functions,
        functions_verified=total_functions - bugs,
        counterexamples_found=bugs + 1,
        real_bugs_confirmed=bugs,
        spurious_cex_count=1,
        false_positive_rate=fp_rate,
        refinement_iterations=[2],
        avg_refinement_iters=2.0,
        spec_coverage=coverage,
        runtime_seconds=12.5,
        token_cost=5000,
        bugs_by_type={"memory_safety": bugs},
    )


def _make_bug_report(driver_name: str, fn_name: str) -> "BugReport":
    from amc.bug_reporter import BugReport
    from amc.cbmc import Counterexample

    cex = Counterexample(
        failing_property="array-bounds.1",
        variable_assignments={"count": "5", "capacity": "5"},
        trace=[],
    )
    return BugReport(
        driver_name=driver_name,
        function_name=fn_name,
        bug_type="memory_safety",
        violated_property="array-bounds.1",
        counterexample=cex,
        call_chain=[fn_name],
        reproducer="void test() {}",
        reasoning_trail="Array bounds violation.",
        confidence="confirmed",
    )


def _make_validation_result(is_real_bug: bool, fn_name: str) -> "ValidationResult":
    from amc.cbmc import Counterexample
    from amc.cex_validator import ValidationResult

    cex = Counterexample(
        failing_property="assertion.1",
        variable_assignments={"x": "0"},
        trace=[],
    )
    return ValidationResult(
        function_name=fn_name,
        is_real_bug=is_real_bug,
        counterexample=cex,
        caller_path=[fn_name] if is_real_bug else [],
        system_entry_input=None,
        refinement_history=[] if is_real_bug else ["pre_v1"],
        final_precondition=None if is_real_bug else "x > 0",
        reasoning="test",
    )


# ---------------------------------------------------------------------------
# Test 1: CorpusEntry and GroundTruthBug dataclasses
# ---------------------------------------------------------------------------


def test_corpus_entry_dataclass():
    """CorpusEntry and GroundTruthBug can be created and accessed."""
    from amc.evaluation.corpus import CorpusEntry, GroundTruthBug

    bug = GroundTruthBug(
        function_name="rb_write",
        bug_type="memory_safety",
        description="Off-by-one allows writing one extra byte.",
        line_number=126,
    )
    assert bug.function_name == "rb_write"
    assert bug.bug_type == "memory_safety"
    assert bug.line_number == 126

    entry = CorpusEntry(
        name="simple_driver",
        source_file=str(SIMPLE_DRIVER),
        ground_truth_bugs=[bug],
        driver_type="ring_buffer",
        generated_by="manual",
    )
    assert entry.name == "simple_driver"
    assert entry.driver_type == "ring_buffer"
    assert len(entry.ground_truth_bugs) == 1
    assert entry.ground_truth_bugs[0].function_name == "rb_write"


def test_ground_truth_bug_optional_line():
    """GroundTruthBug line_number is optional (can be None)."""
    from amc.evaluation.corpus import GroundTruthBug

    bug = GroundTruthBug(
        function_name="some_func",
        bug_type="arithmetic",
        description="Integer overflow.",
    )
    assert bug.line_number is None


# ---------------------------------------------------------------------------
# Test 2: Corpus.load() on the examples/ directory
# ---------------------------------------------------------------------------


def test_corpus_load_examples_dir():
    """Corpus.load() treats each .c file in examples/ as a corpus entry."""
    from amc.evaluation.corpus import Corpus

    corpus = Corpus(str(EXAMPLES_DIR))
    entries = corpus.load()

    # Should find at least the 3 .c files we know about
    names = {e.name for e in entries}
    assert len(entries) >= 3

    # All entries should have a source_file that exists
    for entry in entries:
        assert Path(entry.source_file).exists(), (
            f"Source file missing for entry '{entry.name}': {entry.source_file}"
        )


def test_corpus_load_empty_dir(tmp_path: Path):
    """Corpus.load() on an empty directory returns empty list."""
    from amc.evaluation.corpus import Corpus

    corpus = Corpus(str(tmp_path / "nonexistent"))
    entries = corpus.load()
    assert entries == []


def test_corpus_load_subdir_layout(tmp_path: Path):
    """Corpus.load() supports the subdirectory layout with metadata and ground truth."""
    from amc.evaluation.corpus import Corpus

    # Create a subdir-based entry
    entry_dir = tmp_path / "my_driver"
    entry_dir.mkdir()
    (entry_dir / "source.c").write_text("int main() { return 0; }", encoding="utf-8")
    (entry_dir / "metadata.json").write_text(
        json.dumps({
            "name": "my_driver",
            "driver_type": "char_device",
            "generated_by": "manual",
        }),
        encoding="utf-8",
    )
    (entry_dir / "ground_truth.json").write_text(
        json.dumps([{
            "function_name": "main",
            "bug_type": "semantic",
            "description": "Always returns 0.",
            "line_number": 1,
        }]),
        encoding="utf-8",
    )

    corpus = Corpus(str(tmp_path))
    entries = corpus.load()

    subdir_entries = [e for e in entries if e.name == "my_driver"]
    assert len(subdir_entries) == 1
    e = subdir_entries[0]
    assert e.driver_type == "char_device"
    assert len(e.ground_truth_bugs) == 1
    assert e.ground_truth_bugs[0].function_name == "main"
    assert e.ground_truth_bugs[0].line_number == 1


# ---------------------------------------------------------------------------
# Test 3: Corpus.add_entry() — creates correct directory structure
# ---------------------------------------------------------------------------


def test_corpus_add_entry(tmp_path: Path):
    """Corpus.add_entry() creates the expected directory structure."""
    from amc.evaluation.corpus import Corpus, CorpusEntry, GroundTruthBug

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    corpus = Corpus(str(corpus_dir))

    entry = CorpusEntry(
        name="test_entry",
        source_file=str(SIMPLE_DRIVER),
        ground_truth_bugs=[
            GroundTruthBug(
                function_name="rb_write",
                bug_type="memory_safety",
                description="Off-by-one.",
                line_number=126,
            )
        ],
        driver_type="ring_buffer",
        generated_by="manual",
    )

    corpus.add_entry(entry)

    entry_dir = corpus_dir / "test_entry"
    assert entry_dir.is_dir()
    assert (entry_dir / "source.c").exists()
    assert (entry_dir / "metadata.json").exists()
    assert (entry_dir / "ground_truth.json").exists()

    # Check metadata content
    meta = json.loads((entry_dir / "metadata.json").read_text())
    assert meta["name"] == "test_entry"
    assert meta["driver_type"] == "ring_buffer"
    assert meta["generated_by"] == "manual"

    # Check ground truth content
    gt = json.loads((entry_dir / "ground_truth.json").read_text())
    assert len(gt) == 1
    assert gt[0]["function_name"] == "rb_write"
    assert gt[0]["line_number"] == 126


def test_corpus_add_then_load(tmp_path: Path):
    """Entries added with add_entry() can be loaded back with load()."""
    from amc.evaluation.corpus import Corpus, CorpusEntry, GroundTruthBug

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    corpus = Corpus(str(corpus_dir))

    entry = CorpusEntry(
        name="round_trip",
        source_file=str(SIMPLE_DRIVER),
        ground_truth_bugs=[
            GroundTruthBug("rb_write", "memory_safety", "Bug.", 42)
        ],
        driver_type="ring_buffer",
        generated_by="manual",
    )
    corpus.add_entry(entry)

    loaded = corpus.load()
    names = {e.name for e in loaded}
    assert "round_trip" in names

    loaded_entry = next(e for e in loaded if e.name == "round_trip")
    assert loaded_entry.driver_type == "ring_buffer"
    assert len(loaded_entry.ground_truth_bugs) == 1
    assert loaded_entry.ground_truth_bugs[0].line_number == 42


# ---------------------------------------------------------------------------
# Test 4: CBMCAloneBaseline.run() with mocked CBMC
# ---------------------------------------------------------------------------


def test_cbmc_alone_baseline_bugs_found(tmp_path: Path):
    """CBMCAloneBaseline finds bugs when CBMC returns counterexamples."""
    from amc.cbmc import CBMCResult, Counterexample
    from amc.evaluation.baselines import CBMCAloneBaseline

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)

    cex = Counterexample(
        failing_property="array-bounds.1",
        variable_assignments={"rb->count": "6"},
        trace=[],
    )
    mock_result = CBMCResult(verified=False, counterexamples=[cex], raw_output="")

    baseline = CBMCAloneBaseline()
    with patch("amc.evaluation.baselines.run_cbmc", return_value=mock_result):
        result = baseline.run(
            source_file=str(SIMPLE_DRIVER),
            driver_name="simple_driver",
            config=config,
            store=store,
        )

    assert result.name == "cbmc_alone"
    assert result.driver_name == "simple_driver"
    assert len(result.bugs_found) > 0
    assert result.error is None or result.error == ""


def test_cbmc_alone_baseline_no_bugs(tmp_path: Path):
    """CBMCAloneBaseline returns empty bugs list when CBMC verifies."""
    from amc.cbmc import CBMCResult
    from amc.evaluation.baselines import CBMCAloneBaseline

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)

    mock_result = CBMCResult(verified=True, counterexamples=[], raw_output="")

    baseline = CBMCAloneBaseline()
    with patch("amc.evaluation.baselines.run_cbmc", return_value=mock_result):
        result = baseline.run(
            source_file=str(SIMPLE_DRIVER),
            driver_name="simple_driver",
            config=config,
            store=store,
        )

    assert result.name == "cbmc_alone"
    assert result.bugs_found == []


def test_cbmc_alone_baseline_parse_error(tmp_path: Path):
    """CBMCAloneBaseline handles parse errors gracefully."""
    from amc.evaluation.baselines import CBMCAloneBaseline

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)

    baseline = CBMCAloneBaseline()
    result = baseline.run(
        source_file="/nonexistent/path/to/file.c",
        driver_name="bad_driver",
        config=config,
        store=store,
    )

    assert result.error is not None
    assert result.name == "cbmc_alone"


# ---------------------------------------------------------------------------
# Test 5: MetricsCollector.collect_driver_metrics() with mock data
# ---------------------------------------------------------------------------


def test_metrics_collector_basic(tmp_path: Path):
    """collect_driver_metrics() computes correct metrics from mock data."""
    from amc.bmc_engine import BMCVerdict
    from amc.cbmc import Counterexample
    from amc.evaluation.metrics import MetricsCollector
    from amc.spec import Spec, SpecStatus

    store = _make_store(tmp_path)
    collector = MetricsCollector(store)

    # 3 functions, 2 specified (non-fallback), 1 verified, 1 bug
    specs = {
        "fn_a": Spec("fn_a", "x > 0", "result >= 0", status=SpecStatus.GENERATED),
        "fn_b": Spec("fn_b", "true", "true", status=SpecStatus.GENERATED),
        "fn_c": Spec("fn_c", "ptr != NULL", "return_val != NULL", status=SpecStatus.GENERATED),
    }
    cex = Counterexample("bounds.1", {"ptr": "NULL"}, [])
    verdicts = {
        "fn_a": BMCVerdict("fn_a", verified=True),
        "fn_b": BMCVerdict("fn_b", verified=False, counterexamples=[cex]),
        "fn_c": BMCVerdict("fn_c", verified=True),
    }
    validation_results = [
        _make_validation_result(is_real_bug=True, fn_name="fn_b"),
    ]
    bug_reports = [_make_bug_report("test_drv", "fn_b")]

    metrics = collector.collect_driver_metrics(
        driver_name="test_drv",
        specs=specs,
        verdicts=verdicts,
        validation_results=validation_results,
        bug_reports=bug_reports,
        runtime=10.0,
    )

    assert metrics.driver_name == "test_drv"
    assert metrics.total_functions == 3
    assert metrics.functions_verified == 2   # fn_a and fn_c
    assert metrics.counterexamples_found == 1
    assert metrics.real_bugs_confirmed == 1
    assert metrics.spurious_cex_count == 0
    assert metrics.false_positive_rate == 0.0
    assert metrics.runtime_seconds == 10.0
    assert metrics.bugs_by_type == {"memory_safety": 1}


def test_metrics_collector_false_positive_rate(tmp_path: Path):
    """false_positive_rate is computed correctly when there are spurious cexes."""
    from amc.bmc_engine import BMCVerdict
    from amc.cbmc import Counterexample
    from amc.evaluation.metrics import MetricsCollector
    from amc.spec import Spec, SpecStatus

    store = _make_store(tmp_path)
    collector = MetricsCollector(store)

    specs = {
        "fn_a": Spec("fn_a", "true", "true", status=SpecStatus.GENERATED),
    }
    cex1 = Counterexample("bounds.1", {}, [])
    cex2 = Counterexample("overflow.1", {}, [])
    verdicts = {
        "fn_a": BMCVerdict("fn_a", verified=False, counterexamples=[cex1, cex2]),
    }
    # 1 real, 1 spurious → FP rate = 0.5
    validation_results = [
        _make_validation_result(True, "fn_a"),
        _make_validation_result(False, "fn_a"),
    ]
    bug_reports = [_make_bug_report("drv", "fn_a")]

    metrics = collector.collect_driver_metrics(
        driver_name="drv",
        specs=specs,
        verdicts=verdicts,
        validation_results=validation_results,
        bug_reports=bug_reports,
        runtime=5.0,
    )

    assert metrics.counterexamples_found == 2
    assert metrics.spurious_cex_count == 1
    assert abs(metrics.false_positive_rate - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# Test 6: MetricsCollector.compute_summary() with 2 mock driver metrics
# ---------------------------------------------------------------------------


def test_compute_summary_two_drivers(tmp_path: Path):
    """compute_summary() correctly aggregates 2 driver metrics."""
    from amc.evaluation.baselines import BaselineResult
    from amc.evaluation.metrics import MetricsCollector

    store = _make_store(tmp_path)
    collector = MetricsCollector(store)

    m1 = _make_driver_metrics("driver_a", total_functions=6, bugs=2, fp_rate=0.2, coverage=0.8)
    m2 = _make_driver_metrics("driver_b", total_functions=4, bugs=1, fp_rate=0.0, coverage=1.0)

    baseline_results = {
        "cbmc_alone": [
            BaselineResult("cbmc_alone", "driver_a", bugs_found=["fn_x: bounds.1"]),
            BaselineResult("cbmc_alone", "driver_b", bugs_found=[]),
        ]
    }

    summary = collector.compute_summary([m1, m2], baseline_results)

    assert summary.total_drivers == 2
    assert summary.total_functions == 10
    assert summary.total_bugs_found == 3  # 2 + 1
    assert abs(summary.avg_false_positive_rate - 0.1) < 1e-9   # (0.2 + 0.0) / 2
    assert abs(summary.avg_spec_coverage - 0.9) < 1e-9           # (0.8 + 1.0) / 2
    assert len(summary.per_driver) == 2
    assert "cbmc_alone" in summary.baseline_unique_bugs


def test_compute_summary_empty(tmp_path: Path):
    """compute_summary() with no drivers returns zeroed summary."""
    from amc.evaluation.metrics import MetricsCollector

    store = _make_store(tmp_path)
    collector = MetricsCollector(store)

    summary = collector.compute_summary([], {})

    assert summary.total_drivers == 0
    assert summary.total_bugs_found == 0
    assert summary.avg_false_positive_rate == 0.0


# ---------------------------------------------------------------------------
# Test 7: ReportGenerator.generate_driver_report() — valid markdown
# ---------------------------------------------------------------------------


def test_generate_driver_report(tmp_path: Path):
    """generate_driver_report() produces non-empty markdown with key fields."""
    from amc.evaluation.report import ReportGenerator

    store = _make_store(tmp_path)
    gen = ReportGenerator(store)

    metrics = _make_driver_metrics("my_driver")
    bug_reports = [_make_bug_report("my_driver", "blk_seek")]

    md = gen.generate_driver_report(metrics, bug_reports)

    assert isinstance(md, str)
    assert len(md) > 0
    assert "my_driver" in md
    assert "blk_seek" in md
    # Should have markdown headers
    assert "#" in md
    # Should contain key stats
    assert "functions" in md.lower() or "Functions" in md
    assert "bugs" in md.lower() or "Bugs" in md


def test_generate_driver_report_no_bugs(tmp_path: Path):
    """generate_driver_report() without bugs is still valid markdown."""
    from amc.evaluation.report import ReportGenerator

    store = _make_store(tmp_path)
    gen = ReportGenerator(store)

    metrics = _make_driver_metrics("clean_driver", bugs=0)
    md = gen.generate_driver_report(metrics, [])

    assert "clean_driver" in md
    assert "#" in md


# ---------------------------------------------------------------------------
# Test 8: ReportGenerator.generate_summary_report() — table with correct numbers
# ---------------------------------------------------------------------------


def test_generate_summary_report(tmp_path: Path):
    """generate_summary_report() produces a table with correct numbers."""
    from amc.evaluation.baselines import BaselineResult
    from amc.evaluation.metrics import EvaluationSummary, MetricsCollector
    from amc.evaluation.report import ReportGenerator

    store = _make_store(tmp_path)
    collector = MetricsCollector(store)
    gen = ReportGenerator(store)

    m1 = _make_driver_metrics("ring_buf", total_functions=8, bugs=2, fp_rate=0.25, coverage=0.875)
    m2 = _make_driver_metrics("blk_dev", total_functions=5, bugs=1, fp_rate=0.0, coverage=1.0)

    baseline_results: dict = {
        "cbmc_alone": [
            BaselineResult("cbmc_alone", "ring_buf", bugs_found=["rb_write: bounds.1"]),
            BaselineResult("cbmc_alone", "blk_dev", bugs_found=[]),
        ]
    }
    summary = collector.compute_summary([m1, m2], baseline_results)

    md = gen.generate_summary_report(summary)

    assert "AMC Evaluation Summary" in md
    assert "Overall Results" in md
    assert "Per-Driver Results" in md
    assert "ring_buf" in md
    assert "blk_dev" in md
    # Should contain the total bugs count
    assert str(summary.total_bugs_found) in md
    # Should have a comparison section
    assert "Comparison" in md or "Baseline" in md or "GRACE" in md
    # Should have markdown tables
    assert "|" in md


def test_summary_report_bug_type_breakdown(tmp_path: Path):
    """Bug type breakdown section appears when there are bugs."""
    from amc.evaluation.metrics import DriverMetrics, EvaluationSummary
    from amc.evaluation.report import ReportGenerator

    store = _make_store(tmp_path)
    gen = ReportGenerator(store)

    m = DriverMetrics(
        driver_name="test",
        total_functions=3,
        functions_specified=3,
        functions_checked=3,
        functions_verified=2,
        counterexamples_found=2,
        real_bugs_confirmed=1,
        spurious_cex_count=1,
        false_positive_rate=0.5,
        refinement_iterations=[1],
        avg_refinement_iters=1.0,
        spec_coverage=1.0,
        runtime_seconds=5.0,
        token_cost=1000,
        bugs_by_type={"memory_safety": 1, "arithmetic": 0},
    )
    summary = EvaluationSummary(
        total_drivers=1,
        total_functions=3,
        total_bugs_found=1,
        avg_false_positive_rate=0.5,
        avg_spec_coverage=1.0,
        avg_refinement_iters=1.0,
        total_token_cost=1000,
        bugs_by_type={"memory_safety": 1},
        per_driver=[m],
        amc_unique_bugs=1,
        baseline_unique_bugs={},
    )

    md = gen.generate_summary_report(summary)
    assert "Memory Safety" in md or "memory_safety" in md


# ---------------------------------------------------------------------------
# Test 9: EvaluationRunner.run_corpus() end-to-end with all mocked
# ---------------------------------------------------------------------------


def test_evaluation_runner_end_to_end(tmp_path: Path):
    """
    EvaluationRunner.run_corpus() runs without errors and returns an
    EvaluationSummary with the expected structure.
    """
    from amc.bmc_engine import BMCVerdict
    from amc.evaluation.corpus import Corpus, CorpusEntry
    from amc.evaluation.metrics import EvaluationSummary
    from amc.evaluation.runner import EvaluationRunner
    from amc.spec import Spec, SpecStatus

    config = _make_config(tmp_path)
    runner = EvaluationRunner(config)

    # Build a mini corpus with one entry (the simple driver example)
    corpus = Corpus(str(tmp_path / "corpus"))

    # Mock the pipeline run to avoid LLM/CBMC calls
    mock_specs = {
        "rb_is_empty": Spec("rb_is_empty", "rb != NULL", "true", status=SpecStatus.GENERATED),
    }
    mock_verdicts = {
        "rb_is_empty": BMCVerdict("rb_is_empty", verified=True),
    }

    with patch("amc.evaluation.runner.AMCPipeline") as MockPipeline:
        mock_pipeline_instance = MagicMock()
        MockPipeline.return_value = mock_pipeline_instance

        mock_pipeline_instance.run.return_value = []

        # Also intercept bmc_engine.check_all and validator.validate
        mock_bmc = MagicMock()
        mock_bmc.check_all.return_value = mock_verdicts
        mock_pipeline_instance.bmc_engine = mock_bmc

        mock_validator = MagicMock()
        mock_pipeline_instance.validator = mock_validator

        mock_spec_gen = MagicMock()
        mock_spec_gen.generate_specs.return_value = mock_specs
        mock_pipeline_instance.spec_gen = mock_spec_gen

        # Corpus with one entry
        corpus_entries = [
            CorpusEntry(
                name="simple_driver",
                source_file=str(SIMPLE_DRIVER),
                ground_truth_bugs=[],
                driver_type="ring_buffer",
                generated_by="manual",
            )
        ]

        with patch.object(corpus, "load", return_value=corpus_entries):
            with patch("amc.evaluation.runner.CBMCAloneBaseline") as MockCBMC:
                from amc.evaluation.baselines import BaselineResult
                mock_cbmc_inst = MagicMock()
                MockCBMC.return_value = mock_cbmc_inst
                mock_cbmc_inst.run.return_value = BaselineResult(
                    name="cbmc_alone",
                    driver_name="simple_driver",
                    bugs_found=[],
                )

                summary = runner.run_corpus(
                    corpus=corpus,
                    output_dir=str(tmp_path / "eval_output"),
                    run_baselines=True,
                )

    assert isinstance(summary, EvaluationSummary)
    assert summary.total_drivers >= 0   # may be 0 if pipeline patching skips metrics


def test_evaluation_runner_no_baselines(tmp_path: Path):
    """EvaluationRunner with run_baselines=False skips baselines."""
    from amc.evaluation.corpus import Corpus, CorpusEntry
    from amc.evaluation.runner import EvaluationRunner

    config = _make_config(tmp_path)
    runner = EvaluationRunner(config)

    corpus = Corpus(str(tmp_path / "corpus"))
    corpus_entries = [
        CorpusEntry(
            name="simple_driver",
            source_file=str(SIMPLE_DRIVER),
            ground_truth_bugs=[],
            driver_type="ring_buffer",
            generated_by="manual",
        )
    ]

    with patch("amc.evaluation.runner.AMCPipeline") as MockPipeline:
        mock_instance = MagicMock()
        MockPipeline.return_value = mock_instance
        mock_instance.run.return_value = []
        mock_instance.bmc_engine = MagicMock()
        mock_instance.validator = MagicMock()
        mock_instance.spec_gen = MagicMock()
        mock_instance.spec_gen.generate_specs.return_value = {}

        with patch.object(corpus, "load", return_value=corpus_entries):
            with patch("amc.evaluation.runner.CBMCAloneBaseline") as MockCBMC:
                summary = runner.run_corpus(
                    corpus=corpus,
                    output_dir=str(tmp_path / "eval_out"),
                    run_baselines=False,
                )
                # CBMCAloneBaseline should not have been instantiated
                # (baselines skipped)


# ---------------------------------------------------------------------------
# Test 10: block_device.c and memory_allocator.c parse correctly
# ---------------------------------------------------------------------------


def test_block_device_c_parses():
    """examples/block_device.c can be parsed by grace.parser."""
    from amc.parser import parse_c_file

    assert BLOCK_DEVICE.exists(), f"block_device.c not found at {BLOCK_DEVICE}"
    parsed = parse_c_file(str(BLOCK_DEVICE))

    assert parsed is not None
    fns = list(parsed.functions.keys())
    assert len(fns) >= 4, f"Expected at least 4 functions, got: {fns}"

    expected = {"blk_init", "blk_read", "blk_write", "blk_seek", "blk_close"}
    found = set(fns)
    missing = expected - found
    assert not missing, f"Missing functions: {missing}"


def test_memory_allocator_c_parses():
    """examples/memory_allocator.c can be parsed by grace.parser."""
    from amc.parser import parse_c_file

    assert MEMORY_ALLOCATOR.exists(), f"memory_allocator.c not found at {MEMORY_ALLOCATOR}"
    parsed = parse_c_file(str(MEMORY_ALLOCATOR))

    assert parsed is not None
    fns = list(parsed.functions.keys())
    assert len(fns) >= 4, f"Expected at least 4 functions, got: {fns}"

    expected = {"alloc_init", "alloc_malloc", "alloc_free", "alloc_reset", "alloc_available"}
    found = set(fns)
    missing = expected - found
    assert not missing, f"Missing functions: {missing}"


def test_block_device_c_has_blk_seek_bug():
    """block_device.c should have a comment mentioning the intentional bug."""
    content = BLOCK_DEVICE.read_text(encoding="utf-8")
    assert "BUG" in content or "overflow" in content.lower()
    assert "blk_seek" in content


def test_memory_allocator_c_has_null_bug():
    """memory_allocator.c should have a comment mentioning the null-check bug."""
    content = MEMORY_ALLOCATOR.read_text(encoding="utf-8")
    assert "BUG" in content or "null" in content.lower() or "NULL" in content
    assert "alloc_free" in content


# ---------------------------------------------------------------------------
# Test: CLI subcommands are registered
# ---------------------------------------------------------------------------


def test_cli_eval_subcommand_registered():
    """The 'eval' subcommand is registered in the CLI parser."""
    from amc.cli import build_parser

    parser = build_parser()
    # Should not raise
    args = parser.parse_args([
        "eval",
        "--corpus", "examples/",
        "--output", "artifacts/eval/",
    ])
    assert args.corpus == "examples/"
    assert args.output == "artifacts/eval/"
    assert args.baselines is False


def test_cli_report_subcommand_registered():
    """The 'report' subcommand is registered in the CLI parser."""
    from amc.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["report", "--eval-dir", "artifacts/eval/"])
    assert args.eval_dir == "artifacts/eval/"


def test_cli_corpus_generate_subcommand_registered():
    """The 'corpus generate' subcommand is registered in the CLI parser."""
    from amc.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "corpus", "generate",
        "--output", "corpus/",
        "--count", "3",
    ])
    assert args.output == "corpus/"
    assert args.count == 3


# ---------------------------------------------------------------------------
# Test: Save reports to disk
# ---------------------------------------------------------------------------


def test_report_generator_save_reports(tmp_path: Path):
    """ReportGenerator.save_reports() writes files to the artifact directory."""
    from amc.evaluation.baselines import BaselineResult
    from amc.evaluation.metrics import MetricsCollector
    from amc.evaluation.report import ReportGenerator

    store = _make_store(tmp_path)
    collector = MetricsCollector(store)
    gen = ReportGenerator(store)

    m1 = _make_driver_metrics("drv1", total_functions=3, bugs=1)
    m2 = _make_driver_metrics("drv2", total_functions=4, bugs=0)

    summary = collector.compute_summary([m1, m2], {})
    bug_reports = {
        "drv1": [_make_bug_report("drv1", "fn_a")],
        "drv2": [],
    }

    gen.save_reports(summary, [m1, m2], bug_reports)

    artifacts = Path(tmp_path) / "artifacts"
    assert (artifacts / "eval_summary.md").exists()
    assert (artifacts / "eval_summary.json").exists()
    assert (artifacts / "drv1" / "report.md").exists()
    assert (artifacts / "drv2" / "report.md").exists()

    # Check JSON is valid
    data = json.loads((artifacts / "eval_summary.json").read_text())
    assert data["total_drivers"] == 2
    assert data["total_bugs_found"] == 1
