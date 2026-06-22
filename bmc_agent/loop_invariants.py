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
    start_offset: int = -1   # char index of the `for`/`while` keyword
    end_offset: int = -1     # char index just AFTER the body-closing '}'


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


_CTRL_KW = re.compile(r"\b(?:for|while|if|do|switch)\b")


def brace_braceless_loops(source: str) -> str:
    """Wrap a brace-less single-statement loop body in ``{ ... }`` so the rest of
    the pipeline (which assumes braced bodies — find_loops, both oracle insertion
    paths) handles it. Idempotent on already-braced loops (returns them byte-for-
    byte). Conservatively SKIPS a body that begins with a control keyword
    (``for (...) for (...) ...`` / ``if``) — handling nested/compound brace-less
    bodies needs full statement parsing; the inner simple loop still gets braced,
    so a single brace-less loop works and a nested one degrades safely rather than
    misparsing. Semantically identical (added braces around one statement)."""
    edits = []
    for m in _LOOP_HEADER.finditer(source):
        _guard, after = _balanced_arg(source, m.end() - 1)
        j = after
        # Skip whitespace AND comments to find the real body start; a // or /*
        # comment between a loop header and its (possibly nested-loop) body must
        # not be mistaken for the body, or the wrap lands inside the next statement.
        while j < len(source):
            if source[j] in " \t\r\n":
                j += 1
            elif source[j:j+2] == "//":
                nl = source.find("\n", j); j = len(source) if nl < 0 else nl + 1
            elif source[j:j+2] == "/*":
                e = source.find("*/", j); j = len(source) if e < 0 else e + 2
            else:
                break
        if j >= len(source) or source[j] in "{;":
            continue                          # already braced, or empty / do-while cond
        if _CTRL_KW.match(source, j):
            continue                          # nested/compound body — skip (safe)
        depth, k = 0, j                       # find the statement's top-level ';'
        while k < len(source):
            c = source[k]
            if c in "([":
                depth += 1
            elif c in ")]":
                depth -= 1
            elif c == ";" and depth == 0:
                break
            k += 1
        if k >= len(source):
            continue
        edits.append((j, k + 1))
    for a, b in sorted(edits, key=lambda e: -e[0]):
        source = source[:a] + "{ " + source[a:b] + " }" + source[b:]
    return source


def find_loops(source: str) -> list[LoopSite]:
    """Find brace-bodied ``for``/``while`` loops, with the insertion point just
    inside the body. Single-statement (brace-less) bodies are skipped here — run
    ``brace_braceless_loops`` first to normalise them into braced form."""
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
                              ordinal=len(loops), start_offset=m.start(),
                              end_offset=close + 1))
    return loops


# --- DSL -> oracle renderers (the quantified fragment invariants need) --------

_CHAIN_RE_TMPL = (
    r"([\w\[\]\.]+(?:\s*[-+*/]\s*[\w\[\]\.]+)*)\s*(<=?|>=?)\s*"
    r"(\b{var}\b)\s*(<=?|>=?)\s*([\w\[\]\.]+(?:\s*[-+*/]\s*[\w\[\]\.]+)*)")


def _expand_chained_comparisons(body: str, var: str) -> str:
    """Expand a math-style chained comparison around the quantifier variable
    (valid DSL/ACSL, INVALID C): ``LO <= var < HI`` -> ``(LO <= var) && (var < HI)``.
    C parses ``0 <= k < i`` as ``(0<=k) < i`` (a 0/1 vs i compare) — a semantic
    bug — so the C renderer must split it; ACSL keeps the chained form natively."""
    rx = re.compile(_CHAIN_RE_TMPL.format(var=re.escape(var)))
    prev = None
    out = body
    while out != prev:
        prev = out
        out = rx.sub(r"((\1 \2 \3) && (\3 \4 \5))", out)
    return out


def _inv_to_cbmc(expr: str) -> str:
    """Render a DSL invariant to a C boolean expression for CBMC.

    ``forall k : G ==> B``  ->  ``__CPROVER_forall { int k; (G ==> B) }``
    Chained comparisons around the bound variable are expanded (C has none).
    Plain boolean expressions pass through unchanged. ``==>`` is accepted by
    CBMC inside ``__CPROVER_forall`` and at top level it is normalised to
    ``(!(a) || (b))`` so a bare implication is also checkable.
    """
    expr = expr.strip()
    m = _FORALL.match(expr)
    if m:
        var, body = m.group(1), m.group(2).strip()
        body = _expand_chained_comparisons(body, var)
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
    """Render a DSL invariant to ACSL (delegates to the shared serializer)."""
    from bmc_agent.acsl import expr_to_acsl
    return expr_to_acsl(expr)


def _split_top_implication(expr: str):
    """Split ``ANTE ==> CONS`` at the first top-level ``==>``; (None, expr) if none."""
    depth, i, n = 0, 0, len(expr)
    while i < n - 2:
        c = expr[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and expr[i:i + 3] == "==>":
            return expr[:i].strip(), expr[i + 3:].strip()
        i += 1
    return None, expr.strip()


def _loophead_assert(inv: str, tag: str) -> str:
    """Loop-head assertion statement for one invariant.

    For a quantified ``forall k : ANTE ==> CONS``, emit the single-nondet-WITNESS
    form — ``{ int k = nondet; assume(ANTE); assert(CONS); }`` — which is O(1) per
    unwound iteration instead of O(N) for ``__CPROVER_forall`` (the forall expands
    to a conjunction over the array). This keeps a large literal trip bound (e.g.
    1024) tractable. Sound: an arbitrary witness covers all k. Plain invariants
    and quantified-without-implication fall back to a direct assert."""
    inv = inv.strip()
    m = _FORALL.match(inv)
    if m:
        var, body = m.group(1), m.group(2).strip()
        ante, cons = _split_top_implication(body)
        if ante is not None:
            ante_c = _expand_chained_comparisons(ante, var)
            return (f'\n    {{ int {var} = __VERIFIER_nondet_int();'
                    f' __CPROVER_assume({ante_c});'
                    f' __CPROVER_assert({cons}, "{tag}"); }}')
    return f'\n    __CPROVER_assert({_inv_to_cbmc(inv)}, "{tag}");'


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
            edits.append((lp.head_offset, _loophead_assert(inv, tag)))
    out = source
    for off, stmt in sorted(edits, key=lambda e: -e[0]):
        out = out[:off] + stmt + out[off:]
    return out


def render_loop_invariants_acsl(annotations: dict, loops: list = None,
                                variants: dict = None, assigns: dict = None) -> str:
    """Render the synthesized invariants as ACSL ``loop invariant`` blocks (one
    block per loop), for the benchmark output / Frama-C. ``assigns`` (loop ordinal
    -> frame expr) emits the ``loop assigns`` clause that was VERIFIED, so the shown
    spec is the complete, re-checkable loop contract — not a frame-less subset.
    ``variants`` (loop ordinal -> expr) adds a ``loop variant`` for termination.
    ACSL clause order is fixed: invariant(s), then assigns, then variant."""
    blocks = []
    for ordinal in sorted(annotations):
        invs = annotations.get(ordinal) or []
        if not invs:
            continue
        lines = "\n".join(f"  loop invariant {_inv_to_acsl(inv)};" for inv in invs)
        frame = (assigns or {}).get(ordinal, "")
        if frame:
            lines += f"\n  loop assigns {frame};"
        var = (variants or {}).get(ordinal, "")
        if var:
            lines += f"\n  loop variant {var};"
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


_STATIC_ASSERT_RX = re.compile(r"\b(?:static_assert|_Static_assert)\s*\(")


def _prep_goals(source: str) -> str:
    """Make the program's verification GOALS checkable by CBMC: translate
    ``//@ assert`` and ``static_assert`` to runtime ``__CPROVER_assert`` and shim
    ``__VERIFIER_assert`` (``assert`` is native).

    ``static_assert`` is compile-time in standard C, but these benchmarks use it
    with RUNTIME expressions as the goal — so treat it as a runtime assertion.
    """
    from bmc_agent.standalone import translate_acsl_asserts
    from bmc_agent.assert_driven_specs import _balanced_arg, _strip_assert_message
    src, _ = translate_acsl_asserts(source)

    out, i = [], 0
    while True:
        m = _STATIC_ASSERT_RX.search(src, i)
        if not m:
            out.append(src[i:]); break
        out.append(src[i:m.start()])
        arg, after = _balanced_arg(src, m.end() - 1)
        out.append(f'__CPROVER_assert({_strip_assert_message(arg)}, "GOAL")')
        i = after
    src = "".join(out)

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
    instrumented: str = ""                                    # the source CBMC actually checked


def check_loop_invariants(source: str, annotations: dict, config,
                          entry: str = "main", unwind: int = 64,
                          timeout: int = 120) -> LoopCheck:
    """Instrument the loop heads with the candidate invariants, make the goals
    checkable, and run CBMC. With ``--unwinding-assertions`` (on in run_cbmc) an
    under-sized unwind is reported, not silently assumed — so a clean pass means
    Local Validity (per-iteration invariant) AND Global Adequacy (goals) hold."""
    from bmc_agent.assert_driven_specs import _run
    instrumented = _NONDET_PRELUDE + _prep_goals(insert_loop_invariants(source, annotations))
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
                     unwinding_failed=unwinding, result=res, instrumented=instrumented)


# --- havoc/assume loop abstraction (UNBOUNDED scalar loops; no unwinding) -----
# For a loop CBMC cannot unwind (while(unknown()), symbolic bound), abstract it
# by its invariant: assert(inv) [base] ; havoc(assigns) ; assume(inv) ;
# if(guard){ body ; assert(inv) [step] ; assume(0) } ; <goal under inv && !guard>.
# Sound for SCALAR invariants (the symbolic-bound `forall` problem only hits the
# unwinding path). --math-ints assumes the body's signed arithmetic doesn't
# overflow (= the mathematical-integer semantics these IC3-style benchmarks use).

_DECL_RE = re.compile(
    r"\b(?:unsigned\s+|signed\s+)?(?:int|long\s+long|long|short|char|size_t|"
    r"u?int\d+_t|_Bool|bool|float|double)\b[\s*]*([A-Za-z_]\w*)")
_ASSIGN_RE = re.compile(r"([A-Za-z_]\w*)\s*(?:=(?!=)|[-+*/%&|^]=|<<=|>>=)")
_INCDEC_RE = re.compile(r"(?:([A-Za-z_]\w*)\s*(?:\+\+|--)|(?:\+\+|--)\s*([A-Za-z_]\w*))")
_ARRAYW_RE = re.compile(r"([A-Za-z_]\w*)\s*\[[^\]]*\]\s*(?:=(?!=)|[-+*/%]=)")

_NONDET = {
    "int": "__VERIFIER_nondet_int", "unsigned int": "__VERIFIER_nondet_uint",
    "unsigned": "__VERIFIER_nondet_uint", "long": "__VERIFIER_nondet_long",
    "long long": "__VERIFIER_nondet_longlong", "short": "__VERIFIER_nondet_short",
    "char": "__VERIFIER_nondet_char", "size_t": "__VERIFIER_nondet_ulong",
    "_Bool": "__VERIFIER_nondet_bool", "bool": "__VERIFIER_nondet_bool",
}

_NONDET_PRELUDE = (
    "int __VERIFIER_nondet_int(void); unsigned __VERIFIER_nondet_uint(void);\n"
    "long __VERIFIER_nondet_long(void); long long __VERIFIER_nondet_longlong(void);\n"
    "short __VERIFIER_nondet_short(void); char __VERIFIER_nondet_char(void);\n"
    "unsigned long __VERIFIER_nondet_ulong(void); _Bool __VERIFIER_nondet_bool(void);\n")

_BINOP_ASSIGN = re.compile(
    r"([A-Za-z_]\w*(?:\[[^\]]*\])?)\s*=\s*([^;=]+?)\s*([-+*])\s*([^;]+?)\s*;")


def modified_vars(body: str) -> tuple:
    """(scalars, arrays) assigned in the loop body that are NOT declared inside it
    — i.e. the loop's frame (`assigns` set). Body-local temporaries are excluded."""
    declared = {m.group(1) for m in _DECL_RE.finditer(body)}
    assigned = {m.group(1) for m in _ASSIGN_RE.finditer(body)}
    for m in _INCDEC_RE.finditer(body):
        assigned.add(m.group(1) or m.group(2))
    arrays = {m.group(1) for m in _ARRAYW_RE.finditer(body)} - declared
    scalars = (assigned - declared) - arrays
    return sorted(scalars), sorted(arrays)


def _var_type(source: str, var: str) -> str:
    m = re.search(rf"\b((?:unsigned|signed)\s+)?(int|long\s+long|long|short|char|"
                  rf"size_t|u?int\d+_t|_Bool|bool|float|double)\b[\s*]*\b{re.escape(var)}\b", source)
    return ((m.group(1) or "") + m.group(2)).strip() if m else ""


def _havoc_stmt(var: str, vtype: str) -> str:
    fn = _NONDET.get(vtype)
    return f"{var} = {fn}();" if fn else f"__CPROVER_havoc_object(&{var});"


def _inject_no_overflow(body: str) -> str:
    """Best-effort math-int mode: before each `lhs = A <op> B;` (op in + - *),
    assume the signed operation does not overflow (widen to long long to compute
    the true result and bound it to int range)."""
    def repl(m):
        a, op, b = m.group(2).strip(), m.group(3), m.group(4).strip()
        chk = (f'__CPROVER_assume((long long)({a}) {op} (long long)({b}) <= 2147483647LL '
               f'&& (long long)({a}) {op} (long long)({b}) >= -2147483648LL); ')
        return chk + m.group(0)
    return _BINOP_ASSIGN.sub(repl, body)


def build_havoc_abstraction(source: str, loop: LoopSite, invariants: list,
                            math_ints: bool = False) -> str:
    """Replace `loop` in `source` with its invariant abstraction (see module note)."""
    o = loop.ordinal
    inv_c = [_inv_to_cbmc(inv) for inv in invariants] or ["1"]
    base = "\n    ".join(f'__CPROVER_assert({c}, "loopinv_{o}_{n}");' for n, c in enumerate(inv_c))
    step = "\n        ".join(f'__CPROVER_assert({c}, "loopinv_{o}_{n}");' for n, c in enumerate(inv_c))
    assume_inv = " && ".join(f"({c})" for c in inv_c)
    scalars, arrays = modified_vars(loop.body)
    havoc = "\n    ".join([_havoc_stmt(v, _var_type(source, v)) for v in scalars]
                          + [f"__CPROVER_havoc_object(&{a});" for a in arrays])
    if loop.kind == "while":
        guard, body, init, incr = (loop.guard or "1"), loop.body, "", ""
    else:  # for(init; cond; incr)
        parts = loop.guard.split(";")
        init = parts[0].strip()
        guard = (parts[1].strip() if len(parts) > 1 else "") or "1"
        incr = parts[2].strip() if len(parts) > 2 else ""
    if math_ints:
        body = _inject_no_overflow(body)
    nl = "\n    "
    block = (
        f"/* loop #{o} abstracted by its invariant (havoc/assume) */{nl}"
        + (f"{init};{nl}" if init else "")
        + base + nl
        + (havoc + nl if havoc else "")
        + f"__CPROVER_assume({assume_inv});{nl}"
        + "if (" + guard + ") {\n        "
        + body.strip() + "\n        "
        + (f"{incr};\n        " if incr else "")
        + step + "\n        "
        + "__CPROVER_assume(0);\n    }\n"
    )
    return source[:loop.start_offset] + block + source[loop.end_offset:]


def check_havoc_abstraction(source: str, annotations: dict, config, entry: str = "main",
                            timeout: int = 120, math_ints: bool = False) -> LoopCheck:
    """Validity+adequacy via the havoc/assume abstraction (no unwinding)."""
    loops = find_loops(source)
    instrumented = source
    for lp in sorted(loops, key=lambda l: -l.start_offset):
        invs = annotations.get(lp.ordinal) or []
        if invs:
            instrumented = build_havoc_abstraction(instrumented, lp, invs, math_ints)
    instrumented = _NONDET_PRELUDE + _prep_goals(instrumented)
    from bmc_agent.assert_driven_specs import _run
    res = _run(instrumented, config, entry, unwind=1, timeout=timeout)
    finv = failing_loopinvs(res)
    goal_failed = any(not re.match(r"loopinv_\d+_\d+", (ce.description or ""))
                      for ce in getattr(res, "counterexamples", []) or [])
    return LoopCheck(verified=bool(res.verified) and not finv and not goal_failed,
                     failing_invariants=finv, goal_failed=goal_failed,
                     unwinding_failed=False, result=res, instrumented=instrumented)


# --- the gen+refine driver (reuses the engine: LLM proposes, CBMC disposes) ---

_PROPOSE_SYS = (
    "You are a formal-methods engineer synthesizing LOOP INVARIANTS. You output "
    "ONLY invariant expressions, one per line, no prose, no code fences.")

