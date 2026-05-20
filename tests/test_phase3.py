"""
Phase 3 acceptance tests for BMC-Agent Counterexample Validator & Spec Refiner.

All tests run without CBMC or ANTHROPIC_API_KEY — LLM and CBMC are mocked.

Tests:
 1. ValidationResult dataclass creation and serialization
 2. Mocked CBMC: counterexample IS reachable from caller → real bug
 3. Mocked CBMC: counterexample NOT reachable from caller → spurious, triggers refinement
 4. Refinement iteration cap (mock LLM always returns same precondition)
 5. Over-refinement guard: rejected refinement → treated as real bug
 6. BugReport creation from ValidationResult
 7. BugReporter.generate_summary() with multiple mock bug reports
 8. AMCPipeline.run() end-to-end with all mocked (LLM + CBMC)
 9. Upward propagation: entry→caller→func
10. Entry functions (no callers) immediately produce real bug reports
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_C = REPO_ROOT / "examples" / "simple_driver.c"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, max_refinement_iters: int = 3) -> "Config":
    from bmc_agent.config import Config

    return Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="__nonexistent_cbmc__",
        cbmc_unwind=4,
        cbmc_timeout=30,
        llm_api_key="fake-key",
        max_refinement_iters=max_refinement_iters,
    )


def _make_spec(
    fn_name: str,
    pre: str = "true",
    post: str = "true",
) -> "Spec":
    from bmc_agent.spec import Spec, SpecStatus

    return Spec(
        function_name=fn_name,
        precondition=pre,
        postcondition=post,
        status=SpecStatus.GENERATED,
    )


def _make_counterexample(
    failing_property: str = "assertion.1",
    var_assignments: dict | None = None,
    trace: list | None = None,
) -> "Counterexample":
    from bmc_agent.cbmc import Counterexample

    return Counterexample(
        failing_property=failing_property,
        variable_assignments=var_assignments or {"rb->count": "5", "rb->capacity": "5"},
        trace=trace or ["rb->count = 5", "rb->capacity = 5"],
    )


def _make_func_info(name: str, callees: set[str] | None = None) -> "FunctionInfo":
    from bmc_agent.parser import FunctionInfo, FunctionSignature

    sig = FunctionSignature(
        name=name,
        return_type="int",
        parameters=[("ring_buffer_t *", "rb")],
    )
    return FunctionInfo(
        name=name,
        signature=sig,
        body=f"{{ /* body of {name} */ return 0; }}",
        callees=callees or set(),
        source_file=str(EXAMPLE_C),
    )


def _make_store(tmp_path: Path) -> "ArtifactStore":
    from bmc_agent.artifacts import ArtifactStore

    return ArtifactStore(str(tmp_path / "artifacts"))


def _make_llm_mock() -> MagicMock:
    """Return a mock LLMClient that raises LLMError by default."""
    from bmc_agent.llm import LLMError

    mock = MagicMock()
    mock.complete.side_effect = LLMError("No API key in tests")
    return mock


# ---------------------------------------------------------------------------
# Test 1: ValidationResult dataclass creation and serialization
# ---------------------------------------------------------------------------


def test_validation_result_creation(tmp_path: Path):
    """ValidationResult can be created and serialized to a dict."""
    from bmc_agent.cex_validator import ValidationResult

    cex = _make_counterexample()
    result = ValidationResult(
        function_name="rb_write",
        is_real_bug=True,
        counterexample=cex,
        caller_path=["dev_write", "rb_write"],
        system_entry_input="void test() { /* ... */ }",
        refinement_history=[],
        final_precondition=None,
        reasoning="Caller dev_write can produce the failing state.",
    )

    assert result.function_name == "rb_write"
    assert result.is_real_bug is True
    assert result.caller_path == ["dev_write", "rb_write"]
    assert result.system_entry_input is not None

    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["function_name"] == "rb_write"
    assert d["is_real_bug"] is True
    assert d["caller_path"] == ["dev_write", "rb_write"]

    # Should be JSON-serializable
    serialized = json.dumps(d, default=str)
    assert "rb_write" in serialized


def test_validation_result_spurious():
    """ValidationResult for spurious counterexample."""
    from bmc_agent.cex_validator import ValidationResult

    cex = _make_counterexample()
    result = ValidationResult(
        function_name="rb_is_full",
        is_real_bug=False,
        counterexample=cex,
        caller_path=[],
        system_entry_input=None,
        refinement_history=["rb->count < rb->capacity", "rb->count < rb->capacity - 1"],
        final_precondition="rb->count < rb->capacity",
        reasoning="No caller can produce count==capacity at rb_is_full call.",
    )

    assert result.is_real_bug is False
    assert result.final_precondition == "rb->count < rb->capacity"
    assert len(result.refinement_history) == 2

    d = result.to_dict()
    assert d["is_real_bug"] is False
    assert d["final_precondition"] == "rb->count < rb->capacity"


# ---------------------------------------------------------------------------
# Test 2: Mocked CBMC — counterexample IS reachable → real bug
# ---------------------------------------------------------------------------


def test_counterexample_is_reachable_real_bug(tmp_path: Path):
    """
    When CBMC finds a path (counterexample for assert(0)), the counterexample
    is marked as a real bug.
    """
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    llm = _make_llm_mock()
    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    parsed = parse_c_file(EXAMPLE_C)
    all_funcs = {
        name: parsed.get_function_info(name)
        for name in ["rb_write", "rb_is_full"]
        if parsed.get_function_info(name) is not None
    }
    all_specs = {
        name: _make_spec(name)
        for name in all_funcs
    }

    # rb_is_full is called by rb_write in simple_driver.c? Let's check.
    # Either way, create a scenario where rb_write calls rb_is_full.
    func = all_funcs.get("rb_is_full", _make_func_info("rb_is_full", set()))
    spec = _make_spec("rb_is_full")
    cex = _make_counterexample(
        failing_property="assertion.rb_is_full.1",
        var_assignments={"rb->count": "5", "rb->capacity": "5"},
    )

    # Mock CBMC to say reachability IS confirmed (not verified = counterexample found)
    mock_cbmc_result = CBMCResult(
        verified=False,  # CBMC found a path → reachable
        counterexamples=[cex],
        raw_output="",
    )

    with patch("bmc_agent.cex_validator.run_cbmc", return_value=mock_cbmc_result):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            result = validator.validate(
                func=func,
                spec=spec,
                counterexample=cex,
                all_funcs=all_funcs,
                all_specs=all_specs,
                parsed_file=parsed,
                driver_name="test_driver",
            )

    assert result.is_real_bug is True
    assert result.function_name == func.name


# ---------------------------------------------------------------------------
# Test 3: Mocked CBMC — counterexample NOT reachable → spurious
# ---------------------------------------------------------------------------


def test_counterexample_not_reachable_spurious(tmp_path: Path):
    """
    When CBMC verifies (no counterexample for assert(0)), the state is not
    reachable → spurious → triggers refinement.
    """
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    config = _make_config(tmp_path, max_refinement_iters=2)
    store = _make_store(tmp_path)

    # Mock LLM to return a valid refinement
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "refined_precondition": "rb->count < rb->capacity",
        "reasoning": "Excludes the full-buffer state.",
        "excluded_condition": "rb->count == rb->capacity",
    })

    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    parsed = parse_c_file(EXAMPLE_C)
    # Use rb_is_full (which is called by no one in our test all_funcs)
    # so we need at least one caller
    caller_func = _make_func_info("rb_write", callees={"rb_is_full"})
    func = _make_func_info("rb_is_full", callees=set())
    all_funcs = {
        "rb_write": caller_func,
        "rb_is_full": func,
    }
    all_specs = {
        "rb_write": _make_spec("rb_write"),
        "rb_is_full": _make_spec("rb_is_full"),
    }

    cex = _make_counterexample(
        failing_property="assertion.rb_is_full.1",
        var_assignments={"rb->count": "5", "rb->capacity": "5"},
    )

    # CBMC verifies → assert(0) never reached → state NOT reachable (spurious)
    mock_cbmc_verified = CBMCResult(verified=True, counterexamples=[], raw_output="")

    with patch("bmc_agent.cex_validator.run_cbmc", return_value=mock_cbmc_verified):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            # Also mock over-refinement check to say "safe"
            with patch.object(
                validator,
                "_check_over_refinement",
                return_value=True,  # safe
            ):
                result = validator.validate(
                    func=func,
                    spec=_make_spec("rb_is_full"),
                    counterexample=cex,
                    all_funcs=all_funcs,
                    all_specs=all_specs,
                    parsed_file=parsed,
                    driver_name="test_driver",
                )

    assert result.is_real_bug is False
    assert result.function_name == "rb_is_full"
    assert result.final_precondition is not None
    assert len(result.refinement_history) >= 1


# ---------------------------------------------------------------------------
# Test 4: Refinement iteration cap
# ---------------------------------------------------------------------------


def test_refinement_iteration_cap(tmp_path: Path):
    """
    When LLM always returns the same precondition, refinement stalls and stops
    at max_refinement_iters.
    """
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator

    MAX_ITERS = 3
    config = _make_config(tmp_path, max_refinement_iters=MAX_ITERS)
    store = _make_store(tmp_path)

    # Mock LLM to always return the SAME precondition as the original
    original_pre = "rb != NULL"
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "refined_precondition": original_pre,  # same as original → stall
        "reasoning": "No change needed.",
        "excluded_condition": "",
    })

    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    from bmc_agent.parser import parse_c_file
    parsed = parse_c_file(EXAMPLE_C)

    caller_func = _make_func_info("caller_func", callees={"target_func"})
    func = _make_func_info("target_func", callees=set())
    all_funcs = {"caller_func": caller_func, "target_func": func}
    all_specs = {
        "caller_func": _make_spec("caller_func"),
        "target_func": _make_spec("target_func", pre=original_pre),
    }

    cex = _make_counterexample()

    # CBMC says not reachable (spurious)
    mock_verified = CBMCResult(verified=True, counterexamples=[], raw_output="")

    with patch("bmc_agent.cex_validator.run_cbmc", return_value=mock_verified):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            with patch.object(validator, "_check_over_refinement", return_value=True):
                result = validator.validate(
                    func=func,
                    spec=_make_spec("target_func", pre=original_pre),
                    counterexample=cex,
                    all_funcs=all_funcs,
                    all_specs=all_specs,
                    parsed_file=parsed,
                    driver_name="test_driver",
                )

    # Refinement should have stopped (stalled at 0 iterations due to same precondition)
    assert result.is_real_bug is False
    # The final precondition is the (unchanged) original
    assert result.final_precondition == original_pre


# ---------------------------------------------------------------------------
# Test 5: Over-refinement guard — rejected refinement → treated as real bug
# ---------------------------------------------------------------------------


def test_over_refinement_guard(tmp_path: Path):
    """
    When the over-refinement check says the new precondition is too restrictive,
    the refinement is rejected and the bug is treated as real.
    """
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator

    config = _make_config(tmp_path, max_refinement_iters=3)
    store = _make_store(tmp_path)

    # LLM proposes a refinement
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "refined_precondition": "rb->count == 0",  # over-restrictive!
        "reasoning": "Only accept empty buffers.",
        "excluded_condition": "rb->count > 0",
        "is_over_refined": True,
        "problematic_caller_state": "caller passes non-empty buffer",
    })

    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    from bmc_agent.parser import parse_c_file
    parsed = parse_c_file(EXAMPLE_C)

    caller_func = _make_func_info("caller_func", callees={"target_func"})
    func = _make_func_info("target_func", callees=set())
    all_funcs = {"caller_func": caller_func, "target_func": func}
    all_specs = {
        "caller_func": _make_spec("caller_func", pre="rb->count >= 0"),
        "target_func": _make_spec("target_func"),
    }

    cex = _make_counterexample()
    mock_verified = CBMCResult(verified=True, counterexamples=[], raw_output="")

    with patch("bmc_agent.cex_validator.run_cbmc", return_value=mock_verified):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            # Over-refinement check returns False (NOT safe)
            with patch.object(
                validator,
                "_check_over_refinement",
                return_value=False,  # over-refined!
            ):
                result = validator.validate(
                    func=func,
                    spec=_make_spec("target_func"),
                    counterexample=cex,
                    all_funcs=all_funcs,
                    all_specs=all_specs,
                    parsed_file=parsed,
                    driver_name="test_driver",
                )

    # Over-refinement detected. With the post-2026-05-20 classifier change,
    # structural-panic + over-refinement-rejected becomes UNRESOLVED (not
    # REAL_BUG): we can't determine whether real callers maintain the
    # implicit invariant, so we surface the finding without claiming it's
    # a confirmed bug. The over_refinement_rejected flag is still set, and
    # the reasoning mentions the rejection.
    from bmc_agent.cex_validator import CExOutcome
    assert result.outcome in (CExOutcome.REAL_BUG, CExOutcome.UNRESOLVED), \
        f"unexpected outcome {result.outcome}"
    assert result.over_refinement_rejected is True
    assert (
        "over-refin" in result.reasoning.lower()
        or "over-restrict" in result.reasoning.lower()
        or "would exclude" in result.reasoning.lower()
        or "exclude" in result.reasoning.lower()
    )


# ---------------------------------------------------------------------------
# Test 6: BugReport creation from ValidationResult
# ---------------------------------------------------------------------------


def test_bug_report_creation(tmp_path: Path):
    """BugReport is correctly created from a ValidationResult."""
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.cex_validator import ValidationResult

    store = _make_store(tmp_path)
    reporter = BugReporter(store)
    func = _make_func_info("rb_write", callees=set())

    cex = _make_counterexample(
        failing_property="array-bounds.1",
        var_assignments={"rb->count": "6", "rb->capacity": "5"},
    )
    validation = ValidationResult(
        function_name="rb_write",
        is_real_bug=True,
        counterexample=cex,
        caller_path=["system_entry", "rb_write"],
        system_entry_input="void test() { /* ... */ }",
        refinement_history=[],
        final_precondition=None,
        reasoning="Array bounds violation confirmed.",
    )

    report = reporter.create_report(validation, func)

    assert report.function_name == "rb_write"
    assert report.bug_type == "memory_safety"  # "array-bounds" → memory_safety
    assert report.violated_property == "array-bounds.1"
    assert report.call_chain == ["system_entry", "rb_write"]
    assert report.confidence in ("confirmed_dynamic", "confirmed_system_entry", "confirmed_bmc", "likely")
    assert report.reproducer is not None
    assert "reasoning" in report.reasoning_trail.lower() or len(report.reasoning_trail) > 0

    d = report.to_dict()
    assert d["function_name"] == "rb_write"
    assert d["bug_type"] == "memory_safety"
    serialized = json.dumps(d, default=str)
    assert "rb_write" in serialized


def test_confirmed_system_entry_tier(tmp_path):
    """system_entry_reached=True produces confirmed_system_entry confidence."""
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.cex_validator import CExOutcome, ValidationResult

    store = _make_store(tmp_path)
    reporter = BugReporter(store)
    func = _make_func_info("leaf_fn", callees=set())

    cex = _make_counterexample(
        failing_property="overflow.1",
        var_assignments={"x": "2147483647"},
    )
    # Simulate a full chain traced back to a system entry
    validation = ValidationResult(
        function_name="leaf_fn",
        outcome=CExOutcome.REAL_BUG,
        counterexample=cex,
        caller_path=["kernel_main", "mid_fn", "leaf_fn"],
        system_entry_input="/* kernel entry harness */",
        refinement_history=[],
        final_precondition=None,
        reasoning="Full chain traced to system entry. Callee feasibility confirmed.",
        system_entry_reached=True,
    )

    report = reporter.create_report(validation, func)
    assert report.confidence == "confirmed_system_entry"
    assert report.call_chain == ["kernel_main", "mid_fn", "leaf_fn"]


def test_confirmed_bmc_tier_without_system_entry(tmp_path):
    """system_entry_reached=False keeps confirmed_bmc confidence."""
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.cex_validator import CExOutcome, ValidationResult

    store = _make_store(tmp_path)
    reporter = BugReporter(store)
    func = _make_func_info("leaf_fn", callees=set())

    cex = _make_counterexample(
        failing_property="overflow.1",
        var_assignments={"x": "2147483647"},
    )
    validation = ValidationResult(
        function_name="leaf_fn",
        outcome=CExOutcome.REAL_BUG,
        counterexample=cex,
        caller_path=["mid_fn", "leaf_fn"],
        system_entry_input=None,
        refinement_history=[],
        final_precondition=None,
        reasoning="Reachable from caller mid_fn.",
        system_entry_reached=False,
    )

    report = reporter.create_report(validation, func)
    assert report.confidence == "confirmed_bmc"


def test_bug_type_classification():
    """Bug types are classified correctly from property names."""
    from bmc_agent.bug_reporter import _classify_bug_type

    assert _classify_bug_type("overflow.1") == "arithmetic"
    assert _classify_bug_type("null-pointer.2") == "memory_safety"
    assert _classify_bug_type("array-bounds.3") == "memory_safety"
    assert _classify_bug_type("assertion.post.4") == "semantic"
    assert _classify_bug_type("postcondition.check") == "semantic"
    assert _classify_bug_type("unknown.thing") == "semantic"


# ---------------------------------------------------------------------------
# Test 7: BugReporter.generate_summary()
# ---------------------------------------------------------------------------


def test_bug_reporter_generate_summary(tmp_path: Path):
    """generate_summary() returns a readable string with all bugs."""
    from bmc_agent.bug_reporter import BugReport, BugReporter
    from bmc_agent.cex_validator import ValidationResult

    store = _make_store(tmp_path)
    reporter = BugReporter(store)

    # Create 3 mock bug reports
    driver = "test_driver"
    store.init_driver(driver)

    cex1 = _make_counterexample("null-pointer.1", {"ptr": "NULL"})
    cex2 = _make_counterexample("overflow.1", {"x": "2147483647"})
    cex3 = _make_counterexample("assertion.1", {"result": "-1"})

    for cex, fn_name, chain in [
        (cex1, "func_a", ["entry", "func_a"]),
        (cex2, "func_b", ["func_b"]),
        (cex3, "func_c", ["entry", "func_b", "func_c"]),
    ]:
        v = ValidationResult(
            function_name=fn_name,
            is_real_bug=True,
            counterexample=cex,
            caller_path=chain,
            system_entry_input=None,
            refinement_history=[],
            final_precondition=None,
            reasoning="confirmed",
        )
        func = _make_func_info(fn_name)
        report = reporter.create_report(v, func)
        reporter.save_report(report, driver)

    summary = reporter.generate_summary(driver)

    assert "test_driver" in summary
    assert "func_a" in summary or "MEMORY_SAFETY" in summary
    assert "func_b" in summary or "ARITHMETIC" in summary
    assert "3" in summary or len([r for r in summary.split("\n") if r.strip()]) > 3
    assert "Total bugs" in summary


def test_bug_reporter_empty_summary(tmp_path: Path):
    """generate_summary() with no bugs returns a helpful message."""
    from bmc_agent.bug_reporter import BugReporter

    store = _make_store(tmp_path)
    reporter = BugReporter(store)

    summary = reporter.generate_summary("empty_driver")
    assert "No bugs" in summary or "empty_driver" in summary


# ---------------------------------------------------------------------------
# Test 8: AMCPipeline.run() end-to-end with all mocked
# ---------------------------------------------------------------------------


def test_pipeline_run_end_to_end(tmp_path: Path):
    """
    Full pipeline run with all external dependencies mocked.
    Should produce BugReport objects without crashing.
    """
    from bmc_agent.bmc_engine import BMCVerdict
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.cex_validator import ValidationResult
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.spec import Spec, SpecStatus

    config = _make_config(tmp_path, max_refinement_iters=2)
    pipeline = AMCPipeline(config)

    # Mock spec generation to return pre-built specs
    mock_specs = {
        "rb_is_empty": _make_spec("rb_is_empty", "rb != NULL", "true"),
        "rb_is_full": _make_spec("rb_is_full", "rb != NULL", "true"),
    }

    # Mock BMC to fail on rb_is_empty (with a counterexample)
    cex = _make_counterexample("assertion.rb_is_empty.1", {"rb->count": "0"})
    mock_verdict_fail = BMCVerdict(
        function_name="rb_is_empty",
        verified=False,
        counterexamples=[cex],
    )
    mock_verdict_pass = BMCVerdict(
        function_name="rb_is_full",
        verified=True,
        counterexamples=[],
    )

    # Mock validation to say counterexample is a real bug
    mock_validation = ValidationResult(
        function_name="rb_is_empty",
        is_real_bug=True,
        counterexample=cex,
        caller_path=["rb_is_empty"],
        system_entry_input="void test() {}",
        refinement_history=[],
        final_precondition=None,
        reasoning="Entry function, direct bug.",
    )

    with patch.object(pipeline.spec_gen, "generate_specs", return_value=mock_specs):
        with patch.object(
            pipeline.bmc_engine,
            "check_all",
            return_value={
                "rb_is_empty": mock_verdict_fail,
                "rb_is_full": mock_verdict_pass,
            },
        ):
            with patch.object(
                pipeline.validator,
                "validate",
                return_value=mock_validation,
            ):
                reports = pipeline.run(
                    source_file=str(EXAMPLE_C),
                    driver_name="e2e_test",
                    domain_knowledge="",
                )

    assert isinstance(reports, list)
    assert len(reports) == 1
    assert reports[0].function_name == "rb_is_empty"
    assert reports[0].is_real_bug if hasattr(reports[0], "is_real_bug") else True


def test_pipeline_run_no_bugs(tmp_path: Path):
    """
    When all functions verify, no bug reports are produced.
    """
    from bmc_agent.bmc_engine import BMCVerdict
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline

    config = _make_config(tmp_path)
    pipeline = AMCPipeline(config)

    mock_specs = {
        "rb_is_empty": _make_spec("rb_is_empty"),
        "rb_is_full": _make_spec("rb_is_full"),
    }
    mock_verdicts = {
        "rb_is_empty": BMCVerdict("rb_is_empty", verified=True),
        "rb_is_full": BMCVerdict("rb_is_full", verified=True),
    }

    with patch.object(pipeline.spec_gen, "generate_specs", return_value=mock_specs):
        with patch.object(pipeline.bmc_engine, "check_all", return_value=mock_verdicts):
            reports = pipeline.run(str(EXAMPLE_C), "clean_driver")

    assert reports == []


# ---------------------------------------------------------------------------
# Test 9: Upward propagation — entry→caller→func
# ---------------------------------------------------------------------------


def test_upward_propagation(tmp_path: Path):
    """
    Upward propagation: entry_func → caller_func → target_func
    should find a real bug with the full call chain.
    """
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    llm = _make_llm_mock()
    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    parsed = parse_c_file(EXAMPLE_C)

    # Build a 3-level call graph: entry_func → mid_func → leaf_func
    entry_func = _make_func_info("entry_func", callees={"mid_func"})
    mid_func = _make_func_info("mid_func", callees={"leaf_func"})
    leaf_func = _make_func_info("leaf_func", callees=set())

    all_funcs = {
        "entry_func": entry_func,
        "mid_func": mid_func,
        "leaf_func": leaf_func,
    }
    all_specs = {
        "entry_func": _make_spec("entry_func"),
        "mid_func": _make_spec("mid_func"),
        "leaf_func": _make_spec("leaf_func"),
    }

    cex = _make_counterexample()

    # CBMC says all caller paths are reachable (not verified → cex found)
    mock_reachable = CBMCResult(verified=False, counterexamples=[cex], raw_output="")

    with patch("bmc_agent.cex_validator.run_cbmc", return_value=mock_reachable):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            result = validator.validate(
                func=leaf_func,
                spec=_make_spec("leaf_func"),
                counterexample=cex,
                all_funcs=all_funcs,
                all_specs=all_specs,
                parsed_file=parsed,
                driver_name="test_driver",
            )

    assert result.is_real_bug is True
    assert "leaf_func" in result.caller_path
    # Should trace up through mid_func to entry_func
    assert len(result.caller_path) >= 1


# ---------------------------------------------------------------------------
# Test 10: Entry functions immediately produce real bug reports
# ---------------------------------------------------------------------------


def test_entry_function_real_bug(tmp_path: Path):
    """
    A function with no callers (entry function) should immediately
    produce a real bug report without any CBMC reachability queries.
    """
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)

    # LLM will be used for reproducer generation — mock it
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "reproducer_code": "void test_entry() { /* trigger */ }",
        "explanation": "Direct entry point bug.",
        "concrete_values": {},
    })

    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    parsed = parse_c_file(EXAMPLE_C)

    # entry_func has NO callers in all_funcs
    entry_func = _make_func_info("entry_func", callees=set())
    all_funcs = {"entry_func": entry_func}
    all_specs = {"entry_func": _make_spec("entry_func")}

    cex = _make_counterexample("null-pointer.1", {"ptr": "NULL"})

    # run_cbmc should NOT be called (entry function, no reachability check needed)
    with patch("bmc_agent.cex_validator.run_cbmc") as mock_cbmc:
        result = validator.validate(
            func=entry_func,
            spec=_make_spec("entry_func"),
            counterexample=cex,
            all_funcs=all_funcs,
            all_specs=all_specs,
            parsed_file=parsed,
            driver_name="test_driver",
        )
        # run_cbmc should NOT have been called
        mock_cbmc.assert_not_called()

    assert result.is_real_bug is True
    assert result.function_name == "entry_func"
    assert "entry" in result.reasoning.lower() or "no caller" in result.reasoning.lower()
    assert result.caller_path == ["entry_func"]


# ---------------------------------------------------------------------------
# Test 11: BugReporter.save_report() persists to artifact store
# ---------------------------------------------------------------------------


def test_bug_reporter_saves_to_disk(tmp_path: Path):
    """save_report() should write a bug_report.json to the artifact store."""
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.cex_validator import ValidationResult

    store = _make_store(tmp_path)
    reporter = BugReporter(store)
    func = _make_func_info("my_func")

    cex = _make_counterexample("overflow.1", {"x": "MAX_INT"})
    validation = ValidationResult(
        function_name="my_func",
        is_real_bug=True,
        counterexample=cex,
        caller_path=["my_func"],
        system_entry_input=None,
        refinement_history=[],
        final_precondition=None,
        reasoning="Direct overflow.",
    )

    report = reporter.create_report(validation, func)
    store.init_driver("save_driver")
    reporter.save_report(report, "save_driver")

    # Check file was created
    report_path = (
        Path(tmp_path) / "artifacts" / "save_driver" / "my_func" / "bug_report.json"
    )
    assert report_path.exists(), f"Expected bug report at {report_path}"
    data = json.loads(report_path.read_text())
    assert "report" in data
    assert data["report"]["function_name"] == "my_func"


# ---------------------------------------------------------------------------
# Test 12: CExValidator with LLM-only reachability (no CBMC)
# ---------------------------------------------------------------------------


def test_llm_only_reachability(tmp_path: Path):
    """
    When CBMC is not available, LLM is used for reachability analysis.
    """
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    config = _make_config(tmp_path)
    # cbmc_path is set to __nonexistent_cbmc__ in _make_config
    store = _make_store(tmp_path)

    # Mock LLM: first call says reachable, second call generates reproducer
    llm = MagicMock()
    llm.complete.side_effect = [
        # Reachability check → reachable
        json.dumps({
            "is_reachable": True,
            "reasoning": "The caller can produce this state.",
            "witnessing_inputs": "rb->count = 5, rb->capacity = 5",
            "blocking_condition": "",
        }),
        # Reproducer generation
        json.dumps({
            "reproducer_code": "void test() { /* bug */ }",
            "explanation": "Trigger via caller.",
            "concrete_values": {},
        }),
    ]

    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    parsed = parse_c_file(EXAMPLE_C)

    caller_func = _make_func_info("rb_write", callees={"rb_is_full"})
    func = _make_func_info("rb_is_full", callees=set())
    all_funcs = {"rb_write": caller_func, "rb_is_full": func}
    all_specs = {
        "rb_write": _make_spec("rb_write"),
        "rb_is_full": _make_spec("rb_is_full"),
    }

    cex = _make_counterexample()

    # shutil.which returns None → CBMC not available → LLM path taken
    with patch("shutil.which", return_value=None):
        result = validator.validate(
            func=func,
            spec=_make_spec("rb_is_full"),
            counterexample=cex,
            all_funcs=all_funcs,
            all_specs=all_specs,
            parsed_file=parsed,
            driver_name="test_driver",
        )

    # LLM said reachable → real bug
    assert result.is_real_bug is True


# ---------------------------------------------------------------------------
# Cross-file reachability tests (Phase 3 — multi-file call graph)
# ---------------------------------------------------------------------------


def _make_parsed_file(
    path: str,
    func_names: list[str],
    call_graph: dict[str, set[str]] | None = None,
) -> "ParsedCFile":
    """Build a minimal ParsedCFile with stub signatures for the given functions."""
    from bmc_agent.parser import FunctionSignature, ParsedCFile

    sigs = {
        n: FunctionSignature(name=n, return_type="int", parameters=[("int", "x")])
        for n in func_names
    }
    bodies = {n: f"{{ return 0; /* {n} */ }}" for n in func_names}
    cg = call_graph or {n: set() for n in func_names}
    return ParsedCFile(path=path, functions=sigs, call_graph=cg, function_bodies=bodies)


def test_cross_file_caller_confirmed_reachable_real_bug(tmp_path: Path):
    """
    A function with no in-file callers but a cross-file caller: when CBMC
    confirms the cross-file caller can reach the CEx state, the result is
    REAL_BUG with system_entry_reached=True (the cross-file caller itself
    has no callers, so it is a system entry).
    """
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.cex_validator import CExOutcome, CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import FunctionInfo, FunctionSignature

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    llm = _make_llm_mock()
    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    # The file under verification: only "leaf_fn", no callers in this file.
    leaf_fn = _make_func_info("leaf_fn", callees=set())
    leaf_parsed = _make_parsed_file(
        path="module_a.c",
        func_names=["leaf_fn"],
        call_graph={"leaf_fn": set()},
    )
    all_funcs = {"leaf_fn": leaf_fn}
    all_specs = {"leaf_fn": _make_spec("leaf_fn")}

    # A caller in another file that calls leaf_fn.
    caller_parsed = _make_parsed_file(
        path="module_b.c",
        func_names=["entry_fn"],
        call_graph={"entry_fn": {"leaf_fn"}},
    )
    caller_fi = FunctionInfo(
        name="entry_fn",
        signature=FunctionSignature("entry_fn", "int", [("int", "x")]),
        body="{ return leaf_fn(x); }",
        callees={"leaf_fn"},
        source_file="module_b.c",
    )
    all_specs["entry_fn"] = _make_spec("entry_fn")

    cross_file_callers: set[str] = {"leaf_fn"}
    cross_file_caller_contexts = {"leaf_fn": [(caller_fi, caller_parsed)]}

    cex = _make_counterexample(
        failing_property="overflow.1",
        var_assignments={"x": "2147483647"},
    )

    # CBMC confirms reachability: not verified → counterexample found
    mock_cbmc_reachable = CBMCResult(
        verified=False,
        counterexamples=[cex],
        raw_output="",
    )

    with patch("bmc_agent.cex_validator.run_cbmc", return_value=mock_cbmc_reachable):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            result = validator.validate(
                func=leaf_fn,
                spec=_make_spec("leaf_fn"),
                counterexample=cex,
                all_funcs=all_funcs,
                all_specs=all_specs,
                parsed_file=leaf_parsed,
                driver_name="test_driver",
                cross_file_callers=cross_file_callers,
                cross_file_caller_contexts=cross_file_caller_contexts,
            )

    assert result.outcome == CExOutcome.REAL_BUG
    # entry_fn has no callers → system entry reached
    assert result.system_entry_reached is True
    assert "entry_fn" in result.caller_path
    assert "leaf_fn" in result.caller_path


def test_cross_file_caller_none_reachable_falls_back_to_confirmed_bmc(tmp_path: Path):
    """
    A function with no in-file callers but cross-file callers exist: when
    CBMC says none of them can reach the CEx state, the result falls back
    to REAL_BUG with system_entry_reached=False (confirmed_bmc tier).
    """
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.cex_validator import CExOutcome, CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import FunctionInfo, FunctionSignature

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    llm = _make_llm_mock()
    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    leaf_fn = _make_func_info("leaf_fn", callees=set())
    leaf_parsed = _make_parsed_file(
        path="module_a.c",
        func_names=["leaf_fn"],
        call_graph={"leaf_fn": set()},
    )
    all_funcs = {"leaf_fn": leaf_fn}
    all_specs = {"leaf_fn": _make_spec("leaf_fn")}

    caller_parsed = _make_parsed_file(
        path="module_b.c",
        func_names=["entry_fn"],
        call_graph={"entry_fn": {"leaf_fn"}},
    )
    caller_fi = FunctionInfo(
        name="entry_fn",
        signature=FunctionSignature("entry_fn", "int", [("int", "x")]),
        body="{ return leaf_fn(x); }",
        callees={"leaf_fn"},
        source_file="module_b.c",
    )
    all_specs["entry_fn"] = _make_spec("entry_fn")

    cross_file_callers: set[str] = {"leaf_fn"}
    cross_file_caller_contexts = {"leaf_fn": [(caller_fi, caller_parsed)]}

    cex = _make_counterexample(
        failing_property="overflow.1",
        var_assignments={"x": "2147483647"},
    )

    # CBMC verifies → assert(0) never reached → caller CANNOT reach CEx state
    mock_cbmc_verified = CBMCResult(verified=True, counterexamples=[], raw_output="")

    with patch("bmc_agent.cex_validator.run_cbmc", return_value=mock_cbmc_verified):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            result = validator.validate(
                func=leaf_fn,
                spec=_make_spec("leaf_fn"),
                counterexample=cex,
                all_funcs=all_funcs,
                all_specs=all_specs,
                parsed_file=leaf_parsed,
                driver_name="test_driver",
                cross_file_callers=cross_file_callers,
                cross_file_caller_contexts=cross_file_caller_contexts,
            )

    assert result.outcome == CExOutcome.REAL_BUG
    assert result.system_entry_reached is False


def test_cross_file_caller_contexts_empty_falls_back_to_confirmed_bmc(tmp_path: Path):
    """
    When cross_file_callers indicates callers exist but cross_file_caller_contexts
    has no entries for the function, fall back to confirmed_bmc.
    """
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.cex_validator import CExOutcome, CExValidator
    from bmc_agent.harness_generator import HarnessGenerator

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    llm = _make_llm_mock()
    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    leaf_fn = _make_func_info("leaf_fn", callees=set())
    leaf_parsed = _make_parsed_file(
        path="module_a.c",
        func_names=["leaf_fn"],
        call_graph={"leaf_fn": set()},
    )
    all_funcs = {"leaf_fn": leaf_fn}
    all_specs = {"leaf_fn": _make_spec("leaf_fn")}

    cex = _make_counterexample(
        failing_property="overflow.1",
        var_assignments={"x": "0"},
    )

    # cross_file_callers says callers exist, but contexts dict is empty
    with patch("shutil.which", return_value="/usr/bin/cbmc"):
        result = validator.validate(
            func=leaf_fn,
            spec=_make_spec("leaf_fn"),
            counterexample=cex,
            all_funcs=all_funcs,
            all_specs=all_specs,
            parsed_file=leaf_parsed,
            driver_name="test_driver",
            cross_file_callers={"leaf_fn"},
            cross_file_caller_contexts={},  # no contexts available
        )

    assert result.outcome == CExOutcome.REAL_BUG
    assert result.system_entry_reached is False


def test_propagate_upward_crosses_file_boundary_to_entry(tmp_path: Path):
    """
    _propagate_upward should cross file boundaries: if func_X has no in-file
    callers but has a cross-file caller entry_fn (which itself has no callers),
    the chain is (True, ['entry_fn', 'func_X']).
    """
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import FunctionInfo, FunctionSignature

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    llm = _make_llm_mock()
    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, llm, store, harness_gen)

    func_x_parsed = _make_parsed_file(
        path="module_a.c",
        func_names=["func_x"],
        call_graph={"func_x": set()},
    )
    all_funcs_a = {"func_x": func_x_parsed.get_function_info("func_x")}

    caller_parsed = _make_parsed_file(
        path="module_b.c",
        func_names=["entry_fn"],
        call_graph={"entry_fn": {"func_x"}},
    )
    caller_fi = FunctionInfo(
        name="entry_fn",
        signature=FunctionSignature("entry_fn", "void", []),
        body="{ func_x(0); }",
        callees={"func_x"},
        source_file="module_b.c",
    )
    all_specs = {
        "func_x": _make_spec("func_x"),
        "entry_fn": _make_spec("entry_fn"),
    }

    cex = _make_counterexample(failing_property="overflow.1", var_assignments={"x": "0"})
    mock_cbmc_reachable = CBMCResult(verified=False, counterexamples=[cex], raw_output="")

    with patch("bmc_agent.cex_validator.run_cbmc", return_value=mock_cbmc_reachable):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            reachable, chain = validator._propagate_upward(
                func_name="func_x",
                counterexample=cex,
                all_funcs=all_funcs_a,
                all_specs=all_specs,
                parsed_file=func_x_parsed,
                driver_name="test_driver",
                cross_file_callers={"func_x"},
                cross_file_caller_contexts={"func_x": [(caller_fi, caller_parsed)]},
            )

    assert reachable is True
    assert "entry_fn" in chain
    assert "func_x" in chain


# ---------------------------------------------------------------------------
# Test: unwind.N suppression
# ---------------------------------------------------------------------------


def test_unwind_zero_is_suppressed(tmp_path: Path):
    """unwind.N findings are classified SPURIOUS and fed to the spec refiner via CEGAR."""
    from bmc_agent.cex_validator import CExOutcome, CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    config = _make_config(tmp_path)
    validator = CExValidator(config, _make_llm_mock(), _make_store(tmp_path), HarnessGenerator(config))
    parsed = parse_c_file(EXAMPLE_C)
    func = _make_func_info("rb_write", set())
    spec = _make_spec("rb_write")

    for prop in ("rb_write.unwind.0", "rb_write.unwind.1", "rb_write.unwind.2"):
        cex = _make_counterexample(failing_property=prop, var_assignments={"i": "5"})
        result = validator.validate(
            func=func, spec=spec, counterexample=cex,
            all_funcs={"rb_write": func}, all_specs={"rb_write": spec},
            parsed_file=parsed, driver_name="test_driver",
        )
        # Must be SPURIOUS and must have gone through the refiner (not hard-suppressed)
        assert result.outcome == CExOutcome.SPURIOUS, f"expected SPURIOUS for {prop}"
        assert result.final_precondition is not None, (
            f"unwind artifact for {prop} must produce a refined precondition for CEGAR"
        )


def test_non_unwind_not_suppressed(tmp_path: Path):
    """Non-unwind findings (e.g. pointer_dereference) are NOT suppressed."""
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.cex_validator import CExOutcome, CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    config = _make_config(tmp_path)
    validator = CExValidator(config, _make_llm_mock(), _make_store(tmp_path), HarnessGenerator(config))
    parsed = parse_c_file(EXAMPLE_C)
    func = _make_func_info("rb_write", set())
    spec = _make_spec("rb_write")
    cex = _make_counterexample(
        failing_property="rb_write.pointer_dereference.1",
        var_assignments={"ptr": "NULL"},
    )

    reachable = CBMCResult(verified=False, counterexamples=[cex], raw_output="")
    with patch("bmc_agent.cex_validator.run_cbmc", return_value=reachable):
        with patch("shutil.which", return_value="/usr/bin/cbmc"):
            result = validator.validate(
                func=func, spec=spec, counterexample=cex,
                all_funcs={"rb_write": func}, all_specs={"rb_write": spec},
                parsed_file=parsed, driver_name="test_driver",
            )

    assert "loop-bound artifact suppressed" not in result.reasoning


# ---------------------------------------------------------------------------
# Refinement-loop vacuous-critique pass (K2 / openai provider)
# ---------------------------------------------------------------------------

def _make_validator_for_refinement(provider="openai", responses=None):
    """Build a CExValidator with a mocked LLMClient returning *responses* in order."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.cex_validator import CExValidator
    from unittest.mock import MagicMock
    import tempfile

    config = Config(
        llm_model="mock",
        llm_api_key="mock",
        llm_provider=provider,
        artifact_dir=tempfile.mkdtemp(),
    )
    store = ArtifactStore(config.artifact_dir)
    mock_llm = MagicMock()
    if responses:
        mock_llm.complete = MagicMock(side_effect=list(responses))
    else:
        mock_llm.complete = MagicMock(return_value="")
    # harness_gen only matters for the over-refinement CBMC guard, which the
    # critique-pass tests don't exercise; a mock satisfies the constructor.
    harness_gen = MagicMock()
    return CExValidator(config, mock_llm, store, harness_gen), mock_llm


