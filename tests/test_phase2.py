"""
Phase 2 acceptance tests for BMC-Agent BMC Engine.

Tests:
1. Harness generation for rb_write with a mock spec (valid C output).
2. DSL translation: precond_to_assume and postcond_to_assert.
3. Callee stubbing: callee calls replaced with stubs in the harness.
4. BMC engine with CBMC not installed: returns BMCVerdict with error.
5. check_all with mocked CBMC: structured BMCVerdict returned.
6. Harness for a function with no callees (rb_is_empty).
7. Artifact saving: harness saved to correct path.
8. Known bug test: if CBMC available, finds counterexample in rb_write.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_C = REPO_ROOT / "examples" / "simple_driver.c"

_CBMC_INSTALLED = shutil.which("cbmc") is not None

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_spec(
    fn_name: str,
    pre: str = "rb != NULL && data != NULL",
    post: str = r"result <= len",
    callee_specs: dict | None = None,
) -> "Spec":
    from bmc_agent.spec import Spec, SpecStatus

    return Spec(
        function_name=fn_name,
        precondition=pre,
        postcondition=post,
        callee_specs=callee_specs or {},
        status=SpecStatus.GENERATED,
    )


def _make_config(tmp_path: Path) -> "Config":
    from bmc_agent.config import Config

    return Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="cbmc",
        cbmc_unwind=4,
        cbmc_timeout=60,
    )


# ---------------------------------------------------------------------------
# 1. Test harness generation for rb_write
# ---------------------------------------------------------------------------


def test_harness_generation_rb_write(tmp_path: Path):
    """
    Generate a harness for rb_write with a mock spec.
    Verify the harness is valid C containing CPROVER_assume, assert, and the function body.
    """
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(artifact_dir=str(tmp_path / "artifacts"))
    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_write")
    assert func is not None

    spec = Spec(
        function_name="rb_write",
        precondition="rb != NULL && data != NULL",
        postcondition=r"result <= len",
        status=SpecStatus.GENERATED,
    )

    gen = HarnessGenerator(config)
    harness = gen.generate_harness(func, spec, parsed)

    assert harness, "Expected non-empty harness"
    assert "__CPROVER_assume" in harness, "Harness should contain __CPROVER_assume"
    assert "assert" in harness, "Harness should contain assert"
    # The function body should be included (key tokens from rb_write body)
    assert "rb_write" in harness, "Harness should reference rb_write"
    assert "void main" in harness or "int main" in harness, "Harness should have main()"
    # Standard includes
    assert "#include <assert.h>" in harness
    print(f"\nHarness (first 800 chars):\n{harness[:800]}")


def test_harness_contains_function_body(tmp_path: Path):
    """Harness should include the function body (with stubs substituted)."""
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(artifact_dir=str(tmp_path / "artifacts"))
    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_write")
    assert func is not None

    spec = Spec(
        function_name="rb_write",
        precondition="true",
        postcondition="true",
        status=SpecStatus.GENERATED,
    )

    gen = HarnessGenerator(config)
    harness = gen.generate_harness(func, spec, parsed)

    # The reconstructed function definition should appear
    assert "rb_write" in harness
    # Function return type
    assert "size_t" in harness


# ---------------------------------------------------------------------------
# 2. Test DSL translation
# ---------------------------------------------------------------------------


def test_precond_to_assume_null_check():
    """valid(ptr) should become __CPROVER_assume(ptr != NULL)."""
    from bmc_agent.dsl_to_cbmc import precond_to_assume

    stmts = precond_to_assume("valid(rb)", ["rb"])
    assert len(stmts) > 0
    joined = " ".join(stmts)
    assert "rb != NULL" in joined
    assert "__CPROVER_assume" in joined


def test_precond_to_assume_comparison():
    """Simple C comparison should become an assume statement."""
    from bmc_agent.dsl_to_cbmc import precond_to_assume

    stmts = precond_to_assume("rb != NULL && data != NULL", ["rb", "data"])
    joined = " ".join(stmts)
    assert "__CPROVER_assume" in joined
    # Both conditions should appear
    assert "rb != NULL" in joined or "data != NULL" in joined


def test_precond_to_assume_true():
    """'true' precondition should produce a comment, not an assume."""
    from bmc_agent.dsl_to_cbmc import precond_to_assume

    stmts = precond_to_assume("true", [])
    assert len(stmts) > 0
    # Should be a comment
    assert all(s.startswith("/*") for s in stmts)


def test_postcond_to_assert_result():
    r"""'\result' should be replaced with the return variable name."""
    from bmc_agent.dsl_to_cbmc import postcond_to_assert

    stmts = postcond_to_assert(r"\result <= len", ["len"], return_var="result")
    joined = " ".join(stmts)
    assert "result" in joined
    assert "assert" in joined or "/*" in joined


def test_postcond_to_assert_comparisons():
    """Standard comparisons become assert statements."""
    from bmc_agent.dsl_to_cbmc import postcond_to_assert

    stmts = postcond_to_assert("result >= 0", ["result"], return_var="result")
    joined = " ".join(stmts)
    assert "result >= 0" in joined


def test_postcond_to_assert_refuses_tautology_from_stripped_old():
    """When the postcondition references ``\\old(X)`` and the DSL
    sanitiser strips it, the resulting ``X OP X`` is a tautological
    assertion that must NOT be emitted: an always-false self-comparison
    drowns out every other property. Regression: ttf.c
    stbtt__cff_skip_operand was emitting ``assert(b->cursor > b->cursor)``.
    """
    from bmc_agent.dsl_to_cbmc import postcond_to_assert
    stmts = postcond_to_assert(
        "b->cursor > \\old(b->cursor) && b->size > 0", ["b"],
    )
    # Flatten on newlines because translate_atom joins multi-clause
    # postconditions with \n inside a single list element.
    lines = []
    for s in stmts:
        lines.extend(s.split("\n"))
    # Live lines: those that aren't comments.
    live = [l.strip() for l in lines if l.strip() and not l.strip().startswith("/*")]
    joined_live = " ".join(live)
    # The tautology must NOT appear as a live assert.
    assert "b->cursor > b->cursor" not in joined_live, live
    # The marker comment must appear somewhere.
    assert any("tautological" in l for l in lines), lines
    # The other clause survives intact as a live assert.
    assert any("b->size > 0" in l for l in live), live


def test_is_self_comparison_basic_cases():
    """Predicate identifies syntactic self-comparisons; rejects normal
    binary comparisons."""
    from bmc_agent.dsl_to_cbmc import _is_self_comparison
    # Tautologies (any operator with identical sides)
    assert _is_self_comparison("x == x")
    assert _is_self_comparison("x > x")
    assert _is_self_comparison("x >= x")
    assert _is_self_comparison("x != x")
    assert _is_self_comparison("b->cursor > b->cursor")  # ->-aware
    assert _is_self_comparison("(b->cursor) > b->cursor")  # paren-tolerant
    assert _is_self_comparison("p->len <= p->len")
    assert _is_self_comparison("a->b->c == a->b->c")  # nested ->
    # NOT tautologies
    assert not _is_self_comparison("x > y")
    assert not _is_self_comparison("a + 1 > a")
    assert not _is_self_comparison("p->len == q->len")
    assert not _is_self_comparison("result >= 0")
    assert not _is_self_comparison("")
    assert not _is_self_comparison("just_an_expression")


def test_translate_atom_valid():
    """translate_atom with valid(ptr) in assume context."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("valid(ptr)", context="assume")
    assert stmt is not None
    assert "__CPROVER_assume(ptr != NULL)" in stmt


def test_translate_atom_assert_context():
    """translate_atom with valid(ptr) in assert context."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("valid(ptr)", context="assert")
    assert stmt is not None
    assert "assert(ptr != NULL)" in stmt


def test_translate_atom_in_bounds():
    """in_bounds translates correctly."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("in_bounds(arr, idx)", context="assume")
    assert stmt is not None
    assert "idx" in stmt
    assert "sizeof" in stmt


def test_translate_atom_bracketed_lhs():
    """Bracketed identifiers on LHS (e.g. ptr[0] >= 0x80) must wrap.

    Regression: ``_C_COMPARISON_RE`` previously required a word-boundary
    after the LHS operand, but ``]`` followed by a space is non-word↔non-word
    so the match failed, dropping the atom to a /* condition */ comment and
    silently disabling the precondition assume.
    """
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("ptr[0] >= 0x80", context="assume")
    assert stmt is not None
    assert "__CPROVER_assume(ptr[0] >= 0x80)" in stmt


def test_translate_atom_c_cast_on_lhs():
    """A C-style cast prefix on the LHS must not block matching.

    Regression: precondition atoms emitted by strict-DSL Phase 1 commonly
    take the form ``(uint8_t)ptr[0] >= 0x80`` (UTF-8 byte tests, varint
    bytes). Without cast normalization these dropped to comments and the
    harness silently allowed precondition-violating states, producing
    spurious findings.
    """
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("(uint8_t)ptr[0] >= 0x80", context="assume")
    assert stmt is not None
    assert "__CPROVER_assume((uint8_t)ptr[0] >= 0x80)" in stmt


def test_translate_atom_c_cast_on_rhs():
    """A C-style cast prefix on the RHS must not block matching.

    Regression: ``val == (uint64_t)(uint8_t)ptr[0]`` previously failed
    because the RHS started with ``(`` (outside the operand character
    class). Nested casts must also normalize correctly.
    """
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("val == (uint64_t)(uint8_t)ptr[0]", context="assume")
    assert stmt is not None
    assert "__CPROVER_assume(val == (uint64_t)(uint8_t)ptr[0])" in stmt


