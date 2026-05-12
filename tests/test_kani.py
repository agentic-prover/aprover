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
    _is_slice_type,
    _param_init_block,
    _reconstruct_fn_definition,
    _slice_element_type,
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


def test_dsl_translation_implication():
    """(A ==> B) must translate to (!(A) || (B)) — Rust has no ==> operator."""
    assert _translate_dsl("(x > 0 ==> y > 0)") == "(!(x > 0) || (y > 0))"


def test_dsl_translation_implication_chained_in_conjunction():
    """The implication rewrite must apply to every paren-wrapped occurrence."""
    spec = (
        "result >= 1 && result <= 4 && "
        "(b < 0xC0 ==> result == 1) && "
        "(b >= 0xC0 && b < 0xE0 ==> result == 2)"
    )
    out = _translate_dsl(spec)
    assert "==>" not in out
    assert "(!(b < 0xC0) || (result == 1))" in out
    assert "(!(b >= 0xC0 && b < 0xE0) || (result == 2))" in out


def test_dsl_translation_in_bounds_slice_idx():
    """in_bounds(slice, idx) becomes (idx) < slice.len() — Rust slice DSL."""
    assert _translate_dsl("in_bounds(input, pos)") == "(pos) < input.len()"
    # Mixed with other clauses.
    out = _translate_dsl("in_bounds(buf, i) && i > 0")
    assert "(i) < buf.len()" in out
    assert "i > 0" in out


def test_slice_type_detection():
    assert _is_slice_type("&[u8]") is True
    assert _is_slice_type("&mut [i32]") is True
    assert _is_slice_type("& [u8]") is True
    assert _is_slice_type("*mut u8") is False
    assert _is_slice_type("&i32") is False
    assert _is_slice_type("Vec<u8>") is False
    assert _is_slice_type("u8") is False


def test_slice_element_type_extraction():
    assert _slice_element_type("&[u8]") == "u8"
    assert _slice_element_type("&mut [i32]") == "i32"
    assert _slice_element_type("& [u64]") == "u64"


def test_param_init_block_primitive_single_line():
    lines = _param_init_block("i32", "x")
    assert lines == ["    let x: i32 = kani::any::<i32>();"]


def test_param_init_block_raw_pointer_single_line():
    lines = _param_init_block("*mut u8", "p")
    assert lines == ["    let p: *mut u8 = kani::any::<usize>() as *mut u8;"]


def test_param_init_block_slice_multiline():
    """Shared slice → backing array + nondeterministic length + borrow."""
    lines = _param_init_block("&[u8]", "input", slice_bound=4)
    src = "\n".join(lines)
    assert "let mut _backing_input: [u8; 4] = kani::any();" in src
    assert "let _len_input: usize = kani::any();" in src
    assert "kani::assume(_len_input <= 4);" in src
    assert "let input: &[u8] = &_backing_input[.._len_input];" in src


def test_param_init_block_mutable_slice():
    lines = _param_init_block("&mut [i32]", "buf", slice_bound=2)
    src = "\n".join(lines)
    assert "let buf: &mut [i32] = &mut _backing_buf[.._len_buf];" in src


def test_param_init_block_bound_is_configurable():
    lines = _param_init_block("&[u8]", "s", slice_bound=16)
    src = "\n".join(lines)
    assert "[u8; 16]" in src
    assert "_len_s <= 16" in src


def test_reconstruct_fn_definition_from_signature():
    from dataclasses import dataclass, field

    @dataclass
    class _Sig:
        name: str
        return_type: str
        parameters: list
        modifiers: list = field(default_factory=list)
        type_parameters: str = ""
        where_clause: str = ""

    @dataclass
    class _Func:
        name: str
        signature: _Sig
        body: str

    func = _Func(
        name="utf8_len",
        signature=_Sig(
            name="utf8_len",
            return_type="usize",
            parameters=[("u8", "b")],
        ),
        body="{ if b < 0xC0 { 1 } else { 4 } }",
    )
    src = _reconstruct_fn_definition(func)
    assert src.startswith("fn utf8_len(b: u8) -> usize")
    assert src.endswith("}")
    assert "if b < 0xC0" in src


def test_reconstruct_fn_definition_passes_through_full_def_body():
    """Legacy callers may pass a body that already includes the fn header;
    don't double-wrap."""
    from dataclasses import dataclass, field

    @dataclass
    class _Sig:
        name: str = "add"
        return_type: str = "i32"
        parameters: list = field(default_factory=list)
        modifiers: list = field(default_factory=list)
        type_parameters: str = ""
        where_clause: str = ""

    @dataclass
    class _Func:
        name: str = "add"
        signature: _Sig = field(default_factory=_Sig)
        body: str = "fn add(a: i32, b: i32) -> i32 { a + b }"

    src = _reconstruct_fn_definition(_Func())
    assert src == "fn add(a: i32, b: i32) -> i32 { a + b }"


def test_dsl_translation_result_with_implication():
    """End-to-end on the postcondition shape Phase 1 actually emits for
    utf8_sequence_length: \\result substitution + implication rewrite + AND chain."""
    spec = (
        "\\result >= 1 && \\result <= 4 && "
        "(b < 0xC0 ==> \\result == 1)"
    )
    out = _translate_dsl(spec)
    assert "\\result" not in out
    assert "result >= 1" in out
    assert "(!(b < 0xC0) || (result == 1))" in out


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
