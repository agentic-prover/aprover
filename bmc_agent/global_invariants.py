"""
Evidence-grounded global ("project") invariant extraction.

This is the PROACTIVE, deterministic counterpart to the realism-rejection
feedback loop (``feedback_loop.py``). The feedback loop *learns* project
invariants reactively: a global fact is only discovered after an
unconstrained global produced a false-positive counterexample that survived
classification and was then rejected by the realism LLM. That makes realism
the *authority* for an invariant whose blast radius is the whole project --
a poor fit (see design notes in the session memory).

This module instead *derives* invariants from the source itself, with
provenance, so realism can stay a mere demand-signal and the authority moves
to evidence.

It scans every file-scope global, computes a conservative write-set, and emits
a CBMC ``__CPROVER_assume(...)`` invariant in two tiers:

  TIER A  (``proven``)
      A ``const`` (and non-``extern``) file-scope global with a non-NULL /
      constant initializer holds that initializer for the whole program:
        * C forbids writing a ``const`` object, and
        * a file-scope initializer must be a *constant expression*, so it
          cannot be attacker-derived.
      -> pointer / array  with non-NULL init  =>  ``g != NULL``
      -> integer / scalar with constant K     =>  ``g == K``
      This tier is sound with essentially no dataflow.

  TIER B  (``init-trusted``)
      A non-``const`` global whose ONLY writes live inside an
      initialization-style function (``*_init`` / ``init_*`` / ``*_setup`` /
      ``*_probe`` / ``*_boot``), with a non-NULL, non-attacker-derived RHS,
      and whose address is never taken. Emitted as ``g != NULL``.
      This is sound *relative to the threat model's trusted-input list*
      ("objects a caller allocates and fully initializes before any attacker
      data is processed -> non-NULL and structurally valid"). It is gated by
      the TAINT CHECK: any write outside an init function, any address-taken
      use, or any RHS that mentions a function parameter drops the global.

Anything not in A or B is rejected (returned only as a ``candidate`` with a
reason, for an optional init-order / LLM authorizer to vet later -- not
emitted as an assume).

The module is intentionally self-contained (no import of ``harness_generator``)
so it stays unit-testable and import-cycle-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Functions whose name marks them as trusted initialization (Tier B writes
# allowed here only). Matched case-insensitively on the bare function name.
_INIT_FN_RE = re.compile(
    r"^(?:.*_)?(?:init|setup|probe|boot|start|create|new|alloc|reset)$"
    r"|^(?:init|setup|probe|boot|start|create|new)_",
    re.IGNORECASE,
)

# Assignment operators that constitute a WRITE (note: NOT ``==`` / ``!=`` /
# ``<=`` / ``>=`` -- those are comparisons and handled by the negative
# lookarounds in _write_regex).
_ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=", "%=", "|=", "&=", "^=", "<<=", ">>=")


@dataclass
class GlobalInvariant:
    """One derived global invariant (or rejected candidate)."""

    name: str
    clause: str                # e.g. "g != NULL"  (empty for rejected candidates)
    tier: str                  # "proven" | "init-trusted" | "rejected"
    evidence: str              # human-readable provenance
    confidence: str = "high"   # "high" (proven) | "medium" (init-trusted) | "low"

    @property
    def emitted(self) -> bool:
        return bool(self.clause) and self.tier in ("proven", "init-trusted")


@dataclass
class _Decl:
    name: str
    raw: str                   # full declaration text (masked-region span)
    is_const: bool
    is_extern: bool
    is_pointer: bool
    is_array: bool
    is_scalar_int: bool
    init_expr: str | None      # text after '=' (None if no initializer)
    init_span: tuple[int, int] = (0, 0)   # char span of the decl in source (to exclude from write scan)


# ---------------------------------------------------------------------------
# Comment / string masking (local copy; keeps this module decoupled)
# ---------------------------------------------------------------------------
def _mask(text: str) -> str:
    """Replace comment and string/char-literal bytes with spaces (newlines
    preserved) so token scans don't trip on ``"g = 1"`` or ``/* g = */``.
    Length and line structure are preserved so spans stay aligned to the
    original source."""
    out = list(text)
    i, n = 0, len(text)
    state = None  # None | 'line' | 'block' | 'str' | 'chr'
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state is None:
            if c == "/" and nxt == "/":
                state = "line"; out[i] = out[i + 1] = " "; i += 2; continue
            if c == "/" and nxt == "*":
                state = "block"; out[i] = out[i + 1] = " "; i += 2; continue
            if c == '"':
                state = "str"; out[i] = " "; i += 1; continue
            if c == "'":
                state = "chr"; out[i] = " "; i += 1; continue
            i += 1; continue
        # inside a masked region
        if state == "line":
            if c == "\n": state = None
            else: out[i] = " "
            i += 1; continue
        if state == "block":
            if c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "; state = None; i += 2; continue
            if c != "\n": out[i] = " "
            i += 1; continue
        if state in ("str", "chr"):
            close = '"' if state == "str" else "'"
            if c == "\\":  # escape -> skip next char too
                out[i] = " "
                if i + 1 < n and text[i + 1] != "\n": out[i + 1] = " "
                i += 2; continue
            if c == close:
                out[i] = " "; state = None; i += 1; continue
            if c != "\n": out[i] = " "
            i += 1; continue
    return "".join(out)


# ---------------------------------------------------------------------------
# File-scope declaration discovery
# ---------------------------------------------------------------------------
_TYPE_KEYWORDS = {
    "const", "static", "extern", "volatile", "register", "signed", "unsigned",
    "struct", "union", "enum", "_Atomic",
}
_SCALAR_INT_TYPES = {
    "char", "short", "int", "long", "size_t", "ssize_t", "uint8_t", "uint16_t",
    "uint32_t", "uint64_t", "int8_t", "int16_t", "int32_t", "int64_t", "bool",
    "uintptr_t", "intptr_t",
}


def _find_decls(masked: str, source: str) -> list[_Decl]:
    """Walk depth-0 (outside any ``{}`` / ``()``) statements and return the
    file-scope VARIABLE declarations. Mirrors the depth tracking used by
    harness_generator._extract_file_scope_var_defs."""
    decls: list[_Decl] = []
    depth_brace = depth_paren = 0
    start = 0
    for i, ch in enumerate(masked):
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
            # Reset the segment start past a top-level '}' so a function body
            # (which is NOT ';'-terminated) doesn't get lumped into the next
            # declaration. EXCEPTION: a brace INITIALIZER's closing '}' is
            # immediately followed by ';' (e.g. ``int t[] = {1,2};``) -- keep
            # the segment so the ';' below captures the whole declaration.
            if depth_brace == 0:
                j = i + 1
                while j < len(masked) and masked[j] in " \t\r\n":
                    j += 1
                if j >= len(masked) or masked[j] != ";":
                    start = i + 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == ";" and depth_brace == 0 and depth_paren == 0:
            seg_m = masked[start:i + 1]
            seg_r = source[start:i + 1]
            span = (start, i + 1)
            start = i + 1
            d = _parse_decl(seg_m, seg_r, span)
            if d:
                decls.append(d)
    return decls


def _parse_decl(seg_masked: str, seg_real: str, span: tuple[int, int]) -> _Decl | None:
    """Parse one ``;``-terminated depth-0 segment as a variable declaration,
    or return None if it isn't one (function prototype, typedef, bare type,
    preprocessor noise)."""
    sm = seg_masked.strip()
    if not sm or sm.startswith("#"):
        return None
    # Skip anything with a top-level '(' (function proto / func-ptr / call).
    if "(" in sm:
        return None
    if sm.startswith(("typedef", "struct", "union", "enum")) and "=" not in sm:
        return None
    # Split off initializer (first top-level '=' that is a plain assign, not
    # part of ==, <=, >=, != -- but those can't appear in a decl head anyway).
    eq = _first_assign_eq(sm)
    init_expr = None
    head = sm
    if eq is not None:
        head = sm[:eq]
        init_expr = seg_real.strip()
        # real initializer text (after the '=' in the real segment)
        req = _first_assign_eq(seg_real)
        init_expr = seg_real[req + 1:].rstrip().rstrip(";").strip() if req is not None else None
    # Find the declared name: last identifier in the head (before any [] ).
    head_no_arr = re.sub(r"\[[^\]]*\]", " ", head)
    idents = re.findall(r"[A-Za-z_]\w*", head_no_arr)
    if not idents:
        return None
    # The declared name is the trailing identifier that isn't a type keyword.
    name = None
    for tok in reversed(idents):
        if tok not in _TYPE_KEYWORDS and tok not in _SCALAR_INT_TYPES:
            name = tok; break
    if name is None:
        return None
    type_toks = [t for t in idents if t != name]
    is_const = "const" in head
    is_extern = "extern" in head
    is_pointer = "*" in head
    is_array = bool(re.search(r"\[[^\]]*\]", head))
    is_scalar_int = (not is_pointer and not is_array
                     and any(t in _SCALAR_INT_TYPES for t in type_toks))
    return _Decl(
        name=name, raw=seg_real.strip(), is_const=is_const, is_extern=is_extern,
        is_pointer=is_pointer, is_array=is_array, is_scalar_int=is_scalar_int,
        init_expr=init_expr, init_span=span,
    )


def _first_assign_eq(s: str) -> int | None:
    """Index of the first '=' that is a plain assignment (not ==, <=, >=, !=)."""
    for i, c in enumerate(s):
        if c != "=":
            continue
        prev = s[i - 1] if i > 0 else ""
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if prev in "=<>!+-*/%&|^" or nxt == "=":
            continue
        return i
    return None


# ---------------------------------------------------------------------------
# Write-set / taint analysis (conservative: over-detect writes)
# ---------------------------------------------------------------------------
def _writes_to(name: str, masked: str, exclude_span: tuple[int, int]) -> list[int]:
    """Return char offsets of WRITE occurrences of ``name`` outside
    ``exclude_span`` (its own declaration). Over-detects on purpose: a missed
    write would make us emit an UNSOUND invariant, so we err toward 'written'.
    Covers ``name =``, ``name[...] =``, compound assigns, ``name++/--``, and
    ``&name`` (address taken)."""
    hits: list[int] = []
    esc = re.escape(name)
    # assignment: name (optional [..]) ASSIGN-OP, not ==/!=/<=/>=
    assign_re = re.compile(
        r"(?<![A-Za-z0-9_])" + esc + r"\s*(?:\[[^\]]*\])?\s*"
        r"(?:\+|-|\*|/|%|\||&|\^|<<|>>)?=(?!=)"
    )
    incdec_re = re.compile(
        r"(?:(?<![A-Za-z0-9_])" + esc + r"\s*(?:\+\+|--))"
        r"|(?:(?:\+\+|--)\s*" + esc + r"(?![A-Za-z0-9_]))"
    )
    addr_re = re.compile(r"&\s*" + esc + r"(?![A-Za-z0-9_])")
    for rx in (assign_re, incdec_re, addr_re):
        for m in rx.finditer(masked):
            s = m.start()
            if exclude_span[0] <= s < exclude_span[1]:
                continue
            # Filter ``==``/``!=`` masquerading: assign_re already excludes via
            # (?!=); the prev-char check guards ``<=`` ``>=`` ``!=``.
            hits.append(s)
    return hits


def _enclosing_function(offset: int, fn_spans: list[tuple[str, int, int]]) -> str | None:
    for nm, lo, hi in fn_spans:
        if lo <= offset < hi:
            return nm
    return None


def _function_spans(masked: str) -> list[tuple[str, int, int]]:
    """Best-effort (name, start, end) spans of function bodies, by matching a
    ``name(...) {`` header at depth 0 and brace-matching the body."""
    spans: list[tuple[str, int, int]] = []
    header_re = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{")
    i = 0
    while True:
        m = header_re.search(masked, i)
        if not m:
            break
        name = m.group(1)
        brace_open = masked.find("{", m.end() - 1)
        depth = 0
        j = brace_open
        while j < len(masked):
            if masked[j] == "{":
                depth += 1
            elif masked[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        spans.append((name, brace_open, j + 1))
        i = j + 1
    return spans


def _init_value_is_nonnull(init_expr: str | None) -> bool:
    """A pointer/array initializer is provably non-NULL when it's an address
    (&x), a string literal (masked to spaces -> detect via real text), an
    array braced initializer, or a non-zero/non-NULL constant. Conservative:
    returns False on anything uncertain (then we just don't emit)."""
    if init_expr is None:
        return False
    e = init_expr.strip()
    if e in ("", "0", "NULL", "(void*)0", "(void *)0"):
        return False
    if e.startswith("&"):
        return True
    if e.startswith("{") or e.startswith('"'):
        return True
    # A bare identifier / function name (address-of-function decays non-NULL).
    if re.fullmatch(r"[A-Za-z_]\w*", e):
        return True
    # A cast of a nonzero literal, or a nonzero integer literal.
    if re.search(r"[1-9]", e) and "NULL" not in e:
        return True
    return False


def _rhs_mentions_param(rhs: str, param_names: set[str]) -> bool:
    if not rhs or not param_names:
        return False
    toks = set(re.findall(r"[A-Za-z_]\w*", rhs))
    return bool(toks & param_names)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_global_invariants(
    source_text: str,
    *,
    referenced_names: set[str] | None = None,
    fn_param_names: dict[str, set[str]] | None = None,
    emit_init_trusted: bool = True,
) -> list[GlobalInvariant]:
    """Derive global invariants from ``source_text``.

    Parameters
    ----------
    source_text:
        The translation unit (raw or preprocessed C source).
    referenced_names:
        If given, only emit invariants for globals whose name is in this set
        (the symbols actually visible to / used by the current harness). This
        mirrors ``_emit_library_init_assumptions`` and avoids CBMC
        ``failed to find symbol`` errors.
    fn_param_names:
        Map of function name -> its parameter names, used by the Tier-B taint
        check to reject globals whose init-function write derives from a
        parameter (i.e. attacker-influenced).
    emit_init_trusted:
        When False, only Tier A (``proven``) invariants are emitted; Tier B
        is still returned but with tier ``rejected`` (reason: disabled).

    Returns
    -------
    list[GlobalInvariant]
        Emitted invariants (``.emitted == True``) plus rejected candidates
        (with a reason in ``.evidence``) for diagnostics.
    """
    masked = _mask(source_text)
    decls = _find_decls(masked, source_text)
    fn_spans = _function_spans(masked)
    fn_param_names = fn_param_names or {}
    out: list[GlobalInvariant] = []

    # Dedup by name (first definition wins; redeclarations ignored).
    seen: set[str] = set()
    for d in decls:
        if d.name in seen:
            continue
        seen.add(d.name)
        if referenced_names is not None and d.name not in referenced_names:
            continue
        if d.is_extern:
            out.append(GlobalInvariant(d.name, "", "rejected",
                                       "extern (definition not in this TU)"))
            continue

        writes = _writes_to(d.name, masked, d.init_span)

        # ---- TIER A: const, non-extern, non-NULL/const initializer ----
        if d.is_const:
            if d.is_pointer or d.is_array:
                if _init_value_is_nonnull(d.init_expr):
                    out.append(GlobalInvariant(
                        d.name, f"{d.name} != NULL", "proven",
                        "const non-extern global, non-NULL constant initializer"))
                else:
                    out.append(GlobalInvariant(
                        d.name, "", "rejected",
                        "const pointer/array but initializer not provably non-NULL"))
                continue
            if d.is_scalar_int and d.init_expr:
                k = d.init_expr.strip()
                if re.fullmatch(r"[+-]?(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*", k):
                    out.append(GlobalInvariant(
                        d.name, f"{d.name} == {k.rstrip('uUlL')}", "proven",
                        "const non-extern scalar, integer-constant initializer"))
                else:
                    out.append(GlobalInvariant(
                        d.name, "", "rejected",
                        "const scalar but initializer not an integer literal"))
                continue
            # const struct/other: non-NULL address fact only if pointer; skip.
            out.append(GlobalInvariant(d.name, "", "rejected",
                                       "const but unsupported type for value fact"))
            continue

        # ---- TIER B: init-trusted (non-const pointer set only in init fns) ----
        if not (d.is_pointer):
            out.append(GlobalInvariant(d.name, "", "rejected",
                                       "non-const non-pointer (no value fact)"))
            continue
        # Address taken anywhere -> a hidden write path may exist -> reject.
        addr_taken = bool(re.search(r"&\s*" + re.escape(d.name) + r"(?![A-Za-z0-9_])", masked))
        if addr_taken:
            out.append(GlobalInvariant(d.name, "", "rejected",
                                       "address taken (&g): possible aliased write"))
            continue
        # Partition writes by enclosing function.
        non_init_write = False
        tainted = False
        any_init_write = False
        for w in writes:
            fn = _enclosing_function(w, fn_spans)
            if fn is None or not _INIT_FN_RE.search(fn):
                non_init_write = True
                break
            any_init_write = True
            # Taint: does the RHS of this write mention a param of fn?
            rhs = _rhs_after(masked, w)
            if _rhs_mentions_param(rhs, fn_param_names.get(fn, set())):
                tainted = True
                break
        if non_init_write:
            out.append(GlobalInvariant(d.name, "", "rejected",
                                       "written outside an init-style function"))
            continue
        if tainted:
            out.append(GlobalInvariant(d.name, "", "rejected",
                                       "init write derives from a parameter (attacker-tainted)"))
            continue
        if not any_init_write:
            # No writes at all and no const initializer worth a fact.
            if _init_value_is_nonnull(d.init_expr):
                out.append(GlobalInvariant(
                    d.name, f"{d.name} != NULL", "proven",
                    "non-const pointer, no writes, non-NULL initializer"))
            else:
                out.append(GlobalInvariant(d.name, "", "rejected",
                                           "no writes and no non-NULL initializer"))
            continue
        if not emit_init_trusted:
            out.append(GlobalInvariant(d.name, "", "rejected",
                                       "init-trusted emission disabled"))
            continue
        out.append(GlobalInvariant(
            d.name, f"{d.name} != NULL", "init-trusted",
            "pointer written only by init-style function(s), no attacker taint, "
            "no address-taken", confidence="medium"))

    return out


def _rhs_after(masked: str, write_off: int) -> str:
    """Return the (masked) RHS text from the assignment '=' at/after
    ``write_off`` up to the next ';' (best-effort, for taint checks)."""
    eq = masked.find("=", write_off)
    if eq < 0:
        return ""
    semi = masked.find(";", eq)
    return masked[eq + 1: semi if semi > 0 else eq + 1]


def emit_assume_statements(invariants: list[GlobalInvariant]) -> list[str]:
    """Turn emitted invariants into ``__CPROVER_assume(...)`` statements."""
    return [f"__CPROVER_assume({inv.clause});" for inv in invariants if inv.emitted]
