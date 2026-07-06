"""
Universal preconditions for bmc-agent-lite.

Lite-mode strips the LLM-inferred precondition (every function gets
``pre=true``), which causes CBMC to find caller-contract slips —
violations that are syntactically legal but unreachable from any real
caller in practice (paired pointers from unrelated buffers, NULL
function-pointer ops tables, etc.). This module synthesises a small
set of *universally-true* preconditions from parameter names + types
(and optionally struct field info), *without* asking an LLM, so the
lite-mode harness gets meaningful input constraints for the dominant
FP classes.

Patterns covered:

* **Paired pointers** (always on) — canonical pair names
  (``start``/``end``, ``src``/``dst``, ``first``/``last``, …) when
  both are pointer-typed emit ``a <= b``. The existing
  ``_detect_paired_pointers`` in ``harness_generator.py`` picks this
  up and allocates a single shared backing buffer per pair.
  Eliminates the libarchive ``ismode(const char *start, const char *
  end, …)`` family of FPs (2026-05-23 calibration data).

* **Ops/vtable non-null** (when ``struct_definitions`` is available) —
  when a struct param has a recognisable ops/vtable field (name in
  ``{ops, vtable, vtbl, callbacks}``) the synthesised precondition
  emits ``param->ops != NULL`` and, for every function-pointer field
  in the ops struct's body, ``param->ops->X != NULL``. This is the
  fix for the libarchive ``__archive_rb_tree_*`` family of FPs from
  the 2026-05-22 archive_rb.c sweep: every callback in the ops
  table is unconditionally invoked by the tree-utility functions,
  and real callers always populate them via ``__archive_rb_tree_init``.

* **Length-bound** (always on, bound from ``cbmc_unwind``) — when a
  pointer param is paired with a canonical length param (``buf``/
  ``len``, ``data``/``size``, ``p``/``n``, …) the synthesised
  precondition emits ``len <= cbmc_unwind``. Pairs with
  ``infer_array_param_bounds`` so the harness gets a buffer sized to
  match the upper-bounded length.

* **Magic-field invariant** (when ``struct_definitions`` is available)
  — when a struct body has a ``magic`` / ``sentinel`` / ``valid``
  integer field, emit ``param->magic != 0``. Conservative — catches
  only the ubiquitous "0 = uninitialised" convention; deeper magic-
  value reasoning is the job of LLM spec gen.

By design, universal contracts are **conservative**: they encode
properties every real caller maintains, so adding them as
preconditions doesn't mask any real bug a real caller could trigger.
The trade-off is that the contracts hide CBMC findings that would
require the property to be VIOLATED — which is exactly what we want
for the FP classes targeted here.

Off by default for non-lite modes (the LLM spec gen already produces
better preconditions per function). On by default in lite-mode via
``config.lite_with_contracts``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from bmc_agent.parser import FunctionInfo


# Pairs of parameter names that real callers always derive from the
# same buffer (and where ``a <= b`` always holds). Each tuple is
# ordered: the first is the "left" pointer, the second is the "right"
# pointer; the synthesised precondition is ``<left> <= <right>``.
_PAIRED_POINTER_NAMES: tuple[tuple[str, str], ...] = (
    ("start", "end"),
    ("begin", "end"),
    ("first", "last"),
    ("head", "tail"),
    ("low", "high"),
    ("from", "to"),
    ("src", "dst"),
    ("source", "destination"),
)

# Pairs of (buffer-pointer-param-name, length-param-name) where every
# real caller satisfies ``length <= |buffer|``. The buffer is keyed
# first (must be pointer-typed); each value lists candidate length-
# param names in order of preference.
_PAIRED_BUF_LEN_NAMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("buf", ("buflen", "bufsize", "len", "size", "n")),
    ("data", ("datalen", "datasize", "len", "size", "n")),
    ("ptr", ("len", "size", "n")),
    ("s", ("slen", "len", "size", "n")),
    ("str", ("len", "strlen")),
    ("p", ("n", "len")),
    ("dest", ("destlen", "len", "size", "n")),
    ("dst", ("dstlen", "len", "size", "n")),
    ("src", ("srclen", "len", "size", "n")),
    ("input", ("input_len", "inlen", "len", "n")),
    ("output", ("output_len", "outlen", "len", "n")),
)

# Struct field names that identify an ops/vtable subobject. When such
# a field exists on a parameter's struct, the synthesised precondition
# asserts the field is non-NULL, and (when the pointed-to struct's body
# is available) every function-pointer member of the ops table is
# non-NULL too.
_OPS_FIELD_NAMES: frozenset[str] = frozenset({
    "ops", "vtable", "vtbl", "callbacks", "cb",
})

# Struct field names that conventionally hold a non-zero "valid" sentinel
# (e.g. a four-byte magic constant). When the field exists and is integer-
# typed, the synthesised precondition asserts it's non-zero — the universal
# part of every magic-field convention.
_MAGIC_FIELD_NAMES: frozenset[str] = frozenset({
    "magic", "sentinel", "valid", "marker", "cookie",
})


def _is_pointer_type(c_type: str) -> bool:
    """Conservative pointer-type detector. Matches anything containing
    a ``*``, which captures ``char *``, ``const char *``, ``void *``,
    ``struct foo *``, ``foo **``, etc. — every form we care about for
    universal contracts.
    """
    return "*" in (c_type or "")


def _is_integer_type(c_type: str) -> bool:
    """Conservative integer-type detector. Excludes pointers and
    floating types; covers ``int``, ``unsigned``, ``size_t``,
    ``uint32_t``, ``long``, ``char``, etc.
    """
    if not c_type:
        return False
    t = c_type.lower()
    if "*" in t:
        return False
    if any(f in t for f in ("float", "double")):
        return False
    return any(
        kw in t for kw in (
            "int", "char", "short", "long", "size_t", "ssize_t",
            "off_t", "uint", "byte", "u8", "u16", "u32", "u64",
            "i8", "i16", "i32", "i64", "bool",
        )
    )


def _is_function_pointer_type(c_type: str) -> bool:
    """Detect function-pointer field types.

    Matches either the literal ``(*`` (canonical syntax like
    ``int (*compare)(void *, void *)``) or names ending in known
    function-pointer typedef suffixes (``_fn``, ``_callback``, ``_cb``).
    """
    if not c_type:
        return False
    if "(*" in c_type:
        return True
    t = c_type.strip()
    return t.endswith("_fn") or t.endswith("_callback") or t.endswith("_cb")


def _struct_tag_from_param_type(c_type: str) -> Optional[str]:
    """Return the struct tag a pointer-to-struct param refers to, or
    None when the type doesn't resolve to a struct in the visible
    universe of struct definitions.

    Handles:
      * ``struct Foo *``       → ``Foo``
      * ``const struct Foo *`` → ``Foo``
      * ``Foo *`` (typedef)    → ``Foo``      (caller verifies vs dict)
      * ``int``                → None
    """
    if not c_type:
        return None
    s = c_type.strip()
    s = re.sub(r"\bconst\b", "", s).strip()
    s = re.sub(r"\*+\s*$", "", s).strip()
    if not s:
        return None
    if s.startswith("struct "):
        return s[len("struct "):].strip() or None
    # Could be a typedef name; caller checks against struct_definitions.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
        return s
    return None


def derive_universal_precondition(
    func: "FunctionInfo",
    struct_definitions: Optional[dict] = None,
    cbmc_unwind: int = 4,
) -> str:
    """Return a deterministic precondition string for *func*, built
    only from parameter names + types (+ optionally struct field
    info) — no LLM, no parsed body.

    Returns ``"true"`` when no universal pattern matches, so the
    caller can use the result as a drop-in replacement for the
    lite-mode default. Multiple clauses are joined with ``&&``.

    Parameters
    ----------
    func:
        FunctionInfo with signature populated.
    struct_definitions:
        Optional ``{tag: [(field_type, field_name), …]}`` mapping. When
        supplied, ops/vtable + magic-field contracts can fire; when
        absent, only the name-and-type-only patterns (paired pointers,
        length bounds) emit clauses.
    cbmc_unwind:
        The CBMC ``--unwind`` bound. Used as the upper-bound for
        length-param contracts.

    Examples
    --------
    >>> derive_universal_precondition(fn_with_start_end_char_ptrs)
    'start <= end'

    >>> derive_universal_precondition(fn_with_buf_len_pair, cbmc_unwind=4)
    'len <= 4'
    """
    sig = getattr(func, "signature", None)
    if sig is None or not sig.parameters:
        return "true"

    pname_to_type: dict[str, str] = {}
    for ptype, pname in sig.parameters:
        if pname and ptype:
            pname_to_type[pname] = ptype

    clauses: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Pattern 1: paired pointers.
    for left, right in _PAIRED_POINTER_NAMES:
        if left in pname_to_type and right in pname_to_type:
            if not _is_pointer_type(pname_to_type[left]):
                continue
            if not _is_pointer_type(pname_to_type[right]):
                continue
            pair_key = tuple(sorted((left, right)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            clauses.append(f"{left} <= {right}")

    # Pattern 2: (buf, len) length-bound. ``len <= cbmc_unwind`` matches
    # the harness's backing-buffer size (the harness allocates
    # ``cbmc_unwind+1`` bytes for char* buffers by default).
    seen_len_params: set[str] = set()
    for buf_name, len_candidates in _PAIRED_BUF_LEN_NAMES:
        if buf_name not in pname_to_type:
            continue
        if not _is_pointer_type(pname_to_type[buf_name]):
            continue
        for ln in len_candidates:
            if ln in seen_len_params:
                continue
            if ln in pname_to_type and _is_integer_type(pname_to_type[ln]):
                clauses.append(f"{ln} <= {cbmc_unwind}")
                seen_len_params.add(ln)
                break

    # Pattern 2b (generalized, caller-grounded): a single-indirection pointer
    # IMMEDIATELY followed by a size-named integer is the universal (buf, len)
    # contract for ANY buffer name (not just the fixed set) -- e.g.
    # sum(int *a, size_t n), fill(float *x, int n). Emit valid_range(buf, 0, len):
    # the EXACT-size caller contract (every real caller passes >= len elements).
    # Exact (vs len<=unwind) keeps an off-by-one past len a real OOB. Excludes
    # char*/wchar_t (the NUL-terminated string convention, handled separately).
    # Sound/universal: a caller violating it has the bug (caught at the caller or
    # surfaced latent), so assuming it masks no in-contract bug.
    _SIZE_NAMES = {"n", "len", "length", "size", "sz", "count", "num", "nmemb",
                   "nbytes", "bytes", "buflen", "bufsize", "datalen", "slen",
                   "width", "height", "nelem", "nelems", "elems"}
    _seq = [(pt, pn) for pt, pn in sig.parameters if pn]
    for _i in range(len(_seq) - 1):
        _pt, _pn = _seq[_i]
        _lt, _ln = _seq[_i + 1]
        if not _is_pointer_type(_pt) or _pt.count("*") != 1:
            continue
        _base = re.sub(r"\bconst\b", "", _pt).replace("*", "").strip()
        if _base in ("char", "wchar_t"):        # string convention -> NUL path
            continue
        if _ln.lower() not in _SIZE_NAMES or not _is_integer_type(_lt):
            continue
        _vr = f"valid_range({_pn}, 0, {_ln})"
        if _vr not in clauses:
            clauses.append(_vr)

    # Pattern 3: ops/vtable non-null. Requires struct_definitions to
    # know the param's struct body. Multi-level: the ops field itself
    # is non-NULL AND every function-pointer member of the ops struct
    # (when its body is also known) is non-NULL.
    if struct_definitions:
        for pname, ptype in pname_to_type.items():
            if not _is_pointer_type(ptype):
                continue
            tag = _struct_tag_from_param_type(ptype)
            if tag is None or tag not in struct_definitions:
                continue
            for ftype, fname in struct_definitions[tag]:
                if not fname:
                    continue
                if fname.lower() in _OPS_FIELD_NAMES and _is_pointer_type(ftype):
                    clauses.append(f"{pname}->{fname} != NULL")
                    # Recurse one level: function-pointer fields of the
                    # pointed-to ops struct.
                    inner_tag = _struct_tag_from_param_type(ftype)
                    if inner_tag and inner_tag in struct_definitions:
                        for i_ftype, i_fname in struct_definitions[inner_tag]:
                            if i_fname and _is_function_pointer_type(i_ftype):
                                clauses.append(
                                    f"{pname}->{fname}->{i_fname} != NULL"
                                )

    # Pattern 4: magic / sentinel / valid integer fields non-zero.
    if struct_definitions:
        for pname, ptype in pname_to_type.items():
            if not _is_pointer_type(ptype):
                continue
            tag = _struct_tag_from_param_type(ptype)
            if tag is None or tag not in struct_definitions:
                continue
            for ftype, fname in struct_definitions[tag]:
                if not fname:
                    continue
                if fname.lower() in _MAGIC_FIELD_NAMES and _is_integer_type(ftype):
                    clauses.append(f"{pname}->{fname} != 0")

    if not clauses:
        return "true"
    return " && ".join(clauses)


def derive_contract_summary(func: "FunctionInfo") -> dict[str, list[str]]:
    """Same as :func:`derive_universal_precondition` but returns a
    structured digest the autonomous-mode summary can log per round.
    """
    sig = getattr(func, "signature", None)
    pname_to_type: dict[str, str] = {}
    if sig and sig.parameters:
        for ptype, pname in sig.parameters:
            if pname and ptype:
                pname_to_type[pname] = ptype

    paired: list[str] = []
    for left, right in _PAIRED_POINTER_NAMES:
        if (
            left in pname_to_type
            and right in pname_to_type
            and _is_pointer_type(pname_to_type[left])
            and _is_pointer_type(pname_to_type[right])
        ):
            paired.append(f"{left} <= {right}")
    return {"paired_pointers": paired}


def known_param_pairs() -> Iterable[tuple[str, str]]:
    """Expose the pair table for tests / docs."""
    return iter(_PAIRED_POINTER_NAMES)