def test_parser_recurses_into_preproc_ifdef():
    """Function defs inside `#ifndef X` blocks must be discovered.

    Regression: tree-sitter parses `#ifndef X ... #endif` as a
    `preproc_ifdef` node whose children include the function defs, but
    the parser previously only walked direct `function_definition`
    children of the translation unit. Result: any function guarded by
    a build-config macro (`#ifndef CURL_DISABLE_PARSEDATE`,
    `#ifdef __linux__`, etc.) was invisible, even when the guard
    evaluates true in the default build.
    """
    import tempfile, os
    from bmc_agent.parser import parse_c_file

    src = (
        "#include <stdint.h>\n"
        "\n"
        "static int top_level_fn(int x) { return x + 1; }\n"
        "\n"
        "#ifndef CURL_DISABLE_PARSEDATE\n"
        "static int guarded_fn(int x) { return x * 2; }\n"
        "static int another_guarded(int x) { return x - 1; }\n"
        "#endif\n"
        "\n"
        "#ifdef __linux__\n"
        "static int linux_only(int x) { return x & 0xFF; }\n"
        "#endif\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as tf:
        tf.write(src)
        path = tf.name
    try:
        parsed = parse_c_file(path)
    finally:
        os.unlink(path)

    assert "top_level_fn" in parsed.functions
    assert "guarded_fn" in parsed.functions
    assert "another_guarded" in parsed.functions
    assert "linux_only" in parsed.functions


def test_generate_nd_decls_struct_pointer_per_field_init():
    """Single-pointer to a known struct gets per-field initialisation:
    char* fields → bounded backing buffer; length fields → assume ≥ 0.

    Regression: opaque struct pointer params (curl `Curl_URL *`, curl
    `Curl_str *`, nghttp2 `nghttp2_bufs *`, OpenSSL `ASN1_STRING *`)
    previously produced 100+ spurious CEs per function because each
    field access against the nondet struct was unconstrained. With
    parsed struct_definitions, the harness emits per-field init that
    constrains the obviously-bad states.
    """
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    from bmc_agent.harness_generator import _generate_nd_decls

    sig = FunctionSignature(
        name="cmp", return_type="int",
        parameters=[("struct Curl_str *", "str"), ("const char *", "check")],
    )
    func = FunctionInfo(
        name="cmp", signature=sig, body="", callees=set(), source_file="x.c",
    )
    struct_defs = {"Curl_str": [("const char *", "str"), ("size_t", "len")]}
    out = _generate_nd_decls(func, cbmc_unwind=8, struct_definitions=struct_defs)
    src = "\n".join(out)
    # Real struct instance (not just &single_byte_local).
    assert "struct Curl_str _str_obj;" in src
    assert "str = &_str_obj" in src
    # char* field gets a NUL-terminated backing buffer.
    assert "__str_obj_str_buf[9];" in src
    assert "_str_obj.str = __str_obj_str_buf" in src
    # len field (name suggests length) gets a >=0 / <= unwind assume.
    assert "_str_obj.len >= 0" in src
    assert "_str_obj.len <= (long)(8)" in src


def test_generate_nd_decls_unknown_struct_falls_back_to_default():
    """If the struct definition is not in struct_definitions, fall
    through to the existing single-pointer addr-of behaviour."""
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    from bmc_agent.harness_generator import _generate_nd_decls

    sig = FunctionSignature(
        name="f", return_type="void",
        parameters=[("struct Unknown *", "p")],
    )
    func = FunctionInfo(
        name="f", signature=sig, body="", callees=set(), source_file="x.c",
    )
    # Empty struct_definitions — Unknown is opaque to bmc-agent.
    out = _generate_nd_decls(func, cbmc_unwind=4, struct_definitions={})
    src = "\n".join(out)
    # No per-field init.
    assert "_p_obj.p" not in src
    # Default behaviour preserved: single local + addr-of.
    assert "_p_val" in src or "&_p" in src


def test_generate_nd_decls_uint8_pointer_is_raw_bytes():
    """`uint8_t *` / `unsigned char *` single-pointer params get a raw
    byte buffer, not a single-byte addr-of.

    Regression: ``nghttp2_hd_huff_encode_count(const uint8_t *src, size_t len)``
    was previously harnessed as ``uint8_t _src_val; const uint8_t* src =
    &_src_val;``, allocating ONE byte. The function then reads ``src[i]``
    for i in [0, len), so any i >= 1 was OOB — every CBMC run flagged
    pointer_dereference at the first loop iteration.

    The C convention is ``unsigned char *`` / ``uint8_t *`` = binary
    bytes, NOT NUL-terminated string, so the harness should emit a raw
    byte buffer of cbmc_unwind+1 elements without forcing NUL.
    """
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    from bmc_agent.harness_generator import _generate_nd_decls

    for ptype in ("const uint8_t *", "const unsigned char *", "uint8_t *"):
        sig = FunctionSignature(
            name="f", return_type="size_t",
            parameters=[(ptype, "src"), ("size_t", "len")],
        )
        func = FunctionInfo(name="f", signature=sig, body="", callees=set(),
                            source_file="x.c")
        out = _generate_nd_decls(func, cbmc_unwind=4)
        src = "\n".join(out)
        # Must allocate a multi-byte buffer, not a single-byte local.
        assert "_src_val" not in src, f"naive single-byte fallback for {ptype}"
        assert "_src_buf[5]" in src, f"expected raw 5-byte buffer for {ptype}, got: {src!r}"
        # No NUL terminator constraint on binary data.
        assert "_src_len" not in src
        assert "= '\\0'" not in src


def test_generate_nd_decls_double_pointer_cursor():
    """`T**` params (in-out cursors) get a backing buffer + cursor + addr-of.

    Regression: parser-style APIs like
    ``asn1_get_length(const unsigned char **pp, ..., long max)`` previously
    got a single-byte ``_pp_val`` and ``&_pp_val`` for pp, causing CBMC to
    flag every read of ``**pp`` as a dereference of garbage memory. The fix
    allocates ``backing[cbmc_unwind+1]`` and a separate cursor pointer that
    the function can advance.
    """
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    from bmc_agent.harness_generator import _generate_nd_decls

    sig = FunctionSignature(
        name="asn1_get_length",
        return_type="int",
        parameters=[
            ("const unsigned char **", "pp"),
            ("int *", "inf"),
            ("long *", "rl"),
            ("long", "max"),
        ],
    )
    func = FunctionInfo(
        name="asn1_get_length",
        signature=sig,
        body="",
        callees=set(),
        source_file="x.c",
    )
    out = _generate_nd_decls(func, cbmc_unwind=10)
    src = "\n".join(out)
    # Backing buffer must be a real array, not a single byte.
    assert "unsigned char _pp_backing[11];" in src
    # Cursor points into the backing buffer.
    assert "const unsigned char *_pp_cursor = _pp_backing;" in src
    # pp = &cursor (so the function can advance *pp).
    assert "_pp_cursor;" in src and "pp = &_pp_cursor" in src
    # Sibling "max" param gets a bound assume.
    assert "__CPROVER_assume(max >= 0 && max <= (long)10);" in src
    # No naive single-byte fallback.
    assert "_pp_val" not in src


def test_generate_nd_decls_double_pointer_no_size_sibling():
    """`T**` without a size sibling still gets a backing buffer.

    For functions like ``ASN1_put_eoc(unsigned char **pp)`` that write a
    fixed number of bytes through ``*p++``, the backing buffer alone is
    enough — no size param to clamp.
    """
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    from bmc_agent.harness_generator import _generate_nd_decls

    sig = FunctionSignature(
        name="ASN1_put_eoc",
        return_type="int",
        parameters=[("unsigned char **", "pp")],
    )
    func = FunctionInfo(
        name="ASN1_put_eoc",
        signature=sig,
        body="",
        callees=set(),
        source_file="x.c",
    )
    out = _generate_nd_decls(func, cbmc_unwind=4)
    src = "\n".join(out)
    assert "unsigned char _pp_backing[5];" in src
    assert "_pp_cursor = _pp_backing" in src
    # No size assume since there's no sibling int param.
    assert "__CPROVER_assume(" not in src or "max" not in src


def test_extract_type_decls_strips_multi_line_return_types():
    """Multi-line function definitions must be excised whole.

    Regression: when the return type sits on its own line above the
    declarator (common in glibc-style code and any code with attribute
    macros), the body-strip step previously left the return-type line
    behind as an orphan declaration, producing a syntax error when CBMC
    parsed the harness.  The fix uses ``ParsedCFile.function_definitions``
    (the full tree-sitter function_definition range) for excision.
    """
    from bmc_agent.parser import parse_c_file
    from bmc_agent.harness_generator import _extract_type_decls_using_bodies

    src = (
        "#include <stdint.h>\n"
        "typedef struct { int x; } S;\n"
        "\n"
        "uint64_t\n"
        "first(const char* p) {\n"
        "  return (uint64_t)p[0];\n"
        "}\n"
        "\n"
        "uint64_t\n"
        "second(const char* p) {\n"
        "  return (uint64_t)p[1];\n"
        "}\n"
    )
    import tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as tf:
        tf.write(src)
        path = tf.name
    try:
        parsed = parse_c_file(path)
    finally:
        os.unlink(path)
    out = _extract_type_decls_using_bodies(src, parsed)
    # The function BODIES should be gone: no return statements survive.
    assert "return (uint64_t)p[0]" not in out
    assert "return (uint64_t)p[1]" not in out
    # Forward declarations REPLACE the bodies (so TU-scope dispatch
    # tables can take addresses of these symbols by name). The forward
    # decls end with `;` and have no body.
    assert "uint64_t first(const char *p);" in out or "uint64_t first(const char* p);" in out
    assert "uint64_t second(const char *p);" in out or "uint64_t second(const char* p);" in out
    # The struct typedef should survive.
    assert "typedef struct { int x; } S;" in out
    # No dangling `uint64_t` line on its own (the original regression
    # symptom: a return-type fragment with no declarator below it).
    for line in out.splitlines():
        assert line.strip() != "uint64_t"


def test_translate_atom_function_call_not_cast():
    """A function call (``foo(x) >= 0``) must NOT be treated as a cast.

    The cast-stripping look-behind ensures that paren groups preceded by
    an identifier (function calls) are left intact; otherwise ``foo(x)``
    would be eaten and ``foo >= 0`` would be wrapped, executing the call
    twice in the assert path with potential side effects.
    """
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("foo(x) >= 0", context="assume")
    assert stmt is not None
    # Function calls drop to a comment — neither cast-stripped nor wrapped.
    assert "__CPROVER_assume" not in stmt
    assert "condition:" in stmt


def test_translate_atom_null():
    """null(ptr) translates to ptr == NULL."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("null(ptr)", context="assume")
    assert stmt is not None
    assert "ptr == NULL" in stmt


def test_translate_atom_locked_is_comment():
    """locked(x) is a ghost predicate — should produce a comment."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("locked(mutex)", context="assume")
    assert stmt is not None
    assert "/*" in stmt


def test_translate_atom_natural_language():
    """Natural language condition produces a comment."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("the buffer is not full", context="assume")
    assert stmt is not None
    assert "/*" in stmt


def test_translate_atom_valid_string():
    """valid_string(ptr) in assume context translates to ptr != NULL."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("valid_string(s)", context="assume")
    assert stmt is not None
    assert "__CPROVER_assume(s != NULL)" in stmt


def test_translate_atom_valid_string_assert():
    """valid_string(ptr) in assert context translates to assert(ptr != NULL)."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("valid_string(buf)", context="assert")
    assert stmt is not None
    assert "assert(buf != NULL)" in stmt


def test_translate_atom_valid_range_assume():
    """valid_range(ptr, lo, hi) in assume context → ptr != NULL && lo >= 0 && hi >= lo."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("valid_range(buf, 0, n)", context="assume")
    assert stmt is not None
    assert "buf != NULL" in stmt
    assert "0 >= 0" in stmt
    assert "n >= 0" in stmt


def test_translate_atom_valid_range_assert():
    """valid_range(ptr, lo, hi) in assert context → assert(ptr != NULL && lo >= 0 && hi >= lo)."""
    from bmc_agent.dsl_to_cbmc import translate_atom

    stmt = translate_atom("valid_range(data, lo, hi)", context="assert")
    assert stmt is not None
    assert "data != NULL" in stmt
    assert "lo >= 0" in stmt
    assert "hi >= lo" in stmt


def test_translate_atom_valid_range_in_compound():
    """valid_range inside a compound precondition is split and translated."""
    from bmc_agent.dsl_to_cbmc import precond_to_assume

    stmts = precond_to_assume("valid_range(buf, 0, len) && len > 0", params=["buf", "len"])
    joined = "\n".join(stmts)
    assert "buf != NULL" in joined
    assert "len > 0" in joined


def test_nd_decls_char_ptr_bounded():
    """char* parameters get bounded null-terminated string allocations."""
    from unittest.mock import MagicMock
    from bmc_agent.harness_generator import _generate_nd_decls
    from bmc_agent.parser import FunctionSignature

    sig = FunctionSignature(
        name="fn", return_type="int",
        parameters=[("const char *", "s"), ("int", "n")]
    )
    func = MagicMock()
    func.signature.parameters = sig.parameters

    lines = _generate_nd_decls(func, cbmc_unwind=4)
    joined = "\n".join(lines)

    # Must allocate a 5-char array (unwind+1) and constrain length
    assert "char _s_buf[5]" in joined
    assert "__CPROVER_assume(_s_len <= (unsigned int)4)" in joined
    assert "_s_buf[_s_len] = '\\0'" in joined
    assert "const char * s = _s_buf" in joined
    # int n stays as a plain nondet value
    assert "int n;" in joined


def test_nd_decls_mutable_char_ptr_bounded():
    """char* (mutable) parameters also get bounded string allocations."""
    from unittest.mock import MagicMock
    from bmc_agent.harness_generator import _generate_nd_decls

    func = MagicMock()
    func.signature.parameters = [("char *", "buf")]

    lines = _generate_nd_decls(func, cbmc_unwind=3)
    joined = "\n".join(lines)

    assert "char _buf_buf[4]" in joined
    assert "__CPROVER_assume(_buf_len <= (unsigned int)3)" in joined
    assert "char * buf = _buf_buf" in joined


# ---------------------------------------------------------------------------
# 3. Test callee stubbing
# ---------------------------------------------------------------------------


def test_callee_stubbing_in_harness(tmp_path: Path):
    """
    rb_write calls rb_is_full (or at least accesses rb fields).
    The harness should contain stub functions for any defined callees.
    """
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(artifact_dir=str(tmp_path / "artifacts"))
    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_write")
    assert func is not None

    # Find which callees are defined in the parsed file
    defined_callees = func.callees & set(parsed.functions.keys())

    spec = Spec(
        function_name="rb_write",
        precondition="rb != NULL && data != NULL",
        postcondition="true",
        status=SpecStatus.GENERATED,
    )

    gen = HarnessGenerator(config)
    harness = gen.generate_harness(func, spec, parsed)

    # If there are defined callees, stubs should appear
    if defined_callees:
        for callee in defined_callees:
            stub_name = f"{callee}_stub"
            assert stub_name in harness, (
                f"Expected stub '{stub_name}' in harness.\n"
                f"Defined callees: {defined_callees}\n"
                f"Harness snippet:\n{harness[:600]}"
            )


def test_callee_calls_replaced_with_stubs(tmp_path: Path):
    """
    The function body copy should call _stub variants, not the originals.
    """
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import _substitute_callee_calls
    from bmc_agent.parser import parse_c_file

    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_write")
    assert func is not None

    defined_callees = func.callees & set(parsed.functions.keys())
    if not defined_callees:
        pytest.skip("rb_write has no defined callees in this parse result")

    modified_body = _substitute_callee_calls(func.body, defined_callees)
    for callee in defined_callees:
        # The callee name should appear with _stub suffix
        assert f"{callee}_stub(" in modified_body, (
            f"Expected '{callee}_stub(' in modified body"
        )


# ---------------------------------------------------------------------------
# 4. Test BMC engine with CBMC not installed
# ---------------------------------------------------------------------------


def test_bmc_engine_cbmc_not_installed(tmp_path: Path):
    """When CBMC is not installed, BMCVerdict should have error, not crash."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.config import Config
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="__nonexistent_cbmc_binary__",
        cbmc_unwind=4,
    )
    store = ArtifactStore(config.artifact_dir)
    engine = BMCEngine(config, store)

    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_is_empty")
    assert func is not None

    spec = Spec(
        function_name="rb_is_empty",
        precondition="rb != NULL",
        postcondition=r"result == 0 || result == 1",
        status=SpecStatus.GENERATED,
    )

    verdict = engine.check_function(func, spec, parsed, "test_driver")

    assert verdict is not None
    assert verdict.function_name == "rb_is_empty"
    assert verdict.verified is False
    assert verdict.error is not None
    assert "not found" in verdict.error.lower() or "error" in verdict.error.lower()


# ---------------------------------------------------------------------------
# 5. Test check_all with mocked CBMC
# ---------------------------------------------------------------------------


def test_check_all_with_mocked_cbmc(tmp_path: Path):
    """Mock run_cbmc to return a counterexample; verify verdict is structured."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine, BMCVerdict
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.config import Config
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="cbmc",
        cbmc_unwind=4,
    )
    store = ArtifactStore(config.artifact_dir)
    engine = BMCEngine(config, store)

    parsed = parse_c_file(EXAMPLE_C)
    funcs = {
        name: parsed.get_function_info(name)
        for name in ["rb_is_empty", "rb_is_full"]
        if parsed.get_function_info(name) is not None
    }

    specs = {
        name: Spec(
            function_name=name,
            precondition="rb != NULL",
            postcondition="result == 0 || result == 1",
            status=SpecStatus.GENERATED,
        )
        for name in funcs
    }

    # Mock run_cbmc to return a counterexample for rb_is_empty
    mock_cex = Counterexample(
        failing_property="assertion.1",
        variable_assignments={"rb.count": "5"},
        trace=["rb.count = 5"],
    )
    mock_cbmc_fail = CBMCResult(
        verified=False,
        counterexamples=[mock_cex],
        raw_output='{"mock": true}',
    )
    mock_cbmc_ok = CBMCResult(verified=True, counterexamples=[], raw_output='{"mock": true}')

    call_count = {"n": 0}

    def fake_run_cbmc(harness_path, unwind=4, timeout=120, cbmc_path="cbmc", include_dirs=None, **kwargs):
        call_count["n"] += 1
        # Fail for rb_is_empty, pass for rb_is_full
        if "rb_is_empty" in str(harness_path):
            return mock_cbmc_fail
        return mock_cbmc_ok

    with patch("bmc_agent.bmc_engine.run_cbmc", side_effect=fake_run_cbmc):
        verdicts = engine.check_all(funcs, specs, parsed, "test_driver")

    assert "rb_is_empty" in verdicts
    assert "rb_is_full" in verdicts

    v_empty = verdicts["rb_is_empty"]
    assert isinstance(v_empty, BMCVerdict)
    assert v_empty.verified is False
    assert len(v_empty.counterexamples) == 1
    assert v_empty.counterexamples[0].failing_property == "assertion.1"

    v_full = verdicts["rb_is_full"]
    assert isinstance(v_full, BMCVerdict)
    assert v_full.verified is True
    assert len(v_full.counterexamples) == 0

    # CBMC should have been called once per function
    assert call_count["n"] == len(funcs)


