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

_FORALL = re.compile(r"^\s*forall\s+(\w+)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
_EXISTS = re.compile(r"^\s*exists\s+(\w+)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)


def expr_to_acsl(expr: str) -> str:
    """Render a DSL boolean/arithmetic expression to ACSL."""
    expr = (expr or "").strip()
    if not expr or expr.lower() == "true":
        return "\\true"
    if expr.lower() == "false":
        return "\\false"
    m = _FORALL.match(expr)
    if m:
        return f"\\forall integer {m.group(1)}; {expr_to_acsl(m.group(2).strip())}"
    m = _EXISTS.match(expr)
    if m:
        return f"\\exists integer {m.group(1)}; {expr_to_acsl(m.group(2).strip())}"
    out = expr
    # valid_range(p, lo, hi) -> \valid(p + (lo .. (hi) - 1))   (before the bare valid())
    out = re.sub(r"\bvalid_range\s*\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
                 r"\\valid(\1 + (\2 .. (\3) - 1))", out)
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
