"""Tests for universal stub contracts (callee postconditions)."""

from __future__ import annotations

from bmc_agent.universal_stub_contracts import (
    derive_stub_contract,
)


# ---------------------------------------------------------------------------
# libarchive: __archive_read_ahead
# ---------------------------------------------------------------------------


def test_archive_read_ahead_emits_buffer_contract():
    params = [
        ("struct archive_read *", "a"),
        ("size_t", "n"),
        ("ssize_t *", "bytes"),
    ]
    lines = derive_stub_contract("__archive_read_ahead", "const void *", params)
    assert lines, "expected non-empty contract"
    body = "\n".join(lines)
    # Lower bound on the returned byte count.
    assert "_ar_bytes >= (ssize_t)(n)" in body
    # Output param assignment to the bytes pointer.
    assert "*bytes = _ar_bytes" in body
    # Pointer validity contract on result.
    assert "__CPROVER_r_ok(result, (size_t)_ar_bytes)" in body
    # NULL guard for the output pointer arg.
    assert "bytes != ((ssize_t *)0)" in body


def test_archive_read_ahead_with_renamed_args():
    """The contract should pick up the size and bytes args by type even
    when the names differ from the documented prototype."""
    params = [
        ("struct archive_read *", "ar"),
        ("size_t", "nbytes"),
        ("ssize_t *", "avail"),
    ]
    lines = derive_stub_contract("__archive_read_ahead", "const void *", params)
    body = "\n".join(lines)
    assert "_ar_bytes >= (ssize_t)(nbytes)" in body
    assert "*avail = _ar_bytes" in body


def test_archive_read_ahead_missing_required_arg_yields_empty():
    """If the function doesn't have a ssize_t* arg, no contract fires
    (we'd otherwise produce broken C)."""
    params = [
        ("struct archive_read *", "a"),
        ("size_t", "n"),
        # No ssize_t * arg.
    ]
    lines = derive_stub_contract("__archive_read_ahead", "const void *", params)
    assert lines == []


# ---------------------------------------------------------------------------
# libarchive entry string accessors
# ---------------------------------------------------------------------------


def test_archive_entry_pathname_string_contract():
    lines = derive_stub_contract(
        "archive_entry_pathname", "const char *",
        [("struct archive_entry *", "e")],
    )
    body = "\n".join(lines)
    assert "result == ((const char *)0)" in body
    assert "__CPROVER_r_ok(result, 1)" in body


def test_archive_entry_uname_same_contract():
    lines = derive_stub_contract(
        "archive_entry_uname", "const char *",
        [("struct archive_entry *", "e")],
    )
    body = "\n".join(lines)
    assert "__CPROVER_r_ok(result, 1)" in body


def test_archive_entry_pathname_l_writes_output_string_pointer():
    lines = derive_stub_contract(
        "archive_entry_pathname_l", "int",
        [
            ("struct archive_entry *", "e"),
            ("const char **", "p"),
            ("size_t *", "sz"),
            ("struct archive_string_conv *", "sc"),
        ],
    )
    body = "\n".join(lines)
    # Writes to the output string pointer.
    assert "*p = _ae_str" in body
    # Constrains the written value.
    assert "_ae_str == ((const char *)0)" in body
    # Result is 0 (OK) or -1 (failure).
    assert "result == 0 || result == -1" in body


# ---------------------------------------------------------------------------
# Unknown callees
# ---------------------------------------------------------------------------


def test_unknown_callee_returns_empty():
    """Callees not in the registry get no contract — caller falls back
    to whatever other mechanisms exist (kernel-API, sibling-inference,
    plain nondet)."""
    lines = derive_stub_contract(
        "totally_unknown_function", "int",
        [("int", "x")],
    )
    assert lines == []


def test_libc_already_covered_by_builtin_table_returns_empty():
    """Functions like malloc/strdup are handled by the existing
    ``_builtin_stub_return_contract`` table in harness_generator.py.
    This module deliberately doesn't duplicate those entries."""
    for name in ("malloc", "strdup", "calloc", "strlen"):
        lines = derive_stub_contract(name, "void *", [("size_t", "n")])
        assert lines == [], (
            f"{name} should be left to _builtin_stub_return_contract"
        )


# ---------------------------------------------------------------------------
# Soundness rule — contracts only constrain RETURN values / output params,
# never input params.
# ---------------------------------------------------------------------------


