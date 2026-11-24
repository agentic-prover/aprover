r"""DSL → ACSL rendering.

The gen+refine engine works in bmc-agent's internal DSL; ACSL is a *render
target* for output (and, later, the Frama-C oracle). This module is the single
place that maps the DSL fragment we use onto ACSL syntax, so both the contract
path (`requires`/`ensures`) and the loop path (`loop invariant`) render
consistently.

Mapping (the fragment that actually occurs):
  forall <v> : <body>      -> \forall integer <v>; <body>
  result                   -> \result
  old(e)                   -> \old(e)
  valid_range(p, lo, hi)   -> \valid(p + (lo .. (hi) - 1))
  valid(p)                 -> \valid(p)
  ==> , && , || , comparisons, arithmetic  -> pass through (already valid ACSL)
"""
from __future__ import annotations

import re

from bmc_agent.dsl_to_cbmc import fully_parenthesize, _looks_like_c_expr

_FORALL = re.compile(r"^\s*forall\s+(\w+)\s*(?::|,)\s*(.+)$", re.IGNORECASE | re.DOTALL)
_EXISTS = re.compile(r"^\s*exists\s+(\w+)\s*(?::|,)\s*(.+)$", re.IGNORECASE | re.DOTALL)
_BOOL_OPS = ("<==>", "==>", "||", "&&")

# DSL aggregate form `\sum k : LO <= k < HI : TERM` (likewise \product, \numof).
# Frama-C/WP spells these `\sum(low, high, \lambda integer k; term)` with an
# INCLUSIVE high bound — so the colon-form below must be rewritten, not passed
# through (Frama-C rejects it with "unexpected token 'k'"). Header only; the
# guard and term are scanned with paren-balancing in _rewrite_aggregates.
_AGG_HEAD = re.compile(r"\\(sum|product|numof)\s+(\w+)\s*:\s*", re.IGNORECASE)


def _scan_to_depth0(expr: str, start: int, stops: str) -> int:
    """Index of the first char in ``stops`` at bracket-depth 0 from ``start``, or
    the index of the unbalanced closing bracket / end of string."""
    depth, j = 0, start
    while j < len(expr):
        c = expr[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return j
            depth -= 1
        elif depth == 0 and c in stops:
            return j
        j += 1
    return len(expr)


def _agg_bound(guard: str, var: str):
    """(low, high_inclusive) ACSL bounds from a chained guard `LO <= k < HI`.
    Frama-C's high is inclusive, so `< HI` -> `(HI) - 1`, and a strict low
    `LO < k` -> `(LO) + 1`. Returns (None, None) if the guard isn't recognised."""
    m = re.match(rf"\s*(.+?)\s*(<=|<)\s*{re.escape(var)}\s*(<=|<)\s*(.+?)\s*$", guard,
                 re.DOTALL)
    if not m:
        return None, None
    lo_e, lo_op, hi_op, hi_e = m.group(1), m.group(2), m.group(3), m.group(4)
    low = lo_e.strip() if lo_op == "<=" else f"({lo_e.strip()}) + 1"
    high = f"({hi_e.strip()}) - 1" if hi_op == "<" else hi_e.strip()
    return low, high


def _rewrite_aggregates(expr: str) -> str:
    """Rewrite every DSL `\\sum k : guard : term` (and \\product/\\numof) — possibly
    nested inside a larger expression — into Frama-C's
    `\\sum(low, high, \\lambda integer k; term)`. Left unchanged if the bound can't
    be parsed (so the failure is visible at the oracle, not silently wrong)."""
    while True:
        m = _AGG_HEAD.search(expr)
        if not m:
            return expr
        kw, var = m.group(1).lower(), m.group(2)
        guard_end = _scan_to_depth0(expr, m.end(), ":")
        if guard_end >= len(expr) or expr[guard_end] != ":":
            return expr  # no term separator — malformed, leave as-is
        guard = expr[m.end():guard_end]
        term_end = _scan_to_depth0(expr, guard_end + 1, "")
        term = expr[guard_end + 1:term_end].strip()
        low, high = _agg_bound(guard, var)
        if low is None:
            return expr
        rendered = f"\\{kw}({low}, {high}, \\lambda integer {var}; {expr_to_acsl(term)})"
        expr = expr[:m.start()] + rendered + expr[term_end:]


def _top_level_split(text: str, op: str) -> list[str]:
    """Split *text* on top-level *op*, ignoring nested parentheses/brackets."""
    parts, cur = [], []
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif depth == 0 and text[i:i + len(op)] == op:
            parts.append("".join(cur).strip())
            cur = []
            i += len(op)
            continue
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur).strip())
    return parts if len(parts) > 1 else [text]


def _top_level_split_first(text: str, op: str) -> tuple[str, str] | None:
    """Split *text* at the first top-level *op*."""
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and text[i:i + len(op)] == op:
            return text[:i].strip(), text[i + len(op):].strip()
        i += 1
    return None


