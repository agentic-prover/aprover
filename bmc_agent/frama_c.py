r"""Frama-C / WP oracle backend.

CBMC is a *bounded* model checker: it discharges loop invariants by UNWINDING
(so it needs a concrete/small bound) and models *machine* integers (wrapping).
That leaves two classes of Specification-Synthesis goal it cannot do cleanly:

  * UNBOUNDED loops (``while(unknown())``) — nothing to unwind.
  * MATHEMATICAL-INTEGER / AGGREGATE invariants — e.g. ``x>=1`` under ``x=x+y``
    (inductive over ℤ, not over wrapping int) or ``sum == \sum a[0..p-1]``
    (a recursive aggregate CBMC has no predicate for).

Frama-C's **WP** plugin is built for exactly this: it consumes ACSL
(``loop invariant`` / ``requires`` / ``ensures`` / ``assigns``) natively, proves
base + preservation + the goal via weakest-precondition + an SMT prover, and uses
mathematical integers by default. This module is that oracle: render the
synthesized DSL specs to ACSL (via :mod:`bmc_agent.acsl`), splice them at the
right source locations, run ``frama-c -wp``, and parse the proved/total goals.

The DSL→ACSL renderers are shared with the CBMC path, so the SAME synthesized
invariant can be discharged by whichever oracle fits — this module only adds the
ACSL *placement* + the ``frama-c`` invocation/parse. Degrades gracefully (a clear
"not installed" result) when ``frama-c`` is absent, so it is inert here and live
wherever Frama-C is on PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from bmc_agent.acsl import contract_to_acsl, loop_invariants_to_acsl
from bmc_agent.logger import get_logger

logger = get_logger("frama_c")


def frama_c_available(frama_c_path: str = "frama-c") -> bool:
    return shutil.which(frama_c_path) is not None


# Preference order for the WP SMT backend. Alt-Ergo is fastest on the typical
# arithmetic/array goals; z3 and cvc5 are stronger on others (e.g. quantifier
# instantiation / unfolding chains alt-ergo stalls on). WP runs them per goal in
# this order and the FIRST to prove wins — so adding the fallbacks only ever
# proves MORE, never fewer (a goal alt-ergo already closes still closes first).
_WP_PROVER_PREFERENCE = ("alt-ergo", "z3", "cvc5")
_WP_PROVERS_CACHE: "dict[str, list[str]]" = {}
_WP_PROVER_ID_RE = re.compile(r"\[([a-z0-9][a-z0-9-]*)[|\]]")


def available_wp_provers(frama_c_path: str = "frama-c") -> list:
    """The preferred WP provers actually registered (via ``frama-c -wp-detect`` —
    which reads why3's config, so absolute prover paths there work without the
    binaries being on PATH). Cached per frama_c_path. Falls back to ``alt-ergo``
    if detection yields nothing. An explicit ``BMC_AGENT_WP_PROVERS`` env (comma
    list) overrides detection entirely."""
    override = os.environ.get("BMC_AGENT_WP_PROVERS", "").strip()
    if override:
        return [p.strip() for p in override.split(",") if p.strip()]
    if frama_c_path in _WP_PROVERS_CACHE:
        return _WP_PROVERS_CACHE[frama_c_path]
    found: set = set()
    try:
        proc = subprocess.run([frama_c_path, "-wp-detect"],
                              capture_output=True, text=True, timeout=60)
        for m in _WP_PROVER_ID_RE.finditer((proc.stdout or "") + (proc.stderr or "")):
            found.add(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    provers = [p for p in _WP_PROVER_PREFERENCE if p in found] or ["alt-ergo"]
    _WP_PROVERS_CACHE[frama_c_path] = provers
    return provers


# --- ACSL placement ----------------------------------------------------------

def insert_loop_invariants_acsl(source: str, annotations: dict,
                                assigns: dict = None) -> str:
    """Splice an ACSL ``/*@ loop invariant …; [loop assigns …;] */`` block
    immediately BEFORE each annotated loop (Frama-C attaches it to the next loop).

    ``annotations``: loop ordinal -> list of DSL invariant expressions.
    ``assigns``:     loop ordinal -> ACSL assigns clause (frame), optional.
    """
    from bmc_agent.loop_invariants import find_loops
    loops = find_loops(source)
    edits = []
    for lp in loops:
        invs = annotations.get(lp.ordinal) or []
        if not invs:
            continue
        block = loop_invariants_to_acsl(invs, (assigns or {}).get(lp.ordinal, ""))
        edits.append((lp.start_offset, block + "\n"))
    out = source
    for off, text in sorted(edits, key=lambda e: -e[0]):
        out = out[:off] + text + out[off:]
    return out


_FUNC_DEF_TMPL = (
    r"(?:^|\n)[ \t]*(?:static\s+|inline\s+|extern\s+)*"
    r"[A-Za-z_][\w \t\*]*\b{name}\s*\([^;{{]*\)\s*\{{")


def _find_brace_block(source: str, open_idx: int) -> int:
    """Index of the '}' matching '{' at ``source[open_idx]`` (quote-aware), or -1."""
    depth, i, n, quote = 0, open_idx, len(source), None
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


# a write that escapes the function's frame: store through a pointer (`*x =`),
# array element (`x[i] =`), or struct field (`x->f =` / `x.f =`), incl. compound
# assignment and ++/--. Writes to plain local scalars don't touch the frame.
_MEM_STORE_RX = re.compile(
    r"(?:\*\s*[A-Za-z_]\w*|[A-Za-z_]\w*\s*\[[^\]]*\]|[A-Za-z_]\w*\s*(?:->|\.)\s*[A-Za-z_]\w*)"
    r"\s*(?:[-+*/%&|^]?=(?!=)|<<=|>>=)"
    r"|(?:\+\+|--)\s*\*?\s*[A-Za-z_]\w*\s*(?:\[|->|\.)"
    r"|\*\s*[A-Za-z_]\w*\s*(?:\+\+|--)")


def function_assigns_nothing(source: str, fn: str) -> bool:
    """True iff ``fn``'s body performs NO write that escapes its frame — no store
    through a pointer/array/field. Such a (leaf) function is ``assigns \\nothing;``.
    Conservative: any escaping store (or inability to locate the body) => False, so
    we only ever ADD a sound frame, never claim a false one. Writes to plain local
    scalars are irrelevant to the frame and ignored."""
    m = re.search(_FUNC_DEF_TMPL.format(name=re.escape(fn)), source)
    if not m:
        return False
    open_brace = source.index("{", m.end() - 1)
    close = _find_brace_block(source, open_brace)
    if close < 0:
        return False
    body = source[open_brace + 1:close]
    return _MEM_STORE_RX.search(body) is None


def insert_contract_acsl(source: str, fn: str, requires: str = "",
                         ensures: str = "", assigns: str = "") -> str:
    """Splice an ACSL function contract ``/*@ requires…; ensures…; */`` immediately
    before the definition of ``fn``. No-op if the contract is vacuous or the
    definition isn't found."""
    block = contract_to_acsl(requires, ensures, assigns)
    if not block:
        return source
    m = re.search(_FUNC_DEF_TMPL.format(name=re.escape(fn)), source)
    if not m:
        logger.info("frama-c: definition of %r not found — contract not inserted", fn)
        return source
    # insert at the start of the matched definition (skip a leading newline)
    pos = m.start()
    if source[pos] == "\n":
        pos += 1
    return source[:pos] + block + "\n" + source[pos:]