_PROPOSE_PROMPT = """\
Synthesize loop invariant(s) for the loop below so a verifier can prove the
program's GOALS. An invariant must be INDUCTIVE: true when the loop is first
reached, and preserved by every iteration.

An invariant is evaluated at the TOP of the loop body (the loop head), BEFORE
that iteration's statements execute. So for a loop `for(i=0;i<N;i++)` whose body
sets `A[i]=i`, at the head the elements A[0..i-1] are already set but A[i] is NOT
yet — write `forall k : 0 <= k < i ==> A[k] == k`, and do NOT write `A[i] == i`
(it is false at the head).

Prefer BEHAVIORAL, generalizable invariants that SUMMARIZE the loop over facts
that merely restate a goal. E.g. prefer
    forall k : 0 <= k < i ==> A[k] == k
over
    A[1023] == 1023
Always include the FULL index-bound invariant — BOTH the lower and upper bound
(e.g. `0 <= i` AND `i <= N`, or written together `0 <= i <= N`). The lower bound
is not optional: a value-summary invariant (a running sum/relationship) is only
inductive when the counter's lower bound is also pinned, so omitting `0 <= i`
makes the summary clause fail to verify.

Aim for the FEWEST, most GENERAL clauses that suffice: the index bound plus a
behavioral summary of what the loop computes (a running sum/relationship that holds
for ANY input). Prefer expressing the relationship over restating the caller's
concrete input values — clauses like `n == 5` or `len == 1024` are usually
redundant (the verifier already knows them from the call site). But correctness and
provability come FIRST: if a per-element fact is genuinely needed for the invariant
to be inductive (e.g. relating a symbolic `a[p]` to its value), include it.
Redundant clauses are pruned automatically afterward, so never drop a fact the
proof needs just to look minimal.

INVARIANT CLASSES TO CONSIDER (use whichever FIT this loop; the verifier checks each,
so propose the ones that plausibly hold -- these are general patterns, not a recipe):
  1. BOUNDS: both the lower AND upper bound of each counter (e.g. 0 <= i AND i <= N).
  2. PRESERVED RELATIONSHIP: a quantity the loop keeps constant -- a difference or
     linear combination of variables that advance together -- equals its INITIAL
     value. Write it with old(...): e.g. `a - b == old(a - b)`. Do NOT write
     `expr == expr` or any identity that simplifies to true (it is vacuous and will
     be dropped); anchor the preserved quantity to old(...).
  3. DISJUNCTION for a CONDITIONALLY-updated variable: a variable assigned only under
     a guard is either its initial value or what that guard guarantees:
     `v == old(v) || <fact the guard ensures>`. Use this when a bare relation between
     current variables is false at entry / not inductive.
  4. CLOSED-FORM ladder for an accumulator (running sum/count), as described above.

OUTPUT FORMAT (one invariant per line):
  - a boolean expression over the loop variables/arrays, e.g.  i <= 1024
  - or a quantified fact:  forall <var> : <range/guard> ==> <fact>
Use `==>` for implication. Do NOT use `\\` or ACSL syntax — plain C-style names.

For a running SUM / PRODUCT / COUNT the loop accumulates, summarize it as an
explicit per-index ladder the verifier can DISCHARGE — one case per reached
index, guarded on the counter — e.g. a loop summing a[0..p-1] into `sum`:
    sum == (p == 0 ? 0 : (p == 1 ? a[0] : (p == 2 ? a[0] + a[1] : sum)))
Do NOT use an ACSL `\\sum`/`\\product` aggregate: although it is more general, an
SMT prover cannot discharge the symbolic recursive-aggregate axioms (even the
empty-aggregate base case times out), so it makes the goal unprovable.

GOALS to enable (these are inputs, NOT invariants — do not just restate them):
{goals}

FUNCTION (the loop is inside it):
```c
{fn_src}
```

LOOP header: {kind} ({guard})
Output ONLY the invariant lines for THIS loop.
"""

_REFINE_SYS = _PROPOSE_SYS

_REFINE_PROMPT = """\
The current loop invariants for this loop are:
{current}

{problem}

Propose a CORRECTED / STRONGER set of loop invariant(s) (one per line) that are
INDUCTIVE (true at entry, preserved each iteration) AND sufficient to prove the
goals. Keep them behavioral/generalizable; keep the index-bound invariant.
If a clause keeps being refuted at the loop ENTRY (false initially), or assumes a
bound that need not hold (e.g. a free variable could be small), it is likely a
NEAR-MISS for a DISJUNCTIVE fact, not a missing auxiliary. A variable updated only
CONDITIONALLY inside the loop (set only under an enclosing `if (C)` or while the
loop guard holds) is typically
    v == <its initial value>  ||  <what that guard guarantees about v>
i.e. on any iteration v is either still its initial value or a value set while the
guard held. Prefer this disjunctive form over a bare relation between current
variables when the bare relation is not inductive.

GOALS:
{goals}

FUNCTION:
```c
{fn_src}
```
LOOP header: {kind} ({guard})
Output ONLY the corrected invariant lines.
"""

# Appended to a refinement prompt when one or more clauses that are NEEDED to
# imply the goal were rejected as non-inductive. Such a clause usually fails not
# because it is wrong but because it needs an AUXILIARY companion invariant; the
# proposer tends to re-offer the bare clause and loop forever. This steers it to
# derive the missing companion instead.
_AUX_REFINE_HINT = """

AUXILIARY-INVARIANT NEEDED. The following clause(s) are needed to imply the goal
but were REJECTED as non-inductive (true at entry, NOT preserved by one iteration):
{dropped}
A clause usually fails to be inductive not because it is false but because it
needs an AUXILIARY companion invariant — a separate fact that, once also assumed
at the loop head, makes the clause preserved. Re-derive preservation BY HAND:
assume the clause at the head, symbolically execute ONE iteration's writes, and
read off the extra fact you must already know for it to still hold afterward;
that fact is the auxiliary invariant to add.
Worked example (illustrates the METHOD — your loop's variables/update differ):
a clause `r >= 0` under an update `r = r + s` is not inductive alone — stepping it
once gives `r + s >= 0`, which you can only guarantee if you ALSO know `s >= 0`;
so the auxiliary is `s >= 0` and you emit `{{s >= 0, r >= 0}}`. Apply the same
step-and-read-off reasoning to THIS loop's actual updates — do not reuse `r`/`s`.
VERIFY your candidate before emitting: substitute the post-state back into the
clause and check it holds with NO gap. If a gap remains, your auxiliary is too
WEAK — tighten it (a strict `>`/`>=k` margin is often needed, not just `>= 0`)
until the stepped clause closes exactly. A non-strict bound that leaves the step
one short is the most common mistake.
Output the FULL set: the auxiliary clause(s) you derived PLUS the original
goal-relevant clause(s) — do not drop the clause that was rejected."""


def _refine_problem(base: str, dropped) -> str:
    """Augment a refinement ``problem`` message with the auxiliary-invariant hint
    when goal-relevant clauses have been dropped as non-inductive."""
    if dropped:
        return base + _AUX_REFINE_HINT.format(
            dropped="\n".join(f"    {c}" for c in dropped))
    return base


def _reinject(new: list, dropped, reinj_set: set) -> list:
    """Re-add the remembered non-inductive (goal-relevant) clauses that a refinement
    DROPPED instead of keeping. A clause and the auxiliary that makes it inductive
    must be in the set TOGETHER (``x>=y`` is only preserved alongside ``x>=1``); the
    LLM, asked to strengthen, often emits the auxiliary but forgets to re-state the
    original clause, so the two never co-occur and the loop oscillates. Re-injecting
    pairs them deterministically. Each re-injected clause is recorded so that if it
    STILL fails next iteration (no auxiliary actually rescues it ⇒ it was false), the
    caller gives up on it rather than re-injecting forever."""
    out = list(new)
    for c in (dropped or []):
        if c not in out:
            out.append(c)
            reinj_set.add(c)
    return out


# A plain (non-compound) assignment to a loop-carried scalar: `<var> = <rhs>;`.
# The leading anchor (line/brace/`;`) rejects a declaration's initializer
# (`int t1 = x;` — `int` is consumed as the var, then the `=` check fails) and the
# `[^=]` after `=` rejects the `==` comparison. The anchor is a ZERO-WIDTH lookbehind
# so a statement's trailing `;` still serves as the NEXT statement's anchor (two
# adjacent assignments `x=E; y=E;` must both match).
_PLAIN_ASSIGN_RX = re.compile(
    r"(?:(?<=[;{}\n])|^)\s*([A-Za-z_]\w*)\s*=\s*([^=][^;]*);")


def equal_update_invariants(lp) -> list:
    """Relational EQUALITY invariants for a loop that updates two or more loop-
    carried scalars to the SAME value in one iteration (``x = E; y = E;`` ⇒
    ``x == y``). These are inductive BEHAVIORAL facts a particular goal usually does
    not force (the dual of the goal-minimal set) — so the caller adds each only if
    the augmented invariant set STILL verifies, i.e. the equality is established at
    entry and preserved. Returns DSL equality strings; never claims, only proposes."""
    body = lp.body or ""
    scalars, _arrays = modified_vars(body)
    sset = set(scalars)
    groups: dict = {}
    for m in _PLAIN_ASSIGN_RX.finditer(body):
        var, rhs = m.group(1), re.sub(r"\s+", "", m.group(2))
        if var in sset:
            groups.setdefault(rhs, []).append(var)
    out = []
    for _rhs, vs in groups.items():
        uniq = list(dict.fromkeys(vs))          # de-dup, keep first-seen order
        for other in uniq[1:]:                  # chain equalities to the first var
            out.append(f"{uniq[0]} == {other}")
    return out


def relational_equality_candidates(lp, max_scalars: int = 6) -> list:
    """Candidate equality invariants ``a == b`` for EVERY pair of loop-carried
    scalars — independent of HOW each variable is updated. The caller verification-
    gates each (kept iff the augmented set still verifies = established + preserved),
    so this proposes broadly and the prover decides which hold. Being update-shape-
    agnostic, it catches BOTH ``x=E; y=E;`` and lockstep ``i=i+1; j=j+1;`` (which a
    syntactic same-RHS match misses). Pairs whose updates share an RHS are ordered
    FIRST (most likely to hold → fast wins); capped at ``max_scalars`` to bound the
    number of prover calls. NOT tied to any specific program or variable name."""
    scalars = list(dict.fromkeys(modified_vars(lp.body or "")[0]))
    if len(scalars) < 2 or len(scalars) > max_scalars:
        return []
    likely = {frozenset(c.split(" == ")) for c in equal_update_invariants(lp)}
    pairs = [f"{scalars[i]} == {scalars[j]}"
             for i in range(len(scalars)) for j in range(i + 1, len(scalars))]
    pairs.sort(key=lambda c: frozenset(c.split(" == ")) not in likely)
    return pairs


def _prep_goals_acsl(source: str) -> str:
    """For the Frama-C oracle: express every goal as an ACSL ``//@ assert`` (WP
    proves those natively). ``//@ assert`` stays; the executable forms
    (assert / static_assert / __VERIFIER_assert / europa_assert) are rewritten to
    ``/*@ assert E; */`` and their call (incl. trailing ``;``) consumed.  SV-COMP /
    Europa-style benchmark helper calls that have no Frama-C semantics are removed:
    they are input-generation or candidate-invariant hints, not proof obligations."""
    from bmc_agent.assert_driven_specs import _balanced_arg, _strip_assert_message
    rx = re.compile(
        r"\b(?:__VERIFIER_assert|europa_assert|static_assert|_Static_assert|assert)\s*\(",
        re.IGNORECASE,
    )
    # ACSL asserts already written as //@ assert(...) or /*@ assert ... */ are
    # native WP goals -- leave them intact. Re-wrapping them produced the
    # malformed `//@ /*@ assert ...; */` (Frama-C parse error -> 0/0 goals).
    _acsl_spans = [(mm.start(), mm.end()) for mm in re.finditer(r"/\*@.*?\*/", source, re.S)]
    _acsl_spans += [(mm.start(), mm.end()) for mm in re.finditer(r"//@[^\n]*", source)]
    def _in_acsl(pos):
        return any(a <= pos < b for a, b in _acsl_spans)
    out, i = [], 0
    while True:
        m = rx.search(source, i)
        if not m:
            out.append(source[i:]); break
        if _in_acsl(m.start()):
            out.append(source[i:m.end()]); i = m.end(); continue
        out.append(source[i:m.start()])
        arg, after = _balanced_arg(source, m.end() - 1)
        out.append(f"/*@ assert {_strip_assert_message(arg)}; */")
        j = after
        while j < len(source) and source[j] in " \t":
            j += 1
        if j < len(source) and source[j] == ";":
            j += 1
        i = j
    source = "".join(out)

    source = re.sub(
        r"^[ \t]*[A-Za-z_]\w*\s*=\s*__VERIFIER_nondet_[A-Za-z0-9_]*\s*\([^;]*\)\s*;\s*\n?",
        "",
        source,
        flags=re.MULTILINE,
    )

    helper_rx = re.compile(
        r"\b(?:europa_make_symbolic|europa_invariant|europa_assume|"
        r"__VERIFIER_assume|assume)\s*\(",
        re.IGNORECASE,
    )
    out, i = [], 0
    while True:
        m = helper_rx.search(source, i)
        if not m:
            out.append(source[i:]); break
        out.append(source[i:m.start()])
        _arg, after = _balanced_arg(source, m.end() - 1)
        j = after
        while j < len(source) and source[j] in " \t":
            j += 1
        if j < len(source) and source[j] == ";":
            j += 1
        i = j
    return "".join(out)


def _loop_assigns(lp) -> str:
    """Best-effort ACSL ``loop assigns`` (frame) for a loop: modified scalars plus
    each modified array as ``arr[..]``. WP needs the frame to prove preservation.

    The scan covers the body AND, for a ``for`` loop, the header's init/increment
    clauses — the loop COUNTER is updated there (``i++``), not in the body. Omitting
    it makes the frame unsound (WP assumes ``i`` is unchanged while the loop mutates
    it), so preservation of ``i <= N`` and the whole goal fail. Frama-C also expects
    a counter declared in the init (``for (int i = ...``) to be listed in the loop
    frame; AutoSpec's verified annotations do this as well."""
    scan = lp.body
    header_scalars = []
    if getattr(lp, "kind", "") == "for":
        parts = (lp.guard or "").split(";")
        init = parts[0] if parts else ""
        incr = parts[2] if len(parts) > 2 else ""
        for rx in (
            r"(?:\+\+\s*([A-Za-z_]\w*)|([A-Za-z_]\w*)\s*\+\+)",
            r"\b([A-Za-z_]\w*)\s*(?:[-+*/%]?=|<<=|>>=)",
        ):
            for m in re.finditer(rx, incr):
                name = next((g for g in m.groups() if g), "")
                if name and name not in header_scalars:
                    header_scalars.append(name)
        scan = f"{lp.body}\n{init};\n{incr};"
    scalars, arrays = modified_vars(scan)
    scalars = list(dict.fromkeys(header_scalars + scalars))
    return ", ".join(scalars + [f"{a}[..]" for a in arrays])


# --- accumulator-loop recognition → recursive-logic-function synthesis --------
#
# A loop that folds an array into a scalar (`sum = sum + a[p]`, `prod *= a[i]`)
# has a GENERAL invariant `acc == Fn(a, 0, idx)` where `Fn` is a user-defined
# recursive logic function. The built-in ACSL `\sum`/`\product` aggregate is the
# obvious form but is NOT auto-dischargeable — WP renders it to opaque quantified
# axioms no SMT prover (Alt-Ergo/Z3/CVC5) unfolds, so even the empty-aggregate
# base case times out. A recursive logic function with an explicit `reads` clause
# IS dischargeable: its preservation is a single axiom application (the step
# axiom at k=idx+1), not an induction. So instead of leaning on the LLM (which
# emits the unprovable aggregate, or a bound-specific per-index ladder), we
# DETECT the accumulator mechanically and SYNTHESIZE the axiomatic + invariant.

@dataclass
class AccumulatorSpec:
    loop_ord: int
    acc: str            # accumulator scalar, e.g. "sum"
    kind: str           # "sum" | "product"
    array: str          # folded array/pointer, e.g. "a"
    index: str          # loop counter, e.g. "p"
    elem_type: str      # array element type, e.g. "int"
    bound: str          # loop upper bound from the guard, e.g. "n"
    fn: str             # synthesized logic-function name

    @property
    def identity(self) -> str:
        return "0" if self.kind == "sum" else "1"

    @property
    def op(self) -> str:
        return "+" if self.kind == "sum" else "*"


@dataclass
class ArrayMapSpec:
    loop_ord: int
    fn: str
    array: str
    index: str
    bound: str
    value_at_k: str


@dataclass
class ConditionalArraySetSpec:
    loop_ord: int
    fn: str
    array: str
    index: str
    bound: str
    condition_at_k: str
    value_at_k: str


@dataclass
class ArrayScanSpec:
    loop_ord: int
    fn: str
    arrays: tuple[str, ...]
    qvar: str
    index: str
    bound: str
    condition_at_k: str
    negated_condition_at_k: str
    early_return: str
    default_return: str
    kind: str              # "bool_present" | "bool_all" | "index_find"


@dataclass
class ArrayMaxSpec:
    loop_ord: int
    fn: str
    array: str
    qvar: str
    index: str
    bound: str
    max_var: str
    start: str


@dataclass
class ConditionalCountSpec:
    loop_ord: int
    fn: str
    array: str
    index: str
    bound: str
    condition_at_k: str
    count_var: str
    out_ptr: str
    addend: str


@dataclass
class CountdownCounterSpec:
    loop_ord: int
    fn: str
    counter: str
    result_var: str
    input_var: str


_SCALAR_TYPES = {"int", "char", "short", "long", "unsigned", "signed",
                 "float", "double", "size_t"}


def _elem_type_of(source: str, var: str) -> str:
    """Element type of array/pointer ``var`` from its declaration (`int *a` /
    `int a[..]`); defaults to ``int`` when not confidently found."""
    for rx in (rf"\b([A-Za-z_]\w*)\s*\*\s*{re.escape(var)}\b",
               rf"\b([A-Za-z_]\w*)\s+{re.escape(var)}\s*\["):
        m = re.search(rx, source)
        if m and m.group(1) in _SCALAR_TYPES:
            return m.group(1)
    return "int"