def _strip_outer_parens(expr: str) -> str:
    """Remove one or more complete outer parenthesis pairs."""
    s = (expr or "").strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        ok = True
        for i, c in enumerate(s):
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    ok = False
                    break
        if not ok:
            break
        s = s[1:-1].strip()
    return s


def _normalize_logical_syntax(expr: str) -> str:
    """Normalize common LLM DSL variants before ACSL rendering."""
    expr = (expr or "").strip()
    expr = expr.replace("≤", "<=").replace("≥", ">=").replace("∧", "&&").replace("∨", "||")
    # CBMC-side overflow guards use C integer suffixes (`1LL`, `2147483647LL`).
    # ACSL logic integer expressions do not need those suffixes, and older
    # Frama-C parsers reject some suffixed literals inside annotations.
    expr = re.sub(r"\b(\d+)(?:ULL|LLU|LL|UL|LU|U|L)\b", r"\1", expr)
    # LLMs often emit `=>`; ACSL uses `==>`. Do not touch >=, <=, !=, ==>.
    expr = re.sub(r"(?<![<>=!])=>(?![=])", "==>", expr)
    return expr


def _repair_guard_only_forall_implication(expr: str) -> str:
    """Repair `(forall k : GUARD) ==> FACT(k)` to `forall k : GUARD ==> FACT(k)`.

    LLMs frequently use the quantifier as if it bound only the guard and then put
    the quantified fact in the implication consequent. ACSL parses that as a
    closed boolean on the left and an unbound `k` on the right. When the RHS uses
    the bound variable and the guard itself contains no implication, the intended
    ACSL is the universally quantified implication.
    """
    s = _strip_outer_parens((expr or "").strip())
    parts = _top_level_split(s, "==>")
    if len(parts) != 2:
        return expr
    lhs, rhs = parts[0].strip(), parts[1].strip()
    q = _strip_outer_parens(lhs)
    m = _FORALL.match(q)
    if not m:
        return expr
    var, guard = m.group(1), m.group(2).strip()
    if "==>" in guard or not re.search(rf"\b{re.escape(var)}\b", rhs):
        return expr
    return f"forall {var} : ({guard}) ==> ({rhs})"


def _has_obvious_prose(expr: str) -> bool:
    """Conservative prose filter for clauses that are not formal expressions."""
    s = _strip_outer_parens((expr or "").strip())
    if not s:
        return True
    if re.search(r"\b(i\.e\.|e\.g\.|multiset|permutation|original values?)\b", s, re.I):
        return True
    # Let the existing CBMC-side prose heuristic reject English fragments.
    if not re.match(r"^\s*(forall|exists)\b", s, re.I) and not _looks_like_c_expr(s):
        return True
    return False


def _wrap_bool_operand(expr: str) -> str:
    e = expr.strip()
    if not e:
        return e
    if e.startswith("(") and _strip_outer_parens(e) != e:
        return e
    if re.match(r"^\\(?:forall|exists)\b", e):
        return f"({e})"
    if re.match(r"^\\?[A-Za-z_]\w*(?:\[[^\]]+\]|\.[A-Za-z_]\w*|->[A-Za-z_]\w*)*$", e):
        return e
    if re.match(r"^\d+$", e):
        return e
    return f"({e})"


