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
    _call_site_expr,
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
    kani_slice_bound: int = 4


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


def test_dsl_translation_implication_with_nested_parens_on_rhs():
    """Nested parens on either side must be paren-balanced by the rewriter.
    This is the shape Phase 1 emits when it adds a type suffix or grouping
    expression inside the RHS of an implication."""
    out = _translate_dsl("(result.1 == 3 ==> (result.0 >= 0x80u8))")
    # Outer rewrite produced; inner parens preserved verbatim.
    assert out == "(!(result.1 == 3) || ((result.0 >= 0x80u8)))"
    assert "==>" not in out


def test_dsl_translation_implication_with_nested_parens_on_lhs():
    out = _translate_dsl("((a > 0 && b > 0) ==> c == 1)")
    assert out == "(!((a > 0 && b > 0)) || (c == 1))"
    assert "==>" not in out


def test_dsl_translation_multiple_implications_in_conjunction():
    """Real decode_pua_byte postcondition shape — five chained implications
    each wrapped in its own paren group."""
    spec = (
        "(result.1 == 1 || result.1 == 3) "
        "&& (result.1 == 1 ==> result.0 == input[pos]) "
        "&& (result.1 == 3 ==> (result.0 >= 0x80u8)) "
        "&& (result.1 == 3 ==> pos + 2 < input.len())"
    )
    out = _translate_dsl(spec)
    assert "==>" not in out
    # All four rewrites should appear.
    assert "(!(result.1 == 1) || (result.0 == input[pos]))" in out
    assert "(!(result.1 == 3) || ((result.0 >= 0x80u8)))" in out
    assert "(!(result.1 == 3) || (pos + 2 < input.len()))" in out


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


def test_param_init_block_vec_multiline():
    """Vec<T> → bounded backing array + .to_vec() copy."""
    lines = _param_init_block("Vec<u8>", "bytes", slice_bound=4)
    src = "\n".join(lines)
    assert "let _backing_bytes: [u8; 4] = kani::any();" in src
    assert "let _len_bytes: usize = kani::any();" in src
    assert "kani::assume(_len_bytes <= 4);" in src
    assert "let bytes: Vec<u8> = _backing_bytes[.._len_bytes].to_vec();" in src


def test_param_init_block_vec_inner_type_preserved():
    """Inner type is whatever the parameter declared — verbatim."""
    lines = _param_init_block("Vec<i32>", "xs", slice_bound=2)
    src = "\n".join(lines)
    assert "[i32; 2]" in src
    assert "Vec<i32>" in src


def test_call_site_expr_primitives_no_clone():
    assert _call_site_expr("i32", "x") == "x"
    assert _call_site_expr("u64", "y") == "y"
    assert _call_site_expr("bool", "b") == "b"


def test_call_site_expr_pointers_and_refs_no_clone():
    assert _call_site_expr("*mut u8", "p") == "p"
    assert _call_site_expr("&[u8]", "s") == "s"
    assert _call_site_expr("&mut [i32]", "buf") == "buf"
    assert _call_site_expr("&str", "s") == "s"


def test_call_site_expr_owned_vec_string_clones():
    """Vec<T>/String are moved when passed by value; clone so the
    postcondition can still reference the original."""
    assert _call_site_expr("Vec<u8>", "bytes") == "bytes.clone()"
    assert _call_site_expr("String", "s") == "s.clone()"


def test_call_site_expr_option_clones():
    assert _call_site_expr("Option<u32>", "x") == "x.clone()"


def test_param_init_block_option_branches_on_kani_any():
    """Option<T> picks Some/None via a nondeterministic bool."""
    lines = _param_init_block("Option<u32>", "x")
    src = "\n".join(lines)
    assert "let _some_x: bool = kani::any();" in src
    assert "if _some_x { Some(kani::any::<u32>()) } else { None }" in src
    assert "Option<u32>" in src


def test_param_init_block_str_ref_ascii_bounded():
    """&str → bounded u8 backing array with ASCII bytes, then from_utf8."""
    lines = _param_init_block("&str", "text", slice_bound=4)
    src = "\n".join(lines)
    assert "let _backing_text: [u8; 4] = kani::any();" in src
    assert "let _len_text: usize = kani::any();" in src
    assert "kani::assume(_len_text <= 4);" in src
    assert "kani::assume(_backing_text[_i] < 0x80);" in src
    assert "let text: &str = std::str::from_utf8(&_backing_text[.._len_text]).unwrap();" in src


