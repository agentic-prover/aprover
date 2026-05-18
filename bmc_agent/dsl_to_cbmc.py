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

# owns(ptr)  → ptr != NULL (treat like valid)
_OWNS_RE = re.compile(r"\bowns\(\s*([^)]+)\s*\)")

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
    if _LOCKED_RE.search(atom):
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
    m = _VALID_STRING_RE.search(atom)
    if m:
        ptr = m.group(1).strip()
        return wrap(f"{ptr} != NULL")

    # valid_range(ptr, lo, hi)  → ptr != NULL && lo >= 0 && hi >= lo
    m = _VALID_RANGE_RE.search(atom)
    if m:
        ptr = m.group(1).strip()
        lo = m.group(2).strip()
        hi = m.group(3).strip()
        return wrap(f"{ptr} != NULL && {lo} >= 0 && {hi} >= {lo}")

    # valid(ptr) / owns(ptr)  → ptr != NULL
    m = _VALID_RE.search(atom) or _OWNS_RE.search(atom)
    if m:
        ptr = m.group(1).strip()
        return wrap(f"{ptr} != NULL")

    # null(ptr)  → ptr == NULL  (but !null(ptr) → ptr != NULL)
    m = _NULL_RE.search(atom)
    if m:
        ptr = m.group(1).strip()
        # ----- Pathological-LLM-spec filter -----
        # Drop the self-tautology shape ``X != null(X)`` / ``X == null(X)``
        # where the same expression appears on both sides. This is a
        # well-observed LLM artefact (hid_pidff_init produced
        # ``hid->dev != null(hid->dev)``) which has no semantic
        # meaning and, worse, translates to ``X == NULL`` over a
        # struct-by-value member, breaking CBMC's type-check.
        # Look at the atom's text BEFORE the ``null(...)`` match: if
        # it contains the same expression as ``ptr`` joined to it
        # via ``==`` or ``!=``, the predicate is vacuous.
        prefix = atom[: m.start()].rstrip()
        for op in ("!=", "=="):
            sep_idx = prefix.rfind(op)
            if sep_idx != -1:
                lhs = prefix[:sep_idx].strip()
                # Normalise (strip outer parens) and compare.
                if _strip_outer_parens(lhs).strip() == ptr:
                    return f"/* condition (vacuous self-comparison dropped): {atom} */"
        # ----- End filter -----
        # Detect negation. The historic check inspected only the
        # immediate previous char for ``!``; broaden it to recognise
        # the ``X != null(...)`` shape (where ``!=`` precedes the
        # match, possibly with whitespace).
        negated = False
        if m.start() > 0:
            prev = atom[m.start() - 1]
            if prev == "!":
                negated = True
        if not negated:
            prev_chunk = atom[: m.start()].rstrip()
            if prev_chunk.endswith("!="):
                negated = True
        return wrap(f"{ptr} != NULL" if negated else f"{ptr} == NULL")

    # in_bounds(arr, idx)
    m = _IN_BOUNDS_RE.search(atom)
    if m:
        arr = m.group(1).strip()
        idx = m.group(2).strip()
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
    if m and _looks_like_c_expr(atom):
        stripped_norm = _normalize_casts(_strip_outer_parens(atom).strip()).strip()
        matched_span = m.group(0).strip()
        if matched_span == stripped_norm:
            return wrap(atom)
        # The comparison is embedded in prose (e.g. "otherwise X >= 0",
        # "result is the value if Y"). The qualifying word changes the
        # condition's meaning, so wrapping just the matched comparison
        # would over-constrain. Comment it out instead.
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

    m = _VALID_STRING_RE.search(atom)
    if m:
        return f"{m.group(1).strip()} != NULL"

    m = _VALID_RANGE_RE.search(atom)
    if m:
        ptr = m.group(1).strip()
        lo = m.group(2).strip()
        hi = m.group(3).strip()
        return f"{ptr} != NULL && {lo} >= 0 && {hi} >= {lo}"

    m = _VALID_RE.search(atom) or _OWNS_RE.search(atom)
    if m:
        return f"{m.group(1).strip()} != NULL"

    m = _NULL_RE.search(atom)
    if m:
        negated = m.start() > 0 and atom[m.start() - 1] == "!"
        return f"{m.group(1).strip()} != NULL" if negated else f"{m.group(1).strip()} == NULL"

    m = _IN_BOUNDS_RE.search(atom)
    if m:
        arr, idx = m.group(1).strip(), m.group(2).strip()
        return f"{idx} >= 0 && {idx} < (int)(sizeof({arr})/sizeof({arr}[0]))"

    # Bare-comparison fallback: only accept if the full atom (modulo
    # whitespace/outer parens/casts) is a single C comparison. A clause
    # like ``result == 0`` matches; a clause like ``otherwise result >=
    # 0`` or ``result == 0 and the device is reset`` does not, because
    # the prose tail makes the atom non-C.
    atom_norm = _normalize_casts(atom)
    m = _C_COMPARISON_RE.search(atom_norm)
    if m and _looks_like_c_expr(atom):
        stripped_norm = _normalize_casts(_strip_outer_parens(atom).strip()).strip()
        matched_span = m.group(0).strip()
        if matched_span == stripped_norm:
            return _strip_outer_parens(atom).strip()

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
        Parameter names of the function (used for context; not strictly needed
        for the translation but kept for future use).

    Returns
    -------
    A list of C statement strings.
    """
    return _filter_tautological(_condition_to_stmts(precondition, context="assume"))


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
    return _filter_tautological(_condition_to_stmts(post, context="assert"))


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
        # Return "1" (no-op assume) if everything was dropped
        real = [p for p in safe if not (p.startswith("/*") and p.endswith("*/"))]
        return " && ".join(safe) if real else "1"

    condition = _sanitize_clauses(condition)

    # Multi-line strings: collapse continuation backslashes
    condition = condition.replace("\\\n", " ")

    # Strip carriage returns that may remain
    condition = condition.replace('\r', '')

    return condition.strip()


def _condition_to_stmts(condition: str, context: str) -> list[str]:
    """
    Split a condition string and translate each clause.

    Strategy:
    1.  Strip leading DSL keywords.
    2.  Sanitize away non-C constructs (\\old, ==>, forall/exists).
    3.  Split on sentence boundaries (AND, newlines, semicolons).
    4.  For each clause, call ``translate_atom``.
    5.  Collect resulting statements.
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
    parts = re.split(r"\s+AND\s+|\n|;", condition)
    stmts: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Strip outer parentheses
        part = _strip_outer_parens(part)
        if not part:
            continue
        stmt = translate_atom(part, context=context)
        if stmt:
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