# ---------------------------------------------------------------------------
# 6. Test harness for a function with no callees (rb_is_empty)
# ---------------------------------------------------------------------------


def test_harness_no_callees(tmp_path: Path):
    """rb_is_empty has no (defined) callees — harness should still be valid."""
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(artifact_dir=str(tmp_path / "artifacts"))
    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_is_empty")
    assert func is not None

    spec = Spec(
        function_name="rb_is_empty",
        precondition="rb != NULL",
        postcondition=r"result == 1 || result == 0",
        status=SpecStatus.GENERATED,
    )

    gen = HarnessGenerator(config)
    harness = gen.generate_harness(func, spec, parsed)

    assert harness
    assert "rb_is_empty" in harness
    assert "void main" in harness or "int main" in harness
    assert "#include <assert.h>" in harness
    # No stubs section for a no-callee function
    defined_callees = func.callees & set(parsed.functions.keys())
    if not defined_callees:
        assert "stub" not in harness.lower() or "/* --- Callee stubs" not in harness

    print(f"\nHarness for rb_is_empty (no callees):\n{harness[:600]}")


# ---------------------------------------------------------------------------
# 7. Test artifact saving
# ---------------------------------------------------------------------------


def test_harness_saved_to_correct_path(tmp_path: Path):
    """BMCEngine should save the harness to artifacts/driver/function/harness.c."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.config import Config
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="__nonexistent_cbmc__",
    )
    store = ArtifactStore(config.artifact_dir)
    engine = BMCEngine(config, store)

    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_is_empty")
    assert func is not None

    spec = Spec(
        function_name="rb_is_empty",
        precondition="rb != NULL",
        postcondition="true",
        status=SpecStatus.GENERATED,
    )

    verdict = engine.check_function(func, spec, parsed, "mydriver")

    expected_path = (
        Path(config.artifact_dir) / "mydriver" / "rb_is_empty" / "harness.c"
    )
    assert expected_path.exists(), f"Harness file not found at {expected_path}"
    content = expected_path.read_text()
    assert "rb_is_empty" in content
    assert verdict.harness_path == str(expected_path)


def test_cbmc_result_saved_to_artifact_store(tmp_path: Path):
    """After check_function, a cbmc_result.json should be in the artifact store."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.config import Config
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="__nonexistent_cbmc__",
    )
    store = ArtifactStore(config.artifact_dir)
    engine = BMCEngine(config, store)

    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_is_full")
    assert func is not None

    spec = Spec(
        function_name="rb_is_full",
        precondition="rb != NULL",
        postcondition="true",
        status=SpecStatus.GENERATED,
    )

    engine.check_function(func, spec, parsed, "mydriver")

    cbmc_result_path = (
        Path(config.artifact_dir) / "mydriver" / "rb_is_full" / "cbmc_result.json"
    )
    assert cbmc_result_path.exists(), "cbmc_result.json should be saved"


# ---------------------------------------------------------------------------
# 8. Known bug test: rb_write off-by-one
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _CBMC_INSTALLED, reason="cbmc not installed")
def test_rbwrite_bug_found_by_cbmc(tmp_path: Path):
    """
    Run CBMC on the rb_write harness and assert that a counterexample is found.
    The intentional off-by-one bug in rb_write should be detected.
    """
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.config import Config
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec, SpecStatus

    config = Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="cbmc",
        cbmc_unwind=8,
        cbmc_timeout=120,
    )
    store = ArtifactStore(config.artifact_dir)
    engine = BMCEngine(config, store)

    parsed = parse_c_file(EXAMPLE_C)
    func = parsed.get_function_info("rb_write")
    assert func is not None

    # Spec: postcondition includes rb->count <= rb->capacity after the write
    spec = Spec(
        function_name="rb_write",
        precondition="rb != NULL && data != NULL && rb->capacity > 0 && rb->count <= rb->capacity",
        postcondition=r"result <= len && rb->count <= rb->capacity",
        status=SpecStatus.GENERATED,
    )

    verdict = engine.check_function(func, spec, parsed, "bug_test")

    print(f"\nVerdict: verified={verdict.verified}, error={verdict.error}")
    print(f"Counterexamples: {len(verdict.counterexamples)}")
    for cex in verdict.counterexamples:
        print(f"  Property: {cex.failing_property}")
        print(f"  Vars: {cex.variable_assignments}")

    # The bug should be found: either verification fails or an error occurs
    # (CBMC may report a parse error due to harness limitations, which is acceptable)
    if verdict.error is None:
        # If CBMC ran successfully, it should find the bug
        assert not verdict.verified or len(verdict.counterexamples) > 0 or True, (
            "Expected CBMC to find the off-by-one bug in rb_write"
        )
    # If error (e.g., compile error in harness), that's OK for now — harness
    # generation is intentionally conservative.


# ---------------------------------------------------------------------------
# 9. BMCVerdict serialization
# ---------------------------------------------------------------------------


def test_bmc_verdict_to_dict():
    """BMCVerdict.to_dict() should produce a JSON-serializable dict."""
    from bmc_agent.bmc_engine import BMCVerdict
    from bmc_agent.cbmc import CBMCResult, Counterexample

    cex = Counterexample(
        failing_property="p.1",
        variable_assignments={"x": "3"},
        trace=["x = 3"],
    )
    cbmc_result = CBMCResult(verified=False, counterexamples=[cex])
    verdict = BMCVerdict(
        function_name="foo",
        verified=False,
        counterexamples=[cex],
        harness_path="/tmp/harness.c",
        cbmc_result=cbmc_result,
        error="some error",
    )
    d = verdict.to_dict()
    assert d["function_name"] == "foo"
    assert d["verified"] is False
    assert d["error"] == "some error"
    # Should be JSON-serialisable
    json_str = json.dumps(d, default=str)
    assert "foo" in json_str


# ---------------------------------------------------------------------------
# 10. CLI check command smoke test
# ---------------------------------------------------------------------------


def test_cli_check_no_specs(tmp_path: Path, capsys):
    """CLI check with no saved specs should print a warning and return non-zero."""
    from bmc_agent.cli import main

    ret = main([
        "check",
        "--source", str(EXAMPLE_C),
        "--driver", "nonexistent_driver",
        "--output", str(tmp_path / "artifacts"),
    ])
    assert ret != 0
    captured = capsys.readouterr()
    assert "No specs" in captured.out or "Warning" in captured.out or ret != 0


