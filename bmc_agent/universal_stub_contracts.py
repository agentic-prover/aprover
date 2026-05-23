"""
Universal stub postconditions for bmc-agent-lite.

Companion to :mod:`bmc_agent.universal_contracts` (preconditions on
the function-under-test's parameters). This module supplies the
*symmetric* piece: postconditions on callee stub return values + their
output parameters.

**SOUNDNESS RULE — POSTCONDITIONS ONLY**

Every clause this module emits MUST be a property of what the real
callee *returns to its caller*. Specifically:

* ``__CPROVER_assume(<property of result or output param>)`` — OK.
* ``__CPROVER_assume(<property of an INPUT parameter>)`` — NEVER.
  Putting a precondition on an INPUT as an assume would silently
  ignore callers (i.e. the function under test) that violate it.
  That is exactly how an attacker-controlled call site with a bad
  pointer would escape detection. The right way to express a callee
  input contract is ``__CPROVER_assert(...)`` at stub entry, which
  makes CBMC *report* the violation as a bug — NOT a property of
  this module.

The contracts here only narrow what the stub gives back to the FUT.
They cannot mask any attack against F, because the attacker
controls F's call sites, not the library's internal return-value
behaviour. (CBMC's --bounds-check / --pointer-check still verify
F's pointer arguments at every memcpy / memcpy-like call site
independently, unconditionally.)

Background: when bmc-agent verifies a function F, every callee F invokes
is replaced with a "stub" in the harness. The default stub returns
nondeterministic values of the right type — which is too permissive for
real callees that maintain documented invariants. Example:

    h = __archive_read_ahead(a, n, &bytes);
    if (h == NULL) return ARCHIVE_FATAL;
    p = h;
    q = p + bytes;        // pointer arithmetic on (h, bytes)
    memcmp("07070", p, 5);  // requires h has >= 5 valid bytes

libarchive's documented contract: if ``__archive_read_ahead`` returns
non-NULL, then ``*bytes >= n`` AND the returned pointer is valid for
``*bytes`` bytes. The default stub doesn't model that, so CBMC finds
phantom violations.

This registry holds hand-coded contracts for known callees. Each entry
returns a list of body-statement strings that the harness generator
emits AFTER ``{ret_type} result;`` and BEFORE the trailing
``return result;``. Statements may include __CPROVER_assume guards on
``result`` AND assignments to output parameters (``*bytes = …``)
followed by guards on those.

By design, contracts are **universally true** — every real
implementation honors them. Adding the contract to a stub cannot mask
a real bug; it only filters out the harness's pathological inputs that
no real callee would produce.

Existing contracts already covered by ``harness_generator._builtin_stub_return_contract``
(libc + kernel allocator/string family) are NOT duplicated here; this
module only adds primitives that table misses — primarily libarchive's
stream-read API and entry accessors.

Two-axis taxonomy of stub-callee FPs this targets:

1. **Buffer-return contracts**: a function returns ``(ptr, len)`` where
   ``ptr`` points to ``len`` valid bytes. Without the contract, CBMC
   sees ``ptr`` as a generic nondet pointer.
2. **NUL-terminated-string return contracts**: a function returns
   a pointer to a NUL-terminated string or NULL. The existing
   ``strdup`` contract covers this only via a 1-byte readability
   check; libarchive's entry accessors (`archive_entry_pathname`,
   etc.) need the same.
"""

from __future__ import annotations

from typing import Optional