def expr_to_acsl(expr: str) -> str:
    """Render a DSL boolean/arithmetic expression to ACSL."""
    expr = _normalize_logical_syntax(expr)
    if not expr or expr.lower() == "true":
        return "\\true"
    if expr.lower() == "false":
        return "\\false"
    # Legal quantifiers bind the whole following body. Handle them before the
    # guard-only repair, otherwise `forall k : G ==> P(k)` looks like the broken
    # `(forall k : G) ==> P(k)` pattern and recurses forever.
    m = _FORALL.match(expr)
    if m:
        return f"\\forall integer {m.group(1)}; {expr_to_acsl(m.group(2).strip())}"
    m = _EXISTS.match(expr)
    if m:
        return f"\\exists integer {m.group(1)}; {expr_to_acsl(m.group(2).strip())}"
    repaired = _repair_guard_only_forall_implication(expr)
    if repaired != expr:
        return expr_to_acsl(repaired)
    inner = _strip_outer_parens(expr)
    if inner != expr:
        return f"({expr_to_acsl(inner)})"
    # Rewrite \sum/\product/\numof colon-form to Frama-C's \lambda form before any
    # other handling (they may be nested inside the boolean structure below).
    if "\\sum" in expr or "\\product" in expr or "\\numof" in expr:
        expr = _rewrite_aggregates(expr)
    for op in _BOOL_OPS:
        parts = _top_level_split(expr, op)
        if len(parts) > 1:
            if op == "==>":
                # Implication is right-associative. Rendering `A ==> B ==> C` as
                # a flat three-way join can change the scope of nested quantifier
                # DSL such as `(forall k : G) ==> P(k)`. Split only the first
                # top-level implication and recurse on the RHS.
                first = _top_level_split_first(expr, op)
                if first:
                    lhs, rhs = first
                    return (
                        f"{_wrap_bool_operand(expr_to_acsl(lhs))} ==> "
                        f"{_wrap_bool_operand(expr_to_acsl(rhs))}"
                    )
            return f" {op} ".join(_wrap_bool_operand(expr_to_acsl(p)) for p in parts)
    # Pin the boolean (<==>/==>/||/&&) grouping with explicit parentheses so the
    # rendered ACSL doesn't rely on operator precedence — keeping it readable
    # and, crucially, identical in meaning to the CBMC harness (which renders
    # the same DSL via dsl_to_cbmc.fully_parenthesize). Quantifiers are handled
    # above first, so their bodies are parenthesised in the recursive call.
    out = fully_parenthesize(expr)
    # valid_range(p, lo, hi) -> \valid(p + (lo .. (hi) - 1))   (before the bare valid())
    out = re.sub(r"\bvalid_range\s*\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
                 r"\\valid(\1 + (\2 .. (\3) - 1))", out)
    out = re.sub(r"\bnull\s*\(\s*([^()]+?)\s*\)", r"(\1 == \\null)", out)  # null(p) -> (p == \null)
    out = re.sub(r"\bvalid_string\s*\(\s*([^()]+?)\s*\)", r"\\valid_read(\1)", out)
    out = re.sub(r"(?<!\\)\bvalid\s*\(", r"\\valid(", out)        # valid(p) -> \valid(p)
    out = re.sub(r"(?<!\\)\bold\s*\(", r"\\old(", out)            # old(e)   -> \old(e)
    out = re.sub(r"(?<![\\\w])\bresult\b", r"\\result", out)      # result   -> \result
    return out


def condition_to_acsl_clauses(expr: str) -> list[str]:
    """Render a condition as one or more ACSL clauses.

    Top-level conjunctions are equivalent to separate ACSL clauses, and splitting
    lets us drop a single malformed/prose conjunct without throwing away the rest
    of the contract. This mirrors AutoSpec's invalid-line removal behavior while
    staying conservative: dropped clauses can only make the spec weaker, and WP
    still has to prove the benchmark goals from the remaining clauses.
    """
    expr = _normalize_logical_syntax(expr)
    if not expr or expr.lower() in ("true", "\\true", "1"):
        return []
    clauses = []
    for part in _top_level_split(expr, "&&"):
        part = part.strip()
        if not part or part.lower() in ("true", "\\true", "1"):
            continue
        if _has_obvious_prose(part):
            continue
        rendered = expr_to_acsl(part)
        if rendered and rendered not in ("\\true", "true") and rendered not in clauses:
            clauses.append(rendered)
    return clauses


def contract_to_acsl(requires: str = "", ensures: str = "", assigns: str = "") -> str:
    """Render a function contract as an ACSL block, or "" if it is vacuous
    (``requires true`` / empty ensures are dropped)."""
    lines = []
    r, e, a = (requires or "").strip(), (ensures or "").strip(), (assigns or "").strip()
    for clause in condition_to_acsl_clauses(r):
        lines.append(f"  requires {clause};")
    if a:
        lines.append(f"  assigns {a};")
    for clause in condition_to_acsl_clauses(e):
        lines.append(f"  ensures {clause};")
    return "/*@\n" + "\n".join(lines) + "\n*/" if lines else ""


def _old_to_loop_pre(expr: str) -> str:
    """Loop invariants cannot use ``\\old``; rewrite it to ``\\at(..., Pre)``."""
    out = []
    i = 0
    marker = "\\old("
    while i < len(expr):
        j = expr.find(marker, i)
        if j < 0:
            out.append(expr[i:])
            break
        out.append(expr[i:j])
        start = j + len(marker)
        depth = 1
        k = start
        while k < len(expr) and depth:
            if expr[k] == "(":
                depth += 1
            elif expr[k] == ")":
                depth -= 1
            k += 1
        if depth:
            out.append(expr[j:])
            break
        inner = expr[start:k - 1]
        out.append(f"\\at({inner}, Pre)")
        i = k
    return "".join(out)


def loop_invariants_to_acsl(invariants: list, assigns: str = "",
                            variant: str = "") -> str:
    """Render a loop's invariants (DSL exprs) as an ACSL loop-annotation block.
    ``variant`` (when given) adds a ``loop variant`` clause for termination."""
    lines = [
        f"  loop invariant {_old_to_loop_pre(expr_to_acsl(inv))};"
        for inv in (invariants or []) if inv
    ]
    if assigns:
        lines.append(f"  loop assigns {assigns};")
    if variant:
        lines.append(f"  loop variant {variant};")
    return "/*@\n" + "\n".join(lines) + "\n*/" if lines else ""