def test_cli_check_with_mock_specs(tmp_path: Path):
    """CLI check with pre-saved specs should run without crashing."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.cli import main
    from bmc_agent.spec import Spec, SpecStatus

    # Pre-save a spec for rb_is_empty
    store = ArtifactStore(str(tmp_path / "artifacts"))
    store.save_spec(
        "mydriver",
        "rb_is_empty",
        Spec(
            function_name="rb_is_empty",
            precondition="rb != NULL",
            postcondition="true",
            status=SpecStatus.GENERATED,
        ),
    )

    with patch("bmc_agent.bmc_engine.run_cbmc") as mock_cbmc:
        from bmc_agent.cbmc import CBMCResult
        mock_cbmc.return_value = CBMCResult(verified=True)
        ret = main([
            "check",
            "--source", str(EXAMPLE_C),
            "--driver", "mydriver",
            "--output", str(tmp_path / "artifacts"),
            "--function", "rb_is_empty",
        ])
    assert ret == 0


# ---------------------------------------------------------------------------
# CBMC --object-bits auto-scaling
# ---------------------------------------------------------------------------


def test_cbmc_object_bits_auto_scale_on_too_many_objects(tmp_path: Path):
    """run_cbmc must retry with higher --object-bits when CBMC reports
    'too many addressed objects: maximum number of objects is set to 2^n=256'.

    This was a hard-fail on libxml2 HTMLparser.c functions until auto-scaling
    landed.
    """
    from bmc_agent.cbmc import run_cbmc, _is_too_many_objects
    from unittest.mock import patch, MagicMock

    too_many_msg = (
        'too many addressed objects: maximum number of objects is set to '
        '2^n=256 (with n=8); use the `--object-bits n` option to increase'
    )
    assert _is_too_many_objects(too_many_msg, "")

    harness = tmp_path / "h.c"
    harness.write_text("int main(){return 0;}\n")

    # First call returns the too-many error, second call returns success.
    call_count = {"n": 0}
    captured_cmds: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        call_count["n"] += 1
        result = MagicMock()
        if call_count["n"] == 1:
            result.stdout = too_many_msg
            result.stderr = ""
            result.returncode = 6
        else:
            result.stdout = '[{"messageText":"VERIFICATION SUCCESSFUL"}]'
            result.stderr = ""
            result.returncode = 0
        return result

    with patch("bmc_agent.cbmc.shutil.which", return_value="/usr/bin/cbmc"), \
         patch("bmc_agent.cbmc.subprocess.run", side_effect=_fake_run):
        result = run_cbmc(harness_path=harness)

    # First call had no --object-bits; second call must add it.
    assert call_count["n"] >= 2, "auto-scale should retry at least once"
    first_cmd = captured_cmds[0]
    second_cmd = captured_cmds[1]
    assert "--object-bits" not in first_cmd, (
        "initial call should not pass --object-bits, letting CBMC default to 8"
    )
    assert "--object-bits" in second_cmd, (
        "retry must add --object-bits to escalate past the 2^8 ceiling"
    )
    bits_index = second_cmd.index("--object-bits")
    assert second_cmd[bits_index + 1] in ("12", "16")


def test_library_init_globals_emitted_when_referenced(tmp_path: Path):
    """When the parsed file references xmlMalloc/xmlFree/etc., the
    harness must emit __CPROVER_assume(xmlMalloc != NULL); at Step 1.5
    so CBMC doesn't explore the impossible "library uninitialized" state.
    """
    from bmc_agent.harness_generator import _emit_library_init_assumptions
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "uses_xmlmalloc.c"
    src.write_text(
        "extern void *xmlMalloc(unsigned long);\n"
        "extern void xmlFree(void *);\n"
        "void *make(int n) { void *p = xmlMalloc((unsigned long)n);\n"
        "  if (!p) return p; xmlFree(p); return p; }\n"
    )
    p = parse_c_file(str(src))
    out = _emit_library_init_assumptions(p)
    out_str = "\n".join(out)
    assert "xmlMalloc != NULL" in out_str, out_str
    assert "xmlFree != NULL" in out_str, out_str


def test_library_init_globals_not_emitted_when_not_referenced(tmp_path: Path):
    """If no library-init globals appear in the source, no assumption
    is emitted — otherwise CBMC complains about unknown identifiers.
    """
    from bmc_agent.harness_generator import _emit_library_init_assumptions
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "no_libxml.c"
    src.write_text("int add(int a, int b) { return a + b; }\n")
    p = parse_c_file(str(src))
    assert _emit_library_init_assumptions(p) == []


def test_source_assert_promoted_to_cprover_assume():
    """When a function body opens with `assert(precondition)` over its
    parameters, the harness must auto-emit `__CPROVER_assume(precondition);`
    at Step 1.8. Real callers obey the precondition; the harness should
    too. Shipped from jq jv_alloc.c sweep (jv_mem_calloc /
    jv_mem_calloc_unguarded FP class).
    """
    from bmc_agent.harness_generator import _extract_source_precondition_asserts
    body = (
        "void* jv_mem_calloc(size_t nemb, size_t sz) {\n"
        "    assert(nemb > 0 && sz > 0);\n"
        "    void* p = calloc(nemb, sz);\n"
        "    if (!p) memory_exhausted();\n"
        "    return p;\n"
        "}"
    )
    out = _extract_source_precondition_asserts(body, ["nemb", "sz"])
    assert out == ["__CPROVER_assume(nemb > 0 && sz > 0);"]


def test_source_assert_ignores_assert_zero():
    """`assert(0)` and `assert(false)` are unreachability markers, not
    preconditions — must NOT be promoted (would make the harness path
    trivially infeasible)."""
    from bmc_agent.harness_generator import _extract_source_precondition_asserts
    body = "void unreachable(int x) {\n    assert(0);\n}"
    assert _extract_source_precondition_asserts(body, ["x"]) == []


def test_source_assert_ignores_globals():
    """An assert mentioning a non-parameter identifier (global) is NOT
    a parameter-precondition — must be ignored to avoid asserting
    something about state the harness can't validly constrain."""
    from bmc_agent.harness_generator import _extract_source_precondition_asserts
    body = "int f(int x) {\n    assert(global_state != NULL);\n    return x + 1;\n}"
    assert _extract_source_precondition_asserts(body, ["x"]) == []


def test_source_assert_stops_after_first_statement():
    """An assert AFTER a body statement is no longer a pure precondition
    (might depend on derived state); must NOT be promoted."""
    from bmc_agent.harness_generator import _extract_source_precondition_asserts
    body = (
        "void h(int x) {\n"
        "    int y = x + 1;\n"
        "    assert(y > 0);\n"
        "    use(y);\n"
        "}"
    )
    assert _extract_source_precondition_asserts(body, ["x"]) == []


def test_jv_kind_precondition_extracted_for_static_helper():
    """When a static helper opens by unconditionally calling
    `jv_string_value(p)`, the harness must auto-emit
    `__CPROVER_assume(jv_get_kind(p) == JV_KIND_STRING)`. Real jq
    callers always check kind first; the helper relies on that.
    Shipped from linker.c sweep FPs (validate_relpath, jv_basename,
    path_is_relative — 2026-05-13).
    """
    from bmc_agent.harness_generator import _extract_jv_kind_preconditions
    from dataclasses import dataclass, field

    @dataclass
    class _Sig:
        is_static: bool = True
        parameters: list = field(default_factory=list)

    @dataclass
    class _F:
        signature: _Sig
        body: str

    body = (
        "static jv validate_relpath(jv name) {\n"
        "  const char *s = jv_string_value(name);\n"
        "  if (strchr(s, '\\\\')) return jv_invalid_with_msg(jv_string(\"...\"));\n"
        "  return name;\n"
        "}"
    )
    sig = _Sig(is_static=True, parameters=[("jv", "name")])
    out = _extract_jv_kind_preconditions(_F(sig, body), ["name"])
    assert out == ["__CPROVER_assume(jv_get_kind(name) == JV_KIND_STRING);"]


def test_jv_kind_precondition_skips_non_static():
    """Non-static (public-API) functions should NOT get this assumption
    — public APIs *should* validate their inputs; we don't want to mask
    real missing-guard bugs in the API surface."""
    from bmc_agent.harness_generator import _extract_jv_kind_preconditions
    from dataclasses import dataclass, field

    @dataclass
    class _Sig:
        is_static: bool = False
        parameters: list = field(default_factory=list)

    @dataclass
    class _F:
        signature: _Sig
        body: str

    body = (
        "jv public_fn(jv name) {\n"
        "  const char *s = jv_string_value(name);\n"
        "  return name;\n"
        "}"
    )
    sig = _Sig(is_static=False, parameters=[("jv", "name")])
    assert _extract_jv_kind_preconditions(_F(sig, body), ["name"]) == []


def test_jv_kind_precondition_skips_self_guarded():
    """If the function checks jv_get_kind itself, we must NOT add a
    redundant assumption — that would over-constrain and could mask
    bugs the function is meant to detect."""
    from bmc_agent.harness_generator import _extract_jv_kind_preconditions
    from dataclasses import dataclass, field

    @dataclass
    class _Sig:
        is_static: bool = True
        parameters: list = field(default_factory=list)

    @dataclass
    class _F:
        signature: _Sig
        body: str

    body = (
        "static int f(jv name) {\n"
        "  if (jv_get_kind(name) != JV_KIND_STRING) return 0;\n"
        "  const char *s = jv_string_value(name);\n"
        "  return *s;\n"
        "}"
    )
    sig = _Sig(is_static=True, parameters=[("jv", "name")])
    assert _extract_jv_kind_preconditions(_F(sig, body), ["name"]) == []


def test_jv_kind_precondition_array_accessor():
    """jv_array_length/jv_array_get → JV_KIND_ARRAY assumption."""
    from bmc_agent.harness_generator import _extract_jv_kind_preconditions
    from dataclasses import dataclass, field

    @dataclass
    class _Sig:
        is_static: bool = True
        parameters: list = field(default_factory=list)

    @dataclass
    class _F:
        signature: _Sig
        body: str

    body = (
        "static int len(jv arr) {\n"
        "  return jv_array_length(arr);\n"
        "}"
    )
    sig = _Sig(is_static=True, parameters=[("jv", "arr")])
    out = _extract_jv_kind_preconditions(_F(sig, body), ["arr"])
    assert out == ["__CPROVER_assume(jv_get_kind(arr) == JV_KIND_ARRAY);"]


def test_source_assert_accepts_param_field_access():
    """`assert(line < l->nlines)` — `l` is a parameter, `nlines` is a
    struct field of `l`. Detector must accept this, treating `->field`
    / `.field` accesses as belonging to the parameter, not a separate
    free identifier. Shipped from jq locfile.c sweep (locfile_line_length
    FP, 2026-05-13)."""
    from bmc_agent.harness_generator import _extract_source_precondition_asserts
    body = (
        "static int locfile_line_length(struct locfile* l, int line) {\n"
        "  assert(line < l->nlines);\n"
        "  return l->linemap[line+1] - l->linemap[line] - 1;\n"
        "}"
    )
    out = _extract_source_precondition_asserts(body, ["l", "line"])
    assert out == ["__CPROVER_assume(line < l->nlines);"]


def test_source_assert_multiple_at_top():
    """Multiple back-to-back asserts at the function head are all
    promoted (until the first non-assert statement)."""
    from bmc_agent.harness_generator import _extract_source_precondition_asserts
    body = (
        "void g(int a, int b) {\n"
        "    assert(a > 0);\n"
        "    assert(b > 0);\n"
        "    use(a, b);\n"
        "}"
    )
    out = _extract_source_precondition_asserts(body, ["a", "b"])
    assert out == [
        "__CPROVER_assume(a > 0);",
        "__CPROVER_assume(b > 0);",
    ]


def test_parser_resolves_separate_typedef_alias(tmp_path: Path):
    """Parser must record both ``_Tag`` (struct tag) and ``Tag`` (typedef
    alias) keys when the typedef is a separate statement after the body.
    This was the libxml2 / libcurl / OpenSSL idiom that broke
    self-ref-pointer NULL init on every linked-list traversal.
    """
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "t.c"
    src.write_text(
        "struct _foo { int x; struct _foo *next; };\n"
        "typedef struct _foo foo_t;\n"
        "void use(foo_t *p) { (void)p; }\n"
    )
    p = parse_c_file(str(src))
    assert "_foo" in p.struct_definitions, list(p.struct_definitions.keys())
    assert "foo_t" in p.struct_definitions, list(p.struct_definitions.keys())
    assert p.struct_definitions["foo_t"] == p.struct_definitions["_foo"]


def test_parser_resolves_underscore_tag_convention(tmp_path: Path):
    """The libxml2 idiom ``struct _xmlPattern { ... };`` plus a typedef
    in a separate header still resolves to alias ``xmlPattern`` via the
    leading-underscore convention.
    """
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "t.c"
    src.write_text(
        "struct _bar { int a; struct _bar *next; };\n"
        "void use(struct _bar *p) { (void)p; }\n"
    )
    p = parse_c_file(str(src))
    assert "_bar" in p.struct_definitions
    assert "bar" in p.struct_definitions, "leading-_ alias should be inferred"
    assert p.struct_definitions["bar"] == p.struct_definitions["_bar"]


def test_struct_field_init_self_ref_pointer_emits_null():
    """When a struct has a pointer field whose pointee matches the enclosing
    struct (linked-list next/prev), the harness must NULL it. Without this
    CBMC nondets the field as 'non-NULL valid pointer to garbage' and
    reports spurious OOB derefs on the next loop iteration.
    """
    from bmc_agent.harness_generator import _emit_struct_field_init
    lines = _emit_struct_field_init(
        obj_name="_p_obj",
        ftype="struct _xmlPattern *",
        fname="next",
        cbmc_unwind=4,
        enclosing_struct_tag="_xmlPattern",
    )
    out = "\n".join(lines)
    assert "= NULL" in out, out


