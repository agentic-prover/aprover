"""Detect unbounded string-copy SINKS whose SOURCE is a harness-modeled input.

Motivation (the false-negative dual of the ``(buf,len)`` over-read fix):
``strcpy``/``strcat``/``stpcpy`` are modeled by CBMC as byte-copy LOOPS, so a
copy-into-fixed-buffer overflow is reachable only when (a) the SOURCE string can
be longer than the destination and (b) the loop is unrolled far enough to reach
the overflowing iteration.  The harness, however, models a ``char *`` input as a
NUL-terminated string of length ``<= cbmc_unwind`` (typically 4), BAKED IN at
generation time and independent of the per-function runtime ``--unwind``.  So
even when CBMC runs the function at ``--unwind 64`` the source is still <= 4
bytes and the copy can never overflow a fixed buffer -> the bug is silently
missed (e.g. vibeos ``vfs_open_handle`` ``strcpy(path_copy[256], temp->data)``).

This module finds the ``char *`` inputs (parameters / struct fields) that flow
into such a copy SINK, so the harness can model THOSE sources as longer
(``string_copy_source_max_len``) while the BMC engine raises the per-function
unwind floor to match.  Detection is intentionally syntactic and conservative:
it only widens the few sources that are demonstrably copy sources, leaving every
other input modeled short (tractable, no spurious over-read FPs).
"""
from __future__ import annotations

import re

# strcpy/stpcpy/strcat: dst = arg0, src = arg1 (the string copied INTO dst).
# strncpy/strncat take an explicit bound, so they are NOT unbounded sinks.
_SINK_RE = re.compile(r"\b(?:strcpy|stpcpy|strcat)\s*\(", re.ASCII)
# A leading C cast on the source expression, e.g. ``(char*)`` / ``(const char *)``.
_CAST_RE = re.compile(r"^\s*\(\s*(?:const\s+)?[A-Za-z_]\w*(?:\s+\w+)*\s*\*+\s*\)\s*")
# ``root->field`` / ``root.field``.
_FIELD_RE = re.compile(r"^([A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*)")
# A bare identifier (whole source expression is just a name).
_BARE_RE = re.compile(r"^([A-Za-z_]\w*)\s*$")


def _balanced_args(body: str, open_paren_idx: int) -> str | None:
    """``body[open_paren_idx]`` is ``'('``; return the substring between it and
    its matching ``')'`` (exclusive), or ``None`` if unbalanced."""
    depth = 0
    for i in range(open_paren_idx, len(body)):
        c = body[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return body[open_paren_idx + 1:i]
    return None


def _split_top_level_args(s: str) -> list[str]:
    """Split a call-argument string on TOP-LEVEL commas (paren/bracket aware)."""
    args: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    return args


def _source_root(src_text: str) -> tuple[str | None, str | None]:
    """From a source-argument expression return ``(root_ident, field)``.

    ``(char*)temp->data`` -> ``("temp", "data")``; ``p`` -> ``("p", None)``;
    a non-lvalue expression (string literal, function call) -> ``(None, None)``.
    """
    s = src_text.strip()
    prev = None
    while prev != s:                      # peel casts / address-of / deref
        prev = s
        s = _CAST_RE.sub("", s).strip()
        s = s.lstrip("&* \t").strip()
    m = _FIELD_RE.match(s)
    if m:
        return m.group(1), m.group(2)
    m = _BARE_RE.match(s)
    if m:
        return m.group(1), None
    return None, None


def detect_copy_sources(func) -> tuple[set[str], set[str]]:
    """Return ``(source_param_names, source_field_names)`` — the harness inputs
    used as the SOURCE of an unbounded string copy in ``func.body``.

    A bare-identifier source that names a parameter widens that parameter; a
    ``root->field`` source widens any modeled struct field with that name
    (coarse-by-name, but scoped to this one function's harness).
    """
    body = getattr(func, "body", "") or ""
    try:
        params = {pn for _, pn in func.signature.parameters if pn}
    except Exception:
        params = set()
    src_params: set[str] = set()
    src_fields: set[str] = set()
    for m in _SINK_RE.finditer(body):
        args_txt = _balanced_args(body, m.end() - 1)
        if args_txt is None:
            continue
        args = _split_top_level_args(args_txt)
        if len(args) < 2:
            continue
        root, field = _source_root(args[1])
        if root is None:
            continue
        if field is not None:
            src_fields.add(field)
        elif root in params:
            src_params.add(root)
    return src_params, src_fields


def copy_sink_unwind_floor(func, copy_source_max_len: int) -> int:
    """Per-function unwind floor needed to reach a copy overflow: enough to
    unroll the copy loop past a source of length ``copy_source_max_len``.
    Returns 0 when the function has no qualifying copy sink (no floor)."""
    if not copy_source_max_len or copy_source_max_len <= 0:
        return 0
    src_params, src_fields = detect_copy_sources(func)
    if not (src_params or src_fields):
        return 0
    return copy_source_max_len + 2