def test_param_init_block_str_ref_with_lifetime():
    """&'a str — lifetime token should be tolerated."""
    lines = _param_init_block("&'a str", "s", slice_bound=2)
    src = "\n".join(lines)
    assert "[u8; 2]" in src
    # The original type (with lifetime) is preserved in the binding.
    assert "let s: &'a str = std::str::from_utf8" in src


def test_call_site_expr_str_ref_no_clone():
    """&str is a reference; no clone needed at call site."""
    assert _call_site_expr("&str", "text") == "text"
    assert _call_site_expr("&'a str", "s") == "s"


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


def test_parser_unwind_failure_reported_as_inconclusive():
    """`.unwind.N` rows mean the loop ran past the unwind bound. They're
    inconclusive, not real CEx — surface as error, not counterexample."""
    raw = (
        "RESULTS:\n"
        "Check 1: check_x.unwind.0\n"
        "\t - Status: FAILURE\n"
        "\t - Description: \"unwinding assertion loop 0\"\n"
        "\n"
        "VERIFICATION:- FAILED\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=10)
    assert result.verified is False
    assert result.counterexamples == []
    assert result.error is not None
    assert "unwind" in result.error.lower()


def test_parser_failed_checks_summary_unwinding_is_inconclusive():
    """Kani's real-world output includes a 'Failed Checks: unwinding ...'
    fallback line alongside the per-check FAILURE row. The fallback
    must not be treated as a real CEx — both paths point at the same
    inconclusive unwind failure."""
    raw = (
        "Check 2450: check_x.unwind.0\n"
        "\t - Status: FAILURE\n"
        "\t - Description: \"unwinding assertion loop 0\"\n"
        "\nSUMMARY:\n"
        "Failed Checks: unwinding assertion loop 0\n"
        "VERIFICATION:- FAILED\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=10)
    assert result.counterexamples == []
    assert "unwind" in (result.error or "").lower()


def test_parser_real_cex_takes_precedence_over_unwind():
    """If both a real assertion failure AND an unwind warning fire,
    the real CEx wins — the spec was actually violated."""
    raw = (
        "RESULTS:\n"
        "Check 1: check_x.assertion.1\n"
        "\t - Status: FAILURE\n"
        "\t - Description: \"postcondition violated\"\n"
        "Check 2: check_x.unwind.0\n"
        "\t - Status: FAILURE\n"
        "\t - Description: \"unwinding assertion loop 0\"\n"
        "\n"
        "VERIFICATION:- FAILED\n"
    )
    result = _parse_kani_output(raw, stderr="", returncode=10)
    assert result.verified is False
    assert len(result.counterexamples) == 1
    assert result.counterexamples[0].failing_property == "check_x.assertion.1"
    # Real CEx wins; we do not also surface the unwind error.
    assert result.error is None


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


def test_generate_harness_respects_slice_bound_override():
    """The engine's retry path passes a smaller ``slice_bound_override``
    to regenerate a harness with tighter buffer bounds after a Kani
    timeout. Regression: CCC encoding.rs 2026-05-19 — bytes_to_string
    timed out at default bound=4; bound=1 verifies clean. Harness must
    reflect the override, not the config default."""
    from dataclasses import dataclass, field
    @dataclass
    class _SigFull:
        name: str
        return_type: str
        parameters: list
        modifiers: list = field(default_factory=list)
        type_parameters: str = ""
        where_clause: str = ""
    @dataclass
    class _FuncFull:
        name: str
        signature: _SigFull
        body: str
        callees: set = field(default_factory=set)
        source_file: str = "synthetic.rs"
    config = _Config(kani_slice_bound=4)
    backend = KaniBackend(config)
    func = _FuncFull(
        name="f",
        signature=_SigFull(name="f", return_type="usize",
                           parameters=[("&[u8]", "buf")]),
        body="{ buf.len() }",
    )
    spec = _Spec(function_name="f")
    # Default: bound=4 → backing array [u8; 4]
    h4 = backend.generate_harness(func, spec, {})
    assert "[u8; 4]" in h4, h4
    # Override to 1 → backing array [u8; 1]
    h1 = backend.generate_harness(func, spec, {}, slice_bound_override=1)
    assert "[u8; 1]" in h1, h1
    assert "[u8; 4]" not in h1, h1
    # Length-bound clauses also rewritten in sync.
    assert "<= 1)" in h1, h1
    assert "<= 4)" not in h1, h1


def test_strip_crate_local_fn_items_keeps_target_strips_unrelated_siblings():
    """Every fn that is neither the target nor a recorded callee must
    be stripped. After ``use crate::*`` lines are removed, the bare
    types that USED to be in scope (BinOp, IrConst) are now undefined;
    sibling fn signatures that reference them won't compile. Removing
    those siblings entirely (not just the crate-prefixed ones) leaves
    a self-contained harness.

    Regression: CCC const_arith.rs 2026-05-19 — wrap_result was a
    pure ``v as i32 as i64`` fn that should verify clean, but 12
    polluted siblings made the file fail rustc parse."""
    from bmc_agent.backends.kani_backend import _strip_crate_local_fn_items
    src = """
const SEED: u64 = 42;

pub fn wrap_result(v: i64) -> i64 { v }

pub fn is_zero_expr(expr: &crate::frontend::parser::ast::Expr) -> bool {
    matches!(expr, _)
}

fn eval_const_binop_int(op: &BinOp) -> i64 { 0 }

fn unsigned_op(l: u64) -> u64 { l }
"""
    out = _strip_crate_local_fn_items(src, keep_fn_name="wrap_result")
    # Target survives.
    assert "pub fn wrap_result(v: i64) -> i64 { v }" in out, out
    # Unrelated siblings stripped — both the crate-polluted one AND
    # the one that only used a now-undefined bare type (BinOp).
    assert "is_zero_expr(expr: &crate" not in out, out
    assert "fn eval_const_binop_int" not in out or "/* stripped" in out, out
    assert "fn unsigned_op(l: u64) -> u64 { l }" not in out, out
    # Module-level const stays.
    assert "const SEED: u64 = 42;" in out, out


def test_strip_crate_local_fn_items_keeps_listed_callees():
    """When the target calls helper fns in the same module, those
    helpers must be preserved (passed in via ``keep_callees``).
    Targets calling clean helpers should still produce a compilable
    harness."""
    from bmc_agent.backends.kani_backend import _strip_crate_local_fn_items
    src = """
fn helper(x: u64) -> u64 { x + 1 }
fn polluted_unrelated(e: &crate::A) -> bool { false }
fn target_caller(x: u64) -> u64 { helper(x) }
"""
    out = _strip_crate_local_fn_items(
        src, keep_fn_name="target_caller", keep_callees={"helper"},
    )
    assert "fn helper(x: u64) -> u64 { x + 1 }" in out, out
    assert "fn target_caller(x: u64) -> u64 { helper(x) }" in out, out
    assert "polluted_unrelated" not in out or "/* stripped" in out, out


def test_param_init_block_slice_of_user_type_falls_back_to_empty_vec():
    """``&[ExprToken]`` cannot use ``[T; N] = kani::any()`` because
    ExprToken isn't Arbitrary. Fall back to an empty Vec backing so the
    harness compiles and we get a (degenerate) verdict.

    Regression: CCC asm_expr.rs eval_add — E0277 ``ExprToken: kani::Arbitrary
    is not satisfied`` after the transitive-callee fix unblocked compilation
    on the function side."""
    out = _param_init_block("&[ExprToken]", "tokens")
    text = "\n".join(out)
    # Must NOT try to nondet-init [T; N] of a non-primitive.
    assert "kani::any()" not in text, text
    assert "[ExprToken; " not in text, text
    # Must produce an empty-Vec backing + a shared-slice borrow.
    assert "Vec<ExprToken> = Vec::new();" in text, text
    assert "&_backing_tokens[..]" in text, text


def test_param_init_block_mut_slice_of_user_type_falls_back_to_empty_vec():
    """Same fallback for ``&mut [T]`` of a user-defined type."""
    out = _param_init_block("&mut [SomeStruct]", "buf")
    text = "\n".join(out)
    assert "kani::any()" not in text, text
    assert "Vec<SomeStruct> = Vec::new();" in text, text
    assert "&mut _backing_buf[..]" in text, text


def test_param_init_block_vec_of_user_type_falls_back_to_empty():
    """``Vec<UserEnum>`` — same Arbitrary issue, same fallback."""
    out = _param_init_block("Vec<ExprToken>", "tokens")
    text = "\n".join(out)
    assert "kani::any()" not in text, text
    assert "Vec<ExprToken> = Vec::new();" in text, text


def test_param_init_block_slice_of_primitive_still_uses_nondet():
    """Sanity: primitive-element slices must NOT be empty-Vec-degraded.
    The fallback should kick in ONLY when the element type isn't Arbitrary."""
    out = _param_init_block("&[u8]", "data")
    text = "\n".join(out)
    assert "kani::any()" in text, text
    assert "[u8; " in text, text


def test_transitive_callees_walks_call_graph():
    """``func.callees`` records only direct calls, but the strip helper
    needs every transitively reachable sibling — otherwise indirectly
    called helpers (eval_add -> eval_mul -> eval_unary) get stripped
    and the harness fails to compile.

    Regression: CCC asm_expr.rs 2026-05-19 — eval_add verified 1/12
    because eval_unary kept getting stripped despite being reachable
    through eval_mul."""
    from bmc_agent.backends.kani_backend import _transitive_callees
    class _PF:
        def __init__(self, g):
            self.call_graph = g
    pf = _PF({
        "eval_add": {"eval_mul"},
        "eval_mul": {"eval_unary"},
        "eval_unary": {"eval_tokens"},
        "eval_tokens": set(),
        "stranger": {"unrelated_helper"},
    })
    closure = _transitive_callees({"eval_mul"}, pf)
    assert closure == {"eval_mul", "eval_unary", "eval_tokens"}, closure
    # Unreached fns must NOT enter the keep set.
    assert "stranger" not in closure
    assert "unrelated_helper" not in closure


def test_transitive_callees_handles_cycles():
    """A cyclic call graph (mutual recursion) must terminate."""
    from bmc_agent.backends.kani_backend import _transitive_callees
    class _PF:
        def __init__(self, g):
            self.call_graph = g
    pf = _PF({"a": {"b"}, "b": {"a", "c"}, "c": set()})
    assert _transitive_callees({"a"}, pf) == {"a", "b", "c"}


def test_transitive_callees_no_parsed_file():
    """When parsed_file is None (test fixtures), return the direct set."""
    from bmc_agent.backends.kani_backend import _transitive_callees
    assert _transitive_callees({"x", "y"}, None) == {"x", "y"}
    assert _transitive_callees(set(), None) == set()


def test_param_init_block_mut_ref_vec():
    """&mut Vec<u8> output-param: allocate a backing Vec and pass
    &mut backing. Regression: CCC copy_literal_bytes_raw was blocked
    by ``&mut references in Kani harnesses are not yet supported``."""
    out = _param_init_block("&mut Vec<u8>", "result")
    text = "\n".join(out)
    assert "let mut _owned_result: Vec<u8> = Vec::new();" in text, text
    assert "let result: &mut Vec<u8> = &mut _owned_result;" in text, text


def test_param_init_block_mut_ref_string():
    """&mut String output-param: allocate a backing String, take &mut.
    Regression: CCC copy_literal_bytes_to_string was blocked."""
    out = _param_init_block("&mut String", "buf")
    text = "\n".join(out)
    assert "let mut _owned_buf: String = String::new();" in text, text
    assert "let buf: &mut String = &mut _owned_buf;" in text, text


def test_param_init_block_mut_ref_primitive():
    """&mut <primitive>: bind a nondet mutable scalar, take &mut."""
    out = _param_init_block("&mut u32", "counter")
    text = "\n".join(out)
    assert "let mut _owned_counter: u32 = kani::any::<u32>();" in text, text
    assert "let counter: &mut u32 = &mut _owned_counter;" in text, text


def test_strip_crate_local_fn_items_strips_impl_blocks():
    """``impl`` blocks remain in the source after fn-stripping and
    their bodies reference now-stripped sibling fns (E0425 cascade).
    The parser's M1 scope only analyses top-level free fns, so impl
    blocks are never the target — strip them whole.

    Regression: CCC source.rs 2026-05-19 — 4 pure top-level fns
    all failed Kani parse because impl Span / impl SourceManager
    bodies still referenced compute_line_offsets, memchr_newline,
    and FxHashMap (after their definitions were stripped)."""
    from bmc_agent.backends.kani_backend import _strip_crate_local_fn_items
    src = """
pub struct Foo;

impl Foo {
    pub fn method_that_calls_stripped(&self) -> u32 {
        sibling_fn(42)
    }
}

fn my_target(x: u32) -> u32 { x + 1 }

fn sibling_fn(x: u32) -> u32 { x }
"""
    out = _strip_crate_local_fn_items(src, keep_fn_name="my_target")
    # Target survives.
    assert "fn my_target(x: u32) -> u32 { x + 1 }" in out, out
    # Sibling fn stripped.
    assert "fn sibling_fn(x: u32) -> u32 { x }" not in out, out
    # impl block stripped — body's reference to sibling_fn cannot survive.
    assert "method_that_calls_stripped" not in out, out
    # Struct definition (not an fn or impl) remains.
    assert "pub struct Foo;" in out, out


def test_strip_crate_local_fn_items_preserves_target_even_if_polluted():
    """If the TARGET function itself references crate paths it stays
    in the source — verification will then fail (parse error) for the
    correct reason, rather than being silently dropped."""
    from bmc_agent.backends.kani_backend import _strip_crate_local_fn_items
    src = """
pub fn my_target(x: &crate::A::B) -> i32 { 0 }
"""
    out = _strip_crate_local_fn_items(src, keep_fn_name="my_target")
    assert "my_target" in out and "crate::A::B" in out, out


def test_strip_crate_local_use_statements_comments_out_crate_paths():
    """``use crate::*`` / ``use super::*`` / ``use self::*`` lines fail
    to resolve when the harness is compiled standalone. Strip them
    (preserve via comment so the artifact stays readable). Regression:
    CCC const_arith.rs 2026-05-19 — all 3 primitive harnesses failed
    Kani parse because the file's ``use crate::*;`` lines were copied
    verbatim."""
    from bmc_agent.backends.kani_backend import _strip_crate_local_use_statements
    src = """
use std::collections::HashMap;
use crate::ir::reexports::IrConst;
use crate::frontend::parser::ast::BinOp;
use super::helpers::Foo;
use self::inner::Bar;
pub use crate::api::Public;

fn wrap_result(v: i64) -> i64 { v }
"""
    out = _strip_crate_local_use_statements(src)
    # std:: untouched
    assert "use std::collections::HashMap;" in out, out
    # No bare (uncommented) crate-local use survives at line start
    # — stripped lines are now wrapped in /* ... */ block comments
    # (single- or multi-line). The lambda emits ``/* use ... */``.
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("use ") or stripped.startswith("pub use "):
            assert "crate::" not in stripped, line
            assert "super::" not in stripped, line
            assert "self::" not in stripped, line
    # Function body intact
    assert "fn wrap_result" in out, out


def test_strip_pub_in_path_visibility_rewrites_pub_super():
    """``pub(super)`` and ``pub(crate)`` are crate-internal visibility
    restrictors that fail standalone compilation with E0433 ("too many
    leading `super` keywords" or similar). Replace with plain ``pub``
    so the harness compiles. Regression: CCC macro_defs.rs 2026-05-19
    — every struct field used pub(super) and broke every harness."""
    from bmc_agent.backends.kani_backend import _strip_pub_in_path_visibility
    src = """
pub struct State {
    pub(super) asm_mode: bool,
    pub(crate) flag: u32,
    pub(self) inner: i32,
    pub(in crate::module) path: String,
    pub normal: i64,
    pub final_field: u8,
}
"""
    out = _strip_pub_in_path_visibility(src)
    assert "pub(super)" not in out, out
    assert "pub(crate)" not in out, out
    assert "pub(self)" not in out, out
    assert "pub(in" not in out, out
    # Plain pub stays.
    assert "pub normal: i64," in out, out
    assert "pub final_field: u8," in out, out
    # Replaced ones are now plain pub.
    assert "pub asm_mode: bool," in out, out
    assert "pub flag: u32," in out, out


def test_strip_crate_local_use_handles_multiline_import():
    """``use super::utils::{ a, b, c, };`` wraps over multiple lines
    in the source. The stripper must match the whole statement up to
    the terminating ``;``; the original single-line regex only
    caught the first line and orphaned the closing ``};``, producing
    rustc "unexpected closing delimiter" on every harness.
    Regression: CCC macro_defs.rs 2026-05-19."""
    from bmc_agent.backends.kani_backend import _strip_crate_local_use_statements
    src = """
use std::cell::Cell;
use crate::common::fx_hash::{FxHashMap, FxHashSet};
use super::utils::{
    is_ident_start_byte, is_ident_cont_byte, bytes_to_str,
    skip_literal_bytes, copy_literal_bytes_to_string,
};

fn target() {}
"""
    out = _strip_crate_local_use_statements(src)
    # std:: untouched
    assert "use std::cell::Cell;" in out, out
    # Multi-line use is wrapped in a /* ... */ block comment that
    # includes the closing }; — no orphan }; outside the comment.
    # Find every }; and confirm each is inside a comment.
    cursor = 0
    while True:
        idx = out.find("};", cursor)
        if idx == -1:
            break
        # Walk back to find the most recent /* or */
        prefix = out[:idx]
        last_open = prefix.rfind("/*")
        last_close = prefix.rfind("*/")
        assert last_open > last_close, (
            f"orphan }}; at offset {idx} outside any /* ... */ block:\n{out}"
        )
        cursor = idx + 2
    # Identifier names from the import list don't appear as bare lines
    # — they're inside the /* ... */ comment.
    for ident in ["is_ident_start_byte", "is_ident_cont_byte",
                  "bytes_to_str", "skip_literal_bytes",
                  "copy_literal_bytes_to_string"]:
        for line in out.splitlines():
            stripped = line.strip()
            if stripped == ident or stripped == ident + ",":
                assert False, f"orphan ident {ident} on bare line: {out!r}"


def test_strip_crate_local_use_does_not_touch_absolute_paths():
    """std::, core::, alloc::, and external crate imports (not
    ``crate::``) must NOT be stripped — they resolve fine in standalone
    compilation."""
    from bmc_agent.backends.kani_backend import _strip_crate_local_use_statements
    src = """
use std::fmt;
use core::mem::size_of;
use alloc::vec::Vec;
use serde::Deserialize;
use std::collections::HashMap as Map;
"""
    out = _strip_crate_local_use_statements(src)
    for original in src.splitlines():
        if original.strip().startswith("use "):
            assert original in out, original


def test_check_respects_unwind_and_timeout_overrides(tmp_path):
    """``KaniBackend.check`` must let the engine override unwind and
    timeout without mutating config. Used by the timeout-retry path."""
    config = _Config(kani_unwind=4, kani_timeout=120)
    backend = KaniBackend(config)
    harness = tmp_path / "h.rs"
    harness.write_text("fn main() {}")
    captured: dict = {}

    def fake_run_kani(*, harness_path, harness_name, unwind, timeout, kani_path):
        captured["unwind"] = unwind
        captured["timeout"] = timeout
        return CBMCResult(verified=True, raw_output="VERIFICATION:- SUCCESSFUL")

    with patch("bmc_agent.backends.kani_backend.run_kani", side_effect=fake_run_kani):
        backend.check(str(harness), unwind_override=2, timeout_override=60)
    assert captured == {"unwind": 2, "timeout": 60}
    # No override → config defaults
    captured.clear()
    with patch("bmc_agent.backends.kani_backend.run_kani", side_effect=fake_run_kani):
        backend.check(str(harness))
    assert captured == {"unwind": 4, "timeout": 120}


# ---------------------------------------------------------------------------
# old() snapshot substitution (Phase 1 follow-up)
# ---------------------------------------------------------------------------

def test_extract_old_snapshots_no_old_passthrough():
    """Postcondition without old() returns unchanged with no snapshots."""
    from bmc_agent.backends.kani_backend import _extract_old_snapshots
    post = "result == val + 1"
    rewritten, snaps = _extract_old_snapshots(post)
    assert rewritten == post
    assert snaps == []


def test_extract_old_snapshots_scalar():
    """``old(buf.len())`` snapshots to ``_pre_0`` with a let binding."""
    from bmc_agent.backends.kani_backend import _extract_old_snapshots
    post = "buf.len() == old(buf.len()) + target"
    rewritten, snaps = _extract_old_snapshots(post)
    assert rewritten == "buf.len() == _pre_0 + target", rewritten
    assert snaps == ["    let _pre_0 = (buf.len());"]


def test_extract_old_snapshots_slice_uses_to_vec():
    """``old(buf[..])`` needs ``.to_vec()`` because slice borrows can't
    outlive a subsequent mutation. The helper detects ``[`` in the
    expression and appends to_vec()."""
    from bmc_agent.backends.kani_backend import _extract_old_snapshots
    post = "buf[..old(buf.len())] == old(buf[..])"
    rewritten, snaps = _extract_old_snapshots(post)
    # Two old() calls, both rewritten
    assert "_pre_0" in rewritten
    assert "_pre_1" in rewritten
    # The slice expression got to_vec()
    snap_text = "\n".join(snaps)
    assert ".to_vec()" in snap_text, snap_text
    # The scalar one did not (buf.len() is Copy)
    assert "_pre_0 = (buf.len())" in snap_text, snap_text


def test_extract_old_snapshots_nested_old():
    """``old(buf[..old(buf.len())])`` — inner old strips because the
    outer snapshot already captures pre-state, so the inner reference
    is just ``buf.len()`` evaluated at snapshot time."""
    from bmc_agent.backends.kani_backend import _extract_old_snapshots
    post = "x == old(buf[..old(buf.len())])"
    rewritten, snaps = _extract_old_snapshots(post)
    # One snapshot, since the outer old() is what we capture
    assert "_pre_0" in rewritten
    # The snapshot expression has the inner old() stripped
    snap_text = "\n".join(snaps)
    assert "old(" not in snap_text, snap_text
    assert "buf.len()" in snap_text, snap_text


def test_extract_old_snapshots_does_not_match_identifiers():
    """Identifiers like ``cold_path`` or ``_old_var`` must not match —
    only the ``old(`` call form."""
    from bmc_agent.backends.kani_backend import _extract_old_snapshots
    post = "result == cold_path + _old_var"
    rewritten, snaps = _extract_old_snapshots(post)
    assert rewritten == post
    assert snaps == []


# ---------------------------------------------------------------------------
# Unresolvable-types harness gate
# ---------------------------------------------------------------------------


def test_harness_gate_primitives_only_resolves():
    """Functions touching only Rust primitives never trigger the gate."""
    from bmc_agent.backends.kani_backend import _function_references_unresolvable_types

    class _Sig:
        parameters = [("u32", "x"), ("&[u8]", "data")]
        return_type = "Option<u32>"
    class _F:
        name = "f"
        signature = _Sig()
        body = "fn f(x: u32, data: &[u8]) -> Option<u32> { Some(x) }"

    assert _function_references_unresolvable_types(_F(), None, "") == set()


def test_harness_gate_picks_up_undefined_signature_types():
    from bmc_agent.backends.kani_backend import _function_references_unresolvable_types

    class _Sig:
        parameters = [("Operand", "op")]
        return_type = "Result<EncodeResult, String>"
    class _F:
        name = "encode"
        signature = _Sig()
        body = "fn encode(op: Operand) -> Result<EncodeResult, String> { unimplemented!() }"

    unresolved = _function_references_unresolvable_types(_F(), None, "")
    assert "Operand" in unresolved
    assert "EncodeResult" in unresolved


def test_harness_gate_resolves_types_defined_in_source():
    from bmc_agent.backends.kani_backend import _function_references_unresolvable_types

    class _Sig:
        parameters = [("Operand", "op")]
        return_type = "EncodeResult"
    class _F:
        name = "encode"
        signature = _Sig()
        body = "fn encode(op: Operand) -> EncodeResult { EncodeResult::Word(0) }"

    src = """
pub enum Operand { Reg(u8) }
pub enum EncodeResult { Word(u32) }
"""
    assert _function_references_unresolvable_types(_F(), None, src) == set()


def test_harness_gate_ignores_path_qualified_variant_calls():
    """``EncodeResult::Word`` in a body must not register ``Word`` as a type."""
    from bmc_agent.backends.kani_backend import _function_references_unresolvable_types

    class _Sig:
        parameters = []
        return_type = "EncodeResult"
    class _F:
        name = "make"
        signature = _Sig()
        body = "fn make() -> EncodeResult { EncodeResult::Word(0) }"

    src = "pub enum EncodeResult { Word(u32) }"
    assert _function_references_unresolvable_types(_F(), None, src) == set()


def test_harness_gate_treats_external_aliases_as_resolvable():
    """FxHashMap / FxHashSet alias to std collections, so they don't trigger
    the gate even when not defined in source."""
    from bmc_agent.backends.kani_backend import _function_references_unresolvable_types

    class _Sig:
        parameters = [("FxHashMap<u32, u32>", "m")]
        return_type = "FxHashSet<u32>"
    class _F:
        name = "h"
        signature = _Sig()
        body = "fn h(m: FxHashMap<u32, u32>) -> FxHashSet<u32> { FxHashSet::new() }"

    unresolved = _function_references_unresolvable_types(_F(), None, "")
    # FxHashMap/FxHashSet are external_aliases — gate filters them through the
    # `real_unresolved = unresolved - alias_keys` step at the call site, but
    # the helper itself reports anything not stdlib-or-locally-defined.
    # So we expect the alias names *can* appear in the raw return; the call
    # site is responsible for filtering them out before raising. Verify by
    # checking that the difference against the alias set is empty.
    from bmc_agent.backends.kani_backend import _EXTERNAL_TYPE_ALIASES
    real_unresolved = unresolved - set(_EXTERNAL_TYPE_ALIASES.keys())
    assert real_unresolved == set(), f"unresolved minus aliases: {real_unresolved}"


def test_harness_unresolvable_exception_carries_names():
    from bmc_agent.backends.kani_backend import HarnessUnresolvableTypes

    exc = HarnessUnresolvableTypes("encode", ["Operand", "EncodeResult"])
    assert exc.function_name == "encode"
    assert exc.unresolved_types == ["Operand", "EncodeResult"]
    msg = str(exc)
    assert "Operand" in msg and "EncodeResult" in msg


def test_parse_kani_vacuous_proof():
    """Kani output with 'VERIFICATION: SUCCESSFUL' but every check
    UNREACHABLE (typically from kani::assume(false)) is a vacuous proof
    and must NOT be reported as verified=True."""
    from bmc_agent.kani import _parse_kani_output
    raw = """
Check 1: check_dummy.assertion.1
\t - Status: UNREACHABLE
\t - Description: "should never reach"
\t - Location: src/lib.rs:9:5 in function check_dummy
Check 2: dummy.assertion.1
\t - Status: UNREACHABLE
\t - Description: "attempt to add with overflow"
\t - Location: src/lib.rs:2:27 in function dummy


SUMMARY:
 ** 0 of 2 failed (2 unreachable)

VERIFICATION:- SUCCESSFUL
"""
    res = _parse_kani_output(raw, "", 0)
    assert res.verified is False, "vacuous proof must NOT be verified"
    assert res.error and "vacuous" in res.error.lower()


def test_parse_kani_real_proof_still_verified():
    """A real proof with SUCCESS rows continues to be verified=True
    (sanity check that the vacuous guard doesn't false-positive)."""
    from bmc_agent.kani import _parse_kani_output
    raw = """
Check 1: check_real.assertion.1
\t - Status: SUCCESS
\t - Description: "postcondition violated"
Check 2: check_real.assertion.2
\t - Status: SUCCESS
\t - Description: "attempt to add with overflow"


SUMMARY:
 ** 0 of 2 failed

VERIFICATION:- SUCCESSFUL
"""
    res = _parse_kani_output(raw, "", 0)
    assert res.verified is True
    assert not res.error


def test_param_init_block_ref_to_struct():
    """`&Algorithm<u8>` — generic &T struct ref, mirror &mut T pattern."""
    from bmc_agent.backends.kani_backend import _param_init_block
    lines = _param_init_block("&Algorithm<u8>", "algo")
    src = "\n".join(lines)
    assert "let _owned_algo: Algorithm<u8> = kani::any::<Algorithm<u8>>();" in src
    assert "let algo: &Algorithm<u8> = &_owned_algo;" in src


def test_param_init_block_ref_to_primitive():
    """`&u32` — primitive reference."""
    from bmc_agent.backends.kani_backend import _param_init_block
    lines = _param_init_block("&u32", "x")
    src = "\n".join(lines)
    assert "let _owned_x: u32 = kani::any::<u32>();" in src
    assert "let x: &u32 = &_owned_x;" in src


def test_param_init_block_static_lifetime_ref():
    """`&'static Algorithm<u8>` — Box::leak so the borrow outlives the harness."""
    from bmc_agent.backends.kani_backend import _param_init_block
    lines = _param_init_block("&'static Algorithm<u8>", "algo")
    src = "\n".join(lines)
    assert "Box::leak" in src
    assert "kani::any::<Algorithm<u8>>" in src
    assert "let algo: &'static Algorithm<u8>" in src


def test_param_init_block_str_ref_still_dedicated():
    """`&str` must continue to route to the ASCII-bounded backing path,
    not the generic &T fallback."""
    from bmc_agent.backends.kani_backend import _param_init_block
    lines = _param_init_block("&str", "s", slice_bound=2)
    src = "\n".join(lines)
    assert "std::str::from_utf8" in src


def test_rewrite_implications_top_level_no_parens():
    """`A ==> B` at top level (no enclosing parens) should still rewrite,
    not leak the raw ==> into Rust source. Concrete arrayvec
    raw_ptr_add case had this shape."""
    from bmc_agent.backends.kani_backend import _rewrite_implications
    out = _rewrite_implications("a > 0 ==> b < 10")
    assert "==>" not in out
    assert "!(a > 0)" in out
    assert "(b < 10)" in out


def test_rewrite_implications_chained_top_level():
    """Two top-level implications joined by &&."""
    from bmc_agent.backends.kani_backend import _rewrite_implications
    out = _rewrite_implications("a == 0 ==> b > 5 && c != 0 ==> d < 10")
    assert "==>" not in out


def test_rewrite_implications_already_wrapped():
    """An expression that already has the (A ==> B) shape is unchanged."""
    from bmc_agent.backends.kani_backend import _rewrite_implications
    out = _rewrite_implications("(a > 0 ==> b < 10)")
    assert out == "(!(a > 0) || (b < 10))"