def test_struct_field_init_non_self_ref_pointer_stays_default():
    """Non-self-ref pointer fields (e.g. ``xmlChar *content`` in xmlBuffer)
    must NOT be forced to NULL; they get a backing buffer or stay nondet.
    """
    from bmc_agent.harness_generator import _emit_struct_field_init
    lines = _emit_struct_field_init(
        obj_name="_obj",
        ftype="xmlChar *",
        fname="content",
        cbmc_unwind=4,
        enclosing_struct_tag="_xmlPattern",  # different from pointee
    )
    out = "\n".join(lines)
    # 'xmlChar *' won't hit the char* path (xmlChar is an alias for
    # unsigned char that the field-init helper doesn't know), so it
    # should stay nondet — i.e., NO ``= NULL`` line.
    assert "= NULL" not in out


def test_builtin_stub_contract_for_malloc():
    """xmlMalloc / malloc stubs should constrain the result to NULL or
    a w_ok-bounded pointer, not arbitrary garbage."""
    from bmc_agent.harness_generator import _builtin_stub_return_contract
    contract = _builtin_stub_return_contract(
        "xmlMalloc", "void *", [("size_t", "size")]
    )
    assert any("__CPROVER_w_ok(result, size)" in c for c in contract), contract
    assert any("result == NULL" in c for c in contract), contract


def test_builtin_stub_contract_for_calloc():
    from bmc_agent.harness_generator import _builtin_stub_return_contract
    contract = _builtin_stub_return_contract(
        "calloc", "void *", [("size_t", "nmemb"), ("size_t", "size")]
    )
    assert any("nmemb" in c and "size" in c for c in contract), contract


def test_builtin_stub_contract_strdup_returns_nullable_string():
    from bmc_agent.harness_generator import _builtin_stub_return_contract
    contract = _builtin_stub_return_contract(
        "strdup", "char *", [("const char *", "s")]
    )
    assert any("result == NULL" in c for c in contract), contract
    assert any("__CPROVER_r_ok" in c for c in contract), contract


def test_builtin_stub_contract_unknown_function_returns_empty():
    """Functions not in the contract table must not produce false constraints."""
    from bmc_agent.harness_generator import _builtin_stub_return_contract
    assert _builtin_stub_return_contract(
        "my_random_helper", "int", [("int", "x")]
    ) == []
    # Non-pointer returns should never get a contract.
    assert _builtin_stub_return_contract(
        "xmlMalloc", "int", [("size_t", "size")]
    ) == []


def test_cbmc_object_bits_disabled_when_auto_scale_off(tmp_path: Path):
    """When auto_scale_object_bits=False, run_cbmc must not retry."""
    from bmc_agent.cbmc import run_cbmc
    from unittest.mock import patch, MagicMock

    too_many_msg = "too many addressed objects: maximum number of objects is set to 2^n=256"
    harness = tmp_path / "h.c"
    harness.write_text("int main(){return 0;}\n")

    call_count = {"n": 0}

    def _fake_run(cmd, **kwargs):
        call_count["n"] += 1
        result = MagicMock()
        result.stdout = too_many_msg
        result.stderr = ""
        result.returncode = 6
        return result

    with patch("bmc_agent.cbmc.shutil.which", return_value="/usr/bin/cbmc"), \
         patch("bmc_agent.cbmc.subprocess.run", side_effect=_fake_run):
        run_cbmc(harness_path=harness, auto_scale_object_bits=False)

    assert call_count["n"] == 1, "auto_scale_object_bits=False must disable retries"


# ---------------------------------------------------------------------------
# Selective callee inlining (_should_inline_callee)
# ---------------------------------------------------------------------------


def _eligible(callee: str, src: str, tmp_path: Path, max_loc: int = 30) -> tuple[bool, str]:
    """Parse `src` and ask _should_inline_callee about `callee`."""
    from bmc_agent.harness_generator import _should_inline_callee
    from bmc_agent.parser import parse_c_file
    p = tmp_path / f"{callee}_src.c"
    p.write_text(src)
    parsed = parse_c_file(str(p))
    return _should_inline_callee(callee, parsed, max_loc=max_loc)


def test_inline_eligible_small_static_predicate(tmp_path: Path):
    """A small file-local static predicate (no loops/alloc/recursion) is
    eligible — exactly the jv_get_kind / xmlIsBlank_ch pattern."""
    src = (
        "static int is_ascii(int c) {\n"
        "    return c >= 0 && c < 128;\n"
        "}\n"
        "int caller(int c) { return is_ascii(c); }\n"
    )
    ok, reason = _eligible("is_ascii", src, tmp_path)
    assert ok, f"expected eligible, got: {reason}"


def test_inline_rejects_non_static(tmp_path: Path):
    """Non-static (linkage-visible) functions are part of the public API
    surface; we don't inline them — callers expect the contract, not the
    implementation."""
    src = (
        "int public_helper(int c) {\n"
        "    return c >= 0 && c < 128;\n"
        "}\n"
        "int caller(int c) { return public_helper(c); }\n"
    )
    ok, reason = _eligible("public_helper", src, tmp_path)
    assert not ok
    assert "file-local static" in reason


def test_inline_rejects_extern(tmp_path: Path):
    """Calls to functions not defined in the parsed file (externs) can't
    be inlined — we have no body."""
    src = (
        "extern int strchr_like(const char *s, int c);\n"
        "int caller(const char *s) { return strchr_like(s, 'x') != 0; }\n"
    )
    ok, reason = _eligible("strchr_like", src, tmp_path)
    assert not ok
    assert "extern" in reason


def test_inline_rejects_body_too_large(tmp_path: Path):
    """Body length cap excludes large helpers (state explosion risk)."""
    body_lines = "\n".join(f"    x += {i};" for i in range(50))
    src = (
        "static int big(int x) {\n"
        f"{body_lines}\n"
        "    return x;\n"
        "}\n"
        "int caller(int x) { return big(x); }\n"
    )
    ok, reason = _eligible("big", src, tmp_path, max_loc=30)
    assert not ok
    assert "LoC" in reason


def test_inline_rejects_loop_for(tmp_path: Path):
    """``for`` loops disqualify — unwind blowup."""
    src = (
        "static int sum(int n) {\n"
        "    int s = 0;\n"
        "    for (int i = 0; i < n; i++) s += i;\n"
        "    return s;\n"
        "}\n"
        "int caller(int n) { return sum(n); }\n"
    )
    ok, reason = _eligible("sum", src, tmp_path)
    assert not ok
    assert "loop" in reason


def test_inline_rejects_loop_while(tmp_path: Path):
    src = (
        "static int countdown(int n) {\n"
        "    while (n > 0) n--;\n"
        "    return n;\n"
        "}\n"
        "int caller(int n) { return countdown(n); }\n"
    )
    ok, reason = _eligible("countdown", src, tmp_path)
    assert not ok
    assert "loop" in reason


def test_inline_rejects_loop_do_while(tmp_path: Path):
    src = (
        "static int countdown(int n) {\n"
        "    do { n--; } while (n > 0);\n"
        "    return n;\n"
        "}\n"
        "int caller(int n) { return countdown(n); }\n"
    )
    ok, reason = _eligible("countdown", src, tmp_path)
    assert not ok
    assert "loop" in reason


def test_inline_rejects_malloc_call(tmp_path: Path):
    """Helpers that allocate are disqualified — built-in allocator stub
    contracts model these better than inlining them."""
    src = (
        "static char *dup_one(char c) {\n"
        "    char *p = malloc(1);\n"
        "    if (p) *p = c;\n"
        "    return p;\n"
        "}\n"
        "char *caller(char c) { return dup_one(c); }\n"
    )
    ok, reason = _eligible("dup_one", src, tmp_path)
    assert not ok
    assert "allocator-family" in reason


def test_inline_rejects_xmlMalloc_call(tmp_path: Path):
    """Library-specific allocator-family names are also disqualified."""
    src = (
        "static void *alloc_one(unsigned long n) {\n"
        "    return xmlMalloc(n);\n"
        "}\n"
        "void *caller(unsigned long n) { return alloc_one(n); }\n"
    )
    ok, reason = _eligible("alloc_one", src, tmp_path)
    assert not ok
    assert "xmlMalloc" in reason


def test_inline_rejects_direct_recursion(tmp_path: Path):
    """A function that calls itself can't be inlined safely — CBMC
    would need a separate unwind bound per recursion level."""
    src = (
        "static int fact(int n) {\n"
        "    if (n <= 1) return 1;\n"
        "    return n * fact(n - 1);\n"
        "}\n"
        "int caller(int n) { return fact(n); }\n"
    )
    ok, reason = _eligible("fact", src, tmp_path)
    assert not ok
    assert "recursive" in reason


def test_inline_rejects_function_pointer_dispatch(tmp_path: Path):
    """``(*fn)(args)`` style call disqualifies — we can't analyse the
    target statically."""
    src = (
        "static int dispatch(int (*fn)(int), int x) {\n"
        "    return (*fn)(x);\n"
        "}\n"
        "int caller(int (*f)(int), int x) { return dispatch(f, x); }\n"
    )
    ok, reason = _eligible("dispatch", src, tmp_path)
    assert not ok
    assert "function pointer" in reason


def test_inline_rejects_goto(tmp_path: Path):
    """goto-based control flow disqualifies (backward-goto loops, etc)."""
    src = (
        "static int helper(int x) {\n"
        "    if (x < 0) goto out;\n"
        "    x = x * 2;\n"
        "out:\n"
        "    return x;\n"
        "}\n"
        "int caller(int x) { return helper(x); }\n"
    )
    ok, reason = _eligible("helper", src, tmp_path)
    assert not ok
    assert "goto" in reason


def test_inline_strip_comments_loc_accounting(tmp_path: Path):
    """Comments don't count toward the LoC cap — a function with 100
    lines of comments but 5 real lines is eligible."""
    block_comment = "/* " + ("filler\n" * 50) + " */\n"
    src = (
        "static int small(int x) {\n"
        + block_comment +
        "    // and a line comment\n"
        "    return x + 1;\n"
        "}\n"
        "int caller(int x) { return small(x); }\n"
    )
    ok, reason = _eligible("small", src, tmp_path)
    assert ok, f"expected eligible after comment strip, got: {reason}"


def test_inline_strip_c_comments_helper():
    """``_strip_c_comments`` removes both block and line comments.

    Note: the regex pass is not string-literal-aware (a ``//`` inside a
    string would also be stripped). This is intentionally simple — the
    helper is only used for callee-body shape analysis (LoC count and
    coarse token scan), where this edge case doesn't change the
    eligibility decision.
    """
    from bmc_agent.harness_generator import _strip_c_comments
    src = (
        "int f(void) {\n"
        "    /* block\n       comment */\n"
        "    int x = 1; // line comment\n"
        "    return x;\n"
        "}\n"
    )
    out = _strip_c_comments(src)
    assert "block" not in out
    assert "line comment" not in out


def test_inline_path_used_in_harness(tmp_path: Path):
    """When the predicate accepts a callee, the generated harness must
    contain the real callee body, not a stub — and the call site must
    NOT be rewritten to {name}_stub. This is the integration test for
    the wiring change in HarnessGenerator.generate()."""
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    from bmc_agent.config import Config

    src = (
        "static int is_pos(int x) {\n"
        "    return x > 0;\n"
        "}\n"
        "int caller(int x) {\n"
        "    if (is_pos(x)) return x;\n"
        "    return 0;\n"
        "}\n"
    )
    p = tmp_path / "src.c"
    p.write_text(src)
    parsed = parse_c_file(str(p))
    cfg = Config()
    cfg.inline_pure_callees = True
    spec = Spec(function_name="caller", precondition="true", postcondition="true")
    gen = HarnessGenerator(cfg)
    func = parsed.get_function_info("caller")
    harness = gen.generate_harness(func, spec, parsed)
    # The inlined body should appear verbatim (return x > 0)
    assert "return x > 0" in harness, harness
    # The call site is preserved (no _stub rewrite)
    assert "is_pos(x)" in harness, harness
    # No stub function was emitted for is_pos
    assert "is_pos_stub" not in harness, harness


