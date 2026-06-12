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
into such a copy SINK and, when the destination is a FIXED-size buffer whose
size is resolvable from the (preprocessed) body, widens each source to that
destination size so the overflow is EXACTLY reachable while the unwind cost is
the minimum needed (a 16-byte buffer wants unwind 18, a 256-byte buffer 258 —
both cheap in isolation; CBMC measured 0.29s at unwind 258).  Sources whose
destination size can't be resolved fall back to a flat default cap.  Detection
is intentionally syntactic and conservative: it widens only the few sources that
are demonstrably copy sources, leaving every other input modeled short.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# strcpy/stpcpy/strcat: dst = arg0, src = arg1 (the string copied INTO dst).
# strncpy/strncat take an explicit bound, so they are NOT unbounded sinks.
_SINK_RE = re.compile(r"\b(?:strcpy|stpcpy|strcat)\s*\(", re.ASCII)
# A leading C cast on an expression, e.g. ``(char*)`` / ``(const char *)``.
_CAST_RE = re.compile(r"^\s*\(\s*(?:const\s+)?[A-Za-z_]\w*(?:\s+\w+)*\s*\*+\s*\)\s*")
# ``root->field`` / ``root.field``.
_FIELD_RE = re.compile(r"^([A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*)")
# A bare identifier (whole expression is just a name).
_BARE_RE = re.compile(r"^([A-Za-z_]\w*)\s*$")


@dataclass(frozen=True)
class CopySink:
    """One detected unbounded string copy. ``src_param``/``src_field`` name the
    harness input that is the SOURCE (exactly one is set); ``dest_size`` is the
    resolved fixed destination size in bytes, or ``None`` if unresolvable."""
    src_param: str | None
    src_field: str | None
    dest_size: int | None


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


def _expr_root(expr: str) -> tuple[str | None, str | None]:
    """From an lvalue expression return ``(root_ident, field)``.

    ``(char*)temp->data`` -> ``("temp", "data")``; ``p`` -> ``("p", None)``;
    a non-lvalue (string literal, call) -> ``(None, None)``.
    """
    s = expr.strip()
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


# Backwards-compatible alias (the source-root extractor).
_source_root = _expr_root


def _resolve_dest_size(body: str, dest_expr: str) -> int | None:
    """Resolve the fixed byte size of the destination buffer named by
    ``dest_expr`` from the (preprocessed) function body.  Recognises:

      * ``T dst[N];``                local fixed array (any element of size 1,
                                     i.e. char/unsigned char/...) -> N
      * ``dst = malloc(N)`` / ``alloca(N)`` / ``(T*)malloc(N)``      -> N
      * ``dst = calloc(A, B)``       -> A*B
      * ``dst = realloc(_, N)``      -> N

    Returns ``None`` when the size is not a plain integer literal (e.g.
    ``malloc(strlen(x)+1)`` — a correctly-sized buffer that must NOT be widened
    into, else a false positive).  N is taken literally because the body is
    preprocessed (object-like ``#define``s already expanded).
    """
    root, _field = _expr_root(dest_expr)
    if not root:
        return None
    d = re.escape(root)
    # Fixed local array: `<type> dst [ N ]`. Restrict to single-byte element
    # types so the byte size equals N (char/unsigned char/signed char/u?int8).
    arr = re.search(
        r"\b(?:const\s+)?(?:unsigned\s+char|signed\s+char|char|u?int8_t|[su]8)\b"
        r"[^\n;]*?\b" + d + r"\s*\[\s*(\d+)\s*\]",
        body,
    )
    if arr:
        return int(arr.group(1))
    # calloc(A, B) -> A*B
    cal = re.search(d + r"\s*=\s*(?:\([^)]*\)\s*)?\bcalloc\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", body)
    if cal:
        return int(cal.group(1)) * int(cal.group(2))
    # realloc(ptr, N) -> N
    rea = re.search(d + r"\s*=\s*(?:\([^)]*\)\s*)?\brealloc\s*\([^,]+,\s*(\d+)\s*\)", body)
    if rea:
        return int(rea.group(1))
    # malloc(N) / alloca(N) / k*alloc(N) with a single literal arg.
    mal = re.search(d + r"\s*=\s*(?:\([^)]*\)\s*)?\b[A-Za-z_]*alloc[A-Za-z_]*\s*\(\s*(\d+)\s*\)", body)
    if mal:
        return int(mal.group(1))
    return None