def _guard_index_upper(lp, idx: str):
    """Upper-bound expression for ``idx`` from the loop guard (`idx < N` / `idx <=
    N`), or None. For a ``for`` loop the condition is the middle ``;`` clause."""
    cond = lp.guard or ""
    if getattr(lp, "kind", "") == "for":
        parts = cond.split(";")
        cond = parts[1] if len(parts) > 1 else ""
    m = re.search(rf"\b{re.escape(idx)}\s*<=?\s*(.+?)\s*$", cond.strip())
    return m.group(1).strip() if m else None


def detect_accumulator(lp, source: str):
    """Recognize a folding loop `acc = acc OP arr[idx]` / `acc OP= arr[idx]` whose
    counter `idx` starts at 0 and `acc` at the fold identity. Returns an
    AccumulatorSpec, or None when the pattern (or the 0/identity init reaching the
    loop) isn't clearly present — in which case synthesis safely declines and the
    LLM path is used."""
    body = lp.body or ""
    scalars, _arrays = modified_vars(body)
    for kind, op in (("sum", r"\+"), ("product", r"\*")):
        m = re.search(
            rf"\b([A-Za-z_]\w*)\s*=\s*\1\s*{op}\s*([A-Za-z_]\w*)\s*\[\s*([A-Za-z_]\w*)\s*\]",
            body) or re.search(
            rf"\b([A-Za-z_]\w*)\s*{op}=\s*([A-Za-z_]\w*)\s*\[\s*([A-Za-z_]\w*)\s*\]",
            body)
        if not m:
            continue
        acc, arr, idx = m.group(1), m.group(2), m.group(3)
        # idx must be the loop counter (advanced in the loop); acc the fold target.
        if idx not in scalars or acc not in scalars or idx == acc:
            continue
        bound = _guard_index_upper(lp, idx)
        if not bound:
            continue
        identity = "0" if kind == "sum" else "1"
        pre = source[:getattr(lp, "start_offset", 0)]
        # the invariant `acc == Fn(arr,0,idx)` is only sound if idx==0 & acc==id
        # hold on loop entry; require a reaching initializer for each.
        if not re.search(rf"\b{re.escape(idx)}\s*=\s*0\b", pre):
            continue
        if not re.search(rf"\b{re.escape(acc)}\s*=\s*{identity}\b", pre):
            continue
        fn = f"AccFold_{kind}_{acc}"
        return AccumulatorSpec(lp.ordinal, acc, kind, arr, idx,
                               _elem_type_of(source, arr), bound, fn)
    return None


def accumulator_axiomatic(spec: "AccumulatorSpec") -> str:
    """The recursive-logic-function definition for an accumulator: `Fn(a,m,k)` =
    fold of a[m..k-1]. `reads a[m..k-1]` is REQUIRED — it frames the function so WP
    can discharge preservation by one step-axiom application. Definitional axioms
    (admitted) generate no proof goals, so they don't perturb invariant numbering."""
    p, a, fn = spec.elem_type, spec.array, spec.fn
    return (
        f"/*@ axiomatic {fn}_ax {{\n"
        f"  logic integer {fn}({p} *{a}, integer m, integer k) reads {a}[m .. k-1];\n"
        f"  axiom {fn}_empty: \\forall {p} *{a}, integer m, k;\n"
        f"    m >= k ==> {fn}({a}, m, k) == {spec.identity};\n"
        f"  axiom {fn}_step:  \\forall {p} *{a}, integer m, k;\n"
        f"    m < k ==> {fn}({a}, m, k) == {fn}({a}, m, k-1) {spec.op} {a}[k-1];\n"
        f"}} */\n"
    )


def accumulator_invariants(spec: "AccumulatorSpec") -> list:
    """Deterministic, general invariant set for an accumulator loop: the index
    bounds plus the recursive-logic-function summary."""
    return [f"0 <= {spec.index}",
            f"{spec.index} <= {spec.bound}",
            f"{spec.acc} == {spec.fn}({spec.array}, 0, {spec.index})"]


def accumulator_specs(source: str, loops: list) -> dict:
    """Map of loop ordinal -> AccumulatorSpec for every loop recognized as an
    array fold."""
    out = {}
    for lp in loops:
        spec = detect_accumulator(lp, source)
        if spec:
            out[lp.ordinal] = spec
    return out


def overflow_safe_accumulators(source: str, loops: list, math_ints: bool) -> dict:
    """Loop ordinal -> AccumulatorSpec, but ONLY when an overflow-rigorous proof is
    appropriate: mathematical-integer mode is on (the bench preset) AND every loop in
    the program is a recognized array fold. In that case the no-overflow precondition
    + stepping-stone asserts + loop variant make the spec sound with RTE on (machine-
    and math-int semantics coincide). A program that also has a general (non-fold)
    loop is left on the math-int/RTE-off path, where a per-prefix overflow bound can't
    be expressed mechanically — returns {} so the caller keeps current behavior."""
    if not math_ints or not loops:
        return {}
    specs = accumulator_specs(source, loops)
    return specs if len(specs) == len(loops) else {}


def inject_overflow_asserts(source: str, acc_specs: dict) -> str:
    """Insert the per-fold stepping-stone overflow assertions at the top of each
    accumulator loop body. Edits are applied in descending offset order so earlier
    insertions don't shift later loops' offsets."""
    if not acc_specs:
        return source
    by_ord = {lp.ordinal: lp for lp in find_loops(source)}
    edits = []
    for ordn, spec in acc_specs.items():
        lp = by_ord.get(ordn)
        if lp is None:
            continue
        edits.append((lp.head_offset, "\n" + accumulator_overflow_asserts(spec)))
    out = source
    for off, text in sorted(edits, key=lambda e: -e[0]):
        out = out[:off] + text + out[off:]
    return out


def _function_returns_var(source: str, fn: str, var: str) -> bool:
    """True iff ``fn``'s body has a ``return <var>;`` — i.e. the function's result
    IS the accumulator, so an ``ensures \\result == Fn(...)`` contract is meaningful."""
    m = re.search(rf"\b{re.escape(fn)}\s*\([^;{{)]*\)\s*{{", source)
    if not m:
        return False
    open_brace = source.index("{", m.end() - 1)
    close = _matching_brace(source, open_brace)
    if close < 0:
        return False
    body = source[open_brace + 1:close]
    return re.search(rf"\breturn\s+{re.escape(var)}\s*;", body) is not None


def accumulator_contract_acsl(spec: "AccumulatorSpec",
                              overflow_safe: bool = False) -> str:
    """The function contract for an accumulator-folding function: it reads the
    array (\\valid_read), has no side effect (assigns \\nothing), and returns the
    fold over the whole range (\\result == Fn(a, 0, bound)). Bridges a caller's
    goal to the callee MODULARLY (no inlining), which is the clean general spec.

    ``overflow_safe`` adds the precondition that EVERY partial fold stays within
    the element type's machine range, so the C accumulation ``acc OP= a[i]`` cannot
    overflow (signed overflow is UB). Without it the ACSL fold is mathematical
    while the implementation may wrap — the contract is then only sound under
    mathematical-integer semantics. With it, machine- and math-int semantics
    coincide and the contract is provable with RTE (``-wp-rte``) on."""
    a, b, fn = spec.array, spec.bound, spec.fn
    lines = [
        f"  requires {b} >= 0;",
        f"  requires \\valid_read({a} + (0 .. {b}-1));",
    ]
    if overflow_safe:
        lo, hi = _type_bounds(spec.elem_type)
        lines.append(
            f"  requires \\forall integer k; 0 <= k <= {b} ==>\n"
            f"             {lo} <= {fn}({a}, 0, k) <= {hi};")
    lines += [
        "  assigns \\nothing;",
        f"  ensures \\result == {fn}({a}, 0, {b});",
    ]
    return "/*@\n" + "\n".join(lines) + "\n*/\n"


# element-type -> (ACSL min, max) macro pair for the no-overflow precondition.
# Mathematical bounds expressed as <limits.h> macros (requires the header).
_TYPE_BOUNDS = {
    "int":      ("INT_MIN", "INT_MAX"),
    "short":    ("SHRT_MIN", "SHRT_MAX"),
    "long":     ("LONG_MIN", "LONG_MAX"),
    "char":     ("CHAR_MIN", "CHAR_MAX"),
}


def _type_bounds(elem_type: str) -> tuple:
    """(min, max) <limits.h> macros for a signed accumulator's element type;
    defaults to the int range when the type isn't a known signed integer."""
    return _TYPE_BOUNDS.get(elem_type, ("INT_MIN", "INT_MAX"))


def accumulator_variant(spec: "AccumulatorSpec") -> str:
    """ACSL ``loop variant`` for a counting accumulator: ``bound - index`` is
    non-negative (index <= bound) and strictly decreases each iteration, proving
    termination."""
    return f"{spec.bound} - {spec.index}"


def accumulator_overflow_asserts(spec: "AccumulatorSpec") -> str:
    """In-body ACSL assertions that let WP discharge the RTE signed-overflow check
    on the fold step ``acc OP a[idx]``. WP will not, on its own, rewrite that
    expression to ``Fn(a, 0, idx+1)`` (one step-axiom application) and then bound it
    via the contract's per-prefix precondition — so we state both stepping stones
    explicitly. Inserted at the top of the loop body, before the fold."""
    fn, a, idx, acc, op = spec.fn, spec.array, spec.index, spec.acc, spec.op
    lo, hi = _type_bounds(spec.elem_type)
    return (
        f"        //@ assert {fn}({a}, 0, {idx}+1) == {acc} {op} {a}[{idx}];\n"
        f"        //@ assert {lo} <= {acc} {op} {a}[{idx}] <= {hi};\n"
    )


def accumulator_contracts(source: str, loops: list, entry: str,
                          overflow_safe: bool = False) -> dict:
    """Map fn-name -> contract ACSL for each NON-entry function that folds an array
    into its return value and is side-effect-free. Such a function gets a modular
    contract (so the caller's goal is discharged WITHOUT inlining). Functions that
    aren't pure, don't return the accumulator, or are the entry are skipped.

    ``overflow_safe`` adds the no-overflow precondition to each contract (see
    ``accumulator_contract_acsl``)."""
    from bmc_agent import frama_c
    out = {}
    by_ord = {lp.ordinal: lp for lp in loops}
    for ordn, spec in accumulator_specs(source, loops).items():
        lp = by_ord[ordn]
        fn = _enclosing_function(source, lp.start_offset)
        if (fn and fn != entry
                and _function_returns_var(source, fn, spec.acc)
                and frama_c.function_assigns_nothing(source, fn)):
            out[fn] = accumulator_contract_acsl(spec, overflow_safe)
    return out


def _loop_counter_bound(lp: LoopSite, fn_src: str) -> tuple[str, str] | None:
    """Recognize simple 0..bound counting loops."""
    if lp.kind == "for":
        parts = [p.strip() for p in (lp.guard or "").split(";")]
        if len(parts) != 3:
            return None
        init, cond, inc = parts
        m = re.search(r"(?:\b[A-Za-z_]\w*\s+)*\b([A-Za-z_]\w*)\s*=\s*0\b", init)
        if not m:
            return None
        idx = m.group(1)
        m = re.fullmatch(rf"{re.escape(idx)}\s*<\s*([A-Za-z_]\w*|\d+)", cond)
        if not m:
            return None
        bound = m.group(1)
        if not re.search(rf"(?:\+\+\s*{re.escape(idx)}|{re.escape(idx)}\s*\+\+|"
                         rf"{re.escape(idx)}\s*=\s*{re.escape(idx)}\s*\+\s*1|"
                         rf"{re.escape(idx)}\s*\+=\s*1)", inc):
            return None
        return idx, bound

    if lp.kind == "while":
        m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*<\s*([A-Za-z_]\w*|\d+)\s*", lp.guard or "")
        if not m:
            return None
        idx, bound = m.group(1), m.group(2)
        prefix = fn_src.split(lp.body, 1)[0]
        if not re.search(rf"\b{re.escape(idx)}\s*=\s*0\s*;", prefix):
            return None
        if not re.search(rf"(?:\+\+\s*{re.escape(idx)}|{re.escape(idx)}\s*\+\+|"
                         rf"{re.escape(idx)}\s*=\s*{re.escape(idx)}\s*\+\s*1|"
                         rf"{re.escape(idx)}\s*\+=\s*1)\s*;", lp.body):
            return None
        return idx, bound
    return None


def _array_map_rhs_at_k(array: str, idx: str, rhs: str) -> str | None:
    rhs = rhs.strip()
    arr_i = rf"{re.escape(array)}\s*\[\s*{re.escape(idx)}\s*\]"
    patterns = [
        (rf"^{arr_i}\s*\+\s*(.+)$", rf"\at({array}[k], Pre) + {{}}"),
        (rf"^(.+)\s*\+\s*{arr_i}$", rf"{{}} + \at({array}[k], Pre)"),
        (rf"^{arr_i}\s*\*\s*(.+)$", rf"\at({array}[k], Pre) * {{}}"),
        (rf"^(.+)\s*\*\s*{arr_i}$", rf"{{}} * \at({array}[k], Pre)"),
        (rf"^{re.escape(idx)}\s*\*\s*(.+)$", "k * {}"),
        (rf"^(.+)\s*\*\s*{re.escape(idx)}$", "{} * k"),
        (rf"^{re.escape(idx)}\s*\+\s*(.+)$", "k + {}"),
        (rf"^(.+)\s*\+\s*{re.escape(idx)}$", "{} + k"),
    ]
    for rx, tmpl in patterns:
        m = re.match(rx, rhs)
        if m:
            other = m.group(1).strip()
            if re.search(r"\b[A-Za-z_]\w*\s*\(", other):
                return None
            return tmpl.format(other)
    return None


def detect_array_map(lp: LoopSite, source: str) -> ArrayMapSpec | None:
    """Detect simple array-map loops and synthesize an AutoSpec-style contract.

    Covers loops such as ``a[i] = a[i] + c`` and ``a[p] = a[p] * 2``. These are a
    common ACSL benchmark shape where the caller proof needs both a function
    contract and a loop invariant; a bare loop invariant is not enough modularly.
    """
    fn = _enclosing_function(source, lp.start_offset)
    fn_src = _enclosing_function_source(source, lp.start_offset) or source
    if not fn:
        return None
    counted = _loop_counter_bound(lp, fn_src)
    if not counted:
        return None
    idx, bound = counted
    assign_rx = re.compile(
        rf"\b([A-Za-z_]\w*)\s*\[\s*{re.escape(idx)}\s*\]\s*=\s*([^;]+);")
    matches = assign_rx.findall(lp.body)
    if len(matches) != 1:
        return None
    array, rhs = matches[0]
    value_at_k = _array_map_rhs_at_k(array, idx, rhs)
    if not value_at_k:
        return None
    return ArrayMapSpec(lp.ordinal, fn, array, idx, bound, value_at_k)


def array_map_specs(source: str, loops: list) -> dict:
    out = {}
    for lp in loops:
        spec = detect_array_map(lp, source)
        if spec:
            out[lp.ordinal] = spec
    return out


def array_map_invariants(spec: ArrayMapSpec) -> list[str]:
    a, i, b, val = spec.array, spec.index, spec.bound, spec.value_at_k
    return [
        f"0 <= {i} <= {b}",
        f"forall k : 0 <= k < {i} ==> {a}[k] == {val}",
        f"forall k : {i} <= k < {b} ==> {a}[k] == \\at({a}[k], Pre)",
    ]


def array_map_contract_acsl(spec: ArrayMapSpec) -> str:
    a, b, val = spec.array, spec.bound, spec.value_at_k
    lines = [
        f"  requires {b} >= 0;",
        f"  requires \\valid({a} + (0 .. {b}-1));",
        f"  assigns {a}[0 .. {b}-1];",
        f"  ensures \\forall integer k; 0 <= k < {b} ==> {a}[k] == {val};",
    ]
    return "/*@\n" + "\n".join(lines) + "\n*/\n"


def array_map_loop_assigns(spec: ArrayMapSpec) -> str:
    return f"{spec.index}, {spec.array}[0 .. {spec.bound}-1]"


def array_map_contracts(source: str, loops: list, entry: str) -> dict:
    out = {}
    for spec in array_map_specs(source, loops).values():
        if spec.fn and spec.fn != entry:
            out[spec.fn] = array_map_contract_acsl(spec)
    return out


def _expr_at_k(expr: str, idx: str, array: str | None = None,
               quant_var: str = "k") -> str | None:
    """Translate a simple C expression over the loop counter to ACSL over ``k``.

    This intentionally accepts only side-effect-free scalar expressions. It is used
    for deterministic array-update patterns, not as a general C-to-ACSL converter.
    """
    expr = expr.strip()
    if re.search(r"\b[A-Za-z_]\w*\s*\(", expr):
        return None
    if array:
        expr = re.sub(
            rf"\b{re.escape(array)}\s*\[\s*{re.escape(idx)}\s*\]",
            rf"\\at({array}[{quant_var}], Pre)",
            expr,
        )
    return re.sub(rf"\b{re.escape(idx)}\b", quant_var, expr)