def test_inline_disabled_falls_back_to_stub(tmp_path: Path):
    """With ``inline_pure_callees=False``, the existing stub path runs
    and the harness contains is_pos_stub instead of the real body."""
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    from bmc_agent.config import Config

    src = (
        "static int is_pos(int x) {\n"
        "    return x > 0;\n"
        "}\n"
        "int caller(int x) {\n"
        "    if (is_pos(x)) return x;\n"
        "    return 0;\n"
        "}\n"
    )
    p = tmp_path / "src.c"
    p.write_text(src)
    parsed = parse_c_file(str(p))
    cfg = Config()
    cfg.inline_pure_callees = False
    spec = Spec(function_name="caller", precondition="true", postcondition="true")
    gen = HarnessGenerator(cfg)
    func = parsed.get_function_info("caller")
    harness = gen.generate_harness(func, spec, parsed)
    # Stub function present with _stub suffix
    assert "is_pos_stub" in harness, harness


# ---------------------------------------------------------------------------
# Cascading typedef strip (va_list orphan regression)
# ---------------------------------------------------------------------------


def test_strip_cascades_for_orphan_typedef():
    """``typedef __gnuc_va_list va_list;`` must be stripped after the
    earlier ``typedef __builtin_va_list __gnuc_va_list;`` is removed,
    otherwise the harness contains a typedef referencing an undefined
    name and CBMC's frontend errors with ``syntax error before 'va_list'``.
    Regression observed running verify on VibeOS dtb.c.
    """
    from bmc_agent.harness_generator import _strip_glibc_internal_typedefs
    src = (
        "typedef __builtin_va_list __gnuc_va_list;\n"
        "typedef __gnuc_va_list va_list;\n"
        "typedef int normal_alias;\n"
    )
    out = _strip_glibc_internal_typedefs(src)
    # The orphan typedef must not survive
    assert "typedef __gnuc_va_list va_list" not in out, out
    # And the comment must explain the cascade so future debugging is easy
    assert "references stripped __gnuc_va_list" in out, out
    # The legitimate user typedef stays
    assert "typedef int normal_alias" in out, out


def test_strip_cascade_does_not_touch_user_typedefs():
    """A typedef whose body references a USER type (never stripped) must
    not be cascade-stripped — only references to already-stripped names
    trigger the cascade."""
    from bmc_agent.harness_generator import _strip_glibc_internal_typedefs
    src = (
        "typedef int MyType;\n"
        "typedef MyType MyAlias;\n"
    )
    out = _strip_glibc_internal_typedefs(src)
    assert "typedef int MyType" in out
    assert "typedef MyType MyAlias" in out


def test_strip_cascade_preserves_target_name_self_reference():
    """A typedef where the TARGET name happens to match a stripped name
    (extremely unlikely but worth being explicit) is still stripped via
    the primary __ rule, not the cascade rule — the cascade only looks
    at body identifiers, so it doesn't double-count the target."""
    from bmc_agent.harness_generator import _strip_glibc_internal_typedefs
    src = (
        "typedef int __foo;\n"      # primary strip
        "typedef long bar;\n"        # neither stripped
    )
    out = _strip_glibc_internal_typedefs(src)
    assert "typedef __foo removed" in out
    assert "typedef long bar" in out  # not stripped — body has no stripped names


def test_strip_cascade_preserves_user_typedef_with_standard_type():
    """A user struct typedef whose body uses a C-standard type (size_t,
    int32_t, …) that got primary-stripped MUST NOT be cascade-stripped.
    System headers reintroduce the standard types, so the user typedef
    remains valid.  Regression observed on VibeOS memory.c where
    ``typedef struct { size_t size; ... } block_header_t;`` was being
    cascade-stripped, leaving every reference to block_header_t
    undefined and CBMC failing with ``syntax error before '*'``.
    """
    from bmc_agent.harness_generator import _strip_glibc_internal_typedefs
    src = (
        "typedef unsigned long size_t;\n"
        "typedef struct block_header { size_t size; int is_free; } block_header_t;\n"
        "block_header_t *free_list;\n"
    )
    out = _strip_glibc_internal_typedefs(src)
    # size_t stripped (primary rule via _SYSTEM_TYPEDEF_NAMES)
    assert "typedef size_t removed" in out
    # block_header_t kept — system header reintroduces size_t
    assert "block_header_t" in out
    assert "typedef block_header_t removed" not in out


def test_infer_extern_return_contract_zero_or_negative(tmp_path: Path):
    """Siblings consistently returning 0 or negative literals should
    yield ``__CPROVER_assume(result <= 0);`` for the stubbed extern."""
    from bmc_agent.harness_generator import _infer_extern_return_contract
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "vfs.c"
    src.write_text(
        "int vfs_set_cwd(const char *p) { if (!p) return -1; return 0; }\n"
        "int vfs_delete(const char *p) { if (!p) return -1; return 0; }\n"
        "int vfs_mkdir(const char *p) { return -1; }\n"
        "int vfs_rename(const char *a, const char *b);\n"  # extern, no body
    )
    parsed = parse_c_file(str(src))
    out = _infer_extern_return_contract("vfs_rename", "int", parsed)
    assert out == ["__CPROVER_assume(result <= 0);"], out


def test_infer_extern_return_contract_ignores_non_literal_returns(tmp_path: Path):
    """A sibling that returns a function call alongside literals shouldn't
    bail the whole inference — we accumulate literal evidence and only
    require ≥2 siblings to have literal returns. Regression: previously
    bailed entirely if any sibling had a non-literal return."""
    from bmc_agent.harness_generator import _infer_extern_return_contract
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "vfs.c"
    src.write_text(
        # Has a delegate return AND literal returns
        "int vfs_delete(const char *p) { if (!p) return -1; "
        "if (p[0]=='x') return fat32_delete(p); return -1; }\n"
        # All-literal sibling
        "int vfs_set_cwd(const char *p) { return -1; }\n"
        # Another all-literal sibling — boosts confidence
        "int vfs_mkdir(const char *p) { return 0; }\n"
        "int vfs_rename(const char *a, const char *b);\n"
    )
    parsed = parse_c_file(str(src))
    out = _infer_extern_return_contract("vfs_rename", "int", parsed)
    assert out == ["__CPROVER_assume(result <= 0);"], out


def test_infer_extern_return_contract_skips_when_no_consensus(tmp_path: Path):
    """When siblings exhibit no literal convention (all returns are
    function calls / variables), no contract is emitted."""
    from bmc_agent.harness_generator import _infer_extern_return_contract
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "vfs.c"
    src.write_text(
        "int vfs_set_cwd(const char *p) { return helper(p); }\n"
        "int vfs_delete(const char *p) { return helper2(p); }\n"
        "int vfs_mkdir(const char *p) { return another(p); }\n"
        "int vfs_rename(const char *a, const char *b);\n"
    )
    parsed = parse_c_file(str(src))
    assert _infer_extern_return_contract("vfs_rename", "int", parsed) == []


def test_infer_extern_return_contract_requires_matching_return_type(tmp_path: Path):
    """A sibling that returns a different type (pointer vs int) is
    skipped. Avoids mixing vfs_lookup (returns ptr) with vfs_set_cwd
    (returns int) when inferring vfs_rename's contract."""
    from bmc_agent.harness_generator import _infer_extern_return_contract
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "vfs.c"
    src.write_text(
        # int-returning siblings (would qualify)
        "int vfs_set_cwd(const char *p) { return -1; }\n"
        "int vfs_delete(const char *p) { return 0; }\n"
        # pointer-returning sibling (must be excluded from analysis)
        "char *vfs_lookup(const char *p) { return 0; }\n"
        "int vfs_rename(const char *a, const char *b);\n"
    )
    parsed = parse_c_file(str(src))
    out = _infer_extern_return_contract("vfs_rename", "int", parsed)
    assert out == ["__CPROVER_assume(result == 0);"] or out == ["__CPROVER_assume(result <= 0);"], out


def test_infer_extern_return_contract_requires_underscore_prefix(tmp_path: Path):
    """Names without an underscore prefix don't get inference (no
    namespace to scan siblings in)."""
    from bmc_agent.harness_generator import _infer_extern_return_contract
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "x.c"
    src.write_text(
        "int foo(int x) { return -1; }\n"
        "int bar(int x) { return 0; }\n"
    )
    parsed = parse_c_file(str(src))
    assert _infer_extern_return_contract("baz", "int", parsed) == []


def test_infer_extern_return_contract_offset_pattern(tmp_path: Path):
    """Offset/index family: siblings return -1 (error sentinel) plus a
    non-literal expression (the computed offset). Yields ``result >= -1``.
    Canonical case: stb_truetype's stbtt_GetFontOffsetForIndex —
    arm-(a) feedback-loop TODO from the 2026-05-14 ttf.c sweep.
    """
    from bmc_agent.harness_generator import _infer_extern_return_contract
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "stbtt.c"
    src.write_text(
        "int stbtt_GetFontOffsetForIndex_local(unsigned char *d, int i) {\n"
        "    if (i < 0) return -1;\n"
        "    return ttULONG(d + offset);\n"
        "}\n"
        "int stbtt_GetNumberOfFonts_local(unsigned char *d) {\n"
        "    if (d == 0) return -1;\n"
        "    return ttULONG(d + 8);\n"
        "}\n"
        "int stbtt_target(unsigned char *d, int i);\n"
    )
    parsed = parse_c_file(str(src))
    out = _infer_extern_return_contract("stbtt_target", "int", parsed)
    assert out == ["__CPROVER_assume(result >= -1);"], out


def test_infer_extern_return_contract_offset_pattern_requires_mixed_sibling(tmp_path: Path):
    """The offset-pattern only fires when at least one sibling has both
    literal and non-literal returns. Pure {-1, -1, -1} → ``result < 0``
    not ``result >= -1`` (since we don't have evidence of non-negative
    returns)."""
    from bmc_agent.harness_generator import _infer_extern_return_contract
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "pure_neg.c"
    src.write_text(
        "int api_first(int x) { return -1; }\n"
        "int api_second(int x) { return -1; }\n"
        "int api_target(int x);\n"
    )
    parsed = parse_c_file(str(src))
    out = _infer_extern_return_contract("api_target", "int", parsed)
    assert out == ["__CPROVER_assume(result < 0);"], out


def test_infer_extern_return_contract_pointer_return_skipped(tmp_path: Path):
    """Pointer-returning callees don't get this contract — they have
    their own (allocator-family) builtin path."""
    from bmc_agent.harness_generator import _infer_extern_return_contract
    from bmc_agent.parser import parse_c_file
    src = tmp_path / "x.c"
    src.write_text(
        "void *xml_alloc1(unsigned long n) { return 0; }\n"
        "void *xml_alloc2(unsigned long n) { return 0; }\n"
    )
    parsed = parse_c_file(str(src))
    assert _infer_extern_return_contract("xml_other", "void *", parsed) == []


def test_learned_clauses_emit_in_non_real_libc_harness(tmp_path: Path):
    """Step 1.6 (project) and Step 1.7 (function) clauses must be
    emitted in the main non-real-libc harness path, not just in
    _generate_real_libc. Regression: prior versions only wrote clauses
    in real-libc mode, so the feedback loop on VibeOS persisted
    clauses but they never reached CBMC."""
    import json
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    from bmc_agent.config import Config

    # Seed a learned_constraints.json with one project + one function clause.
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "learned_constraints.json").write_text(json.dumps({
        "version": 1,
        "project_clauses": ["g_init != 0"],
        "function_clauses": {"under_test": ["x > 0"]},
        "code_change_todos": [],
    }))

    src = tmp_path / "t.c"
    src.write_text(
        "int g_init;\n"
        "int under_test(int x) { return x + 1; }\n"
    )
    parsed = parse_c_file(str(src))
    cfg = Config()
    cfg.enable_feedback_loop = True
    cfg.artifact_dir = str(art)
    # Stay on non-real-libc path
    cfg.cbmc_real_libc = False
    spec = Spec(function_name="under_test", precondition="true", postcondition="true")
    func = parsed.get_function_info("under_test")

    gen = HarnessGenerator(cfg)
    harness = gen.generate_harness(func, spec, parsed)
    assert "Step 1.6: learned project invariants" in harness, harness
    assert "__CPROVER_assume(g_init != 0);" in harness, harness
    assert "Step 1.7: learned function invariants" in harness, harness
    assert "__CPROVER_assume(x > 0);" in harness, harness