def detect_copy_sinks(func) -> list[CopySink]:
    """Return one :class:`CopySink` per unbounded string copy in ``func.body``
    whose source is a harness input (bare param, or ``root->field`` whose field
    is modeled)."""
    body = getattr(func, "body", "") or ""
    try:
        params = {pn for _, pn in func.signature.parameters if pn}
    except Exception:
        params = set()
    sinks: list[CopySink] = []
    for m in _SINK_RE.finditer(body):
        args_txt = _balanced_args(body, m.end() - 1)
        if args_txt is None:
            continue
        args = _split_top_level_args(args_txt)
        if len(args) < 2:
            continue
        root, field = _expr_root(args[1])
        if root is None:
            continue
        dest_size = _resolve_dest_size(body, args[0])
        if field is not None:
            sinks.append(CopySink(src_param=None, src_field=field, dest_size=dest_size))
        elif root in params:
            sinks.append(CopySink(src_param=root, src_field=None, dest_size=dest_size))
    return sinks


def detect_copy_sources(func) -> tuple[set[str], set[str]]:
    """Return ``(source_param_names, source_field_names)`` — the harness inputs
    used as the SOURCE of an unbounded string copy.  Thin wrapper over
    :func:`detect_copy_sinks` for callers that only need the name sets."""
    params: set[str] = set()
    fields: set[str] = set()
    for s in detect_copy_sinks(func):
        if s.src_param:
            params.add(s.src_param)
        if s.src_field:
            fields.add(s.src_field)
    return params, fields


def plan_copy_source_widening(
    func, default_cap: int, ceiling: int
) -> tuple[dict[str, int], dict[str, int], int]:
    """Plan the per-source string widening for ``func``.

    Returns ``(param_maxlen, field_maxlen, unwind_floor)``:
      * ``param_maxlen[name]`` / ``field_maxlen[name]`` — the NUL-position upper
        bound to model for that source (in chars).
      * ``unwind_floor`` — the minimum per-function ``--unwind`` so the copy
        loop reaches the overflowing iteration; 0 when no qualifying sink.

    For a sink with a resolved fixed destination of size ``N`` the source is
    widened to ``min(N, ceiling)`` (exactly enough to overflow an ``N``-byte
    buffer, capped for tractability); when ``N`` is unresolvable the source is
    widened to ``default_cap``.  Multiple sinks for the same source take the max.
    """
    if not default_cap or default_cap <= 0:
        return {}, {}, 0
    param_maxlen: dict[str, int] = {}
    field_maxlen: dict[str, int] = {}
    for s in detect_copy_sinks(func):
        if s.dest_size is not None:
            want = min(s.dest_size, ceiling)
        else:
            want = default_cap
        if s.src_param:
            param_maxlen[s.src_param] = max(param_maxlen.get(s.src_param, 0), want)
        if s.src_field:
            field_maxlen[s.src_field] = max(field_maxlen.get(s.src_field, 0), want)
    all_lens = list(param_maxlen.values()) + list(field_maxlen.values())
    unwind_floor = (max(all_lens) + 2) if all_lens else 0
    return param_maxlen, field_maxlen, unwind_floor


def copy_sink_unwind_floor(func, default_cap: int, ceiling: int = 256) -> int:
    """Per-function unwind floor needed to reach a copy overflow (0 when none)."""
    _p, _f, floor = plan_copy_source_widening(func, default_cap, ceiling)
    return floor