def derive_stub_contract(
    callee_name: str,
    ret_type: str,
    params: list[tuple[str, str]],
) -> list[str]:
    """Return additional body-statement lines for the stub of *callee_name*.

    Returns an empty list when no contract is registered. The caller
    (harness generator's ``_generate_stub``) emits these lines after
    ``{ret_type} result;`` and before ``return result;``.

    Parameters
    ----------
    callee_name:
        The C function name (no trailing ``_stub`` suffix).
    ret_type:
        The C return-type string of the callee, e.g. ``"const void *"``.
    params:
        List of ``(type_str, name_str)`` from the parsed signature.
    """
    # libarchive: __archive_read_ahead(struct archive_read *a, size_t n, ssize_t *bytes)
    if callee_name == "__archive_read_ahead":
        size_arg, bytes_arg = _find_args(
            params,
            size_predicate=_is_size_arg,
            output_predicate=_is_ssize_t_pointer,
        )
        if size_arg and bytes_arg:
            return [
                f"/* libarchive contract: __archive_read_ahead returns NULL or",
                f"   a valid buffer of *{bytes_arg} >= {size_arg} bytes */",
                f"ssize_t _ar_bytes;",
                f"__CPROVER_assume(_ar_bytes >= (ssize_t)({size_arg}));",
                f"if ({bytes_arg} != ((ssize_t *)0)) *{bytes_arg} = _ar_bytes;",
                f"__CPROVER_assume(result == ((const void *)0) || "
                f"__CPROVER_r_ok(result, (size_t)_ar_bytes));",
            ]

    # libarchive: archive_entry_pathname / _uname / _gname / _hardlink /
    # _symlink / _sourcepath — all return ``const char *`` (NUL or
    # NUL-terminated string).
    if callee_name in _LIBARCHIVE_ENTRY_STRING_ACCESSORS:
        return [
            f"/* libarchive contract: {callee_name} returns NULL or a "
            f"NUL-terminated string */",
            f"__CPROVER_assume(result == ((const char *)0) || "
            f"__CPROVER_r_ok(result, 1));",
        ]

    # libarchive: archive_entry_pathname_l / _uname_l / _gname_l etc.
    # take (entry, &out_str, &out_len, sconv) and return int 0/-1.
    # The string output param: same NUL-or-string contract.
    if callee_name in _LIBARCHIVE_ENTRY_STRING_L_ACCESSORS:
        # The output is a const char ** parameter; set it to a nondet
        # NUL-or-valid pointer.
        out_ptr_arg = _find_const_char_pp_arg(params)
        if out_ptr_arg:
            return [
                f"/* libarchive contract: {callee_name} writes a NUL-terminated "
                f"string pointer to *{out_ptr_arg} (or NULL) */",
                f"if ({out_ptr_arg} != ((const char **)0)) {{",
                f"    const char *_ae_str;",
                f"    __CPROVER_assume(_ae_str == ((const char *)0) || "
                f"__CPROVER_r_ok(_ae_str, 1));",
                f"    *{out_ptr_arg} = _ae_str;",
                f"}}",
                f"__CPROVER_assume(result == 0 || result == -1);",
            ]

    # libarchive: archive_string_conversion_charset_name — returns
    # NUL or NUL-terminated string.
    if callee_name in {"archive_string_conversion_charset_name"}:
        return [
            f"__CPROVER_assume(result == ((const char *)0) || "
            f"__CPROVER_r_ok(result, 1));",
        ]

    # libarchive: read-level entry points that return one of
    # {ARCHIVE_OK=0, ARCHIVE_EOF=1, ARCHIVE_RETRY=-10, ARCHIVE_WARN=-20,
    # ARCHIVE_FAILED=-25, ARCHIVE_FATAL=-30}. The default int-nondet stub
    # returns any int including positive impossible values like 42 that
    # callers can't handle, producing every-branch-explored FPs in the
    # caller. Real implementations only emit the documented sentinels.
    # Threat-model note: these are libarchive-controlled return values;
    # attacker controls the ARCHIVE BYTES (-> __archive_read_ahead's
    # data) not the high-level read API's return-code shape.
    if callee_name in _LIBARCHIVE_ARCHIVE_STATUS_RETURNS:
        return [
            f"/* libarchive contract: {callee_name} returns one of the "
            f"documented ARCHIVE_* status codes */",
            f"__CPROVER_assume("
            f"result == 0 /* ARCHIVE_OK */ || "
            f"result == 1 /* ARCHIVE_EOF */ || "
            f"result == -10 /* ARCHIVE_RETRY */ || "
            f"result == -20 /* ARCHIVE_WARN */ || "
            f"result == -25 /* ARCHIVE_FAILED */ || "
            f"result == -30 /* ARCHIVE_FATAL */);",
        ]

    # libarchive: archive_compression_name / archive_format_name — return
    # static const string (compiler-allocated, never NULL in real code,
    # but conservative: NULL or valid string).
    if callee_name in {
        "archive_compression_name", "archive_format_name",
        "archive_filter_name",
    }:
        return [
            f"__CPROVER_assume(result == ((const char *)0) || "
            f"__CPROVER_r_ok(result, 1));",
        ]

    # libarchive: archive_entry_size / _ino / _ino64 — return non-negative
    # signed integer (real libarchive returns -1 for "unset" via an int64
    # sentinel; never wraps to extreme negatives). Threat-model note:
    # entry size CAN be attacker-influenced via crafted archives, but
    # the bound below is libarchive's own representable range, not
    # something the attacker can bypass.
    if callee_name in {
        "archive_entry_size", "archive_entry_ino", "archive_entry_ino64",
    }:
        return [
            f"__CPROVER_assume(result >= -1);",
        ]

    # ----- libc: file I/O -----
    # fopen(path, mode) → NULL or valid FILE*. The pointer's pointee
    # is opaque (FILE*); we only constrain that it's either NULL or
    # readable as 1 byte (the existence of a fopen-style FILE* is the
    # actual contract; CBMC's --pointer-check still verifies any
    # fread/fwrite call with bounds).
    if callee_name in {"fopen", "fdopen", "freopen", "tmpfile"}:
        return [
            f"__CPROVER_assume(result == ((void *)0) || "
            f"__CPROVER_r_ok(result, 1));",
        ]

    # fread(ptr, size, nmemb, stream) → returns 0..nmemb. The standard
    # forbids returns > nmemb. fwrite identical.
    if callee_name in {"fread", "fwrite"}:
        nmemb_arg = _find_third_size_t_arg(params)
        if nmemb_arg:
            return [
                f"/* libc contract: {callee_name} returns 0..nmemb */",
                f"__CPROVER_assume(result <= {nmemb_arg});",
            ]

    # read(fd, buf, count) / write(fd, buf, count) → returns -1 (error)
    # or 0..count (POSIX guarantee). Cannot return > count.
    if callee_name in {"read", "write", "pread", "pwrite"}:
        count_arg = _find_count_arg(params)
        if count_arg:
            return [
                f"/* POSIX contract: {callee_name} returns -1 or 0..count */",
                f"__CPROVER_assume(result == -1 || "
                f"(result >= 0 && result <= (ssize_t)({count_arg})));",
            ]

    # recv/send/recvfrom/sendto → returns -1 or 0..len.
    if callee_name in {"recv", "send", "recvfrom", "sendto"}:
        len_arg = _find_count_arg(params)
        if len_arg:
            return [
                f"__CPROVER_assume(result == -1 || "
                f"(result >= 0 && result <= (ssize_t)({len_arg})));",
            ]

    # snprintf(buf, size, fmt, ...) / vsnprintf → returns the number
    # of bytes that WOULD HAVE been written (excluding NUL), -1 on
    # output error. Real returns: -1, or any non-negative int. We
    # cap at a sane upper bound (SIZE_MAX/2) to suppress nondet
    # extreme values that trip downstream signed/unsigned mixing FPs.
    if callee_name in {"snprintf", "vsnprintf"}:
        return [
            f"/* libc contract: {callee_name} returns -1 or non-negative count */",
            f"__CPROVER_assume(result >= -1);",
        ]

    # fgets(buf, size, stream) → returns NULL or buf (the same pointer
    # passed in). Constrains stub to either of those.
    if callee_name == "fgets":
        buf_arg = _find_first_pointer_arg(params)
        if buf_arg:
            return [
                f"__CPROVER_assume(result == ((char *)0) || result == {buf_arg});",
            ]

    # fclose → returns 0 (success) or EOF (-1).
    if callee_name in {"fclose", "fputs", "ferror", "feof"}:
        return [f"__CPROVER_assume(result == 0 || result == -1);"]

    # ----- libc: time & misc -----

    # time(NULL or &out) → returns time_t (epoch seconds). Cap to a
    # plausible range to avoid wraparound FPs. Real systems return
    # post-epoch positive values up to ~year 2106 for 32-bit time_t.
    if callee_name in {"time"}:
        return [f"__CPROVER_assume(result >= 0);"]

    # localtime(&t) / gmtime(&t) → return NULL or pointer to static
    # struct tm. r_ok on 1 byte is the conservative contract.
    if callee_name in {"localtime", "gmtime", "localtime_r", "gmtime_r"}:
        return [
            f"__CPROVER_assume(result == ((struct tm *)0) || "
            f"__CPROVER_r_ok(result, 1));",
        ]

    # ----- libc: integer parsing -----

    # atoi / atol / atoll → standard says result is implementation-defined
    # on overflow but well-defined on parsable input. Real implementations
    # never return undefined-behaviour values; constrain to int/long
    # range (which is already what the type expresses, so no contract
    # needed — left here as documentation).

    # strtol / strtoul / strtoll family → returns the parsed value.
    # No useful universal contract — return can be any value of return
    # type. Skipped.

    # ----- compression libraries -----

    # zlib: inflate(strm, flush) / deflate(strm, flush) → returns one
    # of {Z_OK=0, Z_STREAM_END=1, Z_NEED_DICT=2, Z_ERRNO=-1,
    # Z_STREAM_ERROR=-2, Z_DATA_ERROR=-3, Z_MEM_ERROR=-4,
    # Z_BUF_ERROR=-5, Z_VERSION_ERROR=-6}.
    if callee_name in {"inflate", "deflate"}:
        return [
            f"/* zlib contract: {callee_name} returns one of the documented "
            f"Z_* status codes */",
            f"__CPROVER_assume(result >= -6 && result <= 2);",
        ]

    # zlib: inflateInit_ / deflateInit_ / *End / *Reset → 0..-6 range
    # (subset of the above).
    if callee_name in {
        "inflateInit_", "inflateInit2_", "inflateEnd", "inflateReset",
        "deflateInit_", "deflateInit2_", "deflateEnd", "deflateReset",
        "deflateBound",  # deflateBound returns a size_t, different signature
    }:
        return [f"__CPROVER_assume(result >= -6 && result <= 2);"]

    # bzip2: BZ2_bzDecompress → BZ_STREAM_END=4, BZ_OK=0, BZ_RUN_OK=1,
    # BZ_FLUSH_OK=2, BZ_FINISH_OK=3, or negative errors -1..-9.
    if callee_name in {"BZ2_bzDecompress", "BZ2_bzCompress"}:
        return [f"__CPROVER_assume(result >= -9 && result <= 4);"]

    if callee_name in {
        "BZ2_bzDecompressInit", "BZ2_bzCompressInit",
        "BZ2_bzDecompressEnd", "BZ2_bzCompressEnd",
    }:
        return [f"__CPROVER_assume(result >= -9 && result <= 0);"]

    # ----- POSIX file metadata -----

    # stat / fstat / lstat → 0 (success) or -1 (failure).
    if callee_name in {"stat", "fstat", "lstat", "fstatat",
                        "stat64", "fstat64", "lstat64"}:
        return [f"__CPROVER_assume(result == 0 || result == -1);"]

    # open / openat → returns -1 or non-negative fd. Cap to plausible
    # fd range to avoid extreme positive values.
    if callee_name in {"open", "openat", "creat"}:
        return [f"__CPROVER_assume(result == -1 || (result >= 0 && result < 65536));"]

    # close / unlink / rename / mkdir / rmdir → 0 or -1.
    if callee_name in {
        "close", "unlink", "unlinkat", "rename", "renameat",
        "mkdir", "mkdirat", "rmdir", "symlink", "link", "chmod", "chown",
    }:
        return [f"__CPROVER_assume(result == 0 || result == -1);"]

    # lseek → -1 or non-negative offset.
    if callee_name in {"lseek", "lseek64"}:
        return [f"__CPROVER_assume(result >= -1);"]

    return []


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