def detect_conditional_array_set(lp: LoopSite, source: str) -> ConditionalArraySetSpec | None:
    """Detect simple conditional array writes, e.g. ``if (i % 2 == 0) a[i] = 0``.

    The generated spec states only what the branch guarantees for matching
    indices; it does not claim non-matching elements are unchanged unless the
    benchmark needs and proves such a property through the frame.
    """
    fn = _enclosing_function(source, lp.start_offset)
    fn_src = _enclosing_function_source(source, lp.start_offset) or source
    if not fn:
        return None
    counted = _loop_counter_bound(lp, fn_src)
    if not counted:
        return None
    idx, bound = counted
    if_rx = re.compile(
        rf"\bif\s*\(([^()]+)\)\s*(?:\{{\s*)?"
        rf"([A-Za-z_]\w*)\s*\[\s*{re.escape(idx)}\s*\]\s*=\s*([^;]+);",
        re.S,
    )
    matches = if_rx.findall(lp.body or "")
    if len(matches) != 1:
        return None
    condition, array, rhs = matches[0]
    if len(re.findall(rf"\b[A-Za-z_]\w*\s*\[\s*{re.escape(idx)}\s*\]\s*=", lp.body or "")) != 1:
        return None
    condition_at_k = _expr_at_k(condition, idx)
    value_at_k = _expr_at_k(rhs, idx, array)
    if not condition_at_k or not value_at_k:
        return None
    return ConditionalArraySetSpec(lp.ordinal, fn, array, idx, bound,
                                   condition_at_k, value_at_k)


def conditional_array_set_specs(source: str, loops: list) -> dict:
    out = {}
    for lp in loops:
        spec = detect_conditional_array_set(lp, source)
        if spec:
            out[lp.ordinal] = spec
    return out


def conditional_array_set_invariants(spec: ConditionalArraySetSpec) -> list[str]:
    a, i, b, cond, val = (
        spec.array, spec.index, spec.bound, spec.condition_at_k, spec.value_at_k)
    return [
        f"0 <= {i} <= {b}",
        f"forall k : 0 <= k < {i} && ({cond}) ==> {a}[k] == {val}",
    ]


def conditional_array_set_contract_acsl(spec: ConditionalArraySetSpec) -> str:
    a, b, cond, val = spec.array, spec.bound, spec.condition_at_k, spec.value_at_k
    lines = [
        f"  requires {b} >= 0;",
        f"  requires \\valid({a} + (0 .. {b}-1));",
        f"  assigns {a}[0 .. {b}-1];",
        f"  ensures \\forall integer k; 0 <= k < {b} && ({cond}) ==> {a}[k] == {val};",
    ]
    return "/*@\n" + "\n".join(lines) + "\n*/\n"


def conditional_array_set_loop_assigns(spec: ConditionalArraySetSpec) -> str:
    return f"{spec.index}, {spec.array}[0 .. {spec.bound}-1]"


def conditional_array_set_contracts(source: str, loops: list, entry: str) -> dict:
    out = {}
    for spec in conditional_array_set_specs(source, loops).values():
        if spec.fn and spec.fn != entry:
            out[spec.fn] = conditional_array_set_contract_acsl(spec)
    return out


def _norm_return_expr(expr: str) -> str:
    expr = re.sub(r"\s+", "", (expr or "").strip())
    while expr.startswith("(") and expr.endswith(")"):
        inner = expr[1:-1].strip()
        if not inner:
            break
        expr = re.sub(r"\s+", "", inner)
    return expr


def _array_refs_for_index(expr: str, idx: str) -> tuple[str, ...]:
    refs: list[str] = []
    for m in re.finditer(rf"\b([A-Za-z_]\w*)\s*\[\s*{re.escape(idx)}\s*\]", expr or ""):
        name = m.group(1)
        if name not in refs:
            refs.append(name)
    return tuple(refs)


def _negate_simple_condition(cond: str) -> str | None:
    cond = (cond or "").strip()
    if "&&" in cond or "||" in cond:
        return None
    m = re.fullmatch(r"(.+?)\s*([!=]=)\s*(.+)", cond)
    if m:
        lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()
        return f"{lhs} {'!=' if op == '==' else '=='} {rhs}"
    return None


def _condition_with_index(cond_at_k: str, index_expr: str) -> str:
    return re.sub(r"\bk\b", lambda _m: index_expr, cond_at_k)


def _fresh_logic_var(source: str, base: str = "k") -> str:
    used = set(re.findall(r"\b[A-Za-z_]\w*\b", source or ""))
    if base not in used:
        return base
    i = 0
    while f"{base}{i}" in used:
        i += 1
    return f"{base}{i}"


def _replace_logic_var(expr: str, qvar: str, replacement: str) -> str:
    return re.sub(rf"\b{re.escape(qvar)}\b", lambda _m: replacement, expr)


def _function_signature_and_body(source: str, fn: str) -> tuple[str, str] | None:
    for m in _FUNC_DEF_RX.finditer(source):
        name = m.group(1)
        if name != fn or name in _C_KEYWORDS:
            continue
        open_brace = source.index("{", m.end() - 1)
        close = _matching_brace(source, open_brace)
        if close < 0:
            continue
        return source[m.start():open_brace], source[open_brace + 1:close]
    return None


def _names_from_decl_list(text: str) -> set[str]:
    out: set[str] = set()
    for part in (text or "").split(","):
        lhs = part.split("=", 1)[0].strip()
        lhs = re.sub(r"\[[^\]]*\]", " ", lhs)
        ids = re.findall(r"[A-Za-z_]\w*", lhs)
        if ids:
            out.add(ids[-1])
    return out


def _function_param_names(source: str, fn: str) -> set[str]:
    sig_body = _function_signature_and_body(source, fn)
    if not sig_body:
        return set()
    sig, _body = sig_body
    open_paren, close_paren = sig.find("("), sig.rfind(")")
    if open_paren < 0 or close_paren < open_paren:
        return set()
    params = sig[open_paren + 1:close_paren].strip()
    if not params or params == "void":
        return set()
    return _names_from_decl_list(params)


def _function_local_scalar_names(body: str) -> set[str]:
    type_rx = (
        r"\b(?:unsigned\s+|signed\s+)?(?:int|long\s+long|long|short|char|"
        r"size_t|u?int\d+_t|_Bool|bool|float|double)\b"
    )
    out: set[str] = set()
    for m in re.finditer(type_rx + r"\s+([^;]+);", body or ""):
        out.update(_names_from_decl_list(m.group(1)))
    return out


def _plain_scalar_writes_are_local_or_params(source: str, fn: str) -> bool:
    sig_body = _function_signature_and_body(source, fn)
    if not sig_body:
        return False
    _sig, body = sig_body
    allowed = _function_param_names(source, fn) | _function_local_scalar_names(body)
    assigned = {m.group(1) for m in _ASSIGN_RE.finditer(body or "")}
    for m in _INCDEC_RE.finditer(body or ""):
        assigned.add(m.group(1) or m.group(2))
    return all(name in allowed for name in assigned if name)


_PTR_STORE_RE = re.compile(
    r"\*\s*\(?\s*[A-Za-z_]\w*\s*\)?\s*(?:[-+*/%&|^]?=(?!=)|<<=|>>=)"
    r"|\(\s*\*\s*[A-Za-z_]\w*\s*\)\s*(?:[-+*/%&|^]?=(?!=)|<<=|>>=)"
    r"|\*\s*\(?\s*[A-Za-z_]\w*\s*\)?\s*(?:\+\+|--)"
    r"|(?:\+\+|--)\s*\*\s*\(?\s*[A-Za-z_]\w*\s*\)?")
_FIELD_STORE_RE = re.compile(
    r"\b[A-Za-z_]\w*\s*(?:->|\.)\s*[A-Za-z_]\w*\s*(?:[-+*/%&|^]?=(?!=)|<<=|>>=)"
    r"|\b[A-Za-z_]\w*\s*(?:->|\.)\s*[A-Za-z_]\w*\s*(?:\+\+|--)"
    r"|(?:\+\+|--)\s*\b[A-Za-z_]\w*\s*(?:->|\.)\s*[A-Za-z_]\w*")


def _body_has_escaping_store(body: str) -> bool:
    return bool(_ARRAYW_RE.search(body or "")
                or _PTR_STORE_RE.search(body or "")
                or _FIELD_STORE_RE.search(body or ""))


def _pure_function_frame_ok(source: str, fn: str) -> bool:
    from bmc_agent import frama_c
    sig_body = _function_signature_and_body(source, fn)
    local_pure = bool(sig_body and not _body_has_escaping_store(sig_body[1]))
    return ((frama_c.function_assigns_nothing(source, fn) or local_pure)
            and _plain_scalar_writes_are_local_or_params(source, fn))


def _function_assigns_only(source: str, fn: str, target: str) -> bool:
    from bmc_agent import frama_c
    clause = (frama_c.function_assigns_clause(source, fn) or "").replace(" ", "")
    return clause == target.replace(" ", "") and _plain_scalar_writes_are_local_or_params(
        source, fn)


def _expr_is_loop_invariant(expr: str, lp: LoopSite, extra_forbidden: set[str] | None = None) -> bool:
    expr = (expr or "").strip()
    if not expr or re.search(r"\b[A-Za-z_]\w*\s*\(", expr):
        return False
    if "[" in expr or "]" in expr or "*" in expr:
        return False
    scalars, arrays = modified_vars(lp.body or "")
    forbidden = set(scalars) | set(arrays) | set(extra_forbidden or set())
    return not any(re.search(rf"\b{re.escape(name)}\b", expr) for name in forbidden)


def detect_array_scan(lp: LoopSite, source: str) -> ArrayScanSpec | None:
    """Detect read-only array scans with an early return and a default return.

    Covers common ACSL read-only scan shapes:
      * membership: ``if (a[i] == x) return 1; ... return 0;``
      * all-pass:   ``if (a[i] != b[i]) return 0; ... return 1;``
      * find-index: ``if (a[i] == x) return i; ... return -1;``

    The loop invariant alone proves the callee body, but the caller-side target
    assertion is modular and needs a function contract. This recognizer emits
    both, while declining on writes or multiple early-return tests.
    """
    fn = _enclosing_function(source, lp.start_offset)
    fn_src = _enclosing_function_source(source, lp.start_offset) or source
    if not fn:
        return None
    if not _pure_function_frame_ok(source, fn):
        return None
    counted = _loop_counter_bound(lp, fn_src)
    if not counted:
        return None
    idx, bound = counted
    qvar = _fresh_logic_var(fn_src, "k")
    # This scan recognizer is for read-only loops. Array writes are handled by
    # array-map / conditional-array-set recognizers.
    if _ARRAYW_RE.search(lp.body or ""):
        return None
    if_rx = re.compile(
        r"\bif\s*\((.*?)\)\s*(?:\{\s*)?return\s+([^;]+);",
        re.S,
    )
    matches = if_rx.findall(lp.body or "")
    if len(matches) != 1:
        return None
    condition, early = matches[0]
    arrays = _array_refs_for_index(condition, idx)
    if not arrays:
        return None
    condition_at_k = _expr_at_k(condition, idx, quant_var=qvar)
    if not condition_at_k:
        return None
    negated = _negate_simple_condition(condition_at_k)
    if not negated:
        return None

    fn_range = _enclosing_function_range(source, lp.start_offset)
    if not fn_range:
        return None
    _name, _start, _open, end = fn_range
    suffix = source[lp.end_offset:end]
    returns = re.findall(r"\breturn\s+([^;]+);", suffix)
    if not returns:
        return None
    default = returns[-1]
    early_n, default_n = _norm_return_expr(early), _norm_return_expr(default)

    if early_n == idx and default_n == "-1":
        kind = "index_find"
    elif early_n == "1" and default_n == "0":
        kind = "bool_present"
    elif early_n == "0" and default_n == "1":
        kind = "bool_all"
    else:
        return None
    return ArrayScanSpec(lp.ordinal, fn, arrays, qvar, idx, bound, condition_at_k,
                         negated, early_n, default_n, kind)


def array_scan_specs(source: str, loops: list) -> dict:
    out = {}
    for lp in loops:
        spec = detect_array_scan(lp, source)
        if spec:
            out[lp.ordinal] = spec
    return out


def array_scan_invariants(spec: ArrayScanSpec) -> list[str]:
    return [
        f"0 <= {spec.index} <= {spec.bound}",
        f"forall {spec.qvar} : 0 <= {spec.qvar} < {spec.index} ==> "
        f"{spec.negated_condition_at_k}",
    ]


def array_scan_loop_assigns(spec: ArrayScanSpec) -> str:
    return spec.index


def array_scan_contract_acsl(spec: ArrayScanSpec) -> str:
    b = spec.bound
    lines = [
        f"  requires {b} >= 0;",
        *[f"  requires \\valid_read({a} + (0 .. {b}-1));" for a in spec.arrays],
        "  assigns \\nothing;",
    ]
    cond = spec.condition_at_k
    neg = spec.negated_condition_at_k
    if spec.kind == "bool_present":
        lines += [
            f"  ensures (\\exists integer {spec.qvar}; 0 <= {spec.qvar} < {b} && "
            f"({cond})) ==> \\result == 1;",
            f"  ensures \\result == 0 ==> (\\forall integer {spec.qvar}; "
            f"0 <= {spec.qvar} < {b} ==> {neg});",
        ]
    elif spec.kind == "bool_all":
        lines += [
            f"  ensures (\\forall integer {spec.qvar}; 0 <= {spec.qvar} < {b} ==> "
            f"{neg}) ==> \\result == 1;",
            f"  ensures \\result == 0 ==> (\\exists integer {spec.qvar}; "
            f"0 <= {spec.qvar} < {b} && ({cond}));",
        ]
    else:
        cond_at_result = _replace_logic_var(cond, spec.qvar, "\\result")
        lines += [
            f"  ensures -1 <= \\result < {b};",
            f"  ensures 0 <= \\result < {b} ==> ({cond_at_result});",
            f"  ensures 0 <= \\result < {b} ==> "
            f"(\\forall integer {spec.qvar}; 0 <= {spec.qvar} < \\result ==> {neg});",
            f"  ensures \\result == -1 ==> "
            f"(\\forall integer {spec.qvar}; 0 <= {spec.qvar} < {b} ==> {neg});",
        ]
    return "/*@\n" + "\n".join(lines) + "\n*/\n"


def array_scan_contracts(source: str, loops: list, entry: str) -> dict:
    out = {}
    for spec in array_scan_specs(source, loops).values():
        if spec.fn and spec.fn != entry:
            out[spec.fn] = array_scan_contract_acsl(spec)
    return out


def _loop_counter_bound_start(lp: LoopSite, fn_src: str) -> tuple[str, str, str] | None:
    """Recognize simple counting loops and return (index, bound, start)."""
    if lp.kind == "for":
        parts = [p.strip() for p in (lp.guard or "").split(";")]
        if len(parts) != 3:
            return None
        init, cond, inc = parts
        m = re.search(r"(?:\b[A-Za-z_]\w*\s+)*\b([A-Za-z_]\w*)\s*=\s*(0|1)\b", init)
        if not m:
            return None
        idx, start = m.group(1), m.group(2)
        m = re.fullmatch(rf"{re.escape(idx)}\s*<\s*([A-Za-z_]\w*|\d+)", cond)
        if not m:
            return None
        bound = m.group(1)
        if not re.search(rf"(?:\+\+\s*{re.escape(idx)}|{re.escape(idx)}\s*\+\+|"
                         rf"{re.escape(idx)}\s*=\s*{re.escape(idx)}\s*\+\s*1|"
                         rf"{re.escape(idx)}\s*\+=\s*1)", inc):
            return None
        return idx, bound, start

    if lp.kind == "while":
        m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*<\s*([A-Za-z_]\w*|\d+)\s*", lp.guard or "")
        if not m:
            return None
        idx, bound = m.group(1), m.group(2)
        prefix = fn_src.split(lp.body, 1)[0]
        init = re.search(rf"\b{re.escape(idx)}\s*=\s*(0|1)\s*;", prefix)
        if not init:
            return None
        if not re.search(rf"(?:\+\+\s*{re.escape(idx)}|{re.escape(idx)}\s*\+\+|"
                         rf"{re.escape(idx)}\s*=\s*{re.escape(idx)}\s*\+\s*1|"
                         rf"{re.escape(idx)}\s*\+=\s*1)\s*;", lp.body):
            return None
        return idx, bound, init.group(1)
    return None


def detect_array_max(lp: LoopSite, source: str) -> ArrayMaxSpec | None:
    """Detect a read-only max scan: initialize max from a[0], update on a[i] > max."""
    fn = _enclosing_function(source, lp.start_offset)
    fn_src = _enclosing_function_source(source, lp.start_offset) or source
    if not fn:
        return None
    if not _pure_function_frame_ok(source, fn):
        return None
    counted = _loop_counter_bound_start(lp, fn_src)
    if not counted:
        return None
    idx, bound, start = counted
    qvar = _fresh_logic_var(fn_src, "k")
    if _ARRAYW_RE.search(lp.body or ""):
        return None
    update_rx = re.compile(
        rf"\bif\s*\((.*?)\)\s*(?:\{{\s*)?"
        rf"([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\[\s*{re.escape(idx)}\s*\]\s*;",
        re.S,
    )
    matches = update_rx.findall(lp.body or "")
    if len(matches) != 1:
        return None
    condition, max_var, array = matches[0]
    cond = re.sub(r"\s+", "", condition)
    if cond not in (f"{max_var}<{array}[{idx}]", f"{array}[{idx}]>{max_var}"):
        return None
    prefix = fn_src.split(lp.body, 1)[0]
    if not re.search(rf"\b{re.escape(max_var)}\s*=\s*{re.escape(array)}\s*\[\s*0\s*\]\s*;", prefix):
        return None
    if not _function_returns_var(source, fn, max_var):
        return None
    return ArrayMaxSpec(lp.ordinal, fn, array, qvar, idx, bound, max_var, start)