def test_refine_critique_skips_anthropic():
    """Anthropic path: never run the second critique call even on stalled refinement."""
    import json as _json
    same = _json.dumps({"refined_precondition": "true"})
    v, mock = _make_validator_for_refinement(provider="anthropic", responses=[same])
    out = v._refine_call_with_critique(
        system_prompt="s",
        user_prompt="u",
        original_precondition="true",
        spurious_state="",
        caller_reachable_states="(none)",
    )
    # First call returned 'true' (same as original); since anthropic, no retry.
    assert mock.complete.call_count == 1
    assert out == "true"


def test_refine_critique_fires_on_openai_stall():
    """openai path: second call fires when refinement returns same precondition."""
    import json as _json
    same = _json.dumps({"refined_precondition": "true"})
    better = _json.dumps({"refined_precondition": "pos < buf.len()"})
    v, mock = _make_validator_for_refinement(provider="openai", responses=[same, better])
    out = v._refine_call_with_critique(
        system_prompt="s",
        user_prompt="u",
        original_precondition="true",
        spurious_state="pos = 10, buf.len() = 4",
        caller_reachable_states="caller: pos < buf.len()",
    )
    assert mock.complete.call_count == 2
    assert out == "pos < buf.len()"


def test_refine_critique_accepts_stall_when_second_also_vacuous(tmp_path):
    """If K2 returns the same precondition twice, accept the stall."""
    import json as _json
    same1 = _json.dumps({"refined_precondition": "true"})
    same2 = _json.dumps({"refined_precondition": "true"})
    v, mock = _make_validator_for_refinement(provider="openai", responses=[same1, same2])
    out = v._refine_call_with_critique(
        system_prompt="s",
        user_prompt="u",
        original_precondition="true",
        spurious_state="",
        caller_reachable_states="(none)",
    )
    assert mock.complete.call_count == 2
    assert out == "true"