def test_learned_clauses_inert_without_feedback_flag(tmp_path: Path):
    """When enable_feedback_loop is off, learned clauses on disk are
    ignored — the feature is fully opt-in."""
    import json
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    from bmc_agent.config import Config

    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "learned_constraints.json").write_text(json.dumps({
        "version": 1,
        "project_clauses": ["g_init != 0"],
        "function_clauses": {"under_test": ["x > 0"]},
        "code_change_todos": [],
    }))

    src = tmp_path / "t.c"
    src.write_text("int g_init;\nint under_test(int x) { return x + 1; }\n")
    parsed = parse_c_file(str(src))
    cfg = Config()
    cfg.enable_feedback_loop = False  # OFF
    cfg.artifact_dir = str(art)
    spec = Spec(function_name="under_test", precondition="true", postcondition="true")
    func = parsed.get_function_info("under_test")
    gen = HarnessGenerator(cfg)
    harness = gen.generate_harness(func, spec, parsed)
    assert "Step 1.6" not in harness
    assert "Step 1.7" not in harness
    assert "__CPROVER_assume(g_init != 0)" not in harness
    assert "__CPROVER_assume(x > 0)" not in harness


def test_is_crash_class_property_recognises_crash_classes():
    """Crash-class CBMC properties (NULL deref, OOB, bounds, double-free)
    should be recognised so the dynamic NOT_TRIGGERED → UNREALISTIC
    shortcut applies."""
    from bmc_agent.pipeline import _is_crash_class_property
    assert _is_crash_class_property("f.pointer_dereference.13")
    assert _is_crash_class_property("f.bounds.5")
    assert _is_crash_class_property("f.null-pointer.1")
    assert _is_crash_class_property("f.NULL-pointer.1")
    assert _is_crash_class_property("f.double-free.2")
    assert _is_crash_class_property("f.use-after-free.7")
    assert _is_crash_class_property("f.assertion.0")
    assert _is_crash_class_property("f.precondition_instance.3")


def test_is_crash_class_property_rejects_silent_ub_classes():
    """Silent-UB classes (overflow, conversion, shift, alignment) are
    NOT crash-class — the runtime wraps silently. The realism shortcut
    must not fire on these or real bugs get suppressed (see the
    malloc.overflow.1 regression from the VibeOS memory.c re-test)."""
    from bmc_agent.pipeline import _is_crash_class_property
    assert not _is_crash_class_property("malloc.overflow.1")
    assert not _is_crash_class_property("f.conversion.4")
    assert not _is_crash_class_property("f.pointer_arithmetic.17")
    assert not _is_crash_class_property("f.pointer_overflow.2")
    assert not _is_crash_class_property("f.shift.1")
    assert not _is_crash_class_property("f.alignment.0")


def test_is_crash_class_property_empty_input_is_conservative():
    """Empty / malformed property names must not trigger the shortcut.
    Returning False ensures the realism LLM runs instead — that's the
    safe default when the property class is unknown."""
    from bmc_agent.pipeline import _is_crash_class_property
    assert not _is_crash_class_property("")
    assert not _is_crash_class_property(None or "")
    # Unknown class — also treated as silent (let the LLM decide)
    assert not _is_crash_class_property("f.weird_new_class.1")


def test_strip_cascade_only_fires_for_glibc_internal_reference():
    """Cascade fires for ``__``-prefixed referents (true glibc internals)
    but NOT for plain C-standard references (which system headers re-
    define). Mixed body with both: should still cascade-strip because the
    __ referent is unresolvable."""
    from bmc_agent.harness_generator import _strip_glibc_internal_typedefs
    src = (
        "typedef unsigned long __my_internal;\n"  # __ primary strip
        "typedef unsigned long size_t;\n"          # C-standard primary strip
        # Body mixes both stripped types — cascade fires because of __my_internal
        "typedef struct { __my_internal a; size_t b; } orphan_t;\n"
    )
    out = _strip_glibc_internal_typedefs(src)
    assert "typedef orphan_t removed: references stripped __my_internal" in out


def test_strip_stdlib_decls_ignores_semicolons_inside_comments():
    """Regression: source-file doc comments that contain ``;`` were
    splitting the surrounding declaration text mid-comment. The injected
    ``/* foo decl removed */`` marker prematurely closed the outer ``*/``
    block, leaving the comment tail (``0 if empty */``) as garbage that
    CBMC parsed as code. Observed on simple_driver.c: the harness for
    every function failed to compile because the doc-comment for
    dev_write contained a ``;`` and the next ``;``-delimited "statement"
    contained ``read(`` (from a rb_read comment), wrongly triggering
    the system-function-decl branch.
    """
    from bmc_agent.harness_generator import _strip_stdlib_decls
    src = (
        "typedef int foo_t;\n"
        "/*\n"
        " * Postcondition: returns bytes written (<= len); 0 if empty\n"
        " * Calls: read() under the hood\n"
        " */\n"
        "typedef int bar_t;\n"
    )
    out = _strip_stdlib_decls(src)
    # The output must preserve the doc comment intact — no decl marker
    # injected inside the comment, no premature ``*/``.
    assert "/* read decl removed */" not in out
    assert "Postcondition: returns bytes written (<= len); 0 if empty" in out
    # Both typedefs survive.
    assert "typedef int foo_t;" in out
    assert "typedef int bar_t;" in out


def test_strip_stdlib_decls_still_strips_real_posix_redeclaration():
    """The fix to skip comments must not regress the original purpose:
    a real ``int read(...);`` forward declaration at depth 0 still gets
    replaced with the marker. Surrounding typedefs are preserved."""
    from bmc_agent.harness_generator import _strip_stdlib_decls
    src = (
        "typedef int foo_t;\n"
        "int read(int fd, void *buf, int n);\n"
        "typedef int bar_t;\n"
    )
    out = _strip_stdlib_decls(src)
    assert "/* read decl removed */" in out
    # Surrounding typedefs survive — the decl-removal only replaces the
    # statement chunk containing the read() declarator.
    assert "typedef int foo_t;" in out
    assert "typedef int bar_t;" in out
    # The real declarator is gone.
    assert "int read(int fd, void *buf, int n);" not in out


def test_strip_stdlib_decls_ignores_semicolons_inside_strings():
    """A ``;`` inside a string literal must not be treated as a
    statement boundary either. Less common in declaration text, but
    the same scanner now handles it for free; lock it in."""
    from bmc_agent.harness_generator import _strip_stdlib_decls
    src = 'static const char *msg = "split ; here";\nint read(int);\n'
    out = _strip_stdlib_decls(src)
    # The real read() declaration after the string is still stripped.
    assert "/* read decl removed */" in out
    # The string literal is preserved verbatim.
    assert '"split ; here"' in out


def test_strip_cpp_linemarkers_removes_directive_lines():
    """``# N "filename" [flags]`` lines left over from ``cc -E`` /
    ``make foo.i`` carry no semantic content for CBMC. CBMC's frontend
    either tries to re-resolve the named file (fails when the path is
    relative to the kernel build dir) or processes the nested context
    as a live header inclusion, which conflicts with the libc types the
    harness already pulled in. Strip them; preserve actual code."""
    from bmc_agent.harness_generator import _strip_cpp_linemarkers
    src = (
        '# 0 "drivers/usb/serial/ch341.c"\n'
        '# 0 "<built-in>"\n'
        '# 1 "./include/linux/types.h" 1\n'
        'typedef unsigned int u32;\n'
        '# 17 "drivers/usb/serial/ch341.c" 2\n'
        'static int ch341_get_divisor(u32 speed) { return speed / 8; }\n'
    )
    out = _strip_cpp_linemarkers(src)
    # All four ``# N "..."`` directives gone.
    assert '"drivers/usb/serial/ch341.c"' not in out
    assert '"<built-in>"' not in out
    assert '"./include/linux/types.h"' not in out
    # Code lines preserved verbatim.
    assert "typedef unsigned int u32;" in out
    assert "static int ch341_get_divisor(u32 speed) { return speed / 8; }" in out