def array_max_specs(source: str, loops: list) -> dict:
    out = {}
    for lp in loops:
        spec = detect_array_max(lp, source)
        if spec:
            out[lp.ordinal] = spec
    return out


def array_max_invariants(spec: ArrayMaxSpec) -> list[str]:
    i, b, a, m, q = spec.index, spec.bound, spec.array, spec.max_var, spec.qvar
    return [
        f"0 <= {i} <= {b}",
        f"forall {q} : 0 <= {q} < {i} ==> {m} >= {a}[{q}]",
    ]


def array_max_loop_assigns(spec: ArrayMaxSpec) -> str:
    return f"{spec.index}, {spec.max_var}"


def array_max_contract_acsl(spec: ArrayMaxSpec) -> str:
    a, b, q = spec.array, spec.bound, spec.qvar
    lines = [
        f"  requires {b} > 0;",
        f"  requires \\valid_read({a} + (0 .. {b}-1));",
        "  assigns \\nothing;",
        f"  ensures \\forall integer {q}; 0 <= {q} < {b} ==> \\result >= {a}[{q}];",
    ]
    return "/*@\n" + "\n".join(lines) + "\n*/\n"


def array_max_contracts(source: str, loops: list, entry: str) -> dict:
    out = {}
    for spec in array_max_specs(source, loops).values():
        if spec.fn and spec.fn != entry:
            out[spec.fn] = array_max_contract_acsl(spec)
    return out


def detect_conditional_count(lp: LoopSite, source: str) -> ConditionalCountSpec | None:
    """Detect conditional count/output-sum loops.

    Shape:
      count = 0; *out = 0;
      while (i < n) {
        if (a[i] == x) { count = count + 1; *out = *out + x; }
        i++;
      }
      return count;

    The useful modular contract is the relation between the returned count and
    output parameter, not an exact cardinality expression.
    """
    fn = _enclosing_function(source, lp.start_offset)
    fn_src = _enclosing_function_source(source, lp.start_offset) or source
    if not fn:
        return None
    counted = _loop_counter_bound(lp, fn_src)
    if not counted:
        return None
    idx, bound = counted
    if_matches = list(re.finditer(r"\bif\s*\(", lp.body or ""))
    if len(if_matches) != 1:
        return None
    condition, after_cond = _balanced_arg(lp.body, if_matches[0].end() - 1)
    j = after_cond
    while j < len(lp.body) and lp.body[j].isspace():
        j += 1
    if j >= len(lp.body):
        return None
    if lp.body[j] == "{":
        close = _matching_brace(lp.body, j)
        if close < 0:
            return None
        block = lp.body[j + 1:close]
    else:
        semi = lp.body.find(";", j)
        if semi < 0:
            return None
        block = lp.body[j:semi + 1]
    arrays = _array_refs_for_index(condition, idx)
    if len(arrays) != 1:
        return None
    condition_at_k = _expr_at_k(condition, idx)
    if not condition_at_k:
        return None
    count_m = re.search(
        r"\b([A-Za-z_]\w*)\s*=\s*\1\s*\+\s*1\s*;|\b([A-Za-z_]\w*)\s*\+=\s*1\s*;",
        block,
    )
    out_m = re.search(
        r"\*\s*([A-Za-z_]\w*)\s*=\s*\*\s*\1\s*\+\s*([^;]+?)\s*;"
        r"|\*\s*([A-Za-z_]\w*)\s*\+=\s*([^;]+?)\s*;",
        block,
    )
    if not count_m or not out_m:
        return None
    count_var = count_m.group(1) or count_m.group(2)
    out_ptr = out_m.group(1) or out_m.group(3)
    addend = (out_m.group(2) or out_m.group(4) or "").strip()
    if not count_var or not out_ptr or not addend:
        return None
    if not _expr_is_loop_invariant(addend, lp, {idx, count_var, out_ptr}):
        return None
    prefix = fn_src.split(lp.body, 1)[0]
    if not re.search(rf"\b{re.escape(count_var)}\s*=\s*0\s*;", prefix):
        return None
    if not re.search(rf"\*\s*{re.escape(out_ptr)}\s*=\s*0\s*;", prefix):
        return None
    if not _function_returns_var(source, fn, count_var):
        return None
    if not _function_assigns_only(source, fn, f"*{out_ptr}"):
        return None
    return ConditionalCountSpec(lp.ordinal, fn, arrays[0], idx, bound,
                                condition_at_k, count_var, out_ptr, addend)


def conditional_count_specs(source: str, loops: list) -> dict:
    out = {}
    for lp in loops:
        spec = detect_conditional_count(lp, source)
        if spec:
            out[lp.ordinal] = spec
    return out


def conditional_count_invariants(spec: ConditionalCountSpec) -> list[str]:
    return [
        f"0 <= {spec.index} <= {spec.bound}",
        f"0 <= {spec.count_var} <= {spec.index}",
        f"*{spec.out_ptr} == {spec.count_var} * {spec.addend}",
    ]


def conditional_count_loop_assigns(spec: ConditionalCountSpec) -> str:
    return f"{spec.index}, {spec.count_var}, *{spec.out_ptr}"


def conditional_count_contract_acsl(spec: ConditionalCountSpec) -> str:
    a, b, out = spec.array, spec.bound, spec.out_ptr
    lines = [
        f"  requires {b} >= 0;",
        f"  requires \\valid_read({a} + (0 .. {b}-1));",
        f"  requires \\valid({out});",
        f"  assigns *{out};",
        f"  ensures *{out} == \\result * {spec.addend};",
    ]
    return "/*@\n" + "\n".join(lines) + "\n*/\n"


def conditional_count_contracts(source: str, loops: list, entry: str) -> dict:
    out = {}
    for spec in conditional_count_specs(source, loops).values():
        if spec.fn and spec.fn != entry:
            out[spec.fn] = conditional_count_contract_acsl(spec)
    return out


def detect_countdown_counter(lp: LoopSite, source: str) -> CountdownCounterSpec | None:
    """Detect a copy-and-countdown loop returning the original non-negative input."""
    fn = _enclosing_function(source, lp.start_offset)
    fn_src = _enclosing_function_source(source, lp.start_offset) or source
    if not fn or lp.kind != "while":
        return None
    if not _pure_function_frame_ok(source, fn):
        return None
    m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*!=\s*0\s*", lp.guard or "")
    if not m:
        return None
    counter = m.group(1)
    prefix = fn_src.split(lp.body, 1)[0]
    init = re.search(rf"\b{re.escape(counter)}\s*=\s*([A-Za-z_]\w*)\s*;", prefix)
    if not init:
        return None
    input_var = init.group(1)
    zero_inits = re.findall(r"\b([A-Za-z_]\w*)\s*=\s*0\s*;", prefix)
    candidates = []
    for result_var in zero_inits:
        if result_var == counter:
            continue
        inc = re.search(rf"\b{re.escape(result_var)}\s*=\s*{re.escape(result_var)}\s*\+\s*1\s*;"
                        rf"|\b{re.escape(result_var)}\s*\+\+\s*;"
                        rf"|\b{re.escape(result_var)}\s*\+=\s*1\s*;", lp.body)
        dec = re.search(rf"\b{re.escape(counter)}\s*=\s*{re.escape(counter)}\s*-\s*1\s*;"
                        rf"|\b{re.escape(counter)}\s*--\s*;"
                        rf"|\b{re.escape(counter)}\s*-=\s*1\s*;", lp.body)
        if inc and dec:
            candidates.append(result_var)
    if len(candidates) != 1:
        return None
    result_var = candidates[0]
    if not _function_returns_var(source, fn, result_var):
        return None
    return CountdownCounterSpec(lp.ordinal, fn, counter, result_var, input_var)


def countdown_counter_specs(source: str, loops: list) -> dict:
    out = {}
    for lp in loops:
        spec = detect_countdown_counter(lp, source)
        if spec:
            out[lp.ordinal] = spec
    return out


def countdown_counter_invariants(spec: CountdownCounterSpec) -> list[str]:
    return [
        f"0 <= {spec.counter}",
        f"{spec.result_var} + {spec.counter} == {spec.input_var}",
    ]


def countdown_counter_loop_assigns(spec: CountdownCounterSpec) -> str:
    return f"{spec.counter}, {spec.result_var}"


def countdown_counter_contract_acsl(spec: CountdownCounterSpec) -> str:
    lines = [
        f"  requires {spec.input_var} >= 0;",
        "  assigns \\nothing;",
        f"  ensures \\result == {spec.input_var};",
    ]
    return "/*@\n" + "\n".join(lines) + "\n*/\n"


def countdown_counter_contracts(source: str, loops: list, entry: str) -> dict:
    out = {}
    for spec in countdown_counter_specs(source, loops).values():
        if spec.fn and spec.fn != entry:
            out[spec.fn] = countdown_counter_contract_acsl(spec)
    return out


_FUNC_DEF_RX = re.compile(
    r"(?:^|[;}\s])([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{", re.M)
# control keywords that also match name(...){ but are NOT function definitions
_C_KEYWORDS = {"if", "while", "for", "switch", "do", "else", "return",
               "sizeof", "catch"}


def _enclosing_function(source: str, offset: int) -> str:
    """Name of the function whose body brace-range tightly contains ``offset`` (or
    "" if none). Used to decide which callee a loop lives in vs the entry function."""
    best, best_open = "", -1
    for m in _FUNC_DEF_RX.finditer(source):
        name = m.group(1)
        if name in _C_KEYWORDS:
            continue
        open_brace = source.index("{", m.end() - 1)
        close = _matching_brace(source, open_brace)
        if close < 0:
            continue
        if open_brace < offset < close and open_brace > best_open:
            best, best_open = name, open_brace   # tightest enclosing def wins
    return best


def _enclosing_function_range(source: str, offset: int) -> tuple[str, int, int, int] | None:
    """(name, function-start, body-open, function-end) for the tightest function."""
    best: tuple[str, int, int, int] | None = None
    best_open = -1
    for m in _FUNC_DEF_RX.finditer(source):
        name = m.group(1)
        if name in _C_KEYWORDS:
            continue
        open_brace = source.index("{", m.end() - 1)
        close = _matching_brace(source, open_brace)
        if close < 0:
            continue
        if open_brace < offset < close and open_brace > best_open:
            line_start = source.rfind("\n", 0, m.start()) + 1
            best = (name, line_start, open_brace, close + 1)
            best_open = open_brace
    return best


def _enclosing_function_source(source: str, offset: int) -> str:
    """Source text for the tightest function whose body contains ``offset``.

    Loop-invariant prompts and scope filtering must use the loop's function, not
    the whole translation unit. Otherwise a callee loop can accidentally mention
    caller-local variables that appear elsewhere in the file; CBMC may validate
    them only in the concrete caller context, but Frama-C later rejects the ACSL
    as an unbound variable in the callee.
    """
    best_start, best_end, best_open = -1, -1, -1
    for m in _FUNC_DEF_RX.finditer(source):
        name = m.group(1)
        if name in _C_KEYWORDS:
            continue
        open_brace = source.index("{", m.end() - 1)
        close = _matching_brace(source, open_brace)
        if close < 0:
            continue
        if open_brace < offset < close and open_brace > best_open:
            line_start = source.rfind("\n", 0, m.start()) + 1
            best_start, best_end, best_open = line_start, close + 1, open_brace
    return source[best_start:best_end] if best_start >= 0 else ""


def _loop_function_callees(source: str, entry: str) -> list:
    """Functions that CONTAIN a loop and are NOT the entry — i.e. callees whose
    loop invariant must be inlined into the caller for a caller-resident goal to be
    discharged by WP (modular WP otherwise needs a separate function contract)."""
    callees = []
    for lp in find_loops(source):
        fn = _enclosing_function(source, lp.start_offset)
        if fn and fn != entry and fn not in callees:
            callees.append(fn)
    return callees


_WP_INV_GOAL_RE = re.compile(r"loop_invariant(?:_named)?_(\d+)(?:_(?:established|preserved))?",
                             re.IGNORECASE)


def _wp_failing_invariant_indices(unproved: list, annotations: dict, loops: list) -> list:
    """Map WP unproved ``loop_invariant_<N>_(established|preserved)`` goals to our
    ``(ordinal, n)`` clause coordinates so the refine loop can drop the SPECIFIC
    failing clause. N is Frama-C's 1-based, function-global, source-order index;
    we flatten our clauses the same way (loops in ordinal order, clauses in list
    order) — matching how ``insert_loop_invariants_acsl`` renders them."""
    flat = []  # flat[N-1] = (ordinal, n)
    for lp in sorted(loops, key=lambda l: l.ordinal):
        for n in range(len(annotations.get(lp.ordinal, []) or [])):
            flat.append((lp.ordinal, n))
    out = set()
    for g in unproved:
        m = _WP_INV_GOAL_RE.search(g or "")
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(flat):
                out.add(flat[idx])
    return sorted(out)


def check_loop_invariants_wp(source: str, annotations: dict, config,
                             entry: str = "main", timeout: int = 120,
                             force_rte: bool | None = None,
                             wp_timeout: int = 10) -> "LoopCheck":
    """Frama-C/WP oracle: render the invariants to ACSL, splice them before each
    loop, express goals as ACSL asserts, and run ``frama-c -wp``. Handles unbounded
    loops + mathematical-integer / aggregate invariants that CBMC cannot. Returns a
    LoopCheck (available=False inside .result when frama-c is absent).

    When a goal lives in the entry function but the loop lives in a callee, the
    callee's call sites are inlined (``run_wp(inline=...)``) so the loop invariant
    discharges the caller's goal without a separately-synthesized contract. Goals are
    judged on partial correctness (``exclude_terminates`` — we synthesize asserts, not
    loop variants), matching the CBMC oracle's bounded semantics.

    ``config.math_ints`` selects mathematical-integer semantics (IC3-style
    benchmarks: `x = x + y` in an unbounded loop never overflows). It maps to
    ``run_wp(rte=False)``: with ``-wp-rte`` WP keeps the WRAPPING machine-int VALUE
    model even when the overflow alarm is suppressed, so a textbook invariant like
    ``x >= 1`` under ``x = x + y`` is not preserved (the sum could wrap negative).
    Dropping RTE gives the unbounded-integer reasoning these invariants assume. With
    machine-int semantics (``math_ints`` off) RTE stays on (sound overflow + memory
    safety)."""
    from bmc_agent import frama_c
    math_ints = bool(getattr(config, "math_ints", False))
    loops = find_loops(source)
    assigns = {lp.ordinal: _loop_assigns(lp) for lp in loops}
    assigns.update({ordn: array_map_loop_assigns(spec)
                    for ordn, spec in array_map_specs(source, loops).items()})
    assigns.update({ordn: conditional_array_set_loop_assigns(spec)
                    for ordn, spec in conditional_array_set_specs(source, loops).items()})
    assigns.update({ordn: array_scan_loop_assigns(spec)
                    for ordn, spec in array_scan_specs(source, loops).items()})
    assigns.update({ordn: array_max_loop_assigns(spec)
                    for ordn, spec in array_max_specs(source, loops).items()})
    assigns.update({ordn: conditional_count_loop_assigns(spec)
                    for ordn, spec in conditional_count_specs(source, loops).items()})
    assigns.update({ordn: countdown_counter_loop_assigns(spec)
                    for ordn, spec in countdown_counter_specs(source, loops).items()})
    prepped = _prep_goals_acsl(source)
    # Overflow-rigorous accumulator mode: when every loop is an array fold (and
    # math-int mode is on), emit the no-overflow precondition + per-fold stepping-
    # stone asserts + loop variant and verify WITH RTE on — so signed-overflow is
    # actually checked, not assumed away. The precondition makes machine- and math-
    # int semantics coincide, keeping the AccFold invariant provable.
    ovf_specs = overflow_safe_accumulators(source, loops, math_ints)
    # ``force_rte`` overrides the math-int default — used by the machine-int
    # overflow recheck, which re-runs WP with RTE on over the SAME invariant
    # set to report whether a math-int-proved result is also machine-int sound.
    rte = force_rte if force_rte is not None else ((not math_ints) or bool(ovf_specs))
    variants = ({ordn: accumulator_variant(spec) for ordn, spec in ovf_specs.items()}
                if ovf_specs else None)
    if ovf_specs:
        prepped = inject_overflow_asserts(prepped, ovf_specs)
    inline = _loop_function_callees(prepped, entry)
    annotated = frama_c.insert_loop_invariants_acsl(prepped, annotations, assigns, variants)
    # A pure array-folding callee gets a MODULAR contract (`ensures \result ==
    # Fn(a,0,n)`), so the caller's goal is discharged through the contract rather
    # than by inlining — drop those functions from the inline set.
    contracts = accumulator_contracts(source, loops, entry, overflow_safe=bool(ovf_specs))
    contracts.update(array_map_contracts(source, loops, entry))
    contracts.update(conditional_array_set_contracts(source, loops, entry))
    contracts.update(array_scan_contracts(source, loops, entry))
    contracts.update(array_max_contracts(source, loops, entry))
    contracts.update(conditional_count_contracts(source, loops, entry))
    contracts.update(countdown_counter_contracts(source, loops, entry))
    for fn, block in contracts.items():
        annotated = frama_c.insert_contract_block(annotated, fn, block)
    inline = [fn for fn in inline if fn not in contracts]
    # Prepend the recursive-logic-function definition(s) for any accumulator loop
    # whose invariant references one (`acc == AccFold_*(...)`). The axiomatic must
    # precede first use; definitional axioms add no proof goals, so loop-invariant
    # numbering (used by _wp_failing_invariant_indices) is unperturbed.
    prelude = "".join(
        accumulator_axiomatic(s) for s in accumulator_specs(source, loops).values())
    # The no-overflow precondition/asserts reference <limits.h> macros (INT_MIN, …).
    if ovf_specs:
        prelude = "#include <limits.h>\n" + prelude
    if prelude:
        annotated = prelude + annotated
    wp = frama_c.run_wp(annotated, getattr(config, "frama_c_path", "frama-c"), timeout,
                        inline=inline, exclude_terminates=True, rte=rte, wp_timeout=wp_timeout)
    # WP goal names: "..._loop_invariant_<N>_(established|preserved)" (validity)
    # vs "...assert..." (adequacy). N is Frama-C's 1-based, function-global,
    # source-order index of loop invariants.
    inv_failed = any("invariant" in g.lower() for g in wp.unproved)
    goal_failed = any("assert" in g.lower() for g in wp.unproved) or (
        not wp.proved and not inv_failed)
    # Map each failing invariant to its (ordinal, n) so the refine loop can drop
    # the SPECIFIC bad clause (an unsound extra clause shouldn't poison an
    # otherwise-provable set). Fall back to coarse (ordinal, 0) only if WP named
    # an invariant failure we couldn't index.
    finv = _wp_failing_invariant_indices(wp.unproved, annotations, loops)
    if inv_failed and not finv:
        finv = [(lp.ordinal, 0) for lp in loops]
    return LoopCheck(verified=bool(wp.proved), failing_invariants=finv,
                     goal_failed=goal_failed, unwinding_failed=False,
                     result=wp, instrumented=annotated)


