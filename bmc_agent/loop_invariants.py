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
Always include the index-bound invariant (e.g. `i <= N`).

Aim for the FEWEST, most GENERAL clauses that suffice: the index bound plus a
behavioral summary of what the loop computes (a running sum/relationship that holds
for ANY input). Prefer expressing the relationship over restating the caller's
concrete input values — clauses like `n == 5` or `len == 1024` are usually
redundant (the verifier already knows them from the call site). But correctness and
provability come FIRST: if a per-element fact is genuinely needed for the invariant
to be inductive (e.g. relating a symbolic `a[p]` to its value), include it.
Redundant clauses are pruned automatically afterward, so never drop a fact the
proof needs just to look minimal.

OUTPUT FORMAT (one invariant per line):
  - a boolean expression over the loop variables/arrays, e.g.  i <= 1024
  - or a quantified fact:  forall <var> : <range/guard> ==> <fact>
Use `==>` for implication. Do NOT use `\\` or ACSL syntax — plain C-style names.

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

GOALS:
{goals}

FUNCTION:
```c
{fn_src}
```
LOOP header: {kind} ({guard})
Output ONLY the corrected invariant lines.
"""


def _prep_goals_acsl(source: str) -> str:
    """For the Frama-C oracle: express every goal as an ACSL ``//@ assert`` (WP
    proves those natively). ``//@ assert`` stays; the executable forms
    (assert / static_assert / __VERIFIER_assert) are rewritten to ``/*@ assert E; */``
    and their call (incl. trailing ``;``) consumed."""
    from bmc_agent.assert_driven_specs import _balanced_arg, _strip_assert_message
    rx = re.compile(r"\b(?:__VERIFIER_assert|static_assert|_Static_assert|assert)\s*\(")
    out, i = [], 0
    while True:
        m = rx.search(source, i)
        if not m:
            out.append(source[i:]); break
        out.append(source[i:m.start()])
        arg, after = _balanced_arg(source, m.end() - 1)
        out.append(f"/*@ assert {_strip_assert_message(arg)}; */")
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
    it), so preservation of ``i <= N`` and the whole goal fail. A counter DECLARED
    in the init (``for (int i = ...``) is loop-local and correctly excluded by
    modified_vars' declared-variable filter."""
    scan = lp.body
    if getattr(lp, "kind", "") == "for":
        parts = (lp.guard or "").split(";")
        init = parts[0] if parts else ""
        incr = parts[2] if len(parts) > 2 else ""
        scan = f"{lp.body}\n{init};\n{incr};"
    scalars, arrays = modified_vars(scan)
    return ", ".join(scalars + [f"{a}[..]" for a in arrays])


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


def check_loop_invariants_wp(source: str, annotations: dict, config,
                             entry: str = "main", timeout: int = 120) -> "LoopCheck":
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
    prepped = _prep_goals_acsl(source)
    inline = _loop_function_callees(prepped, entry)
    annotated = frama_c.insert_loop_invariants_acsl(prepped, annotations, assigns)
    wp = frama_c.run_wp(annotated, getattr(config, "frama_c_path", "frama-c"), timeout,
                        inline=inline, exclude_terminates=True, rte=not math_ints)
    # WP goal names: "..._loop_invariant_..." (validity) vs "...assert..." (adequacy).
    inv_failed = any("invariant" in g.lower() for g in wp.unproved)
    goal_failed = any("assert" in g.lower() for g in wp.unproved) or (
        not wp.proved and not inv_failed)
    # frama-c doesn't expose our (ordinal,n); signal "an invariant failed" coarsely.
    finv = [(lp.ordinal, 0) for lp in loops] if inv_failed else []
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


def _parse_inv_lines(text: str) -> list:
    """Invariant expressions from an LLM reply: one per line, fences/bullets/
    trailing semicolons and `loop invariant` keyword stripped."""
    out = []
    for raw in (text or "").splitlines():
        ln = raw.strip().strip("`").strip()
        if not ln or ln.startswith(("//", "/*", "#", "```")):
            continue
        ln = re.sub(r"^\s*(?:[-*]\s*)?(?:loop\s+invariant\s+)?", "", ln, flags=re.IGNORECASE)
        ln = ln.rstrip(";").strip()
        if ln:
            out.append(ln)
    return out


_C_KEYWORDS = {
    "int", "unsigned", "signed", "long", "short", "char", "void", "const", "static",
    "if", "else", "while", "for", "do", "return", "sizeof", "struct", "union", "enum",
    "true", "false", "size_t", "forall", "exists", "result", "_Bool", "bool", "float",
    "double", "NULL", "assert", "static_assert",
}


def _filter_in_scope(clauses: list, source: str) -> list:
    """Drop invariant clauses that reference identifiers not present in the program
    (LLM hallucinations like an invented loop counter `i`). An out-of-scope name
    would make the instrumented source fail to compile, so the check silently
    'fails' every iteration and never converges. Quantifier-bound variables are
    exempt (they're locally bound)."""
    known = set(re.findall(r"[A-Za-z_]\w*", source))
    out = []
    for c in clauses:
        m = _FORALL.match(c)
        body, qvar = (m.group(2), {m.group(1)}) if m else (c, set())
        ids = set(re.findall(r"[A-Za-z_]\w*", body)) - qvar - _C_KEYWORDS - known
        if ids:
            logger.info("loop-inv: dropping clause with out-of-scope %s: %r", sorted(ids), c)
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