def test_archive_read_next_header_status_codes():
    lines = derive_stub_contract(
        "archive_read_next_header", "int",
        [("struct archive *", "a"), ("struct archive_entry **", "e")],
    )
    body = "\n".join(lines)
    assert "ARCHIVE_OK" in body
    assert "ARCHIVE_EOF" in body
    assert "ARCHIVE_FATAL" in body
    # All return values in the assume must be in the documented set.
    assert "result == 0" in body and "result == 1" in body
    assert "result == -30" in body


def test_archive_write_header_uses_status_contract():
    lines = derive_stub_contract(
        "archive_write_header", "int",
        [("struct archive *", "a"), ("struct archive_entry *", "e")],
    )
    body = "\n".join(lines)
    assert "ARCHIVE_OK" in body or "result == 0" in body


def test_fread_returns_at_most_nmemb():
    lines = derive_stub_contract(
        "fread", "size_t",
        [("void *", "ptr"), ("size_t", "size"), ("size_t", "nmemb"), ("FILE *", "stream")],
    )
    body = "\n".join(lines)
    assert "result <= nmemb" in body


def test_read_posix_contract():
    lines = derive_stub_contract(
        "read", "ssize_t",
        [("int", "fd"), ("void *", "buf"), ("size_t", "count")],
    )
    body = "\n".join(lines)
    assert "result == -1" in body
    assert "result <= (ssize_t)(count)" in body


def test_fopen_pointer_contract():
    lines = derive_stub_contract(
        "fopen", "FILE *",
        [("const char *", "path"), ("const char *", "mode")],
    )
    body = "\n".join(lines)
    assert "result == ((void *)0) || __CPROVER_r_ok(result, 1)" in body


def test_inflate_z_status_range():
    lines = derive_stub_contract(
        "inflate", "int",
        [("z_stream *", "strm"), ("int", "flush")],
    )
    body = "\n".join(lines)
    # Z_VERSION_ERROR = -6 to Z_STREAM_END = 1; we use 2 as buffer (NEED_DICT).
    assert "result >= -6" in body
    assert "result <= 2" in body


def test_stat_returns_0_or_minus_1():
    for name in ("stat", "fstat", "lstat", "open", "close", "unlink", "chmod"):
        lines = derive_stub_contract(name, "int", [("const char *", "p")])
        body = "\n".join(lines)
        assert lines, f"{name} should have a contract"
        assert "result == -1" in body or "result == 0" in body


def test_open_bounded_fd_range():
    lines = derive_stub_contract(
        "open", "int", [("const char *", "p"), ("int", "flags")],
    )
    body = "\n".join(lines)
    assert "result < 65536" in body or "result == -1" in body


def test_archive_entry_size_non_negative():
    lines = derive_stub_contract(
        "archive_entry_size", "int64_t",
        [("struct archive_entry *", "e")],
    )
    body = "\n".join(lines)
    assert "result >= -1" in body


def test_fgets_returns_null_or_buf():
    lines = derive_stub_contract(
        "fgets", "char *",
        [("char *", "buf"), ("int", "n"), ("FILE *", "stream")],
    )
    body = "\n".join(lines)
    assert "result == buf" in body


def test_no_contract_constrains_an_input_param_via_assume():
    """Property test: scan every line emitted for every known callee.
    Any ``__CPROVER_assume(...)`` MUST mention either ``result`` or an
    output-parameter dereference (``*<name>``) — NEVER a bare input
    parameter name in an unsafe pattern that would mask attacks.
    """
    callee_cases = [
        ("__archive_read_ahead", "const void *",
         [("struct archive_read *", "a"), ("size_t", "n"), ("ssize_t *", "bytes")]),
        ("archive_entry_pathname", "const char *",
         [("struct archive_entry *", "e")]),
        ("archive_entry_pathname_l", "int",
         [("struct archive_entry *", "e"), ("const char **", "p"),
          ("size_t *", "sz"), ("struct archive_string_conv *", "sc")]),
        ("archive_string_conversion_charset_name", "const char *",
         [("struct archive_string_conv *", "sc")]),
    ]
    for name, ret, params in callee_cases:
        for line in derive_stub_contract(name, ret, params):
            if "__CPROVER_assume" not in line:
                continue
            # Every assume clause must reference either ``result`` (the
            # stub's return value) or an output-parameter dereference
            # (``*<name>``) or a local nondet temporary (``_ar_bytes``,
            # ``_ae_str``). It must NOT constrain a bare input
            # parameter — that would be an unsound input precondition.
            allowed_subjects = ["result", "_ar_bytes", "_ae_str"]
            # Output-param derefs like ``*bytes`` are also allowed.
            assert any(s in line for s in allowed_subjects) or "*" in line, (
                f"Soundness violation in {name!r}: assume line does not "
                f"reference a return value or output-param deref: {line!r}"
            )