@dataclass
class LoopSynthResult:
    ok: bool
    iterations: int
    annotations: dict = field(default_factory=dict)   # ordinal -> [invariants]
    acsl: str = ""
    goals: list = field(default_factory=list)
    note: str = ""
    unwinding_failed: bool = False
    instrumented: str = ""      # the final instrumented source CBMC checked
    cbmc_log: str = ""          # raw CBMC output of the final check
    no_goals: bool = False      # no //@ assert / assert / __VERIFIER_assert → N/A, not a pass


# A real invariant line carries a relational/logical operator. Reasoning prose
# ("Wait, let me reconsider.", "The most direct way:") does not — agentic models
# interleave chain-of-thought with the answer, so the parser must reject it here
# rather than leaning on the downstream out-of-scope filter (which is noisy and
# misses prose built from in-scope identifiers).
_INV_OP_RE = re.compile(r"(==>|<==>|==|!=|<=|>=|<|>|&&|\|\||\\forall|\\exists|\\sum|forall|exists)")


def _normalize_quantifiers(expr: str) -> str:
    """Rewrite ACSL-native quantifiers ``\\forall <type> v; BODY`` into the DSL
    form ``forall v : BODY`` the pipeline expects.

    Capable models routinely answer in ACSL syntax (``\\forall int i; 0<=i<n ==>
    a[i]==i+1``) instead of the requested DSL ``forall i : ...``. Without this the
    DSL ``_FORALL`` (colon form) doesn't match, so the bound variable isn't
    recognised as quantified and the WHOLE clause is dropped by _filter_in_scope as
    "out-of-scope". Normalising at ingest keeps these (often load-bearing)
    invariants. Single-binder only; the last identifier before ``;`` is the var."""
    def repl(m):
        kw, binder, body = m.group(1).lower(), m.group(2).strip(), m.group(3)
        toks = binder.split()
        if not toks:
            return m.group(0)
        return f"{kw} {toks[-1]} : {body}"
    return re.sub(r"\\?(forall|exists)\s+([A-Za-z_][\w\s]*?)\s*;\s*(.+)",
                  repl, expr, flags=re.IGNORECASE | re.DOTALL)


def _parse_inv_lines(text: str) -> list:
    """Invariant expressions from an LLM reply: one per line, fences/bullets/
    trailing semicolons and `loop invariant` keyword stripped. ACSL-native
    quantifiers are normalised to DSL form. Lines that don't look like a
    boolean/quantified expression (no relational/logical operator, or a prose
    lead-in ending in ':') are dropped as interleaved reasoning."""
    out = []
    for raw in (text or "").splitlines():
        ln = raw.strip().strip("`").strip()
        if not ln or ln.startswith(("//", "/*", "#", "```")):
            continue
        ln = re.sub(r"^\s*(?:[-*]\s*)?(?:loop\s+invariant\s+)?", "", ln, flags=re.IGNORECASE)
        ln = ln.rstrip(";").strip()
        if not ln:
            continue
        if ln.endswith(":") or not _INV_OP_RE.search(ln):
            continue   # prose / reasoning, not an invariant
        if "..." in ln or "…" in ln:
            continue   # informal math ellipsis (a[0]+...+a[p-1]) — not valid ACSL/C
        out.append(_normalize_quantifiers(ln))
    return out


_C_KEYWORDS = {
    "int", "unsigned", "signed", "long", "short", "char", "void", "const", "static",
    "if", "else", "while", "for", "do", "return", "sizeof", "struct", "union", "enum",
    "true", "false", "size_t", "forall", "exists", "result", "_Bool", "bool", "float",
    "double", "NULL", "assert", "static_assert",
}

# Binder keywords that introduce a locally-scoped variable: the DSL `forall`/
# `exists` (no backslash) and the ACSL aggregates (`\sum` etc., backslash
# required so the program variable `sum` is never mistaken for the keyword). The
# bound name is the identifier following the keyword, skipping an optional `(`
# and a leading type (`int`/`integer`/...): matches `\sum(int k;`, `\sum k :`,
# and `forall k :` alike.
_BINDER_RE = re.compile(
    r"(?:\b(?:forall|exists)\b|\\(?:sum|product|numof|lambda|max|min)\b)"
    r"[\s(]*(?:(?:integer|int|unsigned|signed|long|short|char|size_t)\s+)*"
    r"([A-Za-z_]\w*)",
    re.IGNORECASE,
)


def _bound_vars(clause: str) -> set:
    """Variables locally bound by a quantifier or aggregate anywhere in the clause.

    Covers the DSL form (`forall k :`, `exists k :`) AND ACSL-native binders that
    capable models emit for aggregate invariants — `\\sum(int k; lo; hi; a[k])`,
    `\\sum k : lo <= k < hi : a[k]`, `\\product`, `\\numof`, `\\lambda`, `\\max`,
    `\\min`. These binders may be NESTED inside a larger expression
    (`sum == \\sum(int k; ...)`), so the top-level `_FORALL` anchor misses them and
    the bound variable looks out-of-scope. Without exempting it, the whole (often
    load-bearing) aggregate invariant is dropped, leaving only a concrete
    index-enumerated fallback."""
    return set(_BINDER_RE.findall(clause))


def _pointer_vars(source: str) -> set:
    """Best-effort pointer variable names visible in a function/source snippet."""
    ptrs = set()
    for m in _FUNC_DEF_RX.finditer(source):
        open_paren = source.find("(", m.start(), m.end())
        if open_paren < 0:
            continue
        params, _after = _balanced_arg(source, open_paren)
        for p in params.split(","):
            if "*" in p:
                ids = re.findall(r"[A-Za-z_]\w*", p)
                if ids:
                    ptrs.add(ids[-1])
    type_words = (
        r"(?:const|volatile|unsigned|signed|long|short|int|char|float|double|void|size_t|"
        r"struct\s+[A-Za-z_]\w*)"
    )
    decl_rx = re.compile(
        rf"(?:^|[;{{]\s*){type_words}(?:\s+{type_words})*\s*\*\s*([A-Za-z_]\w*)",
        re.M,
    )
    for m in decl_rx.finditer(source):
        ptrs.add(m.group(1))
    return ptrs


def _logic_functions(source: str) -> set:
    """Logic/predicate symbols declared in ACSL snippets included in ``source``."""
    return set(re.findall(r"\b(?:logic|predicate)\b[^;{]*?\b([A-Za-z_]\w*)\s*\(", source))


_ALLOWED_ACSL_CALLS = {
    "valid", "valid_read", "valid_string", "valid_range", "old", "at",
    "sum", "product", "numof", "lambda", "max", "min",
}


def _unsupported_logic_calls(clause: str, source: str) -> set:
    """Function-call syntax in ACSL must refer to declared logic symbols.

    A C call such as `pow(i)` or an undefined helper such as `power(i)` is not a
    valid logic term in ACSL. Dropping such clauses is safer than producing an
    annotation that Frama-C rejects before any proof attempt.
    """
    names = set(re.findall(r"(?<!\\)\b([A-Za-z_]\w*)\s*\(", clause))
    return names - _logic_functions(source) - _ALLOWED_ACSL_CALLS - _C_KEYWORDS


def _misused_pointer_vars(clause: str, source: str) -> set:
    """Pointer variables used as arithmetic/integer terms instead of dereferenced.

    Clauses like `r == 1` for `int *r` or `sum == count*x` for `int *sum` are
    type-invalid ACSL. `*r`, `r[i]`, `r == \null`, and validity predicates remain
    allowed.
    """
    bad = set()
    for name in _pointer_vars(source):
        n = re.escape(name)
        if re.search(rf"\\valid(?:_read)?\s*\(\s*{n}\b", clause):
            continue
        null_cmp = rf"(?:{n}\s*(?:==|!=)\s*\\null|\\null\s*(?:==|!=)\s*{n})"
        if re.search(null_cmp, clause):
            continue
        deref_or_index = rf"(?:\*\s*{n}\b|{n}\s*\[|{n}\s*->)"
        if re.search(deref_or_index, clause):
            continue
        if re.search(rf"\b{n}\b\s*(?:[+\-*/%]|<=|>=|<|>|==|!=)", clause):
            bad.add(name)
            continue
        if re.search(rf"(?:[+\-*/%]|<=|>=|<|>|==|!=)\s*\b{n}\b", clause):
            bad.add(name)
    return bad


_TAUT_SKIP = ("\\", "forall", "exists", "old(", "==>", "<==>", "\\sum", "\\product", "?")


def _norm_taut_side(expr: str) -> str:
    expr = (expr or "").strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    balanced = False
                    break
        if not balanced:
            break
        expr = expr[1:-1].strip()
    return re.sub(r"\s+", "", expr)


def _split_top_relation(expr: str):
    depth = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            for op in ("==", "!=", "<=", ">=", "<", ">"):
                if expr.startswith(op, i):
                    return expr[:i], op, expr[i + len(op):]
        i += 1
    return None


def _is_tautology(clause: str) -> bool:
    """True for syntactically vacuous comparisons such as ``x == x``.

    This must be deliberately conservative: bounds such as ``i <= 30`` may look
    true under small random samples, but they are load-bearing loop invariants.
    Do not use testing over sampled environments here; only remove identities
    whose two sides are textually the same after harmless normalization.
    """
    c = clause.strip()
    if any(t in c for t in _TAUT_SKIP):
        return False
    if not _re_taut.search(c):
        return False
    split = _split_top_relation(c)
    if not split:
        return False
    lhs, op, rhs = split
    if _norm_taut_side(lhs) != _norm_taut_side(rhs):
        return False
    return op in ("==", "<=", ">=")


_re_taut = __import__("re").compile(r"==|!=|<=|>=|<|>")
_re_ident = __import__("re").compile(r"[A-Za-z_]\w*")


_AUX_SYNTH_PROMPT = """\
The following candidate loop invariants are NOT inductive on their own -- the verifier
cannot preserve them across one iteration:
{invs}

FUNCTION:
```c
{fn_src}
```

They are missing AUXILIARY invariant(s): additional EXACT relations among the loop
variables -- most often LINEAR EQUALITIES that the loop body's ASSIGNMENTS establish
(e.g. an assignment `a = b + c;` gives the invariant `a == b + c`), or equalities that
LINK the candidates so they become mutually inductive. Examine the assignments in each
loop body and propose the missing auxiliary invariant(s) that make the candidate set
inductive. Propose BOTH (a) LINKING equalities the assignments establish (e.g. a == b + c),
AND (b) any SUPPORTING BOUNDS the failing clauses need to be discharged -- typically
nonnegativity (0 <= v) or simple ranges on the variables involved. A modular or
relational clause often cannot be proved without the relevant variables being bounded.
Output ONLY the auxiliary invariant line(s), one per line, plain C-style."""


def wp_strengthen(source: str, annotations: dict, config, llm, entry: str = "main",
                  rounds: int = 2):
    """STRENGTHENING post-processor (the dual of minimization): when the synthesized set
    is not inductive because clauses fail PRESERVATION, ask the LLM -- with a FOCUSED,
    clean prompt (the whole function + the failing candidates) -- for the AUXILIARY
    companion invariant(s) (the linking relations the body's assignments establish, e.g.
    `w == z + 1`) that make the set inductive. ADD them and re-verify with a clean,
    stable-budget WP run. Emulates autospec's complete mutually-supporting sets. Returns
    (instrumented_source, augmented_annotations) if it then verifies, else None. Sound:
    a fresh WP run must prove all goals on the augmented set."""
    from bmc_agent.llm import agentic_system_prompt
    fn_src = brace_braceless_loops(source)
    ann = {o: list(v) for o, v in (annotations or {}).items()}
    logger.info("wp_strengthen: invoked with %d loops, clauses=%s", len(ann), ann)
    if not ann:
        logger.info("wp_strengthen: no annotations to strengthen"); return None
    for _ in range(rounds):
        cur = sorted({c for invs in ann.values() for c in invs})
        prompt = _AUX_SYNTH_PROMPT.format(
            invs="\n".join(f"  {c}" for c in cur) or "  (none)", fn_src=fn_src)
        try:
            txt = llm.complete(agentic_system_prompt(config, "spec_gen", _PROPOSE_SYS),
                               prompt, max_tokens=400, role="spec_gen")
        except Exception:
            return None
        aux = _filter_in_scope(_parse_inv_lines(txt), fn_src)
        added = False
        for o in ann:
            for a in aux:
                if a not in ann[o]:
                    ann[o].append(a); added = True
        if not added:
            return None
        logger.info("wp_strengthen: aux proposed=%s", aux)
        chk = check_loop_invariants_wp(source, ann, config, entry, wp_timeout=30)
        logger.info("wp_strengthen: augmented verified=%s failing=%s goal_failed=%s", chk.verified, chk.failing_invariants, chk.goal_failed)
        if chk.verified:
            return getattr(chk, "instrumented", ""), ann
    return None


def _filter_in_scope(clauses: list, source: str) -> list:
    """Drop invariant clauses that reference identifiers not present in the program
    (LLM hallucinations like an invented loop counter `i`). An out-of-scope name
    would make the instrumented source fail to compile, so the check silently
    'fails' every iteration and never converges. Quantifier/aggregate-bound
    variables are exempt (they're locally bound)."""
    known = set(re.findall(r"[A-Za-z_]\w*", source))
    out = []
    for c in clauses:
        bad_ptrs = _misused_pointer_vars(c, source)
        if bad_ptrs:
            logger.info("loop-inv: dropping clause with pointer-as-integer %s: %r",
                        sorted(bad_ptrs), c)
            continue
        bad_calls = _unsupported_logic_calls(c, source)
        if bad_calls:
            logger.info("loop-inv: dropping clause with unsupported logic call %s: %r",
                        sorted(bad_calls), c)
            continue
        ids = (set(re.findall(r"[A-Za-z_]\w*", c)) - _bound_vars(c) -
               _C_KEYWORDS - _ALLOWED_ACSL_CALLS - known)
        if ids:
            logger.info("loop-inv: dropping clause with out-of-scope %s: %r", sorted(ids), c)
            continue
        if _is_tautology(c):
            logger.info("loop-inv: dropping tautology (vacuous clause) %r", c)
            continue
        out.append(c)
    return out


def _propose(llm, config, loop, goals, fn_src) -> list:
    from bmc_agent.llm import agentic_system_prompt
    prompt = _PROPOSE_PROMPT.format(goals="\n".join(f"  {g}" for g in goals) or "  (none)",
                                    fn_src=fn_src, kind=loop.kind, guard=loop.guard)
    txt = llm.complete(agentic_system_prompt(config, "spec_gen", _PROPOSE_SYS),
                       prompt, max_tokens=512, role="spec_gen")
    return _filter_in_scope(_parse_inv_lines(txt), fn_src)


_STRENGTHEN_GF_PROMPT = '''This loop has invariants ALREADY PROVEN inductive:
{current}

Loop ({kind}, guard `{guard}`) in context:
{fn_src}

GOAL-FREE STRENGTHENING. There is no goal to prove; capture what the loop ACTUALLY
computes as PRECISELY as possible. Propose ADDITIONAL, STRONGER invariants beyond
those above: exact linear relations among the loop variables (e.g. `a + b == c`,
`x == n - y`), closed-form values of accumulators (e.g. `s == i * (i - 1) / 2`), or
exact equalities — NOT loose bounds you already have. Each must hold on loop entry
and be preserved by one iteration. Output one ACSL predicate per line; no
`loop invariant` keyword, no comments, no prose.'''


def _propose_stronger(llm, config, loop, current, fn_src) -> list:
    """Goal-free: ask the LLM for STRONGER inductive invariants than `current`
    (exact relations / closed forms), to be validity-filtered by the caller."""
    from bmc_agent.llm import agentic_system_prompt
    prompt = _STRENGTHEN_GF_PROMPT.format(
        current="\n".join(f"  {c}" for c in current) or "  (none)",
        fn_src=fn_src, kind=loop.kind, guard=loop.guard)
    txt = llm.complete(agentic_system_prompt(config, "spec_gen", _PROPOSE_SYS),
                       prompt, max_tokens=512, role="spec_gen")
    return _filter_in_scope(_parse_inv_lines(txt), fn_src)