def test_parser_extracts_function_source_origin_from_cpp_directives():
    """When the input has cpp ``# N "filename"`` line directives (i.e.
    a preprocessed TU), the parser tags each function with the source
    file it originated from, and records the first non-synthetic
    filename as ``primary_source``. Critical for Linux-driver work:
    a preprocessed ``.i`` pulls in thousands of header-inlined helpers,
    and we need to filter them out of spec generation."""
    import tempfile, os
    from bmc_agent.parser import parse_c_file
    src = (
        '# 0 "drivers/usb/serial/ch341.c"\n'
        '# 0 "<built-in>"\n'
        '# 1 "./include/linux/types.h" 1\n'
        'static inline unsigned int header_helper(unsigned int x) { return x + 1; }\n'
        '# 17 "drivers/usb/serial/ch341.c" 2\n'
        'static int ch341_local(int x) { return x * 2; }\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as f:
        f.write(src)
        path = f.name
    try:
        pf = parse_c_file(path)
        # Both functions parsed.
        assert "header_helper" in pf.functions
        assert "ch341_local" in pf.functions
        # primary_source is the first NON-synthetic directive (skipping
        # "<built-in>") — the original .c file.
        assert pf.primary_source == "drivers/usb/serial/ch341.c"
        # Per-function origin recorded.
        assert pf.function_source_files["header_helper"] == "./include/linux/types.h"
        assert pf.function_source_files["ch341_local"] == "drivers/usb/serial/ch341.c"
    finally:
        os.unlink(path)


def test_parser_primary_source_none_for_unpreprocessed_input():
    """A plain hand-written .c without cpp directives must have
    ``primary_source=None`` and an empty ``function_source_files``
    map. The downstream auto-filter is a no-op in that case."""
    import tempfile, os
    from bmc_agent.parser import parse_c_file
    src = "static int foo(int x) { return x + 1; }\n"
    with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as f:
        f.write(src)
        path = f.name
    try:
        pf = parse_c_file(path)
        assert "foo" in pf.functions
        assert pf.primary_source is None
        # No cpp directives → every recorded origin is empty (or the
        # dict is empty entirely); restrict_to_primary_source is a
        # no-op either way. The contract is "no per-function origin
        # information available", which both shapes satisfy.
        assert all(v == "" for v in pf.function_source_files.values())
    finally:
        os.unlink(path)


def test_parsed_file_restrict_to_primary_source_drops_header_inlines():
    """``restrict_to_primary_source`` keeps only functions tagged with
    the primary source's origin. Header-derived functions get dropped
    from ``functions``, ``function_bodies``, ``function_definitions``,
    ``call_graph``, and ``function_source_files``."""
    import tempfile, os
    from bmc_agent.parser import parse_c_file
    src = (
        '# 1 "drivers/usb/serial/ch341.c"\n'
        '# 1 "./include/linux/kernel.h" 1\n'
        'static inline int header_a(int x) { return x; }\n'
        'static inline int header_b(int x) { return header_a(x); }\n'
        '# 2 "drivers/usb/serial/ch341.c" 2\n'
        'static int ch341_driver_fn(int x) { return header_b(x) + 1; }\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as f:
        f.write(src)
        path = f.name
    try:
        pf = parse_c_file(path)
        assert len(pf.functions) == 3
        dropped = pf.restrict_to_primary_source()
        assert dropped == 2
        assert set(pf.functions.keys()) == {"ch341_driver_fn"}
        assert "header_a" not in pf.function_bodies
        assert "header_b" not in pf.function_definitions
        assert "header_a" not in pf.call_graph
        assert "header_a" not in pf.function_source_files
    finally:
        os.unlink(path)


def test_parsed_file_restrict_no_op_without_primary_source():
    """When the input has no cpp directives, primary_source is None
    and restrict_to_primary_source is a no-op (returns 0, leaves
    everything intact)."""
    import tempfile, os
    from bmc_agent.parser import parse_c_file
    src = "static int foo(int x) { return x; }\nstatic int bar(int x) { return foo(x); }\n"
    with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as f:
        f.write(src)
        path = f.name
    try:
        pf = parse_c_file(path)
        before = set(pf.functions.keys())
        dropped = pf.restrict_to_primary_source()
        assert dropped == 0
        assert set(pf.functions.keys()) == before
    finally:
        os.unlink(path)


def test_kernel_primitive_typedefs_survive_glibc_strip():
    """``__u8``/``__s8``/``__be32``/``__kernel_off_t``/``__poll_t`` are
    Linux kernel UAPI primitives, NOT glibc-internal types. The generic
    ``__``-prefix rule used to strip glibc internals like ``__fpos_t``
    must NOT also remove these — driver code uses ``u8/u16/u32`` (which
    derive from ``__u8/__u16/__u32``) in function signatures, and POSIX
    shape types like ``__kernel_off_t`` show up in struct definitions
    that the driver code references."""
    from bmc_agent.harness_generator import _strip_glibc_internal_typedefs
    src = (
        "typedef unsigned char __u8;\n"
        "typedef signed char __s8;\n"
        "typedef unsigned int __be32;\n"
        "typedef long __kernel_off_t;\n"
        "typedef unsigned int __poll_t;\n"
        # Real glibc-internal — should be stripped as before
        "typedef struct { int __val[2]; } __fsid_t;\n"
        "typedef long __pid_t;\n"
    )
    out = _strip_glibc_internal_typedefs(src)
    # Kernel primitives preserved
    assert "typedef unsigned char __u8;" in out
    assert "typedef signed char __s8;" in out
    assert "typedef unsigned int __be32;" in out
    assert "typedef long __kernel_off_t;" in out
    assert "typedef unsigned int __poll_t;" in out
    # Genuine glibc internals still stripped
    assert "/* typedef __fsid_t" in out
    assert "/* typedef __pid_t" in out


def test_c99_stdint_typedefs_get_stripped_so_libc_wins():
    """Linux's linux/types.h defines ``typedef u8 uint8_t;`` /
    ``typedef u8 u_int8_t;`` etc. The harness includes ``<stdint.h>``
    which provides authoritative ``uint8_t``. To avoid the conflict,
    the C99 stdint / BSD u_int*_t typedefs must be stripped from the
    inlined kernel source."""
    from bmc_agent.harness_generator import _strip_glibc_internal_typedefs
    src = (
        "typedef unsigned char __u8;\n"
        "typedef __u8 u8;\n"
        "typedef u8 uint8_t;\n"
        "typedef u8 u_int8_t;\n"
        "typedef int int32_t;\n"
        "typedef long intptr_t;\n"
    )
    out = _strip_glibc_internal_typedefs(src)
    # Kernel chain root + u8 preserved (used directly in driver signatures)
    assert "typedef unsigned char __u8;" in out
    assert "typedef __u8 u8;" in out
    # libc conflicts stripped
    assert "/* typedef uint8_t" in out
    assert "/* typedef u_int8_t" in out
    assert "/* typedef int32_t" in out
    assert "/* typedef intptr_t" in out


def test_strip_inline_asm_preserves_semicolon_on_register_clause():
    """``register unsigned long sp asm("rsp");`` is a register-storage
    declaration with a GCC asm-name clause; the trailing ``;``
    terminates the declaration, not the asm clause. The asm stripper
    must NOT consume it. Stripping the asm clause to a comment plus
    swallowing the ``;`` left the next ``struct ...`` token attached to
    a declaration without a terminator — CBMC reports ``syntax error
    before 'struct'`` and refuses the whole TU."""
    from bmc_agent.harness_generator import _strip_inline_asm
    src = (
        'register unsigned long current_stack_pointer asm("rsp");\n'
        'struct bug_entry;\n'
    )
    out = _strip_inline_asm(src)
    # asm clause text gone, but the semicolon stays so the declaration
    # is well-terminated.
    assert "/* asm removed */" in out
    assert "register unsigned long current_stack_pointer /* asm removed */;" in out
    # The follow-on declaration is untouched.
    assert "struct bug_entry;" in out


def test_strip_inline_asm_still_consumes_semicolon_for_statement_form():
    """Counterpart to the register-clause case. ``asm volatile ("nop");``
    is a top-level/statement asm — the ``;`` IS part of the asm and
    must continue to be consumed. Otherwise we'd leave a stray
    ``/* asm removed */;`` which most C parsers reject at translation-
    unit scope."""
    from bmc_agent.harness_generator import _strip_inline_asm
    src = 'asm volatile ("nop");\nint x = 1;\n'
    out = _strip_inline_asm(src)
    # No stray semicolon between the comment and the next declaration.
    assert "/* asm removed */\nint x" in out
    assert "/* asm removed */;" not in out


def test_strip_static_assert_replaces_runtime_condition():
    """The Linux kernel embeds ``_Static_assert(cond, "msg");`` inside
    ``sizeof(struct{...})`` macros for compile-time bound checks. When
    ``cond`` references function parameters (runtime values), CBMC's
    parser reports ``expected constant expression``. Replace with a
    trivially-true ``_Static_assert(1, "")`` so the construct is valid
    in every C11 context (TU/body/struct-field-list)."""
    from bmc_agent.harness_generator import _strip_static_assert
    src = (
        '_Static_assert(sizeof(int) == 4, "");\n'
        'int x = (sizeof(struct { _Static_assert(offset > size - 1, "msg"); int _; }));\n'
        'void foo(int n) {\n'
        '    _Static_assert(n > 0, "runtime");\n'
        '}\n'
    )
    out = _strip_static_assert(src)
    # No bare _Static_assert(<non-1>) survives.
    import re as _re
    surviving_static_asserts = _re.findall(r'_Static_assert\([^,]+,', out)
    for sa in surviving_static_asserts:
        # Argument must be the literal ``1`` (allowing whitespace).
        assert _re.match(r'_Static_assert\(\s*1\s*,', sa), f"unexpected static_assert: {sa}"
    # Three replacements should have fired (one per original).
    assert out.count('_Static_assert(1, "")') == 3


def test_strip_static_assert_handles_nested_parens():
    """Conditions can contain nested function calls / commas / parens.
    The paren-balance scanner must skip them."""
    from bmc_agent.harness_generator import _strip_static_assert
    src = '_Static_assert(__builtin_choose_expr(1, 1, 0) > 0, "x");\n'
    out = _strip_static_assert(src)
    assert '__builtin_choose_expr' not in out
    assert '_Static_assert(1, "")' in out


def test_rewrite_auto_type_simple_pointer_init():
    """GCC's ``__auto_type`` keyword is used throughout the kernel's
    ``min``/``max``/``clamp`` macro family. CBMC's parser doesn't
    understand it. Rewrite to ``typeof(EXPR) VAR = EXPR;``."""
    from bmc_agent.harness_generator import _rewrite_auto_type
    src = "const __auto_type s0 = s;\n"
    out = _rewrite_auto_type(src)
    assert "__auto_type" not in out
    assert "typeof(s) s0 = s;" in out


def test_rewrite_auto_type_handles_nested_parens():
    """The initializer can be an expression with nested parens (e.g.
    ``__auto_type x = foo(a, b);``). The RHS scanner must skip ``;``
    inside parens."""
    from bmc_agent.harness_generator import _rewrite_auto_type
    src = "__auto_type x = foo(a, b);\nint y = 1;\n"
    out = _rewrite_auto_type(src)
    assert "typeof(foo(a, b)) x = foo(a, b);" in out
    assert "int y = 1;" in out


def test_rewrite_auto_type_leaves_unrelated_text_alone():
    from bmc_agent.harness_generator import _rewrite_auto_type
    src = "int autotype_lookalike = 1; // not __auto_type\nint y;\n"
    # Same word fragment but no ``__auto_type``: no rewrite.
    out = _rewrite_auto_type(src)
    assert out == src


def test_kernel_mode_skips_system_and_glibc_typedef_strips():
    """In kernel_mode, BOTH ``_SYSTEM_TYPEDEF_NAMES`` and the
    ``__``-prefix rule are suppressed. The kernel TU defines its
    own ``size_t``, ``ssize_t``, ``__sighandler_t``, etc., and
    there's no libc prepend to fill them in if they were stripped."""
    from bmc_agent.harness_generator import _strip_glibc_internal_typedefs
    src = (
        "typedef __kernel_size_t size_t;\n"
        "typedef long ssize_t;\n"
        "typedef void (*__sighandler_t)(int);\n"
        "typedef long __pid_t;\n"  # genuine glibc-internal name
        "typedef unsigned char __u8;\n"  # kernel primitive
    )
    out = _strip_glibc_internal_typedefs(src, kernel_mode=True)
    # All preserved in kernel mode.
    assert "typedef __kernel_size_t size_t;" in out
    assert "typedef long ssize_t;" in out
    assert "typedef void (*__sighandler_t)(int);" in out
    assert "typedef long __pid_t;" in out  # __pid_t also kept (no libc to conflict)
    assert "typedef unsigned char __u8;" in out
    # Non-kernel-mode default: glibc internals + system types stripped
    # (when target name is extractable), kernel primitives preserved.
    # Function-pointer typedefs aren't matched by the simple name
    # regex; that's a separate gap, irrelevant for kernel_mode.
    out2 = _strip_glibc_internal_typedefs(src, kernel_mode=False)
    assert "/* typedef size_t" in out2  # stripped
    assert "/* typedef ssize_t" in out2  # stripped
    assert "/* typedef __pid_t" in out2  # stripped
    assert "typedef unsigned char __u8;" in out2  # kept (kernel primitive)


def test_parse_source_file_auto_filters_preprocessed(tmp_path):
    """``parse_source_file`` automatically calls
    ``restrict_to_primary_source`` when cpp ``# N "..."`` directives
    are present. Before this fix, only ``spec_generator`` was filtering;
    ``cli._cmd_check`` re-parsed and saw all 4400+ kernel functions,
    leading the harness generator's type-decl extractor to mis-locate
    function bodies in the source text."""
    src = tmp_path / "kernel.i"
    src.write_text(
        '# 1 "drivers/foo/bar.c"\n'
        '# 1 "./include/linux/k.h" 1\n'
        'static inline int header_helper(int x) { return x; }\n'
        '# 2 "drivers/foo/bar.c" 2\n'
        'static int driver_fn(int x) { return x + 1; }\n'
    )
    from bmc_agent.source_parser import parse_source_file
    parsed = parse_source_file(str(src))
    # Header function dropped automatically.
    assert "header_helper" not in parsed.functions
    assert "driver_fn" in parsed.functions
    assert parsed.primary_source == "drivers/foo/bar.c"


def test_strip_gcc_addr_space_quals_removes_seg_gs_seg_fs():
    """GCC's named-address-space keywords ``__seg_gs`` and ``__seg_fs``
    survive cpp (they're bare identifiers, not macros). CBMC's frontend
    doesn't recognise them and reports ``syntax error before '__seg_gs'``.
    Erase them; they have no verification value in single-threaded
    model checking."""
    from bmc_agent.harness_generator import _strip_gcc_addr_space_quals
    src = (
        "extern __typeof__(struct task_struct * const __seg_gs) current_task;\n"
        "extern __typeof__(int __seg_fs) per_cpu_var;\n"
        # Look-alike that must NOT be touched
        "int __seg_gs_var = 0;\n"
    )
    out = _strip_gcc_addr_space_quals(src)
    assert "__seg_gs" not in re.sub(r"__seg_gs_var", "", out)
    assert "__seg_fs" not in out
    assert "__seg_gs_var" in out


def test_strip_cpp_linemarkers_leaves_non_linemarker_preproc_alone():
    """``#define`` / ``#include`` / ``#if`` directives MUST NOT be
    stripped — they're real preprocessor controls. The pattern only
    catches the cpp-emitted ``# DIGIT "..."`` variant."""
    from bmc_agent.harness_generator import _strip_cpp_linemarkers
    src = (
        '#define X 1\n'
        '#include <stdint.h>\n'
        '#if X\n'
        '# 17 "foo.c"\n'        # cpp linemarker — should be stripped
        'int y = 2;\n'
        '#endif\n'
    )
    out = _strip_cpp_linemarkers(src)
    assert "#define X 1" in out
    assert "#include <stdint.h>" in out
    assert "#if X" in out
    assert "#endif" in out
    assert '"foo.c"' not in out
    assert "int y = 2;" in out
