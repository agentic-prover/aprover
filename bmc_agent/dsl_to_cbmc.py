"""
DSL → CBMC translation helpers for BMC-Agent Phase 2.

Converts pre/postcondition strings (DSL or natural language) into
C statements using __CPROVER_assume() and assert().
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# DSL atom patterns
# ---------------------------------------------------------------------------

# valid_string(ptr)  → ptr != NULL  (string bounds handled by harness generator)
_VALID_STRING_RE = re.compile(r"\bvalid_string\(\s*([^)]+)\s*\)")

# valid(ptr)  → ptr != NULL
_VALID_RE = re.compile(r"\bvalid\(\s*([^)]+)\s*\)")

# null(ptr)  → ptr == NULL
_NULL_RE = re.compile(r"\bnull\(\s*([^)]+)\s*\)")

# valid_range(ptr, lo, hi)  → ptr != NULL && lo >= 0 && hi >= lo
_VALID_RANGE_RE = re.compile(r"\bvalid_range\(\s*([^,)]+)\s*,\s*([^,)]+)\s*,\s*([^)]+)\s*\)")

# in_bounds(arr, idx)  → idx >= 0 && idx < sizeof(arr)/sizeof(arr[0])
_IN_BOUNDS_RE = re.compile(r"\bin_bounds\(\s*([^,)]+)\s*,\s*([^)]+)\s*\)")

# owns(ptr) or owns(scope, ptr)  → ptr != NULL
# Two-arg form (the LLM emits ``owns(ctx, a)`` on context-allocated APIs
# like ggml's ggml_context). The scope arg has no semantic content for
# the safety property we're asserting, so we drop it and keep only the
# trailing pointer. Group 1 captures the optional scope; group 2
# captures the actual pointer (always present).
_OWNS_RE = re.compile(
    r"\bowns\(\s*(?:([^,()]+?)\s*,\s*)?([^,()]+?)\s*\)"
)

# locked(lock)  → skip (ghost state)
_LOCKED_RE = re.compile(r"\blocked\(\s*([^)]+)\s*\)")

# \result  → result
_RESULT_RE = re.compile(r"\\result\b")

# Simple C-style comparison: expr op expr  (e.g., ptr != NULL, x > 0, x <= 64)
_C_COMPARISON_RE = re.compile(
    # Trailing \b dropped because operands ending in ']' (e.g. ``ptr[0]``)
    # never satisfy a word-boundary against a following space — both sides
    # of the boundary would be non-word characters.  Greedy matching of the
    # character class already prevents partial-identifier matches.
    r"(\b[\w\->.\[\]]+)\s*(!=|==|<=|>=|<|>)\s*([\w\->.\[\]]+)"
)

# Strips C-style cast prefixes like ``(uint8_t)``, ``(const char *)``,
# ``(unsigned long)`` so we can compare comparison spans against full
# atoms modulo casts. The leading negative look-behind prevents matching
# a function call like ``foo(x)``: a cast's paren cannot be preceded by
# an identifier character.
_CAST_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"\(\s*(?:const\s+|volatile\s+|unsigned\s+|signed\s+)*"
    r"[A-Za-z_]\w*"
    r"(?:\s+(?:const|volatile|unsigned|signed))*"
    r"\s*\*?\s*\)\s*"
)


def _match_call(atom: str, name: str) -> Optional[tuple[int, int, list[str]]]:
    """Locate a call ``name(arg1, arg2, ...)`` inside *atom* with full
    paren-balanced argument extraction.

    Returns ``(start, end, args)`` where:
      - ``start`` is the offset of the first character of ``name``
        (preserves ``re.Match.start()`` semantics consulted by callers
        for negation detection / vacuous-self-comparison filtering).
      - ``end`` is one past the closing ``)``.
      - ``args`` is the list of top-level comma-separated arguments,
        each stripped of leading/trailing whitespace.

    Returns ``None`` if the name isn't found or the parens are
    unbalanced.

    Why: the historical ``[^)]+`` regex pattern stops at the FIRST
    ``)``, which for an argument containing a C cast like
    ``valid((struct ncdev*)x)`` is the closing paren of the cast
    token, not of the outer call. The translator then emits the
    malformed C ``((struct ncdev* != NULL`` and breaks the harness
    compile. Same hazard affects nested ``sizeof(struct foo)`` inside
    args. Balanced scanning fixes both.
    """
    if not atom or not name:
        return None
    # Find ``\bname\s*(``.
    pat = re.compile(rf"\b{re.escape(name)}\s*\(")
    m = pat.search(atom)
    if m is None:
        return None
    start = m.start()
    i = m.end()  # one past the '('
    depth = 1
    args_start = i
    args: list[str] = []
    while i < len(atom):
        ch = atom[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                args.append(atom[args_start:i].strip())
                return (start, i + 1, args)
        elif ch == "," and depth == 1:
            args.append(atom[args_start:i].strip())
            args_start = i + 1
        i += 1
    # Unbalanced — name( found but no matching ).
    return None


def _normalize_casts(s: str) -> str:
    """Remove C-style casts from *s* for span-equality comparison."""
    prev = None
    cur = s
    while cur != prev:
        prev = cur
        cur = _CAST_PREFIX_RE.sub("", cur)
    return cur.strip()

# "NULL" literal (ensure we recognise it)
_NULL_LITERAL_RE = re.compile(r"\bNULL\b")


def translate_atom(atom: str, context: str = "assume") -> Optional[str]:
    """
    Translate a single DSL atom to a C statement.

    Parameters
    ----------
    atom:
        A single predicate or comparison expression.
    context:
        ``"assume"`` → wrap in ``__CPROVER_assume(...)``
        ``"assert"`` → wrap in ``assert(...)``

    Returns
    -------
    A C statement string, or None if the atom cannot be translated.
    """
    atom = atom.strip()

    # If this atom is already a C block comment (produced by sanitization),
    # pass it through directly — do NOT wrap it inside assert() or assume().
    if atom.startswith("/*") and atom.endswith("*/"):
        return atom

    def wrap(expr: str) -> str:
        if context == "assert":
            return f"assert({expr});"
        return f"__CPROVER_assume({expr});"

    # Strip outer parens so that the top-level &&/|| split below can see
    # connectives that the caller's grouping parens would otherwise hide.
    # Without this, "(A || B || C)" is treated as a single atom and the
    # NULL_RE / VALID_RE search anywhere inside it matches the first
    # predicate-looking substring (e.g. ``null(result)`` in disjunct 2),
    # silently dropping the other disjuncts and over-constraining the
    # generated assert/assume.
    atom = _strip_outer_parens(atom)

    # Normalise \result → result
    atom = _RESULT_RE.sub("result", atom)

    # Split on top-level && FIRST — before any pattern matching.
    # This ensures that compound conditions like "valid(hub) && sid >= 0 && sid < N"
    # are split into individual atoms and each is translated independently.
    # Without this, valid(hub) would match first and the rest of the condition
    # would be silently discarded.
    #
    # The split is done before the locked()/comment fallbacks so that a
    # compound like ``valid(tp) && !locked(tp->phy_lock)`` retains the
    # translatable ``valid(tp)`` clause instead of being dropped wholesale
    # (rtl8125 OOT batch, 2026-05-18). It is also done before the
    # comment-wrap fallback so we don't nest ``/* */`` produced by
    # ``_sanitize_condition`` inside another ``/* ghost: … */`` wrapper
    # (the nesting caused a CBMC parse error and lost the whole harness).
    parts_and = _top_level_split(atom, "&&")
    if len(parts_and) > 1:
        stmts = []
        for p in parts_and:
            s = translate_atom(p.strip(), context)
            if s:
                stmts.append(s)
        return "\n    ".join(stmts) if stmts else None

    # locked(x)  → skip (ghost state, not checkable in C). Reached only
    # for *single-clause* atoms after the && split above; escape any
    # nested comment markers carried in by upstream sanitization so the
    # outer ``/* … */`` wrapper doesn't break C parsing.
    if _match_call(atom, "locked") is not None:
        return f"/* ghost: {_escape_for_c_comment(atom)} — skipped */"

    # Split on top-level || SECOND — before individual predicate matching.
    # This ensures that "valid(a) || valid(b)" produces a disjunction rather
    # than silently discarding the second clause.
    parts_or = _top_level_split(atom, "||")
    if len(parts_or) > 1:
        exprs = []
        for p in parts_or:
            inner = _atom_to_expr(p.strip())
            if inner:
                exprs.append(inner)
        if exprs:
            return wrap(" || ".join(f"({e})" for e in exprs))

    # valid_string(ptr)  → ptr != NULL (string length bound is set up in harness)
    mm = _match_call(atom, "valid_string")
    if mm is not None and len(mm[2]) >= 1:
        ptr = mm[2][0]
        return wrap(f"{ptr} != NULL")

    # valid_range(ptr, lo, hi):
    #   * assume context — weak shape ``ptr != NULL && lo >= 0 && hi >= lo``.
    #     The harness already gives ``ptr`` an unbounded backing buffer, so the
    #     extra structural sanity is what callers downstream need. A stricter
    #     ``__CPROVER_r_ok`` shape over-constrains the input space and prunes
    #     genuine bugs.
    #   * assert context — emit a real bounds check
    #     ``ptr != NULL && lo >= 0 && hi >= lo && __CPROVER_r_ok(ptr,
    #     hi * sizeof(*ptr))``. Used when asserting a precondition
    #     directly (e.g. FUT POST asserts via postcond_to_assert); the
    #     r_ok term ensures the allocation actually covers the range,
    #     not just that the pointer is non-NULL.
    mm = _match_call(atom, "valid_range")
    if mm is not None and len(mm[2]) == 3:
        ptr, lo, hi = mm[2]
        base = f"{ptr} != NULL && {lo} >= 0 && {hi} >= {lo}"
        if context == "assert":
            base += f" && __CPROVER_r_ok({ptr}, ({hi}) * sizeof(*{ptr}))"
        return wrap(base)

    # valid(ptr)  → ptr != NULL
    mm = _match_call(atom, "valid")
    if mm is not None and len(mm[2]) >= 1:
        ptr = mm[2][0]
        return wrap(f"{ptr} != NULL")

    # owns(ptr) or owns(scope, ptr)  → ptr != NULL
    # Single-arg form: ``args[0]`` is the pointer.
    # Two-arg form: ``args[0]`` is the scope (discarded), ``args[1]`` is
    # the actual pointer.
    mm = _match_call(atom, "owns")
    if mm is not None and len(mm[2]) >= 1:
        ptr = mm[2][-1]
        return wrap(f"{ptr} != NULL")

    # null(ptr)  → ptr == NULL  (but !null(ptr) → ptr != NULL)
    mm = _match_call(atom, "null")
    if mm is not None and len(mm[2]) >= 1:
        ptr = mm[2][0]
        match_start = mm[0]
        # ----- Pathological-LLM-spec filter -----
        # Drop the self-tautology shape ``X != null(X)`` / ``X == null(X)``
        # where the same expression appears on both sides. This is a
        # well-observed LLM artefact (hid_pidff_init produced
        # ``hid->dev != null(hid->dev)``) which has no semantic
        # meaning and, worse, translates to ``X == NULL`` over a
        # struct-by-value member, breaking CBMC's type-check.
        prefix = atom[:match_start].rstrip()
        for op in ("!=", "=="):
            sep_idx = prefix.rfind(op)
            if sep_idx != -1:
                lhs = prefix[:sep_idx].strip()
                if _strip_outer_parens(lhs).strip() == ptr:
                    return f"/* condition (vacuous self-comparison dropped): {atom} */"
        # ----- End filter -----
        negated = False
        if match_start > 0 and atom[match_start - 1] == "!":
            negated = True
        if not negated:
            prev_chunk = atom[:match_start].rstrip()
            if prev_chunk.endswith("!="):
                negated = True
        return wrap(f"{ptr} != NULL" if negated else f"{ptr} == NULL")

    # in_bounds(arr, idx)
    mm = _match_call(atom, "in_bounds")
    if mm is not None and len(mm[2]) == 2:
        arr, idx = mm[2]
        return wrap(f"{idx} >= 0 && {idx} < (int)(sizeof({arr})/sizeof({arr}[0]))")

    # Only wrap in assert/assume if the entire atom looks like valid C.
    # A simple heuristic: no spaces outside of parens (i.e. a single
    # expression, not a natural-language phrase) AND the matched
    # comparison must cover the full atom (modulo whitespace/parens,
    # and modulo C-style casts) — otherwise a prose-mixed clause like
    # "otherwise result >= 0" would be wrapped verbatim into
    # ``assert(otherwise result >= 0);`` which fails to compile.
    # Cast-normalize before matching: C casts on either operand
    # (e.g. ``val == (uint64_t)(uint8_t)ptr[0]``) defeat the bare
    # comparison regex because the RHS starts with ``(``. We strip
    # casts only for the regex/full-atom check and wrap the original
    # atom (which compiles fine in C with casts intact).
    atom_norm = _normalize_casts(atom)
    m = _C_COMPARISON_RE.search(atom_norm)
    if not m and _looks_like_c_expr(atom) and _safe_arith_bool_fallback(atom_norm):
        return wrap(fully_parenthesize(atom))
    if m and _looks_like_c_expr(atom):
        stripped_norm = _normalize_casts(_strip_outer_parens(atom).strip()).strip()
        matched_span = m.group(0).strip()
        if matched_span == stripped_norm:
            return wrap(atom)
        # The single matched comparison does not span the whole atom. Two
        # cases must be told apart:
        #   (a) the atom is a COMPOUND boolean C expression — multiple
        #       comparisons joined by &&/||/!/== such as a biconditional
        #       postcondition ``(result==1) == (a>0 && b>0 && c>0)``. This is
        #       a perfectly assertable predicate and MUST be wrapped whole;
        #       commenting it out silently drops the property, leaving CBMC
        #       with 0 VCCs (vacuous verification) → the sound-verify step
        #       rejects an actually-correct contract → false NOT SATISFIED.
        #   (b) the comparison is embedded in prose (``otherwise X >= 0``,
        #       ``result is the value if Y``) where the qualifying word
        #       changes the meaning and wrapping would over-constrain.
        # _is_pure_bool_c_expr distinguishes them structurally (the caller's
        # _looks_like_c_expr already screened out obvious prose).
        if _is_pure_bool_c_expr(atom_norm):
            return wrap(fully_parenthesize(atom))
        return f"/* condition: {_escape_for_c_comment(atom)} */"

    # Fall through: emit as a comment (natural language / unknown)
    return f"/* condition: {_escape_for_c_comment(atom)} */"


def _escape_for_c_comment(text: str) -> str:
    """Make *text* safe to embed inside a ``/* ... */`` C comment.

    The LLM-generated natural-language spec atoms occasionally contain
    ``/*`` or ``*/`` sequences (typed-pointer commentary like ``/* all
    initialized pointer fields */ point to valid memory``). Embedding
    them verbatim splits the wrapping comment, leaving orphan tokens
    that CBMC then rejects as ``syntax error before '/'``.

    Replace each comment-terminator/opener with a visually similar but
    non-breaking form: ``*/`` becomes ``* /`` and ``/*`` becomes ``/
    *``. The text remains human-readable while the C lexer no longer
    sees a comment boundary.
    """
    return text.replace("*/", "* /").replace("/*", "/ *")


# Characters permitted in a self-contained compound boolean C expression:
# identifiers, whitespace, comparison/logical/arithmetic/bitwise operators,
# the ternary conditional operator (``?:``), parens/brackets and member
# access. Prose is rejected separately (by _looks_like_c_expr) before this
# is consulted; a top-level comma is excluded so we never mistake an English
# list for an expression. ``?:`` lets a value-pinning postcondition such as
# ``result == (cond ? 1 : 0)`` be asserted whole instead of dropped to a
# comment — dropping it leaves CBMC with 0 VCCs (vacuous verification), which
# makes the sound-verify step reject an actually-correct contract.
_PURE_BOOL_C_CHARS_RE = re.compile(r'^[\w\s!=<>&|()\[\].+\-*/%^~?:]+$')


def _is_pure_bool_c_expr(s: str) -> bool:
    """Return True if *s* is a self-contained, assertable boolean C expression.

    Decides whether an atom whose single matched comparison does NOT span the
    whole atom is nonetheless a compound boolean predicate — multiple
    comparisons / logical connectives, e.g. ``(r==1) == (a>0 && b>0)`` — that
    should be asserted whole, versus a comparison embedded in natural-language
    prose, which must be commented out. Callers gate this behind
    _looks_like_c_expr (the prose screen); here we additionally require:
      * every character is a C-expression token char (no prose, no comma),
      * parentheses/brackets are balanced,
      * at least one relational/equality operator is present, so the
        expression is genuinely boolean rather than a bare arithmetic value.
    """
    s = s.strip()
    if not s or not _PURE_BOOL_C_CHARS_RE.match(s):
        return False
    # Prose tell: two identifier tokens separated only by whitespace
    # (``otherwise result``, ``result is``). A well-formed C boolean
    # expression always puts an operator/paren between two identifiers, so
    # this pattern reliably flags a comparison embedded in natural language
    # that survived the lexical char-class screen above.
    if re.search(r'[A-Za-z_]\w*\s+[A-Za-z_]\w*', s):
        return False
    depth = 0
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth < 0:
                return False
    if depth != 0:
        return False
    return re.search(r"(?:!=|==|<=|>=|<|>)", s) is not None


def _safe_arith_bool_fallback(s: str) -> bool:
    """True for pure arithmetic comparisons safe to wrap as C.

    This fallback exists for generated no-overflow bounds whose operands contain
    parenthesized arithmetic. Keep it narrow: function calls and dereferences are
    intentionally left to existing specific rules, because wrapping them can add
    side effects or require backing memory.
    """
    if not _is_pure_bool_c_expr(s):
        return False
    if re.search(r"\b[A-Za-z_]\w*\s*\(", s):
        return False
    if _has_unary_deref(s):
        return False
    return True


def _has_unary_deref(s: str) -> bool:
    for i, ch in enumerate(s):
        if ch != "*":
            continue
        j = i - 1
        while j >= 0 and s[j].isspace():
            j -= 1
        k = i + 1
        while k < len(s) and s[k].isspace():
            k += 1
        if k >= len(s) or not re.match(r"[A-Za-z_(]", s[k]):
            continue
        if j < 0 or s[j] in "(,=<>!&|+-*/?:":
            return True
    return False


def _looks_like_c_expr(atom: str) -> bool:
    """
    Return True if *atom* looks like a pure C expression (no natural language).

    Heuristic: after stripping known C tokens and operators, if there are
    consecutive lowercase words that look like English prose, it's NL.
    We also reject atoms that contain commas outside parentheses (English lists)
    or words longer than ~20 chars without underscores (English words, not identifiers).
    """
    # Strip outer parens for the check
    s = _strip_outer_parens(atom).strip()

    # If it contains English prose indicators, treat as NL
    nl_indicators = re.compile(
        r'\b(the|from|into|with|where|when|after|before|between|contains|increased|decreased|written|removed|bytes|buffer|value|values|call|called|returned|means|that|which|have|has|been|must|should|will|all|each|every|any)\b',
        re.IGNORECASE,
    )
    if nl_indicators.search(s):
        return False

    # Reject if there are words with spaces that don't look like C identifiers/operators
    # A C expression should only contain: identifiers, numbers, operators, parens, brackets, ->. *
    if re.search(r'[a-zA-Z]{2,}\s+[a-zA-Z]{2,}\s+[a-zA-Z]{2,}', s):
        return False  # three consecutive word-like tokens = prose

    return True


def _atom_to_expr(atom: str) -> Optional[str]:
    """Return a bare C expression (no statement wrapper) for an atom, or None.

    Used by the disjunction path (top-level ``||`` split) where each
    disjunct must be a single bare expression. If the atom is itself a
    conjunction (``cmp && NL_prose``), the bare ``_C_COMPARISON_RE``
    match would historically return the whole atom verbatim, leaking
    natural-language text into ``assert(...)``. Strip the atom first
    by splitting on top-level ``&&`` and keeping only sub-clauses that
    translate cleanly. If nothing translates, return None.
    """
    atom = _RESULT_RE.sub("result", atom).strip()

    # If the atom is itself a top-level conjunction, recursively
    # translate each clause and AND-join the clean ones. A clause that
    # does not translate is dropped (this is a soundness choice: we
    # prefer to under-constrain than to embed prose into C).
    inner_parts = _top_level_split(_strip_outer_parens(atom), "&&")
    if len(inner_parts) > 1:
        sub_exprs: list[str] = []
        for p in inner_parts:
            e = _atom_to_expr(p.strip())
            if e:
                sub_exprs.append(e)
        if not sub_exprs:
            return None
        return " && ".join(f"({e})" for e in sub_exprs) if len(sub_exprs) > 1 else sub_exprs[0]

    mm = _match_call(atom, "valid_string")
    if mm is not None and len(mm[2]) >= 1:
        return f"{mm[2][0]} != NULL"

    mm = _match_call(atom, "valid_range")
    if mm is not None and len(mm[2]) == 3:
        ptr, lo, hi = mm[2]
        return f"{ptr} != NULL && {lo} >= 0 && {hi} >= {lo}"

    mm = _match_call(atom, "valid")
    if mm is not None and len(mm[2]) >= 1:
        return f"{mm[2][0]} != NULL"

    mm = _match_call(atom, "owns")
    if mm is not None and len(mm[2]) >= 1:
        # Single-arg → args[0]; two-arg ``owns(scope, ptr)`` → args[1].
        return f"{mm[2][-1]} != NULL"

    mm = _match_call(atom, "null")
    if mm is not None and len(mm[2]) >= 1:
        match_start = mm[0]
        ptr = mm[2][0]
        negated = match_start > 0 and atom[match_start - 1] == "!"
        return f"{ptr} != NULL" if negated else f"{ptr} == NULL"

    mm = _match_call(atom, "in_bounds")
    if mm is not None and len(mm[2]) == 2:
        arr, idx = mm[2]
        return f"{idx} >= 0 && {idx} < (int)(sizeof({arr})/sizeof({arr}[0]))"

    # Bare-comparison fallback: only accept if the full atom (modulo
    # whitespace/outer parens/casts) is a single C comparison. A clause
    # like ``result == 0`` matches; a clause like ``otherwise result >=
    # 0`` or ``result == 0 and the device is reset`` does not, because
    # the prose tail makes the atom non-C.
    atom_norm = _normalize_casts(atom)
    m = _C_COMPARISON_RE.search(atom_norm)
    if not m and _looks_like_c_expr(atom) and _safe_arith_bool_fallback(atom_norm):
        return fully_parenthesize(_strip_outer_parens(atom).strip())
    if m and _looks_like_c_expr(atom):
        stripped_norm = _normalize_casts(_strip_outer_parens(atom).strip()).strip()
        matched_span = m.group(0).strip()
        if matched_span == stripped_norm:
            return _strip_outer_parens(atom).strip()
        # Compound boolean operand (e.g. a biconditional ``(r==1)==cond``) that
        # the single comparison doesn't span — keep it whole and pinned rather
        # than dropping it, mirroring translate_atom. Without this the
        # disjunction path silently loses such an operand.
        if _is_pure_bool_c_expr(atom_norm):
            return fully_parenthesize(_strip_outer_parens(atom).strip())

    return None


def _top_level_split(text: str, op: str) -> list[str]:
    """Split *text* at top-level occurrences of *op* (not inside parentheses or /* */ comments)."""
    parts: list[str] = []
    depth = 0
    in_comment = False
    current: list[str] = []
    i = 0
    op_len = len(op)
    while i < len(text):
        # Track /* */ comment regions — operators inside comments must not split.
        if not in_comment and text[i:i + 2] == "/*":
            in_comment = True
            current.append(text[i])
            i += 1
            continue
        if in_comment:
            if text[i:i + 2] == "*/":
                in_comment = False
                current.append(text[i])
                current.append(text[i + 1])
                i += 2
                continue
            current.append(text[i])
            i += 1
            continue
        if text[i] == "(":
            depth += 1
            current.append(text[i])
        elif text[i] == ")":
            depth -= 1
            current.append(text[i])
        elif depth == 0 and text[i:i + op_len] == op:
            parts.append("".join(current).strip())
            current = []
            i += op_len
            continue
        else:
            current.append(text[i])
        i += 1
    parts.append("".join(current).strip())
    return parts if len(parts) > 1 else [text]


# Boolean connectives in ACSL/C precedence order, LOWEST-binding first. Used by
# fully_parenthesize to split outermost first so the resulting grouping matches
# standard precedence. ``<==>`` MUST precede ``==>`` (the latter is a substring
# of the former) and both precede ``||``/``&&``. ``==>``/``<==>`` are ACSL-only;
# in a C expression they simply never split (returns one part).
_BOOL_PRECEDENCE = ("<==>", "==>", "||", "&&")

# A primary operand that never needs wrapping under a top-level boolean
# connective: a single identifier / member access / array index / ACSL builtin
# (``\result``), or an integer literal. Anything with an embedded operator is
# wrapped so the &&/|| structure is explicit.
_PRIMARY_OPERAND_RE = re.compile(
    r"^(?:"
    r"\\?[A-Za-z_]\w*"
    r"(?:\s*(?:\.|->)\s*[A-Za-z_]\w*|\s*\[[^\[\]]*\])*"
    r"|\d+"
    r")$"
)


def _wrap_operand(expr: str) -> str:
    """Parenthesise *expr* unless it is a primary operand, a pure comment
    fragment, or already a single fully-parenthesised group."""
    e = expr.strip()
    if not e:
        return e
    if e.startswith("/*") and e.endswith("*/"):
        return e  # already-dropped clause — leave untouched
    if _PRIMARY_OPERAND_RE.match(e):
        return e
    if e.startswith("(") and _strip_outer_parens(e) != e:
        return e  # already a single parenthesised group
    return f"({e})"


def fully_parenthesize(expr: str) -> str:
    """Make the boolean (``<==>``/``==>``/``||``/``&&``) structure of *expr*
    explicit so its meaning no longer DEPENDS on operator precedence.

    Splits at the lowest-binding top-level connective first and parenthesises
    every compound operand, recursively. The result is semantically identical
    under standard C/ACSL precedence — nothing is reordered — but the grouping
    is now pinned by parentheses. This matters because the SAME synthesized
    string is parsed by two engines (CBMC's C front-end and Frama-C's ACSL),
    and a bare ``A == B && C || D`` invites both reader error and a CBMC/WP
    disagreement. Equality/relational/arithmetic sub-structure is left intact
    inside each operand: its precedence is identical across both engines, so
    wrapping the whole operand already pins it unambiguously. Idempotent.
    """
    if not expr:
        return expr
    s = expr.strip()
    for op in _BOOL_PRECEDENCE:
        parts = _top_level_split(s, op)
        if len(parts) > 1:
            return f" {op} ".join(
                _wrap_operand(fully_parenthesize(p)) for p in parts
            )
    # No top-level boolean connective: recurse INTO a single outer-paren group
    # so any connective nested one level down is pinned too.
    inner = _strip_outer_parens(s)
    if inner != s:
        return "(" + fully_parenthesize(inner) + ")"
    return s


# ---------------------------------------------------------------------------
# High-level converters
# ---------------------------------------------------------------------------


def precond_to_assume(precondition: str, params: list[str]) -> list[str]:
    """
    Convert a precondition string to a list of ``__CPROVER_assume()`` C statements.

    Parameters
    ----------
    precondition:
        The precondition string (DSL or natural language).
    params:
        Parameter names of the function. Used to filter out atoms that
        reference identifiers outside the harness's lexical scope (e.g.
        LLM-emitted clauses mentioning function-body locals like
        ``arg``, ``buffer``, or loop-quantifier variables like ``i``).
        Atoms with unbound identifiers are dropped to comments so the
        harness compiles.

    Returns
    -------
    A list of C statement strings.
    """
    return _filter_tautological(
        _condition_to_stmts(precondition, context="assume", params=params)
    )


def postcond_to_assert(
    postcondition: str,
    params: list[str],
    return_var: str = "result",
) -> list[str]:
    """
    Convert a postcondition string to a list of ``assert()`` C statements.

    Parameters
    ----------
    postcondition:
        The postcondition string (DSL or natural language).
    params:
        Parameter names of the function.
    return_var:
        Name of the local variable holding the return value (default ``"result"``).

    Returns
    -------
    A list of C statement strings.
    """
    # Replace \result with the actual return variable name
    post = re.sub(r"\\result\b", return_var, postcondition)
    return _filter_tautological(
        _condition_to_stmts(post, context="assert", params=params)
    )


# ---------------------------------------------------------------------------
# Tautological-assertion detector
# ---------------------------------------------------------------------------

# Comparison operators we treat as potential tautology sites.  Always-true
# (==, >=, <=) and always-false (!=, <, >) self-comparisons are both
# refused: the always-true ones waste solver effort, the always-false
# ones HIDE real bugs because the assert will always fire on every
# execution, drowning out the actual property the spec was supposed to
# check.
_TAUTOLOGY_OPERATORS: tuple[str, ...] = (">=", "<=", "==", "!=", ">", "<")


def _is_self_comparison(expr: str) -> bool:
    """Return True when ``expr`` is of the form ``X OP X`` where both
    sides are syntactically identical after stripping outer parens and
    collapsing whitespace.

    Catches tautologies produced when the spec references ``\\old(X)``
    and the DSL sanitiser strips ``\\old()`` leaving ``X OP X``.  The
    canonical example, observed on ttf.c stbtt__cff_skip_operand:

        postcondition: b->cursor > \\old(b->cursor)
            ↓ \\old() stripped
        b->cursor > b->cursor
            ↓ wrap()
        assert(b->cursor > b->cursor);     ← always false, bug-hiding

    Returns False on anything we can't unambiguously parse.
    """
    if not expr:
        return False
    s = expr.strip()
    # Strip up to a few layers of outer parens.
    for _ in range(4):
        if s.startswith("(") and s.endswith(")"):
            s = s[1:-1].strip()
        else:
            break
    # Try each operator in length-descending order so ``>=`` matches
    # before ``>`` would consume the ``=``.
    for op in sorted(_TAUTOLOGY_OPERATORS, key=len, reverse=True):
        # Find the operator at the TOP level (not nested in parens / brackets).
        depth = 0
        i = 0
        while i < len(s):
            ch = s[i]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif depth == 0 and s[i:i + len(op)] == op:
                # Boundary-check so ``a>=b`` doesn't match for op ``>``.
                if op in ("<", ">") and i + 1 < len(s) and s[i + 1] == "=":
                    i += 1
                    continue
                if op in ("<", ">", "=") and i > 0 and s[i - 1] in "<>=!":
                    i += 1
                    continue
                # Skip ``->`` arrow operator (e.g. b->cursor): when op is
                # ``>`` and the preceding char is ``-``, this is the
                # struct-pointer accessor, not a comparison.
                if op == ">" and i > 0 and s[i - 1] == "-":
                    i += 1
                    continue
                # Skip ``<-`` if it ever appears (rare; not standard C
                # but defensive).
                if op == "<" and i + 1 < len(s) and s[i + 1] == "-":
                    i += 1
                    continue
                lhs = s[:i].strip()
                rhs = s[i + len(op):].strip()
                if not lhs or not rhs:
                    return False
                # Normalise: strip outer parens, collapse whitespace.
                def _norm(t: str) -> str:
                    while t.startswith("(") and t.endswith(")"):
                        t = t[1:-1].strip()
                    return re.sub(r"\s+", "", t)
                return _norm(lhs) == _norm(rhs)
            i += 1
    return False


def _strip_wrapper(stmt: str) -> str | None:
    """Return the inner expression of ``assert(EXPR);`` or
    ``__CPROVER_assume(EXPR);``, or None if the statement isn't an
    assertion/assume call. Used by the tautology filter to inspect the
    expression CBMC will see."""
    s = stmt.strip().rstrip(";").strip()
    for prefix in ("assert(", "__CPROVER_assume("):
        if s.startswith(prefix) and s.endswith(")"):
            return s[len(prefix):-1]
    return None


def _filter_tautological(stmts: list[str]) -> list[str]:
    """Replace any ``assert(EXPR);`` / ``__CPROVER_assume(EXPR);`` where
    EXPR is a self-comparison (``X OP X``) with an inline comment.  Logs
    a warning so the user sees the suppression.

    Rationale: a tautological assert that's always false (``b->cursor >
    b->cursor``) silently hides every other property of the function
    because CBMC reports it on every execution.  A tautological assert
    that's always true (``size >= size``) wastes solver effort.  Both
    cases originate from the same root: the spec referenced ``\\old()``
    or another temporal construct the DSL translator doesn't model, and
    the sanitiser stripped one side leaving an identical comparison.

    Multi-line input handling: ``translate_atom`` joins multiple
    ``&&``-split asserts with ``\\n    `` into a single string, so each
    list element can contain multiple semicolon-terminated statements
    on separate lines.  We split, filter, and re-join per element so
    the structure of the caller's output is preserved.
    """
    import logging as _logging
    out: list[str] = []
    for stmt in stmts:
        # Split on lines so multi-statement entries get filtered per-line.
        lines = stmt.split("\n")
        new_lines: list[str] = []
        for line in lines:
            inner = _strip_wrapper(line.strip())
            if inner is not None and _is_self_comparison(inner):
                _logging.getLogger("bmc_agent").warning(
                    "Refused tautological assertion (likely a stripped "
                    "\\old() or temporal construct): %s", line.strip(),
                )
                # Preserve leading whitespace for the comment.
                leading_ws = line[:len(line) - len(line.lstrip())]
                new_lines.append(
                    f"{leading_ws}/* tautological — refused: "
                    f"{line.strip()} (see harness gen log for why) */"
                )
            else:
                new_lines.append(line)
        out.append("\n".join(new_lines))
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize_condition(condition: str) -> str:
    """
    Remove or comment out constructs that are valid in JML/Eiffel/DSL but
    not in C, so that CBMC can compile the harness.

    Specifically:
    - ``\\result`` / CR+esult  → replaced with ``result``
    - ``\\old(expr)``          → replaced with ``expr``
    - ``==>`` / ``<==>``       → logical implication, not valid C; clause dropped
    - ``forall`` / ``exists``  → quantifiers; clause dropped
    - Invented struct fields like ``foo->count_before`` → commented out
    """
    # Fix \result mangled by JSON parsing: Python reads \r as CR (0x0D),
    # so \result → CR + "esult". Normalise all forms to plain "result".
    # Also defensively map bare "esult" (LLM typo, observed in real spec
    # output) to "result" — esult is not a known C symbol anywhere in
    # bmc-agent's generated harnesses, so this is safe.
    condition = condition.replace('\result', 'result')   # CR+esult → result
    condition = condition.replace('\\result', 'result')  # literal \result → result
    # Word-boundary match so we don't clobber e.g. "best_result".
    condition = re.sub(r'\besult\b', 'result', condition)

    # Lowercase `null` used as a constant (e.g. `result == null`) → NULL
    # Only replace when NOT followed by '(' to avoid clobbering null(ptr) predicates.
    condition = re.sub(r'\bnull\b(?!\s*\()', 'NULL', condition)

    # \old(expr) → expr  (best-effort; avoids parse error)
    condition = re.sub(r"\\old\s*\(([^)]*)\)", r"\1", condition)
    # Also handle old(expr) without backslash
    condition = re.sub(r"\bold\s*\(([^)]*)\)", r"\1", condition)

    # Remove or comment out clauses containing non-C constructs.
    # Process clause-by-clause (split on top-level &&) so that partial-match
    # issues (e.g. forall stopping at a comma inside the clause) are avoided.
    def _sanitize_clauses(text: str) -> str:
        parts = _top_level_split(text, "&&")
        safe = []
        def _comment_out(clause: str) -> str:
            """Wrap clause in /* */ after neutralising internal semicolons
            and any nested comment markers.

            Semicolons inside a block comment would cause the later
            re.split(r';') in _condition_to_stmts to break the comment apart,
            leaking non-C constructs (forall, ==>, …) into generated C code.

            Nested ``/*`` / ``*/`` would prematurely terminate the wrapper
            when downstream code re-wraps the joined text (rtl8125 OOT
            batch, 2026-05-18: an "invented-field" hit on the real kernel
            field ``mmio_addr`` produced ``/* valid(tp->mmio_addr) */``
            which the later ``/* ghost: … */`` wrap unable to nest).
            """
            return f"/* {_escape_for_c_comment(clause).replace(';', ' ')} */"

        for p in parts:
            ps = p.strip()
            # 1. Implication operators (==>, <==>) — not valid C
            if "==>" in ps or "<==>" in ps:
                safe.append(_comment_out(ps))
            # 2. forall / exists quantifiers — comment out entire clause
            elif re.search(r'\b(forall|exists)\b', ps, re.IGNORECASE):
                safe.append(_comment_out(ps))
            # 3. Invented struct fields — LLM commonly hallucinates fields that
            #    don't exist in the actual struct.  Patterns:
            #    a) Historical/snapshot: ->field_before, ->field_after, ->field_old …
            #    b) "Latest" / "index" variants: ->latest_x, ->prev_x, ->next_x
            #    c) Pure compound inventions: ->latest_index, ->read_index …
            elif re.search(
                # Note: ``addr`` was previously in the suffix list but
                # matches real kernel fields (``mmio_addr``, ``phy_addr``,
                # ``mac_addr``) and is dropped here to avoid silently
                # commenting out legitimate clauses (rtl8125 OOT batch).
                r'->\w*_(?:before|after|old|prev|initial|orig|index|idx|ref|new|copy|tmp|snapshot|cache|prev|begin|first|last)\b'
                r'|->(?:latest|current|previous|next|prev|first|last|read|write)_\w+\b'
                # Also catch dot-access invented fields on nested structs
                r'|\.\w*_(?:before|after|old|prev|initial|orig|index|idx|ref|new|copy|tmp|snapshot|cache|begin|first|last)\b'
                r'|\.(?:latest|current|previous|next|prev|first|last|read|write)_\w+\b',
                ps,
            ):
                safe.append(_comment_out(ps))
            # 4. result == void / void == result — LLM wrote \result for a void fn
            elif re.search(r'\bresult\s*==\s*void\b|\bvoid\s*==\s*result\b', ps):
                safe.append(f"/* {ps} */")
            # 5. Clause referencing undeclared 'result' in function-call context
            #    (e.g. "result was called with …")
            elif re.search(r'\bresult\b.*\bwas\b|\bwas\b.*\bresult\b', ps, re.IGNORECASE):
                safe.append(f"/* {ps} */")
            # 6. Already a comment — pass through
            elif ps.startswith("/*") and ps.endswith("*/"):
                safe.append(ps)
            else:
                safe.append(ps)
        # Return "1" (no-op assume) if everything was dropped. A commented-out
        # clause must NOT sit bare in the && chain (``A && /* c */ && B`` becomes
        # ``A &&  && B`` — a comment is whitespace, so the && has no operand and
        # CBMC reports "syntax error before ')'"). Give each commented clause a
        # ``1`` operand so it's a valid no-op that still shows what was dropped.
        real = [p for p in safe if not (p.startswith("/*") and p.endswith("*/"))]
        joined = " && ".join(
            (f"1 {p}" if (p.startswith("/*") and p.endswith("*/")) else p)
            for p in safe
        )
        return joined if real else "1"

    condition = _sanitize_clauses(condition)

    # Multi-line strings: collapse continuation backslashes
    condition = condition.replace("\\\n", " ")

    # Strip carriage returns that may remain
    condition = condition.replace('\r', '')

    # Final cleanup: a clause commented out inside parens by the structural
    # builder leaves ``(/* ... */)`` — an empty parenthesised expression that
    # CBMC rejects ("syntax error before ')'"). Give it a ``1`` operand so the
    # parens hold a valid no-op while preserving the dropped-clause comment.
    condition = re.sub(r"\(\s*(/\*.*?\*/)\s*\)", r"(1 \1)", condition)

    return condition.strip()


# Identifiers that are always considered "in scope" regardless of the
# function's parameter list — C keywords, common stdlib/kernel types
# and macros, CBMC intrinsics, and harness-emitted locals.
_ALWAYS_BOUND_IDENTIFIERS = frozenset({
    # Stdlib / language
    "NULL", "true", "false", "sizeof", "typeof",
    "struct", "union", "enum", "typedef",
    "int", "long", "short", "char", "unsigned", "signed", "void",
    "const", "volatile", "static", "extern", "inline", "register", "restrict",
    "if", "else", "for", "while", "do", "return", "goto", "switch", "case", "default", "break", "continue",
    # CBMC intrinsics + C assert() wrapper that translate_atom emits.
    "__CPROVER_assume", "__CPROVER_assert", "__CPROVER_r_ok",
    "__CPROVER_w_ok", "__CPROVER_rw_ok",
    "assert", "abort", "exit",
    # POSIX/stdint typedefs
    "size_t", "ssize_t", "ptrdiff_t", "uintptr_t", "intptr_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "off_t", "loff_t", "time_t",
    # <limits.h> integer bounds used by generated no-overflow preconditions.
    "CHAR_MIN", "CHAR_MAX", "SCHAR_MIN", "SCHAR_MAX",
    "SHRT_MIN", "SHRT_MAX", "INT_MIN", "INT_MAX",
    "LONG_MIN", "LONG_MAX", "LLONG_MIN", "LLONG_MAX",
    # Kernel primitive typedefs
    "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64",
    "__u8", "__u16", "__u32", "__u64", "__s8", "__s16", "__s32", "__s64",
    "bool", "pid_t", "dev_t", "gfp_t", "umode_t",
    # Kernel constants we define in the harness preamble
    "PAGE_SIZE", "PAGE_SHIFT",
    "EFAULT", "EINVAL", "ENOMEM", "EAGAIN", "EIO", "ENODEV",
    "EPERM", "EBUSY", "ENOTSUPP", "ETIMEDOUT", "EOPNOTSUPP",
    # Harness-emitted locals
    "result",
})

# Match a bare identifier — must NOT be preceded by ``.`` or ``->`` (those
# are field accesses where the trailing name is a struct field, not a
# top-level binding).
_BARE_IDENT_RE = re.compile(r"(?<![\w.])(?<!->)([A-Za-z_]\w*)")

# Type tags inside C casts and struct declarators ``(struct NAME *)x``
# / ``(union NAME)x`` / ``(enum NAME)x``. The trailing NAME is a type,
# not a binding — strip BOTH the keyword AND the type name from the
# scan buffer so the unbound-identifier filter doesn't see the type
# name as a binding.
_TYPE_TAG_PREFIX_RE = re.compile(r"\b(?:struct|union|enum)\s+[A-Za-z_]\w*")


def _atom_has_unbound_ident(stmt: str, bound: set[str]) -> Optional[str]:
    """If *stmt* references a bare identifier outside *bound*, return
    that identifier. Otherwise return None.

    "Bare" means not preceded by ``.`` or ``->`` — i.e., a top-level
    name binding rather than a struct field access. Field accesses
    are skipped because ``p->field`` is always valid when ``p`` is in
    scope (the field name comes from the struct's type definition).

    Used by ``_condition_to_stmts`` to drop atoms that the LLM emitted
    against function-body locals (``arg``, ``param``, ``buffer``) or
    forall-quantifier bound variables (``i``) — these aren't in the
    harness's scope and would cause CONVERSION ERROR if asserted.
    """
    # Skip ``/* … */`` comments; they're already-dropped clauses.
    stripped = stmt.strip()
    if stripped.startswith("/*") and stripped.endswith("*/"):
        return None
    # Strip embedded ``/* … */`` comments from the scan buffer — they
    # carry the LLM's natural-language commentary that the upstream
    # sanitiser commented out, and would otherwise trip the identifier
    # filter on every English word inside the comment. The COMMENT
    # itself was a deliberate "give up on translating this fragment"
    # signal; we should not let it cause the surrounding live C to
    # also be dropped.
    scan = re.sub(r"/\*.*?\*/", "", stmt, flags=re.DOTALL)
    # Strip ``struct NAME`` / ``union NAME`` / ``enum NAME`` so type
    # tags inside C casts (``(struct ncdev*)p``) don't get flagged as
    # bindings. Without this, every cast-shaped clause would fail the
    # filter on the type tag.
    scan = _TYPE_TAG_PREFIX_RE.sub("", scan)
    for m in _BARE_IDENT_RE.finditer(scan):
        ident = m.group(1)
        if ident in bound or ident in _ALWAYS_BOUND_IDENTIFIERS:
            continue
        # Underscore-prefixed locals emitted by the harness generator
        # (``_caller_result``, ``_buf_buf``, etc.).
        if ident.startswith("_"):
            continue
        return ident
    return None


def _condition_to_stmts(
    condition: str, context: str, params: Optional[list[str]] = None
) -> list[str]:
    """
    Split a condition string and translate each clause.

    Strategy:
    1.  Strip leading DSL keywords.
    2.  Sanitize away non-C constructs (\\old, ==>, forall/exists).
    3.  Split on sentence boundaries (AND, newlines, semicolons).
    4.  For each clause, call ``translate_atom``.
    5.  Collect resulting statements.
    6.  Drop translated atoms that reference identifiers outside the
        harness's lexical scope (the function's parameters plus the
        always-bound common set).
    """
    if not condition or condition.strip().lower() in ("true", "1"):
        return ["/* precondition: true — no assumptions needed */"]

    # Strip leading "requires" / "ensures" keywords from DSL
    condition = re.sub(
        r"^(requires?|ensures?|precondition:|postcondition:)\s*",
        "",
        condition.strip(),
        flags=re.IGNORECASE,
    )

    # Sanitize non-C constructs before splitting
    condition = _sanitize_condition(condition)

    # Split on explicit " AND " (case-sensitive: emitted by the spec
    # merger in spec.py with uppercase), newlines, semicolons. Lowercase
    # "and" inside natural-language atoms ("the reset write failed and
    # the PHY state is unchanged") must NOT split, otherwise the
    # surrounding C expression's parens get torn apart and downstream
    # translation drops half the postcondition silently.
    # Split on natural-language separators (AND, newlines, semicolons)
    # AND on the C-like top-level ``&&`` connector so each conjunct can
    # be filtered independently. Without the ``&&`` split, an unbound
    # identifier in one conjunct would drop the entire PRE (observed:
    # ``valid(devnode) && ... && neuron_dev_class != NULL`` lost ALL
    # validity clauses to a single unbound-ident hit on
    # ``neuron_dev_class``).
    coarse_parts = re.split(r"\s+AND\s+|\n|;", condition)
    parts: list[str] = []
    for cp in coarse_parts:
        cp = cp.strip()
        if not cp:
            continue
        # Pin the &&/|| grouping of this clause BEFORE the && split so the
        # emitted C matches standard precedence (and the ACSL render) — e.g.
        # ``(r==1)==c && r==0 || r==1`` is grouped ``((r==1)==c && r==0) ||
        # r==1`` rather than being mis-split into independent && conjuncts.
        cp = fully_parenthesize(cp)
        # Split each coarse part on top-level ``&&``.
        for sub in _top_level_split(cp, "&&"):
            sub = sub.strip()
            if sub:
                parts.append(sub)

    bound = set(params or [])
    stmts: list[str] = []
    for part in parts:
        # Strip outer parentheses
        part = _strip_outer_parens(part)
        if not part:
            continue
        stmt = translate_atom(part, context=context)
        if not stmt:
            continue
        # Drop atoms referencing identifiers outside the harness's scope.
        # Common cases: LLM-emitted clauses that mention function-body
        # locals (``arg``, ``buffer``), loop-quantifier bound variables
        # (``i``), or kernel constants we don't have a preamble define
        # for. Asserting/assuming these would fail compilation with a
        # "failed to find symbol" CONVERSION ERROR.
        unbound = _atom_has_unbound_ident(stmt, bound)
        if unbound is not None:
            stmts.append(
                f"/* dropped (references unbound identifier '{unbound}'): "
                f"{_escape_for_c_comment(part)[:120]} */"
            )
            continue
        stmts.append(stmt)
    return stmts if stmts else [f"/* condition: {condition} */"]


def _strip_outer_parens(s: str) -> str:
    """Strip a single layer of matching outer parentheses if present."""
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        inner = s[1:-1]
        # Make sure the parens really matched at the outermost level
        depth = 0
        for ch in inner:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth < 0:
                return s  # unmatched — leave as-is
        return inner.strip()
    return s