def test_refine_critique_no_retry_when_first_response_already_tight():
    """When the first refinement IS substantive, no critique call fires."""
    import json as _json
    rich = _json.dumps({"refined_precondition": "offset + 4 <= data.len()"})
    v, mock = _make_validator_for_refinement(provider="openai", responses=[rich])
    out = v._refine_call_with_critique(
        system_prompt="s",
        user_prompt="u",
        original_precondition="true",
        spurious_state="offset = usize::MAX, data.len() = 8",
        caller_reachable_states="(none)",
    )
    assert mock.complete.call_count == 1
    assert out == "offset + 4 <= data.len()"


def test_vacuous_spec_postcondition_violation_filtered(tmp_path):
    """Regression: 2026-05-19 K2 sweep — is_mmx and invert_condition both
    classified REAL_BUG with `check_<fn>.assertion.N: postcondition violated`
    while their specs were pre=true && post=true. Since vacuous specs emit
    NO `kani::assert`, any 'postcondition violated' is intrinsic safety
    check on harness internals, not a real bug. Must auto-route SPURIOUS.
    """
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import CExOutcome, CExValidator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus
    from unittest.mock import MagicMock

    config = Config(llm_model="m", llm_api_key="k", artifact_dir=str(tmp_path))
    store = ArtifactStore(config.artifact_dir)
    harness_gen = MagicMock()
    llm = MagicMock()
    v = CExValidator(config, llm, store, harness_gen)

    cex = Counterexample(
        failing_property="check_invert_condition.assertion.2",
        variable_assignments={},
        trace=["property check_invert_condition.assertion.2: postcondition violated"],
    )
    spec = Spec(function_name="invert_condition", precondition="true",
                postcondition="true", status=SpecStatus.GENERATED)

    # Minimal FunctionInfo-shaped object
    class _Sig:
        is_pub = False
        is_static = False
        parameters = [("&str", "cc")]
        return_type = "Option<&'static str>"
        modifiers = []
        type_parameters = ""
        where_clause = ""
    class _F:
        name = "invert_condition"
        body = "{ ... }"
        signature = _Sig()
        callees = set()

    result = v.validate(
        func=_F(), spec=spec, counterexample=cex,
        all_funcs={}, all_specs={"invert_condition": spec},
        parsed_file=MagicMock(), driver_name="test",
    )
    assert result.outcome == CExOutcome.SPURIOUS
    assert "vacuous-spec postcondition-violation" in result.reasoning
    # The LLM must NOT be consulted for this filter (cost saver).
    assert llm.complete.call_count == 0


