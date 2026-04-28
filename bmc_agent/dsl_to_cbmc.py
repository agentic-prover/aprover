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

# valid(ptr)  → ptr != NULL
_VALID_RE = re.compile(r"\bvalid\(\s*([^)]+)\s*\)")

# null(ptr)  → ptr == NULL
_NULL_RE = re.compile(r"\bnull\(\s*([^)]+)\s*\)")

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
    r"(\b[\w\->.\[\]]+\b)\s*(!=|==|<=|>=|<|>)\s*(\b[\w\->.\[\]]+\b)"
)

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

    # Normalise \result → result
    atom = _RESULT_RE.sub("result", atom)

    # locked(x)  → skip (ghost state, not checkable in C)
    if _LOCKED_RE.search(atom):
        return f"/* ghost: {atom} — skipped */"

    # Split on top-level && FIRST — before any pattern matching.
    # This ensures that compound conditions like "valid(hub) && sid >= 0 && sid < N"
    # are split into individual atoms and each is translated independently.
    # Without this, valid(hub) would match first and the rest of the condition
    # would be silently discarded.
    parts_and = _top_level_split(atom, "&&")
    if len(parts_and) > 1:
        stmts = []
        for p in parts_and:
            s = translate_atom(p.strip(), context)
            if s:
                stmts.append(s)
        return "\n    ".join(stmts) if stmts else None

    # valid(ptr) / owns(ptr)  → ptr != NULL
    m = _VALID_RE.search(atom) or _OWNS_RE.search(atom)
    if m:
        ptr = m.group(1).strip()
        return wrap(f"{ptr} != NULL")

    # null(ptr)  → ptr == NULL  (but !null(ptr) → ptr != NULL)
    m = _NULL_RE.search(atom)
    if m:
        ptr = m.group(1).strip()
        # Detect negation: character immediately before the match is '!'
        negated = m.start() > 0 and atom[m.start() - 1] == "!"
        return wrap(f"{ptr} != NULL" if negated else f"{ptr} == NULL")

    # in_bounds(arr, idx)
    m = _IN_BOUNDS_RE.search(atom)
    if m:
        arr = m.group(1).strip()
        idx = m.group(2).strip()
        return wrap(f"{idx} >= 0 && {idx} < (int)(sizeof({arr})/sizeof({arr}[0]))")

    parts_or = _top_level_split(atom, "||")
    if len(parts_or) > 1:
        exprs = []
        for p in parts_or:
            inner = _atom_to_expr(p.strip())
            if inner:
                exprs.append(inner)
        if exprs:
            return wrap(" || ".join(f"({e})" for e in exprs))

    # Only wrap in assert/assume if the entire atom looks like valid C.
    # A simple heuristic: no spaces outside of parens (i.e. a single expression,
    # not a natural-language phrase).
    if _C_COMPARISON_RE.search(atom) and _looks_like_c_expr(atom):
        return wrap(atom)

    # Fall through: emit as a comment (natural language / unknown)
    return f"/* condition: {atom} */"


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
    """Return a bare C expression (no statement wrapper) for an atom, or None."""
    atom = _RESULT_RE.sub("result", atom).strip()

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

    if _C_COMPARISON_RE.search(atom):
        return atom

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
    return _condition_to_stmts(precondition, context="assume")


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
    return _condition_to_stmts(post, context="assert")


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
    # so \result → CR + "esult". Normalise both forms to plain "result".
    condition = condition.replace('\result', 'result')   # CR+esult → result
    condition = condition.replace('\\result', 'result')  # literal \result → result

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
            """Wrap clause in /* */ after neutralising internal semicolons.

            Semicolons inside a block comment would cause the later
            re.split(r';') in _condition_to_stmts to break the comment apart,
            leaking non-C constructs (forall, ==>, …) into generated C code.
            """
            return f"/* {clause.replace(';', ' ')} */"

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
                r'->\w*_(?:before|after|old|prev|initial|orig|index|idx|addr|ref|new|copy|tmp|snapshot|cache|prev|start|end|begin|first|last)\b'
                r'|->(?:latest|current|previous|next|prev|first|last|read|write)_\w+\b'
                # Also catch dot-access invented fields on nested structs
                r'|\.\w*_(?:before|after|old|prev|initial|orig|index|idx|addr|ref|new|copy|tmp|snapshot|cache|start|end|begin|first|last)\b'
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

    # Split on explicit " AND " (from merged specs), newlines, semicolons
    parts = re.split(r"\s+AND\s+|\n|;", condition, flags=re.IGNORECASE)
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