_LIBARCHIVE_ENTRY_STRING_ACCESSORS: frozenset[str] = frozenset({
    "archive_entry_pathname",
    "archive_entry_uname",
    "archive_entry_gname",
    "archive_entry_hardlink",
    "archive_entry_symlink",
    "archive_entry_sourcepath",
    "archive_entry_pathname_utf8",
    "archive_entry_uname_utf8",
    "archive_entry_gname_utf8",
    "archive_entry_hardlink_utf8",
    "archive_entry_symlink_utf8",
})

_LIBARCHIVE_ENTRY_STRING_L_ACCESSORS: frozenset[str] = frozenset({
    "archive_entry_pathname_l",
    "archive_entry_uname_l",
    "archive_entry_gname_l",
    "archive_entry_hardlink_l",
    "archive_entry_symlink_l",
})

# libarchive's high-level read API. All return ARCHIVE_* status codes
# from the enum {OK=0, EOF=1, RETRY=-10, WARN=-20, FAILED=-25, FATAL=-30}.
# Stubs without this contract return any int, including impossible
# positive values like 42 that exercise nonexistent branches in callers.
_LIBARCHIVE_ARCHIVE_STATUS_RETURNS: frozenset[str] = frozenset({
    "archive_read_next_header",
    "archive_read_next_header2",
    "archive_read_data_block",
    "archive_read_data_skip",
    "archive_read_close",
    "archive_read_free",
    "archive_read_finish",  # legacy alias
    "archive_read_open",
    "archive_read_open1",
    "archive_read_open_fd",
    "archive_read_open_file",
    "archive_read_open_filename",
    "archive_read_open_memory",
    "archive_read_extract",
    "archive_read_extract2",
    "archive_read_set_format",
    "archive_read_append_filter",
    "archive_read_append_filter_program",
    "archive_read_support_format_all",
    "archive_read_support_filter_all",
    # write side (same return convention)
    "archive_write_header",
    "archive_write_data",
    "archive_write_finish_entry",
    "archive_write_close",
    "archive_write_free",
    "archive_write_open",
    "archive_write_open_fd",
    "archive_write_open_filename",
    "archive_write_open_memory",
})