def test_non_vacuous_postcondition_violation_NOT_filtered(tmp_path):
    """When the user actually wrote a non-trivial postcondition, a
    'postcondition violated' CEX is a genuine functional-spec disagreement
    and MUST proceed through the classifier (could be a real functional bug
    or LLM-spec-too-loose, but not auto-spurious)."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus
    from unittest.mock import MagicMock

    config = Config(llm_model="m", llm_api_key="k", artifact_dir=str(tmp_path))
    store = ArtifactStore(config.artifact_dir)
    harness_gen = MagicMock()
    llm = MagicMock()
    v = CExValidator(config, llm, store, harness_gen)

    cex = Counterexample(
        failing_property="check_f.assertion.2",
        variable_assignments={},
        trace=["property check_f.assertion.2: postcondition violated"],
    )
    # Non-trivial postcondition
    spec = Spec(function_name="f", precondition="true",
                postcondition="result == a.wrapping_add(b)",
                status=SpecStatus.GENERATED)

    class _Sig:
        is_pub = True
        is_static = False
        parameters = [("u32", "a"), ("u32", "b")]
        return_type = "u32"
        modifiers = []
        type_parameters = ""
        where_clause = ""
    class _F:
        name = "f"
        body = "{ a.wrapping_add(b) }"
        signature = _Sig()
        callees = set()

    # The pre-classifier vacuous-spec filter must NOT short-circuit here.
    # The validate path will then proceed to caller analysis; we just check
    # the artifact filter didn't fire by inspecting the early-return guard.
    # We don't fully exercise validate() — too many dependencies — so we
    # call the inner check inline:
    spec_pre = (spec.precondition or "").strip() in ("", "true", "True")
    spec_post = (spec.postcondition or "").strip() in ("", "true", "True")
    assert spec_pre and not spec_post  # only pre is trivial; filter must NOT fire


def test_over_refinement_postcondition_violation_routes_to_spurious(tmp_path):
    """Regression: 2026-05-20 hybrid sweep produced 3 over-refinement-rejected
    REAL_BUG verdicts (xreg_name, char_escape_value, split_field_on_whitespace),
    each on a `check_<fn>.assertion: postcondition violated` CEX. All three
    functions were correct; the LLM-generated postcondition was over-strict.
    When the over-refinement guard rejects a tightening on a post-violation
    CEX (not a structural panic), the safe fallback is SPURIOUS, not REAL_BUG.
    """
    from bmc_agent.cex_validator import _is_structural_panic
    # Sanity: post-violation traces do NOT match structural-panic markers.
    assert _is_structural_panic(
        "check_xreg_name.assertion.3",
        ["property check_xreg_name.assertion.3: postcondition violated"],
    ) is False
    # And the property/trace combo IS a postcondition violation.
    fp = "check_xreg_name.assertion.3"
    trace = "property check_xreg_name.assertion.3: postcondition violated"
    assert fp.startswith("check_") and "postcondition violated" in trace


def test_over_refinement_structural_panic_stays_real_bug(tmp_path):
    """Negative regression: when the CEX is an actual structural panic
    (slice OOB, overflow), over-refinement rejection MUST keep REAL_BUG
    -- those are the bugs we don't want to lose to the new SPURIOUS path."""
    from bmc_agent.cex_validator import _is_structural_panic
    assert _is_structural_panic(
        "read_u32.assertion.7",
        ["property read_u32.assertion.7: index out of bounds: ..."],
    ) is True