def _refine(llm, config, loop, current, problem, goals, fn_src) -> list:
    from bmc_agent.llm import agentic_system_prompt
    prompt = _REFINE_PROMPT.format(
        current="\n".join(f"  {c}" for c in current) or "  (none)",
        problem=problem, goals="\n".join(f"  {g}" for g in goals) or "  (none)",
        fn_src=fn_src, kind=loop.kind, guard=loop.guard)
    txt = llm.complete(agentic_system_prompt(config, "refinement", _REFINE_SYS),
                       prompt, max_tokens=512, role="refinement")
    return _filter_in_scope(_parse_inv_lines(txt), fn_src)


def _guess_unwind(loops: list, default: int) -> int:
    """Unwind past a literal trip bound (`< N` / `<= N`) found in a guard, so a
    bounded loop is fully covered; fall back to `default` otherwise."""
    best = 0
    for lp in loops:
        for mm in re.finditer(r"<=?\s*(\d+)", lp.guard):
            best = max(best, int(mm.group(1)))
    return min(max(best + 2, default), 4100) if best else default


def _has_literal_bound(loops: list) -> bool:
    """True iff every loop has a literal trip bound CBMC can unwind to (`< N`/`<= N`)."""
    return bool(loops) and all(re.search(r"<=?\s*\d+", lp.guard) for lp in loops)


def _has_array_writes(loops: list) -> bool:
    """True iff any loop body writes an array element. Array-writing loops need a
    QUANTIFIED invariant, which CBMC can only validate via loop-head-assert +
    unwinding (the havoc/assume mode's symbolic-bound `forall` is unsound). Loops
    that write only SCALARS use the havoc abstraction — bound-independent, so it
    also handles huge literal bounds (e.g. y<100000) that are intractable to unwind."""
    return any(modified_vars(lp.body)[1] for lp in loops)


# A clause is "non-behavioral" when it merely pins a concrete value rather than
# expressing a relationship maintained by the loop: `n == 5`, `a[0] == 1`,
# `len == 1024`. These are caller/input constants (true only because the call was
# inlined into a concrete context) — sound but not generalizable. Minimization
# drops these FIRST so the surviving set is the behavioral core (bounds + summary).
_NON_BEHAVIORAL_RX = re.compile(
    r"^\s*[A-Za-z_]\w*\s*(\[\s*\d+\s*\])?\s*==\s*-?\d+\s*$")


def _is_non_behavioral(clause: str) -> bool:
    return bool(_NON_BEHAVIORAL_RX.match(clause))


def _is_behavioral_core(clause: str) -> bool:
    """A clause that SUMMARIZES what the loop computes — the behavioral core worth
    keeping even when a (possibly weak) goal does not strictly need it: a quantified
    fact (``forall k; ...``), an accumulator-fold equation (``sum == AccFold(...)``),
    or an EQUALITY relating program terms (``x == y``). These characterize the loop;
    a value-pin (``n == 5``) or a one-sided bound (``i <= N``) does not. Minimization
    NEVER drops a behavioral-core clause, so a too-weak goal (e.g. ``y >= 1`` when the
    loop actually maintains ``x == y``) can't collapse the spec down to bare bounds."""
    c = clause.strip()
    if re.search(r"\\?\bforall\b|\bAccFold\w*\b", c):
        return True
    # An equality that is NOT a mere value-pin (`var == literal`) relates terms the
    # loop keeps in lockstep — the summary. (`==` only; one-sided bounds stay droppable.)
    return "==" in c and not _is_non_behavioral(c)


def _minimize_invariants(annotations: dict, check_fn, loops, logger) -> dict:
    """Greedily drop every clause that is NOT load-bearing for the proof, so the
    result is a MINIMAL, behavioral invariant set rather than a sound-but-bloated
    one (the verifier proves goals in a concrete/inlined context, so input-restating
    clauses like ``n==5`` / ``a[0]==1`` survive 'for free' — strip them). Non-
    behavioral clauses are tried first; a loop never reduces below one clause, and
    every removal is re-verified with the SAME oracle so minimization stays sound."""
    cur = {o: list(v) for o, v in annotations.items()}

    def _order(o):
        # droppable indices of THIS loop's clauses — behavioral-core clauses (the
        # loop's summary: equalities/quantified facts) are PROTECTED from removal so
        # a weak goal can't strip them; among the rest, scaffolding (value-pins) is
        # tried before bounds, each group high-index-first for stable popping
        idxs = [i for i in range(len(cur[o])) if not _is_behavioral_core(cur[o][i])]
        return sorted(idxs, key=lambda i: (not _is_non_behavioral(cur[o][i]), -i))

    changed = True
    while changed:
        changed = False
        for o in list(cur):
            for idx in _order(o):
                if len(cur[o]) <= 1:           # keep at least one invariant per loop
                    break
                trial = {oo: list(vv) for oo, vv in cur.items()}
                dropped = trial[o].pop(idx)
                if check_fn(trial).verified:
                    cur = trial
                    logger.info("loop-inv: minimized — dropped redundant clause %r", dropped)
                    changed = True
                    break                      # restart this loop's scan over the smaller set
    return cur


def _entails(rest: list, clause: str, config) -> bool:
    """True iff the conjunction of ``rest`` LOGICALLY IMPLIES ``clause`` — i.e. the
    clause is a redundant restatement (e.g. ``y>=1`` given ``x>=1`` and ``x==y``).

    A scalar CBMC query: declare every identifier as a nondet ``int``,
    ``__CPROVER_assume`` each clause in ``rest``, ``__CPROVER_assert(clause)``; valid
    (no counterexample) ⇒ entailed. Quantified / accumulator-fold / array / pointer
    clauses are OUT OF SCOPE — returns False (treat as non-redundant, keep). If CBMC
    is unavailable it returns False, so dedup degrades to keeping everything."""
    if not rest:
        return False
    blob = " ".join(rest) + " || " + clause
    if re.search(r"\\|\bforall\b|\bexists\b|AccFold|\[|\]|\*|->|\.", blob):
        return False
    ids = sorted(set(re.findall(r"[A-Za-z_]\w*", blob)) - _C_KEYWORDS)
    if not ids:
        return False
    import tempfile as _tf
    decls = "\n  ".join(f"int {i};" for i in ids)
    assumes = "\n  ".join(f"__CPROVER_assume({r});" for r in rest)
    src = (f"int main(void) {{\n  {decls}\n  {assumes}\n"
           f"  __CPROVER_assert(({clause}), \"entail\");\n  return 0;\n}}\n")
    try:
        from bmc_agent.cbmc import run_cbmc
        with _tf.NamedTemporaryFile("w", suffix=".c", delete=False) as tf:
            tf.write(src); path = tf.name
        res = run_cbmc(harness_path=path, function="main", unwind=2, timeout=30,
                       cbmc_path=getattr(config, "cbmc_path", "cbmc"))
    except Exception:
        return False
    return bool(getattr(res, "verified", False)) and not getattr(res, "counterexamples", None)


def _dedup_invariants(annotations: dict, check_fn, loops, config, logger) -> dict:
    """Remove ONLY logically-redundant clauses — those ENTAILED by the rest of the
    same loop's invariants (``y>=1`` when ``x>=1 && x==y`` already implies it). This
    is NOT minimization: an INDEPENDENT sound fact the goal happens not to need
    (e.g. a loop bound ``y<=100000``) is RETAINED, because the synthesized spec is the
    loop's behaviour, not a minimal certificate for one goal. Each drop is re-verified
    with the same oracle (redundant ⇒ the goal still proves)."""
    cur = {o: list(v) for o, v in annotations.items()}
    changed = True
    while changed:
        changed = False
        for o in list(cur):
            for c in list(cur[o]):
                if len(cur[o]) <= 1:
                    break
                rest = [x for x in cur[o] if x != c]
                if not _entails(rest, c, config):
                    continue
                trial = {oo: list(vv) for oo, vv in cur.items()}
                trial[o].remove(c)
                if check_fn(trial).verified:
                    cur = trial; changed = True
                    logger.info("loop-inv: dedup dropped redundant clause %r "
                                "(entailed by the rest)", c)
                    break
    return cur


def _generality_gate(annotations: dict, check_fn, loops, logger):
    """Reject caller-specific OVER-FIT. Value-pin clauses (``n==5`` / ``a[0]==1``) hold
    only in a concrete caller context (they survive 'for free' when the callee loop is
    inlined into the caller). Drop each one whose removal STILL leaves the goal
    provable — it was non-load-bearing scaffolding. If the goal proves ONLY via such a
    clause, it cannot be dropped without losing the proof: keep it but FLAG the spec as
    goal-specific (non-behavioral). Returns (annotations, flagged_clauses).

    This is the opposite force from minimization: over-fit must be removed by a
    GENERALITY criterion (caller-specific), not a size one — caller-specific specs are
    typically MORE minimal, so a size-minimizer would never remove them."""
    cur = {o: list(v) for o, v in annotations.items()}
    flagged: list = []
    for o in list(cur):
        for c in list(cur[o]):
            if not _is_non_behavioral(c):
                continue
            if len(cur[o]) <= 1:
                break
            trial = {oo: list(vv) for oo, vv in cur.items()}
            trial[o].remove(c)
            if check_fn(trial).verified:
                cur = trial
                logger.info("loop-inv: generality gate dropped caller-specific "
                            "clause %r", c)
            else:
                flagged.append(c)
                logger.info("loop-inv: generality gate FLAG — proof depends on "
                            "caller-specific clause %r (non-behavioral)", c)
    return cur, flagged


def _strengthen_relational(annotations: dict, check_fn, loops, by_ord,
                           acc_specs: dict, logger):
    """Behavioral strengthening — the DUAL of minimization. After the goal verifies,
    ADD the strongest inductive EQUALITY invariants between loop-carried scalars that
    the (possibly weak) goal did not force, so the spec captures what the loop actually
    maintains (e.g. ``x == y``), per the project's behavioral-spec preference.

    Candidates come from ``relational_equality_candidates`` (ALL scalar pairs — NOT a
    syntactic update pattern), each KEPT only if the augmented set still verifies (so
    it is established + preserved) — the verdict can never weaken. Equality
    transitivity is tracked (union-find) so an implied pair (``a==c`` after ``a==b``,
    ``b==c``) isn't re-added as a redundant clause. Accumulator loops are skipped:
    their fold equation IS the behavioral summary. Returns (annotations, last_chk|None)."""
    cur = {o: list(v) for o, v in annotations.items()}
    final = None
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for lp in loops:
        if lp.ordinal in acc_specs:            # fold loops already carry the summary
            continue
        parent.clear()
        for cl in cur.get(lp.ordinal, []):     # seed classes from equalities present
            m = re.fullmatch(r"\s*(\w+)\s*==\s*(\w+)\s*", cl)
            if m:
                parent[find(m.group(1))] = find(m.group(2))
        for cand in relational_equality_candidates(by_ord[lp.ordinal]):
            a, b = (s.strip() for s in cand.split("=="))
            if find(a) == find(b):             # already present or implied → skip
                continue
            trial = {o: list(v) for o, v in cur.items()}
            trial[lp.ordinal] = cur.get(lp.ordinal, []) + [cand]
            chk = check_fn(trial)
            if chk.verified:
                cur, final = trial, chk
                parent[find(a)] = find(b)
                logger.info("loop-inv: strengthened loop %d with behavioral "
                            "invariant %r", lp.ordinal, cand)
    return cur, final


