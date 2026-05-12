"""Tests for the Kani Rust backend.

These tests exercise the subprocess wrapper's output parser and the
harness generator's DSL translation without requiring Kani to be
installed locally.  The one test that does invoke ``run_kani`` patches
``shutil.which`` and ``subprocess.run`` so no external process is
launched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from bmc_agent.cbmc import CBMCResult, Counterexample
from bmc_agent.kani import run_kani, _parse_kani_output, _extract_counterexamples
from bmc_agent.backends.kani_backend import (
    KaniBackend,
    _initialiser_for,
    _translate_dsl,
)


# ---------------------------------------------------------------------------
# Lightweight FunctionInfo / Spec / Config doubles
# ---------------------------------------------------------------------------


@dataclass
class _Sig:
    return_type: str
    parameters: list[tuple[str, str]]


@dataclass
class _Func:
    name: str
    signature: _Sig
    body: str
    callees: set = field(default_factory=set)
    source_file: str = "synthetic.rs"


@dataclass
class _Spec:
    function_name: str
    precondition: str = "true"
    postcondition: str = "true"


@dataclass
class _Config:
    kani_path: str = "kani"
    kani_unwind: int = 4
    kani_timeout: int = 120


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


def test_parser_recognises_success():
    raw = "Some preamble\nVERIFICATION:- SUCCESSFUL\nDone.\n"
    result = _parse_kani_output(raw, stderr="", returncode=0)
    assert result.verified is True
    assert result.counterexamples == []
    assert result.error is None
    assert result.raw_output == raw


def test_parser_recognises_failure_with_property_rows():
    raw = (
        "Checking harness check_add::\n"
        "[add.overflow.1] arithmetic overflow on signed addition in add: FAILURE\n"
        "[add.assertion.1] postcondition violated: FAILURE\n"
        "VERIFICATION:- FAILED\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=10)
    assert result.verified is False
    assert len(result.counterexamples) == 2
    props = {c.failing_property for c in result.counterexamples}
    assert props == {"add.overflow.1", "add.assertion.1"}


def test_parser_dedups_repeated_property_rows():
    raw = (
        "[add.overflow.1] arithmetic overflow: FAILURE\n"
        "[add.overflow.1] arithmetic overflow: FAILURE\n"
        "VERIFICATION:- FAILED\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=10)
    assert len(result.counterexamples) == 1


def test_parser_falls_back_to_failed_checks_line():
    raw = (
        "Failed Checks: arithmetic overflow on signed addition\n"
        "VERIFICATION:- FAILED\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=10)
    assert result.verified is False
    assert len(result.counterexamples) == 1
    assert result.counterexamples[0].failing_property == "failed_checks"
    assert "arithmetic overflow" in result.counterexamples[0].trace[0]


def test_parser_missing_verdict_is_error():
    raw = "Compilation failed: unresolved symbol foo\n"
    result = _parse_kani_output(raw, stderr="ld: error: foo", returncode=1)
    assert result.verified is False
    assert result.error is not None
    assert "ld: error: foo" in result.error


# ---------------------------------------------------------------------------
# Subprocess wrapper tests
# ---------------------------------------------------------------------------


def test_run_kani_not_installed_returns_clean_error():
    with patch("bmc_agent.kani.shutil.which", return_value=None):
        result = run_kani(harness_path="harness.rs", kani_path="kani")
    assert isinstance(result, CBMCResult)
    assert result.verified is False
    assert result.error == "kani not found"


def test_run_kani_passes_arguments_through():
    """Smoke-test that --harness, --default-unwind and --output-format are
    composed correctly when kani exists on PATH."""

    captured: dict = {}

    class _Done:
        def __init__(self):
            self.stdout = "VERIFICATION:- SUCCESSFUL\n"
            self.stderr = ""
            self.returncode = 0

    def _fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = list(cmd)
        return _Done()

    with patch("bmc_agent.kani.shutil.which", return_value="/usr/local/bin/kani"), \
         patch("bmc_agent.kani.subprocess.run", side_effect=_fake_run):
        result = run_kani(
            harness_path="harness.rs",
            harness_name="check_add",
            unwind=7,
            timeout=42,
            kani_path="kani",
        )

    assert result.verified is True
    cmd = captured["cmd"]
    assert "harness.rs" in cmd
    assert "--harness" in cmd and "check_add" in cmd
    assert "--default-unwind" in cmd and "7" in cmd
    # We deliberately do NOT pass --output-format: Kani's default (regular)
    # is the only format whose verdict and per-check rows can be parsed
    # unambiguously. See bmc_agent/kani.py for why "old" is unsafe.
    assert "--output-format" not in cmd


# ---------------------------------------------------------------------------
# Harness-generation helper tests
# ---------------------------------------------------------------------------


def test_initialiser_for_primitives():
    assert _initialiser_for("i32") == "kani::any::<i32>()"
    assert _initialiser_for("u64") == "kani::any::<u64>()"
    assert _initialiser_for("bool") == "kani::any::<bool>()"


def test_initialiser_for_raw_pointers():
    assert _initialiser_for("*mut i32") == "kani::any::<usize>() as *mut i32"
    assert _initialiser_for("*const u8") == "kani::any::<usize>() as *const u8"


def test_initialiser_for_unsafe_reference_raises():
    with pytest.raises(NotImplementedError):
        _initialiser_for("&mut i32")
    with pytest.raises(NotImplementedError):
        _initialiser_for("&[u8]")


def test_initialiser_for_unknown_type_raises():
    with pytest.raises(NotImplementedError):
        _initialiser_for("MyStruct")


def test_dsl_translation_predicates():
    assert _translate_dsl("valid(p)") == "!p.is_null()"
    assert _translate_dsl("null(p)") == "p.is_null()"
    assert _translate_dsl("owns(p)") == "!p.is_null()"
    assert _translate_dsl("valid_string(s)") == "!s.is_null()"
    assert _translate_dsl("valid_range(buf, 0, n)") == "!buf.is_null()"


def test_dsl_translation_result_substitution():
    assert _translate_dsl("\\result >= 0") == "result >= 0"


def test_dsl_translation_compound_expressions():
    out = _translate_dsl("valid(a) && valid(b) && \\result >= a")
    assert out == "!a.is_null() && !b.is_null() && result >= a"


def test_dsl_translation_empty_or_true_returns_true():
    assert _translate_dsl("") == "true"
    assert _translate_dsl("true") == "true"


# ---------------------------------------------------------------------------
# End-to-end harness emission
# ---------------------------------------------------------------------------


def test_harness_for_primitive_function():
    backend = KaniBackend(_Config())
    func = _Func(
        name="add",
        signature=_Sig(return_type="i32", parameters=[("i32", "a"), ("i32", "b")]),
        body="fn add(a: i32, b: i32) -> i32 { a + b }",
    )
    spec = _Spec(function_name="add", precondition="true", postcondition="\\result >= a")

    src = backend.generate_harness(func, spec)

    # Sanity: contains the function body, a proof attribute, kani::any() for both params,
    # and a kani::assert with the translated postcondition.
    assert "fn add(a: i32, b: i32) -> i32 { a + b }" in src
    assert "#[kani::proof]" in src
    assert "fn check_add()" in src
    assert "let a: i32 = kani::any::<i32>();" in src
    assert "let b: i32 = kani::any::<i32>();" in src
    # Precondition is "true", so kani::assume is omitted.
    assert "kani::assume" not in src
    # Postcondition references the result binding.
    assert "let result: i32 = add(a, b);" in src
    assert "kani::assert(result >= a" in src


def test_harness_for_pointer_function_with_precondition():
    backend = KaniBackend(_Config())
    func = _Func(
        name="zero_out",
        signature=_Sig(return_type="()", parameters=[("*mut u8", "buf"), ("usize", "n")]),
        body="unsafe fn zero_out(buf: *mut u8, n: usize) { /* … */ }",
    )
    spec = _Spec(
        function_name="zero_out",
        precondition="valid_range(buf, 0, n) && n > 0",
        postcondition="true",
    )

    src = backend.generate_harness(func, spec)

    assert "let buf: *mut u8 = kani::any::<usize>() as *mut u8;" in src
    assert "let n: usize = kani::any::<usize>();" in src
    # Precondition is non-trivial → assume present.
    assert "kani::assume(!buf.is_null() && n > 0);" in src
    # () return → no `let result = …` binding.
    assert "let result" not in src
    # Postcondition is "true" → no assert.
    assert "kani::assert" not in src


def test_backend_check_calls_run_kani(tmp_path):
    """KaniBackend.check delegates to run_kani with the configured paths."""
    harness = tmp_path / "h.rs"
    harness.write_text("// stub")

    captured = {}

    def fake_run_kani(harness_path, harness_name, unwind, timeout, kani_path):
        captured["args"] = (harness_path, harness_name, unwind, timeout, kani_path)
        return CBMCResult(verified=True)

    with patch("bmc_agent.backends.kani_backend.run_kani", side_effect=fake_run_kani):
        backend = KaniBackend(_Config(kani_path="kani", kani_unwind=8, kani_timeout=60))
        result = backend.check(harness, harness_name="check_x")

    assert result.verified is True
    assert captured["args"] == (str(harness), "check_x", 8, 60, "kani")


# ---------------------------------------------------------------------------
# CBMCResult / Counterexample shape compatibility
# ---------------------------------------------------------------------------


def test_kani_results_are_cbmcresult_instances():
    """The pipeline downstream of the backend types against CBMCResult.
    Kani's wrapper must return that exact type, not a sibling class."""
    result = _parse_kani_output("VERIFICATION:- SUCCESSFUL\n", stderr="", returncode=0)
    assert isinstance(result, CBMCResult)
    assert all(isinstance(c, Counterexample) for c in result.counterexamples)