def insert_contract_block(source: str, fn: str, block: str) -> str:
    """Splice a pre-rendered ACSL contract ``block`` immediately before ``fn``'s
    definition (no-op if empty or the definition isn't found). Unlike
    ``insert_contract_acsl`` this takes a raw ACSL string verbatim — used for
    synthesized contracts that must keep exact syntax (``\\valid_read``, ranges)."""
    if not block:
        return source
    m = re.search(_FUNC_DEF_TMPL.format(name=re.escape(fn)), source)
    if not m:
        logger.info("frama-c: definition of %r not found — contract not inserted", fn)
        return source
    pos = m.start()
    if source[pos] == "\n":
        pos += 1
    return source[:pos] + block + source[pos:]


# --- frama-c -wp invocation + parse ------------------------------------------

@dataclass
class WPResult:
    available: bool
    proved: bool = False           # all goals proved
    n_proved: int = 0
    n_total: int = 0
    unproved: list = field(default_factory=list)
    raw: str = ""
    error: str = ""


_PROVED_RX = re.compile(r"Proved goals:\s*(\d+)\s*/\s*(\d+)")
# per-goal status lines: "[wp] [Alt-Ergo] typed_… : Valid|Unknown|Timeout|Failed"
_GOAL_RX = re.compile(r"\[wp\].*?\b(\S+)\s*:\s*(Valid|Unknown|Timeout|Failed|Unsuccess)", re.I)
# The per-goal FAILURE format puts the status BEFORE the name, with no colon:
#   ``[wp] [Timeout] typed_main_loop_invariant_4_established (Alt-Ergo)``
# Without capturing this the failing goal NAME is lost, so a caller can't tell
# WHICH invariant/assert failed (it only sees the ``Proved N/M`` shortfall).
_GOAL_STATUS_PREFIX_RX = re.compile(
    r"\[wp\]\s*\[(?:Timeout|Unknown|Failed|Unsuccess|Stuck|Partial)\]\s+(\S+)", re.I)


