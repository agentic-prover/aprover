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

from bmc_agent.dsl_to_cbmc import fully_parenthesize

_FORALL = re.compile(r"^\s*forall\s+(\w+)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
_EXISTS = re.compile(r"^\s*exists\s+(\w+)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)

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


def expr_to_acsl(expr: str) -> str:
    """Render a DSL boolean/arithmetic expression to ACSL."""
    expr = (expr or "").strip()
    if not expr or expr.lower() == "true":
        return "\\true"
    if expr.lower() == "false":
        return "\\false"
    # Rewrite \sum/\product/\numof colon-form to Frama-C's \lambda form before any
    # other handling (they may be nested inside the boolean structure below).
    if "\\sum" in expr or "\\product" in expr or "\\numof" in expr:
        expr = _rewrite_aggregates(expr)
    m = _FORALL.match(expr)
    if m:
        return f"\\forall integer {m.group(1)}; {expr_to_acsl(m.group(2).strip())}"
    m = _EXISTS.match(expr)
    if m:
        return f"\\exists integer {m.group(1)}; {expr_to_acsl(m.group(2).strip())}"
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


def contract_to_acsl(requires: str = "", ensures: str = "", assigns: str = "") -> str:
    """Render a function contract as an ACSL block, or "" if it is vacuous
    (``requires true`` / empty ensures are dropped)."""
    lines = []
    r, e, a = (requires or "").strip(), (ensures or "").strip(), (assigns or "").strip()
    if r and r.lower() != "true":
        lines.append(f"  requires {expr_to_acsl(r)};")
    if a:
        lines.append(f"  assigns {a};")
    if e and e.lower() != "true":
        lines.append(f"  ensures {expr_to_acsl(e)};")
    return "/*@\n" + "\n".join(lines) + "\n*/" if lines else ""


def loop_invariants_to_acsl(invariants: list, assigns: str = "") -> str:
    """Render a loop's invariants (DSL exprs) as an ACSL loop-annotation block."""
    lines = [f"  loop invariant {expr_to_acsl(inv)};" for inv in (invariants or []) if inv]
    if assigns:
        lines.append(f"  loop assigns {assigns};")
    return "/*@\n" + "\n".join(lines) + "\n*/" if lines else ""
