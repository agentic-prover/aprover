"""Loop-invariant synthesis — the loop-annotation arm of the Specification
Synthesis Problem, built as a MINIMAL extension of bmc-agent's gen+refine loop.

The engine is unchanged: an LLM proposes a behavioral summary of a code region,
CBMC checks it, and the proposal is refined on the counterexample. Here the
region is a LOOP and the summary is a loop INVARIANT.

Verification mechanism (vanilla CBMC, no loop-contract support needed):
insert the candidate invariant as ``__CPROVER_assert(inv)`` AT THE LOOP HEAD.
For a loop whose trip count CBMC can unwind, this discharges BOTH

  * Local Validity  (P |= S): the assert is checked on every unwound iteration,
    so the invariant holds at entry (base) and is preserved (step). Because the
    loop index is concrete at each unwind, a quantified ``forall k < i`` has a
    CONCRETE bound — the case CBMC handles soundly (symbolic bounds do not).
  * Global Adequacy (P u S |- G): the goals are proved in the same run.

Output is rendered to ACSL (``loop invariant ...;``) — the DSL is the internal
working form, ACSL is a render target (see ``_inv_to_acsl``). The DSL->C render
(``_inv_to_cbmc``) is what feeds the CBMC oracle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bmc_agent.assert_driven_specs import _balanced_arg, extract_goals
from bmc_agent.logger import get_logger

logger = get_logger("loop_inv")

_LOOP_HEADER = re.compile(r"\b(for|while)\s*\(")
# DSL quantifier form:  forall <ident> : <body-using- ==> >
_FORALL = re.compile(r"^\s*forall\s+(\w+)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)


@dataclass
class LoopSite:
    kind: str           # "for" | "while"
    guard: str          # raw text inside the loop header parens
    head_offset: int    # char index just AFTER the body-opening '{'
    body: str           # loop body text (between the braces)
    ordinal: int        # 0-based source order


def _matching_brace(source: str, open_idx: int) -> int:
    """Index of the '}' matching the '{' at ``source[open_idx]`` (string/char
    literal aware), or -1 if unbalanced."""
    depth, i, n = 0, open_idx, len(source)
    quote = None
    while i < n:
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2; continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def find_loops(source: str) -> list[LoopSite]:
    """Find brace-bodied ``for``/``while`` loops, with the insertion point just
    inside the body. Single-statement (brace-less) bodies are skipped — benchmark
    loops with invariants are braced."""
    loops: list[LoopSite] = []
    for m in _LOOP_HEADER.finditer(source):
        guard, after = _balanced_arg(source, m.end() - 1)
        j = after
        while j < len(source) and source[j] in " \t\r\n":
            j += 1
        if j >= len(source) or source[j] != "{":
            continue
        close = _matching_brace(source, j)
        if close < 0:
            continue
        loops.append(LoopSite(kind=m.group(1), guard=guard.strip(),
                              head_offset=j + 1, body=source[j + 1:close],
                              ordinal=len(loops)))
    return loops


# --- DSL -> oracle renderers (the quantified fragment invariants need) --------

def _inv_to_cbmc(expr: str) -> str:
    """Render a DSL invariant to a C boolean expression for CBMC.

    ``forall k : G ==> B``  ->  ``__CPROVER_forall { int k; (G ==> B) }``
    Plain boolean expressions pass through unchanged. ``==>`` is accepted by
    CBMC inside ``__CPROVER_forall`` and at top level it is normalised to
    ``(!(a) || (b))`` so a bare implication is also checkable.
    """
    expr = expr.strip()
    m = _FORALL.match(expr)
    if m:
        var, body = m.group(1), m.group(2).strip()
        return f"__CPROVER_forall {{ int {var}; ({body}) }}"
    return _top_implication_to_or(expr)


def _top_implication_to_or(expr: str) -> str:
    """Rewrite a top-level ``A ==> B`` to ``(!(A) || (B))`` (depth-0 only)."""
    depth, i, n = 0, 0, len(expr)
    quote = None
    while i < n - 1:
        ch = expr[i]
        if quote:
            if ch == "\\":
                i += 2; continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "=" and expr[i:i + 3] == "==>":
            lhs, rhs = expr[:i], expr[i + 3:]
            return f"(!({lhs.strip()}) || ({_top_implication_to_or(rhs.strip())}))"
        i += 1
    return expr


def _inv_to_acsl(expr: str) -> str:
    """Render a DSL invariant to ACSL.

    ``forall k : BODY`` -> ``\\forall integer k; BODY``;  ``result`` -> ``\\result``.
    ``==>`` is valid ACSL and passes through. Used for output and (later) Frama-C.
    """
    expr = expr.strip()
    m = _FORALL.match(expr)
    if m:
        var, body = m.group(1), m.group(2).strip()
        body = re.sub(r"\bresult\b", r"\\result", body)
        return f"\\forall integer {var}; {body}"
    return re.sub(r"\bresult\b", r"\\result", expr)


# --- source instrumentation ---------------------------------------------------

def insert_loop_invariants(source: str, annotations: dict) -> str:
    """Insert ``__CPROVER_assert(<inv>, "loopinv_<ord>_<n>")`` at each loop head.

    ``annotations`` maps loop ordinal -> list of DSL invariant expressions.
    Inserts back-to-front so earlier offsets stay valid.
    """
    loops = find_loops(source)
    edits = []
    for lp in loops:
        for n, inv in enumerate(annotations.get(lp.ordinal, []) or []):
            tag = f"loopinv_{lp.ordinal}_{n}"
            stmt = f'\n    __CPROVER_assert({_inv_to_cbmc(inv)}, "{tag}");'
            edits.append((lp.head_offset, stmt))
    out = source
    for off, stmt in sorted(edits, key=lambda e: -e[0]):
        out = out[:off] + stmt + out[off:]
    return out


def render_loop_invariants_acsl(annotations: dict, loops: list = None) -> str:
    """Render the synthesized invariants as ACSL ``loop invariant`` blocks (one
    block per loop), for the benchmark output / Frama-C."""
    blocks = []
    for ordinal in sorted(annotations):
        invs = annotations.get(ordinal) or []
        if not invs:
            continue
        lines = "\n".join(f"  loop invariant {_inv_to_acsl(inv)};" for inv in invs)
        blocks.append(f"/* loop #{ordinal} */\n/*@\n{lines}\n*/")
    return "\n".join(blocks)


# --- failing-annotation parsing (which invariant / goal did CBMC reject) ------

def failing_loopinvs(res) -> list:
    """Loop-invariant assertions CBMC could not prove → [(ordinal, n), ...].
    Our tags are 'loopinv_<ord>_<n>'."""
    out = []
    for ce in getattr(res, "counterexamples", []) or []:
        d = (ce.description or "").strip()
        mm = re.match(r"loopinv_(\d+)_(\d+)", d)
        if mm:
            out.append((int(mm.group(1)), int(mm.group(2))))
    return out


def _prep_goals(source: str) -> str:
    """Make the program's verification GOALS checkable by CBMC: translate
    ``//@ assert`` and shim ``__VERIFIER_assert`` (``assert`` is native)."""
    from bmc_agent.standalone import translate_acsl_asserts
    src, _ = translate_acsl_asserts(source)
    if "__VERIFIER_assert" in src and "#define __VERIFIER_assert" not in src:
        src = '#define __VERIFIER_assert(c) __CPROVER_assert((c), "GOAL")\n' + src
    return src