def test_parser_regular_format_check_failure():
    """Real Kani 'regular' output: multi-line Check N: blocks with Status rows."""
    raw = (
        "RESULTS:\n"
        "Check 1: check_add.assertion.1\n"
        "\t - Status: FAILURE\n"
        "\t - Description: \"postcondition violated\"\n"
        "\t - Location: harness.rs:10:5 in function check_add\n"
        "\n"
        "SUMMARY:\n"
        " ** 1 of 1 failed\n"
        "\n"
        "VERIFICATION:- FAILED\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=10)
    assert result.verified is False
    assert len(result.counterexamples) == 1
    cex = result.counterexamples[0]
    assert cex.failing_property == "check_add.assertion.1"
    assert "postcondition violated" in cex.trace[0]


def test_parser_regular_format_check_success():
    """Real Kani 'regular' output for a passing harness — verdict alone is enough."""
    raw = (
        "RESULTS:\n"
        "Check 1: check_x.assertion.1\n"
        "\t - Status: SUCCESS\n"
        "\t - Description: \"assertion holds\"\n"
        "\n"
        "SUMMARY:\n"
        " ** 0 of 1 failed\n"
        "\n"
        "VERIFICATION:- SUCCESSFUL\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=0)
    assert result.verified is True
    assert result.counterexamples == []


def test_parser_old_format_reachability_not_treated_as_failure():
    """Reachability_check FAILURE rows in old-format output indicate the
    assertion was *reached* (a healthy proof), not a property violation.
    The parser must ignore them when the underlying assertion is SUCCESS."""
    raw = (
        "[check_x.assertion.1] line 5 assertion failed: x == 1: SUCCESS\n"
        "[check_x.reachability_check.1] line 5 KANI_CHECK_ID: FAILURE\n"
        "VERIFICATION:- SUCCESSFUL\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=0)
    assert result.verified is True
    # No genuine failure row — only the reachability_check pseudo-row.
    assert result.counterexamples == []