def _minimize_invariants(annotations: dict, check_fn, loops, logger) -> dict:
    """Greedily drop every clause that is NOT load-bearing for the proof, so the
    result is a MINIMAL, behavioral invariant set rather than a sound-but-bloated
    one (the verifier proves goals in a concrete/inlined context, so input-restating
    clauses like ``n==5`` / ``a[0]==1`` survive 'for free' — strip them). Non-
    behavioral clauses are tried first; a loop never reduces below one clause, and
    every removal is re-verified with the SAME oracle so minimization stays sound."""
    cur = {o: list(v) for o, v in annotations.items()}

    def _order(o):
        # indices of THIS loop's clauses, non-behavioral first (drop scaffolding
        # before the behavioral core), each group high-index-first for stable popping
        idxs = list(range(len(cur[o])))
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


def synthesize_loop_invariants(source_file, config, llm, entry: str = "main",
                               max_iters: int = 6, unwind: int = 0,
                               timeout: int = 180) -> LoopSynthResult:
    """Gen+refine loop-invariant synthesis. Propose → CBMC (validity+adequacy) →
    refine on the counterexample, until the invariants are valid AND the goals
    are proved (or a cap/fixpoint). Returns the invariants + their ACSL rendering."""
    from pathlib import Path
    src = Path(source_file).read_text(encoding="utf-8", errors="replace")
    goals = extract_goals(src)
    loops = find_loops(src)
    if not goals:
        # No proof target → N/A, NOT a pass. Without a goal the invariants would
        # "verify" vacuously (nothing to fail adequacy), which would be a misleading
        # pass; report N/A so an assertion-free program is never counted as proved.
        return LoopSynthResult(ok=False, iterations=0, goals=goals, no_goals=True,
                               note="no verification goal (no //@ assert / assert / "
                                    "__VERIFIER_assert) — nothing to prove")
    if not loops:
        return LoopSynthResult(ok=False, iterations=0, goals=goals,
                               note="no loops to annotate")
    uw = unwind or _guess_unwind(loops, 64)
    fn_src = src  # whole TU as context (benchmarks are small)
    by_ord = {lp.ordinal: lp for lp in loops}
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

        annotations = {lp.ordinal: _propose(llm, config, lp, goals, fn_src) for lp in loops}
        for o, invs in annotations.items():
            logger.info("loop-inv proposed for loop %d: %s", o, invs)

        for it in range(1, max_iters + 1):
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
                if oracle == "frama-c" or use_havoc:
                    minimized = _minimize_invariants(annotations, _check, loops, logger)
                    if minimized != annotations:
                        mchk = _check(minimized)
                        if mchk.verified:
                            annotations, final_chk = minimized, mchk
                            _log = getattr(mchk.result, "raw_output", "") or _log
                return LoopSynthResult(
                    ok=True, iterations=it, annotations=annotations,
                    acsl=render_loop_invariants_acsl(annotations, loops), goals=goals,
                    note="invariants are inductive and prove all goals (minimized)",
                    instrumented=getattr(final_chk, "instrumented", chk.instrumented),
                    cbmc_log=_log)
            if chk.unwinding_failed:
                return LoopSynthResult(False, it, annotations,
                                       render_loop_invariants_acsl(annotations, loops), goals,
                                       note=f"loop not fully unwound at unwind={aw} (unbounded? "
                                            "needs a quantifier-capable oracle, e.g. Frama-C/WP)",
                                       unwinding_failed=True,
                                       instrumented=chk.instrumented, cbmc_log=_log)
            changed = False
            if chk.failing_invariants:
                # Deterministically prune non-inductive clauses (often spurious; the
                # inductive behavioral ones that remain frequently suffice — also the
                # minimality objective). Re-checked next iteration.
                fset = set(chk.failing_invariants)
                pruned = {o: [inv for n, inv in enumerate(invs) if (o, n) not in fset]
                          for o, invs in annotations.items()}
                if any(pruned[o] != annotations[o] for o in annotations) and any(pruned.values()):
                    logger.info("loop-inv: pruned non-inductive clauses %s", sorted(fset))
                    annotations = pruned; changed = True
                else:
                    for ordn in {o for (o, _n) in chk.failing_invariants}:
                        lp = by_ord[ordn]
                        new = _refine(llm, config, lp, annotations[ordn],
                                      "Some invariants are NOT preserved by the loop body (CBMC "
                                      "refuted them). Note: an invariant holds at the TOP of the "
                                      "body, BEFORE that iteration's writes — so a fact about the "
                                      "element written THIS iteration is not yet true. Fix them.",
                                      goals, fn_src)
                        if new and new != annotations[ordn]:
                            annotations[ordn] = new; changed = True
            else:  # goal_failed: invariants valid but too weak
                for lp in loops:
                    new = _refine(llm, config, lp, annotations[lp.ordinal],
                                  "The invariants are valid but TOO WEAK: the goals are not "
                                  "provable at loop exit. Strengthen / add invariants that "
                                  "summarize the loop strongly enough to imply the goals.",
                                  goals, fn_src)
                    if new and new != annotations[lp.ordinal]:
                        annotations[lp.ordinal] = new; changed = True
            if not changed:
                return LoopSynthResult(False, it, annotations,
                                       render_loop_invariants_acsl(annotations, loops), goals,
                                       note="refinement reached a fixpoint without proving the goals",
                                       instrumented=chk.instrumented, cbmc_log=_log)
        return LoopSynthResult(False, max_iters, annotations,
                               render_loop_invariants_acsl(annotations, loops), goals,
                               note="max iterations reached",
                               instrumented=chk.instrumented,
                               cbmc_log=getattr(chk.result, "raw_output", "") or "")

    # Frama-C/WP oracle: a single attempt (WP consumes the ACSL loop invariants
    # directly — no bounded/unbounded mode dispatch or CBMC fallback).
    if oracle == "frama-c":
        return _attempt(False, uw)

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