def parse_wp_output(raw: str) -> tuple:
    """(n_proved, n_total, unproved_goals) from ``frama-c -wp`` output.

    Prefers the summary ``Proved goals: N / M``; falls back to counting per-goal
    ``: Valid`` vs not. ``unproved`` lists the goal names not proved Valid —
    including failures reported in the ``[Status] goalname`` prefix form."""
    n_proved = n_total = 0
    m = _PROVED_RX.search(raw or "")
    if m:
        n_proved, n_total = int(m.group(1)), int(m.group(2))
    unproved = []
    seen_total = 0
    for gm in _GOAL_RX.finditer(raw or ""):
        seen_total += 1
        if gm.group(2).lower() != "valid":
            unproved.append(gm.group(1))
    for gm in _GOAL_STATUS_PREFIX_RX.finditer(raw or ""):   # [Timeout]/[Unknown]/… name
        if gm.group(1) not in unproved:
            unproved.append(gm.group(1))
    if not m and seen_total:                 # no summary line; use per-goal tally
        n_total = seen_total
        n_proved = seen_total - len(unproved)
    return n_proved, n_total, unproved


def run_wp(source_with_acsl: str, frama_c_path: str = "frama-c",
           timeout: int = 120, rte: bool = True, prover: str = None,
           wp_timeout: int = 10, inline: "list[str]" = None,
           exclude_terminates: bool = False) -> WPResult:
    """Run ``frama-c -wp`` over an ACSL-annotated source and parse the verdict.

    ``-wp-rte`` adds runtime-error (memory-safety/overflow) goals so the proof is
    sound, not just functional. Returns WPResult(available=False) if frama-c is
    not on PATH (so callers degrade gracefully).

    ``inline`` names functions whose call sites should be inlined (``-inline-calls``)
    and whose now-redundant standalone proof obligations removed (``-remove-inlined``)
    — used when a verification goal lives in a CALLER but the loop/invariant lives in
    a callee: inlining lets the callee's loop invariant discharge the caller's goal
    WITHOUT a separately-synthesized function contract (the modular-WP boundary that
    CBMC's whole-program unwind hides). ``exclude_terminates`` drops ``@terminates``
    goals (``-wp-prop``) — we synthesize partial-correctness specs (the asserts), not
    loop variants, matching CBMC's bounded semantics."""
    if not frama_c_available(frama_c_path):
        return WPResult(available=False, error="frama-c not installed (not on PATH)")
    # Default to ALL registered preferred provers (alt-ergo→z3→cvc5), tried per
    # goal in order; first to prove wins. An explicit `prover` (single id or
    # comma list) always wins.
    if not prover:
        prover = ",".join(available_wp_provers(frama_c_path))
    with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as tf:
        tf.write(source_with_acsl)
        path = tf.name
    cmd = [frama_c_path]
    if inline:
        fns = ", ".join(inline)
        cmd += [f"-inline-calls={fns}", f"-remove-inlined={fns}"]
    cmd += ["-wp"]
    if rte:
        cmd.append("-wp-rte")
    cmd += [f"-wp-prover", prover, f"-wp-timeout", str(wp_timeout)]
    if exclude_terminates:
        cmd += ["-wp-prop=-@terminates"]
    cmd += [path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return WPResult(available=True, error=f"frama-c -wp timed out ({timeout}s)")
    except OSError as exc:
        return WPResult(available=True, error=f"frama-c invocation failed: {exc}")
    raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
    n_proved, n_total, unproved = parse_wp_output(raw)
    return WPResult(available=True, proved=(n_total > 0 and n_proved == n_total),
                    n_proved=n_proved, n_total=n_total, unproved=unproved, raw=raw)