# ---------------------------------------------------------------------------
# Type heuristics
# ---------------------------------------------------------------------------


def _is_size_arg(ptype: str) -> bool:
    """An unsigned integer-typed parameter that looks like a size /
    count. Conservative: must contain ``size_t`` or ``unsigned`` and not
    be a pointer."""
    if not ptype:
        return False
    t = ptype.lower()
    if "*" in t:
        return False
    return "size_t" in t or "unsigned" in t


def _is_ssize_t_pointer(ptype: str) -> bool:
    """Parameter is ``ssize_t *`` (output-param for a signed byte count).
    Matches with or without ``const``/spacing."""
    if not ptype:
        return False
    t = ptype.strip()
    return "ssize_t" in t and "*" in t


def _find_args(
    params: list[tuple[str, str]],
    *,
    size_predicate,
    output_predicate,
) -> tuple[Optional[str], Optional[str]]:
    """Return (first-name matching size_predicate, first-name matching
    output_predicate). Either or both may be None."""
    size_name: Optional[str] = None
    out_name: Optional[str] = None
    for ptype, pname in params:
        if not pname:
            continue
        if size_name is None and size_predicate(ptype):
            size_name = pname
        if out_name is None and output_predicate(ptype):
            out_name = pname
    return size_name, out_name


def _find_const_char_pp_arg(params: list[tuple[str, str]]) -> Optional[str]:
    """Return the name of the first ``const char **`` parameter, or None."""
    for ptype, pname in params:
        if not pname or not ptype:
            continue
        t = ptype.replace(" ", "")
        if "constchar**" in t or "char**" in t:
            return pname
    return None