@dataclass
class LoopCheck:
    verified: bool
    failing_invariants: list = field(default_factory=list)   # (ordinal, n) CBMC rejected
    goal_failed: bool = False                                 # a goal still unprovable
    unwinding_failed: bool = False                            # under-unwound (unsound)
    result: object = None


def check_loop_invariants(source: str, annotations: dict, config,
                          entry: str = "main", unwind: int = 64,
                          timeout: int = 120) -> LoopCheck:
    """Instrument the loop heads with the candidate invariants, make the goals
    checkable, and run CBMC. With ``--unwinding-assertions`` (on in run_cbmc) an
    under-sized unwind is reported, not silently assumed — so a clean pass means
    Local Validity (per-iteration invariant) AND Global Adequacy (goals) hold."""
    from bmc_agent.assert_driven_specs import _run
    instrumented = _prep_goals(insert_loop_invariants(source, annotations))
    res = _run(instrumented, config, entry, unwind, timeout)
    finv = failing_loopinvs(res)
    unwinding = any("unwinding" in (getattr(ce, "failing_property", "") or "").lower()
                    or "unwinding" in (ce.description or "").lower()
                    for ce in getattr(res, "counterexamples", []) or [])
    # a non-loopinv, non-unwinding counterexample == a goal (or safety prop) unproved
    goal_failed = any(not re.match(r"loopinv_\d+_\d+", (ce.description or ""))
                      and "unwinding" not in (ce.description or "").lower()
                      for ce in getattr(res, "counterexamples", []) or [])
    return LoopCheck(verified=bool(res.verified) and not finv and not goal_failed,
                     failing_invariants=finv, goal_failed=goal_failed,
                     unwinding_failed=unwinding, result=res)