def synthesize_loop_invariants(source_file, config, llm, entry: str = "main",
                               max_iters: int = 6, unwind: int = 0,
                               timeout: int = 180) -> LoopSynthResult:
    """Gen+refine loop-invariant synthesis. Propose → CBMC (validity+adequacy) →
    refine on the counterexample, until the invariants are valid AND the goals
    are proved (or a cap/fixpoint). Returns the invariants + their ACSL rendering."""
    from pathlib import Path
    src = Path(source_file).read_text(encoding="utf-8", errors="replace")
    # Normalise brace-less single-statement loop bodies into braced form so
    # find_loops / the oracle insertion paths can annotate them.
    src = brace_braceless_loops(src)
    goals = extract_goals(src)
    loops = find_loops(src)
    gf = bool(getattr(config, "goal_free", False))
    if not goals and not gf:
        # No proof target → N/A, NOT a pass. Without a goal the invariants would
        # "verify" vacuously (nothing to fail adequacy), which would be a misleading
        # pass; report N/A so an assertion-free program is never counted as proved.
        return LoopSynthResult(ok=False, iterations=0, goals=goals, no_goals=True,
                               note="no verification goal (no //@ assert / assert / "
                                    "__VERIFIER_assert) — nothing to prove")
    # Goal-free MINING: with goals empty the oracle's `verified` reduces to "every
    # invariant is inductive" (no goal to discharge). Skip the goal-anchored
    # minimization (generality gate / dedup) and require a NON-EMPTY inductive set.
    if not loops:
        return LoopSynthResult(ok=False, iterations=0, goals=goals,
                               note="no loops to annotate")
    uw = unwind or _guess_unwind(loops, 64)
    by_ord = {lp.ordinal: lp for lp in loops}
    fn_src_by_ord = {
        lp.ordinal: _enclosing_function_source(src, lp.start_offset) or src
        for lp in loops
    }
    math_ints = bool(getattr(config, "math_ints", False))
    oracle = getattr(config, "oracle", "cbmc") or "cbmc"
    if oracle == "frama-c":
        from bmc_agent import frama_c
        if not frama_c.frama_c_available(getattr(config, "frama_c_path", "frama-c")):
            return LoopSynthResult(
                False, 0, {}, "", goals,
                note="--oracle frama-c selected but frama-c is not on PATH "
                     "(install Frama-C + an SMT prover, e.g. alt-ergo)")

    def _attempt(use_havoc: bool, aw: int) -> LoopSynthResult:
        mode = ("frama-c/wp" if oracle == "frama-c" else
                (("havoc-abstraction" + ("/math-ints" if math_ints else ""))
                 if use_havoc else "loop-head+unwind"))
        logger.info("loop-inv mode: %s (unwind=%d)", mode, aw)

        def _check(ann):
            if oracle == "frama-c":
                return check_loop_invariants_wp(src, ann, config, entry, timeout)
            if use_havoc:
                return check_havoc_abstraction(src, ann, config, entry, timeout, math_ints)
            return check_loop_invariants(src, ann, config, entry, aw, timeout)

        # Accumulator loops (array folds) get a DETERMINISTIC, general invariant
        # set (index bounds + recursive-logic-function summary) under the frama-c
        # oracle — the LLM is skipped for them (it emits the unprovable `\sum`
        # aggregate or a bound-specific ladder). Other loops use the LLM proposer.
        acc_specs = accumulator_specs(src, loops) if oracle == "frama-c" else {}
        map_specs = array_map_specs(src, loops) if oracle == "frama-c" else {}
        cond_set_specs = conditional_array_set_specs(src, loops) if oracle == "frama-c" else {}
        scan_specs = array_scan_specs(src, loops) if oracle == "frama-c" else {}
        max_specs = array_max_specs(src, loops) if oracle == "frama-c" else {}
        count_specs = conditional_count_specs(src, loops) if oracle == "frama-c" else {}
        countdown_specs = countdown_counter_specs(src, loops) if oracle == "frama-c" else {}
        annotations = {}
        for lp in loops:
            if lp.ordinal in acc_specs:
                annotations[lp.ordinal] = accumulator_invariants(acc_specs[lp.ordinal])
                logger.info("loop-inv: synthesized accumulator invariant for loop %d (%s fold "
                            "→ %s)", lp.ordinal, acc_specs[lp.ordinal].kind,
                            acc_specs[lp.ordinal].fn)
            elif lp.ordinal in map_specs:
                annotations[lp.ordinal] = array_map_invariants(map_specs[lp.ordinal])
                logger.info("loop-inv: synthesized array-map invariant for loop %d (%s)",
                            lp.ordinal, map_specs[lp.ordinal].array)
            elif lp.ordinal in cond_set_specs:
                annotations[lp.ordinal] = conditional_array_set_invariants(
                    cond_set_specs[lp.ordinal])
                logger.info("loop-inv: synthesized conditional array-set invariant for "
                            "loop %d (%s)", lp.ordinal,
                            cond_set_specs[lp.ordinal].array)
            elif lp.ordinal in scan_specs:
                annotations[lp.ordinal] = array_scan_invariants(scan_specs[lp.ordinal])
                logger.info("loop-inv: synthesized array-scan invariant for loop %d (%s)",
                            lp.ordinal, scan_specs[lp.ordinal].kind)
            elif lp.ordinal in max_specs:
                annotations[lp.ordinal] = array_max_invariants(max_specs[lp.ordinal])
                logger.info("loop-inv: synthesized array-max invariant for loop %d (%s)",
                            lp.ordinal, max_specs[lp.ordinal].array)
            elif lp.ordinal in count_specs:
                annotations[lp.ordinal] = conditional_count_invariants(count_specs[lp.ordinal])
                logger.info("loop-inv: synthesized conditional-count invariant for loop %d "
                            "(%s)", lp.ordinal, count_specs[lp.ordinal].count_var)
            elif lp.ordinal in countdown_specs:
                annotations[lp.ordinal] = countdown_counter_invariants(
                    countdown_specs[lp.ordinal])
                logger.info("loop-inv: synthesized countdown-counter invariant for loop %d "
                            "(%s)", lp.ordinal, countdown_specs[lp.ordinal].counter)
            else:
                annotations[lp.ordinal] = _propose(
                    llm, config, lp, goals, fn_src_by_ord[lp.ordinal])
        for o, invs in annotations.items():
            logger.info("loop-inv proposed for loop %d: %s", o, invs)

        # Per-loop memory of clauses dropped as non-inductive. A clause that is
        # goal-relevant but not self-inductive (it needs an auxiliary companion)
        # gets pruned here, then re-proposed bare, then pruned again — an infinite
        # cycle. Remembering the dropped text lets the next refinement ask for the
        # auxiliary invariant that makes it stick, instead of re-offering it bare.
        pruned_non_inductive: dict = {lp.ordinal: [] for lp in loops}
        reinjected: dict = {lp.ordinal: set() for lp in loops}   # pruned clauses re-paired with an aux

        for it in range(1, max_iters + 2):
            chk = _check(annotations)
            logger.info("loop-inv iter %d: verified=%s failing_inv=%s goal_failed=%s",
                        it, chk.verified, chk.failing_invariants, chk.goal_failed)
            _log = getattr(chk.result, "raw_output", "") or ""
            if chk.verified:
                # Prefer MINIMAL behavioral invariants: once the goals are proved,
                # greedily drop clauses that aren't load-bearing (input/goal-restating
                # scaffolding that survives 'for free' in the concrete context). Only
                # where the invariant is GENUINELY required for the proof — Frama-C/WP
                # and the havoc abstraction. In loop-head+unwind mode CBMC proves the
                # goal by UNWINDING regardless of the invariant, so "not load-bearing"
                # would wrongly strip the very behavioral invariant we synthesized.
                final_chk = chk
                gate_flagged: list = []
                if oracle == "frama-c" or use_havoc:
                    # GENERALITY GATE (replaces size-minimization): drop caller-specific
                    # OVER-FIT (n==5, a[0]==1) that holds only in the inlined concrete
                    # context, when the goal still proves without it; flag it if the
                    # proof genuinely depends on it. Size-minimization is RETIRED — it
                    # could neither remove load-bearing over-fit (dropping breaks the
                    # proof) nor justify dropping sound, independent behavioral facts.
                    gated, gate_flagged = ((annotations, []) if gf else _generality_gate(annotations, _check, loops, logger))
                    if gated != annotations:
                        gchk = _check(gated)
                        if gchk.verified:
                            annotations, final_chk = gated, gchk
                            _log = getattr(gchk.result, "raw_output", "") or _log
                    # BEHAVIORAL STRENGTHENING: ADD the strongest inductive RELATIONAL
                    # invariants the goal didn't force (e.g. `x == y`) — the project's
                    # behavioral-spec preference. Each kept only if the set still verifies.
                    if getattr(config, "enable_spec_strengthen", True):
                        strengthened, schk = _strengthen_relational(
                            annotations, _check, loops, by_ord, acc_specs, logger)
                        if schk is not None:
                            annotations, final_chk = strengthened, schk
                            _log = getattr(schk.result, "raw_output", "") or _log
                    # ENTAILMENT-DEDUP: remove ONLY logically-redundant restatements
                    # (entailed by the rest); KEEP independent sound facts the goal
                    # doesn't need (e.g. a loop bound) — a spec, not a minimal cert.
                    deduped = (annotations if gf else _dedup_invariants(annotations, _check, loops, config, logger))
                    if deduped != annotations:
                        dchk = _check(deduped)
                        if dchk.verified:
                            annotations, final_chk = deduped, dchk
                            _log = getattr(dchk.result, "raw_output", "") or _log
                # Overflow-rigorous mode (all loops are folds, math-int on): the shown
                # spec carries the loop variant + no-overflow precondition that the
                # RTE-checked proof used, so the displayed contract is the sound one.
                ovf_specs = (overflow_safe_accumulators(src, loops, math_ints)
                             if oracle == "frama-c" else {})
                variants = {ordn: accumulator_variant(s) for ordn, s in ovf_specs.items()}
                # Show the SAME `loop assigns` frame the WP oracle verified, so the
                # displayed spec is the complete, re-checkable loop contract (not a
                # frame-less subset). Only for frama-c — CBMC's loop-head+unwind mode
                # has no ACSL frame. The frame was already proven (its assigns goals
                # passed for this SATISFIED set), so showing it can't misrepresent.
                disp_assigns = ({lp.ordinal: _loop_assigns(lp) for lp in loops}
                                if oracle == "frama-c" else {})
                if oracle == "frama-c":
                    disp_assigns.update({ordn: array_map_loop_assigns(spec)
                                         for ordn, spec in array_map_specs(src, loops).items()})
                    disp_assigns.update({ordn: conditional_array_set_loop_assigns(spec)
                                         for ordn, spec
                                         in conditional_array_set_specs(src, loops).items()})
                    disp_assigns.update({ordn: array_scan_loop_assigns(spec)
                                         for ordn, spec
                                         in array_scan_specs(src, loops).items()})
                    disp_assigns.update({ordn: array_max_loop_assigns(spec)
                                         for ordn, spec
                                         in array_max_specs(src, loops).items()})
                    disp_assigns.update({ordn: conditional_count_loop_assigns(spec)
                                         for ordn, spec
                                         in conditional_count_specs(src, loops).items()})
                    disp_assigns.update({ordn: countdown_counter_loop_assigns(spec)
                                         for ordn, spec
                                         in countdown_counter_specs(src, loops).items()})
                rendered = render_loop_invariants_acsl(annotations, loops, variants, disp_assigns)
                # Show the complete synthesized spec above the loop invariants — the
                # recursive-logic-function definition(s) then the function contract(s)
                # — but only for accumulators whose summary survived minimization (the
                # axiomatic/contract are otherwise unused).
                live = [s for s in acc_specs.values() if s.fn in rendered]
                live_fns = {s.fn for s in live}
                prelude = "".join(accumulator_axiomatic(s) for s in live)
                map_contract_blocks = array_map_contracts(src, loops, entry)
                cond_set_contract_blocks = conditional_array_set_contracts(src, loops, entry)
                scan_contract_blocks = array_scan_contracts(src, loops, entry)
                max_contract_blocks = array_max_contracts(src, loops, entry)
                count_contract_blocks = conditional_count_contracts(src, loops, entry)
                countdown_contract_blocks = countdown_counter_contracts(src, loops, entry)
                if prelude and oracle == "frama-c":
                    if ovf_specs:
                        prelude = "#include <limits.h>\n" + prelude
                    for fn, block in accumulator_contracts(
                            src, loops, entry, overflow_safe=bool(ovf_specs)).items():
                        if any(lf in block for lf in live_fns):   # contract for a live fold
                            prelude += f"// contract for {fn}\n{block}"
                if map_contract_blocks and oracle == "frama-c":
                    for fn, block in map_contract_blocks.items():
                        prelude += f"// contract for {fn}\n{block}"
                if cond_set_contract_blocks and oracle == "frama-c":
                    for fn, block in cond_set_contract_blocks.items():
                        prelude += f"// contract for {fn}\n{block}"
                if scan_contract_blocks and oracle == "frama-c":
                    for fn, block in scan_contract_blocks.items():
                        prelude += f"// contract for {fn}\n{block}"
                if max_contract_blocks and oracle == "frama-c":
                    for fn, block in max_contract_blocks.items():
                        prelude += f"// contract for {fn}\n{block}"
                if count_contract_blocks and oracle == "frama-c":
                    for fn, block in count_contract_blocks.items():
                        prelude += f"// contract for {fn}\n{block}"
                if countdown_contract_blocks and oracle == "frama-c":
                    for fn, block in countdown_contract_blocks.items():
                        prelude += f"// contract for {fn}\n{block}"
                if gf and getattr(config, "enable_spec_strengthen", True):
                    # Maximal inductive strengthening: with no goal to drive precision,
                    # iteratively mine stronger invariants and keep each that stays
                    # inductive, until a fixpoint (no new sound clause).
                    for _sr in range(3):
                        _added = False
                        for lp in loops:
                            cur_cl = annotations.get(lp.ordinal, [])
                            for cand in _propose_stronger(
                                    llm, config, by_ord[lp.ordinal], cur_cl,
                                    fn_src_by_ord[lp.ordinal]):
                                if cand in annotations.get(lp.ordinal, []):
                                    continue
                                trial = {o: list(v) for o, v in annotations.items()}
                                trial[lp.ordinal] = annotations.get(lp.ordinal, []) + [cand]
                                _ck = _check(trial)
                                if _ck.verified:
                                    annotations, final_chk = trial, _ck
                                    _added = True
                                    logger.info("goal-free: strengthened loop %d with %r",
                                                lp.ordinal, cand)
                                else:
                                    # CEGAR (same as the main loop): the candidate is not
                                    # inductive -> refine on the counterexample (propose the
                                    # auxiliary that makes it stick) instead of dropping it.
                                    _ref = _refine(
                                        llm, config, by_ord[lp.ordinal],
                                        annotations.get(lp.ordinal, []) + [cand],
                                        _refine_problem(
                                            "The candidate invariant %r holds on entry but is "
                                            "NOT preserved by one iteration. Propose the "
                                            "auxiliary invariant(s) that make it inductive "
                                            "(keep it and add what it needs), or a corrected "
                                            "form." % cand, None),
                                        [], fn_src_by_ord[lp.ordinal])
                                    if _ref:
                                        trial2 = {o: list(v) for o, v in annotations.items()}
                                        base = annotations.get(lp.ordinal, [])
                                        trial2[lp.ordinal] = base + [c for c in _ref if c not in base]
                                        if len(trial2[lp.ordinal]) > len(base):
                                            _ck2 = _check(trial2)
                                            if _ck2.verified:
                                                annotations, final_chk = trial2, _ck2
                                                _added = True
                                                logger.info("goal-free: CEGAR-refined "
                                                            "strengthening on loop %d (from %r)",
                                                            lp.ordinal, cand)
                        if not _added:
                            break
                if gf:
                    # entailment-dedup the strengthened set: drop redundant restatements
                    # (e.g. equivalent closed-form rewrites), keep independent facts, stay
                    # inductive -> a clean strong spec.
                    _ded = _dedup_invariants(annotations, _check, loops, config, logger)
                    if any(_ded.values()):
                        _dchk = _check(_ded)
                        if _dchk.verified:
                            annotations, final_chk = _ded, _dchk
                    # re-render so the returned ACSL reflects the strengthened+deduped set
                    rendered = render_loop_invariants_acsl(annotations, loops, variants, disp_assigns)
                    _nclause = sum(len(v) for v in annotations.values())
                    if _nclause == 0:
                        return LoopSynthResult(
                            False, it, annotations, "", goals,
                            note="goal-free: no inductive invariants could be mined")
                    note = f"goal-free: mined {_nclause} inductive loop-invariant clause(s)"
                else:
                    note = "invariants are inductive and prove all goals"
                    if gate_flagged:
                        note += (" — NOTE goal-specific (non-behavioral): proof depends on "
                                 "caller-specific clause(s) " + "; ".join(gate_flagged))
                return LoopSynthResult(
                    ok=True, iterations=it, annotations=annotations,
                    acsl=(prelude + "\n" + rendered if prelude else rendered), goals=goals,
                    note=note,
                    instrumented=getattr(final_chk, "instrumented", chk.instrumented),
                    cbmc_log=_log)
            if chk.unwinding_failed:
                return LoopSynthResult(False, it, annotations,
                                       render_loop_invariants_acsl(annotations, loops), goals,
                                       note=f"loop not fully unwound at unwind={aw} (unbounded? "
                                            "needs a quantifier-capable oracle, e.g. Frama-C/WP)",
                                       unwinding_failed=True,
                                       instrumented=chk.instrumented, cbmc_log=_log)
            if it > max_iters:
                # Final pass is RE-CHECK ONLY: the last refine/prune may have produced a
                # provable set, but the loop would otherwise exit before re-verifying it
                # (a stale 'max iterations' false-negative). No extra refine round.
                break
            changed = False
            if chk.failing_invariants:
                # Deterministically prune non-inductive clauses (often spurious; the
                # inductive behavioral ones that remain frequently suffice — also the
                # minimality objective). Re-checked next iteration.
                fset = set(chk.failing_invariants)
                # Remember the TEXT of each clause dropped as non-inductive so a
                # later refinement can request the auxiliary companion that makes it
                # inductive, rather than silently losing a goal-relevant fact. A clause
                # that was already RE-INJECTED (paired with a proposed auxiliary) and
                # STILL fails is genuinely non-inductive (likely false) — give up on it
                # so it isn't re-injected forever (the false-clause oscillation guard).
                for (o, n) in fset:
                    invs_o = annotations.get(o) or []
                    if not (0 <= n < len(invs_o)):
                        continue
                    c = invs_o[n]
                    if c in reinjected.get(o, set()):
                        reinjected[o].discard(c)
                        if c in pruned_non_inductive.get(o, []):
                            pruned_non_inductive[o].remove(c)
                    elif c not in pruned_non_inductive.setdefault(o, []):
                        pruned_non_inductive[o].append(c)
                pruned = {o: [inv for n, inv in enumerate(invs) if (o, n) not in fset]
                          for o, invs in annotations.items()}
                if any(pruned[o] != annotations[o] for o in annotations) and any(pruned.values()):
                    logger.info("loop-inv: pruned non-inductive clauses %s", sorted(fset))
                    annotations = pruned; changed = True
                else:
                    for ordn in {o for (o, _n) in chk.failing_invariants}:
                        lp = by_ord[ordn]
                        new = _refine(llm, config, lp, annotations[ordn],
                                      _refine_problem(
                                          "Some invariants are NOT preserved by the loop body (the "
                                          "verifier refuted them). Note: an invariant holds at the "
                                          "TOP of the body, BEFORE that iteration's writes — so a "
                                          "fact about the element written THIS iteration is not yet "
                                          "true. Fix them.",
                                          pruned_non_inductive.get(ordn)),
                                      goals, fn_src_by_ord[ordn])
                        if new:
                            new = _reinject(new, pruned_non_inductive.get(ordn),
                                            reinjected.setdefault(ordn, set()))
                        if new and new != annotations[ordn]:
                            annotations[ordn] = new; changed = True
            else:  # goal_failed: invariants valid but too weak
                for lp in loops:
                    new = _refine(llm, config, lp, annotations[lp.ordinal],
                                  _refine_problem(
                                      "The invariants are valid but TOO WEAK: the goals are not "
                                      "provable at loop exit. Strengthen / add invariants that "
                                      "summarize the loop strongly enough to imply the goals.",
                                      pruned_non_inductive.get(lp.ordinal)),
                                  goals, fn_src_by_ord[lp.ordinal])
                    if new:
                        new = _reinject(new, pruned_non_inductive.get(lp.ordinal),
                                        reinjected.setdefault(lp.ordinal, set()))
                    if new and new != annotations[lp.ordinal]:
                        annotations[lp.ordinal] = new; changed = True
            if not changed:
                return LoopSynthResult(False, it, annotations,
                                       render_loop_invariants_acsl(annotations, loops), goals,
                                       note=("refinement reached a fixpoint without a fully-inductive invariant set"
                                             if gf else "refinement reached a fixpoint without proving the goals"),
                                       instrumented=chk.instrumented, cbmc_log=_log)
        return LoopSynthResult(False, max_iters, annotations,
                               render_loop_invariants_acsl(annotations, loops), goals,
                               note="max iterations reached",
                               instrumented=chk.instrumented,
                               cbmc_log=getattr(chk.result, "raw_output", "") or "")

    # Frama-C/WP oracle: a single attempt (WP consumes the ACSL loop invariants
    # directly — no bounded/unbounded mode dispatch or CBMC fallback).
    if oracle == "frama-c":
        # Random-restart (portfolio) search: LLM proposal is nondeterministic, so
        # independent attempts explore different invariant candidates. Keep the first
        # the WP oracle fully verifies. General search strategy -- no program-specific
        # knowledge; the oracle decides success. attempts=1 preserves prior behavior.
        attempts = max(1, int(getattr(config, "synth_attempts", 1) or 1))
        last = None
        for _k in range(attempts):
            r = _attempt(False, uw)
            if r.ok or getattr(r, "no_goals", False) or r.unwinding_failed:
                return r          # success, or a retry-won't-help terminal state
            last = r
            if attempts > 1:
                logger.info("loop-inv: restart %d/%d did not verify (%s)",
                            _k + 1, attempts, r.note)
        return last

    # Primary mode: array-writing loops -> loop-head+unwind (quantified invariant
    # validated per concrete iteration); scalar loops -> havoc abstraction (bound-
    # independent, handles unbounded + huge bounds).
    primary_havoc = not _has_array_writes(loops)
    r = _attempt(primary_havoc, uw)
    if r.ok or (r.unwinding_failed and not primary_havoc):
        # ok, or an unbounded array-writing loop (loop-head can't unwind AND havoc
        # can't do the quantified invariant) = the genuine Frama-C boundary.
        return r
    # Fallback: the other mode. A scalar loop whose invariant is actually an array
    # AGGREGATE (e.g. sum == sum(a[0..p-1])) can't be expressed for havoc, but a
    # loop-head+unwind validates an array-specific invariant per concrete iteration
    # when the loop is bounded at the call site (e.g. sumArray(arr, 5)). Cap the
    # fallback unwind so a small concrete bound verifies without going intractable.
    fb_havoc = not primary_havoc
    fb_uw = uw if fb_havoc else min(_guess_unwind(loops, 256), 300)
    logger.info("loop-inv: primary mode did not converge (%s) — trying fallback", r.note)
    r2 = _attempt(fb_havoc, fb_uw)
    return r2 if r2.ok else r
