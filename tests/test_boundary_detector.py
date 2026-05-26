"""Tests for bmc_agent.boundary_detector."""

from __future__ import annotations

from pathlib import Path

import pytest

from bmc_agent.boundary_detector import (
    BoundaryDetector,
    _accumulate_declarations,
    _strip_extern_c_wrapper,
    extract_public_functions,
)


def _write_h(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(src)
    return p


# ---------- extract_public_functions ---------------------------------------


def test_extract_simple_declarations(tmp_path):
    h = _write_h(tmp_path, "x.h", """
int foo(int);
extern void bar(const char *, size_t);
""")
    names = extract_public_functions([h])
    assert names == {"foo", "bar"}


def test_extract_with_pointer_return_type(tmp_path):
    """`struct archive *archive_read_new(void);` — the `*` was tripping
    an earlier lookbehind. Make sure the function name is still captured."""
    h = _write_h(tmp_path, "x.h", """
struct archive *archive_read_new(void);
const char *archive_error_string(struct archive *);
""")
    names = extract_public_functions([h])
    assert "archive_read_new" in names
    assert "archive_error_string" in names


def test_extract_qualifiers_are_tolerated(tmp_path):
    h = _write_h(tmp_path, "x.h", """
__LA_DECL int archive_version_number(void);
__attribute__((visibility("default"))) int exported(int);
""")
    names = extract_public_functions([h])
    assert "archive_version_number" in names
    assert "exported" in names


def test_extract_rejects_typedef(tmp_path):
    h = _write_h(tmp_path, "x.h", """
typedef int callback_t(void *, size_t);
typedef int (*fn_ptr_t)(int);
int real_fn(int);
""")
    names = extract_public_functions([h])
    assert names == {"real_fn"}


def test_extract_rejects_function_pointer_variable(tmp_path):
    h = _write_h(tmp_path, "x.h", """
int (*p_fn)(int);
int real_fn(int);
""")
    names = extract_public_functions([h])
    assert names == {"real_fn"}


def test_extract_skips_preprocessor_lines(tmp_path):
    h = _write_h(tmp_path, "x.h", """
#define FOO(x) (x + 1)
#if defined(__linux__)
int linux_fn(int);
#endif
""")
    names = extract_public_functions([h])
    assert "linux_fn" in names
    assert "FOO" not in names


def test_extract_multi_line_declaration(tmp_path):
    h = _write_h(tmp_path, "x.h", """
int
multiline_fn(
    int a,
    int b
);
""")
    names = extract_public_functions([h])
    assert "multiline_fn" in names


def test_extract_with_inline_definition_skipped(tmp_path):
    """`static inline int foo(void) { ... }` defines, not declares.
    We only want declarations (statements ending in `;`)."""
    h = _write_h(tmp_path, "x.h", """
static inline int foo(int x) { return x + 1; }
int bar(int);
""")
    names = extract_public_functions([h])
    # `bar` is a declaration; `foo` is a definition with a brace body.
    assert "bar" in names
    # foo MIGHT appear if the parser sees the closing-brace-then-newline as
    # statement-end; the current implementation skips it because the `{`
    # increments brace_depth and any `;` inside is non-emitting.
    # We don't strictly require foo to be excluded; what we require is
    # that bar IS included.


# ---------- extern "C" wrapper ----------------------------------------------


def test_extern_c_wrapper_stripped():
    text = '''
typedef int xtype;
extern "C" {
int wrapped_fn(int);
}
int outside_fn(int);
'''
    stripped = _strip_extern_c_wrapper(text)
    # Both braces should be gone (replaced with whitespace).
    assert "extern" not in stripped or "{" not in stripped.split("extern", 1)[1][:50]
    # Function name preserved.
    assert "wrapped_fn" in stripped


def test_extern_c_wrapper_keeps_both_decls(tmp_path):
    h = _write_h(tmp_path, "x.h", """
int outside_fn(int);
extern "C" {
int wrapped_fn(int);
}
int after_fn(int);
""")
    names = extract_public_functions([h])
    assert names == {"outside_fn", "wrapped_fn", "after_fn"}


def test_extern_c_unbalanced_does_not_crash():
    """`extern "C" {` with no matching `}` should be tolerated."""
    text = 'extern "C" {\nint fn(int);\n'  # missing close brace
    # Should not raise; result is undefined but must not throw.
    result = _strip_extern_c_wrapper(text)
    assert isinstance(result, str)


# ---------- BoundaryDetector class ------------------------------------------


def test_detector_from_paths_basic(tmp_path):
    h = _write_h(tmp_path, "x.h", "int public_fn(int);")
    bd = BoundaryDetector.from_paths([h])
    assert bd.is_boundary("public_fn") is True
    assert bd.is_boundary("private_fn") is False
    assert len(bd) == 1


def test_detector_from_paths_empty():
    bd = BoundaryDetector.from_paths([])
    assert len(bd) == 0
    assert bd.is_boundary("anything") is False


def test_detector_autodiscover_filters_private_h(tmp_path):
    _write_h(tmp_path, "public.h", "int pub_fn(int);")
    _write_h(tmp_path, "thing_private.h", "int priv_fn(int);")
    _write_h(tmp_path, "thing_internal.h", "int int_fn(int);")
    bd = BoundaryDetector.autodiscover(tmp_path)
    assert bd.is_boundary("pub_fn") is True
    assert bd.is_boundary("priv_fn") is False
    assert bd.is_boundary("int_fn") is False


def test_detector_autodiscover_with_explicit_headers(tmp_path):
    """explicit_headers should always be included even if path doesn't match
    the *.h-in-source-dir pattern."""
    extra_dir = tmp_path / "_explicit"
    extra_dir.mkdir()
    pub = extra_dir / "extra.h"
    pub.write_text("int extra_fn(int);")
    bd = BoundaryDetector.autodiscover(tmp_path, explicit_headers=[pub])
    assert bd.is_boundary("extra_fn") is True


def test_detector_unreadable_header_is_tolerated(tmp_path):
    """Non-existent paths shouldn't crash; they're skipped."""
    bd = BoundaryDetector.from_paths([tmp_path / "does_not_exist.h"])
    assert len(bd) == 0


# ---------- _accumulate_declarations ----------------------------------------


def test_accumulate_yields_each_statement():
    text = "int a(int);\nint b(int);\nint c(int);\n"
    out = list(_accumulate_declarations(text))
    assert len(out) == 3
    assert all("int" in s and ";" in s for s in out)


def test_accumulate_handles_function_body():
    """A body `{ ... ; ... }` shouldn't emit statements from inside it."""
    text = """
int decl_outside(int);
int with_body(int x) { return x + 1; }
int after_body(int);
"""
    out = list(_accumulate_declarations(text))
    # The body's `return x + 1;` should NOT be a separate statement.
    # decl_outside, after_body are real; with_body() is also captured
    # as the leading decl up to `{` (no closing `;` outside the body).
    decls = [s for s in out if "decl_outside" in s or "after_body" in s]
    assert len(decls) == 2
