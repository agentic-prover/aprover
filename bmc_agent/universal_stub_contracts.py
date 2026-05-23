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