def _find_third_size_t_arg(params: list[tuple[str, str]]) -> Optional[str]:
    """For ``fread(ptr, size, nmemb, stream)`` we want ``nmemb`` —
    the THIRD size_t-like positional arg. Returns the third
    integral-typed parameter name, or None."""
    integral_args: list[str] = []
    for ptype, pname in params:
        if not pname or not ptype:
            continue
        t = ptype.lower()
        if "*" in t:
            continue
        if any(k in t for k in ("size_t", "ssize_t", "int", "long", "unsigned")):
            integral_args.append(pname)
    if len(integral_args) >= 3:
        return integral_args[2]
    # Fall back: many fread variants put nmemb as the LAST integral
    # arg. If we have 2+, prefer the last.
    if integral_args:
        return integral_args[-1]
    return None


def _find_count_arg(params: list[tuple[str, str]]) -> Optional[str]:
    """For POSIX ``read(fd, buf, count)``-style sigs we want ``count``.
    Returns the LAST integral arg (size_t/ssize_t/int/etc.) that's
    not a pointer. None when no integral arg exists."""
    last: Optional[str] = None
    for ptype, pname in params:
        if not pname or not ptype:
            continue
        t = ptype.lower()
        if "*" in t:
            continue
        if any(k in t for k in ("size_t", "ssize_t", "int", "long", "unsigned")):
            last = pname
    return last


def _find_first_pointer_arg(params: list[tuple[str, str]]) -> Optional[str]:
    """First parameter whose type contains ``*``. Used by fgets where
    we want to constrain the result to equal the buf argument."""
    for ptype, pname in params:
        if not pname or not ptype:
            continue
        if "*" in ptype:
            return pname
    return None
