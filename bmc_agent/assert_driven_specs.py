"""Assertion-driven spec synthesis.

Mode: given a program annotated with ``//@ assert`` clauses, synthesize the
function POSTCONDITIONS that make every assertion provable — and refine them
when an assertion does not yet hold. The asserts are the GOAL; the function
contracts are the knobs.

Two CBMC checks bound the loop (CBMC is the oracle, the LLM proposes):

  * SUFFICIENCY — do the asserts hold when each callee is replaced by an
    ``__CPROVER_assume(<postcondition>)`` stub? (compositional: the caller is
    proved against the callee CONTRACTS, not their bodies). If not, the
    implicated postcondition is too weak.
  * SOUNDNESS  — is a proposed postcondition actually implied by the callee's
    BODY (nondet inputs)? This is what stops the LLM from "satisfying" a false
    assert by inventing a postcondition the code doesn't honour. If no SOUND
    postcondition makes an assert hold, the assert itself is false — reported,
    not papered over.

The LLM (agentic) builds the stubbed sufficiency harness and proposes stronger
postconditions; CBMC decides. Loop until all asserts hold or a fixpoint /
iteration cap is reached.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from bmc_agent.cbmc import run_cbmc
from bmc_agent.llm import LLMClient, agentic_system_prompt
from bmc_agent.logger import get_logger

logger = get_logger("assert_specs")

_ACSL_ASSERT = re.compile(r"//@\s*assert\s+(.+?)\s*;", re.IGNORECASE)
_CALL_RE_TMPL = r"\b{name}\s*\("

# Verification goals are INPUTS (per the Specification Synthesis Problem): the
# executable goal forms the program already contains, plus the ACSL comment form.
#   assert(E);  static_assert(E[, "msg"]);  __VERIFIER_assert(E);  //@ assert E;
_GOAL_CALL = re.compile(
    r"\b(?:__VERIFIER_assert|static_assert|_Static_assert|assert)\s*\(", re.IGNORECASE)


def _balanced_arg(source: str, open_paren: int) -> tuple[str, int]:
    """Return (arg_text, index_after_close) for the parenthesised argument list
    starting at ``source[open_paren] == '('``. Paren-balanced so nested calls and
    commas inside the expression are handled; respects char/string literals."""
    depth, i, n = 0, open_paren, len(source)
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
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren + 1:i], i + 1
        i += 1
    return source[open_paren + 1:], n


def _strip_assert_message(arg: str) -> str:
    """`static_assert(cond, "msg")` → `cond`. Drop a trailing string-literal
    message argument at top-level (depth 0), keep the condition expression."""
    depth, i, n = 0, 0, len(arg)
    quote = None
    last_top_comma = -1
    while i < n:
        ch = arg[i]
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
        elif ch == "," and depth == 0:
            last_top_comma = i
        i += 1
    if last_top_comma >= 0 and '"' in arg[last_top_comma:]:
        return arg[:last_top_comma].strip()
    return arg.strip()


def extract_goals(source: str) -> list[str]:
    """All verification-goal expressions (INPUTS) in source order, de-duplicated:
    executable ``assert``/``static_assert``/``__VERIFIER_assert`` plus the ACSL
    ``//@ assert`` comment form. The goals are what S must let the verifier prove;
    they are NOT synthesis targets."""
    goals: list[str] = []
    for m in _GOAL_CALL.finditer(source):
        arg, _ = _balanced_arg(source, m.end() - 1)
        expr = _strip_assert_message(arg)
        if expr:
            goals.append(expr.strip())
    goals.extend(extract_asserts(source))   # //@ assert E;
    seen, out = set(), []
    for g in goals:
        if g not in seen:
            seen.add(g); out.append(g)
    return out


@dataclass
class SynthResult:
    ok: bool
    iterations: int
    postconditions: dict = field(default_factory=dict)   # callee -> postcondition
    preconditions: dict = field(default_factory=dict)     # callee -> precondition
    failing_asserts: list = field(default_factory=list)   # asserts still unprovable
    asserts: list = field(default_factory=list)
    note: str = ""
    # No verification goal in the program (no //@ assert / assert / __VERIFIER_assert).
    # Distinct from ok: there is NOTHING to prove, so the run is N/A — reporting it as
    # SATISFIED would be a vacuous pass. ok is forced False so it is never counted as a
    # pass and the oracle-confirmation step is skipped.
    no_goals: bool = False


def extract_asserts(source: str) -> list[str]:
    """Return the list of ``//@ assert`` expressions (in source order)."""
    return [m.group(1).strip() for m in _ACSL_ASSERT.finditer(source)]


def called_functions(source: str, defined: list[str]) -> list[str]:
    """Which defined functions are actually called in the source (call sites)."""
    return [fn for fn in defined if re.search(_CALL_RE_TMPL.format(name=re.escape(fn)), source)
            and re.search(rf"\b\w[\w\s\*]*\b{re.escape(fn)}\s*\([^;]*\)\s*\{{", source)]


def callee_lhs_map(entry_src: str, callees: list[str]) -> dict:
    """Map each callee -> the LHS variables it is assigned to at its call sites.

    Parses ``[type] lhs = callee(...)`` so a failing assert can be traced back to
    the callee whose return value flows into it. Order-preserving, de-duplicated.
    """
    m: dict = {}
    for c in callees:
        lhs = re.findall(rf"(\w+)\s*=\s*{re.escape(c)}\s*\(", entry_src or "")
        if lhs:
            m[c] = list(dict.fromkeys(lhs))
    return m


def attribute_assert(expr: str, lhs_map: dict, callees: list[str]) -> list[str]:
    """Callees implicated by a failing assert, most-likely first.

    A callee is implicated if one of its call-site LHS variables appears as an
    identifier in the assert expression (its return value flows into the assert).
    Implicated callees come first (source order); the remaining callees follow as
    fallbacks, so refinement still progresses when attribution is empty/ambiguous.
    """
    words = set(re.findall(r"\b\w+\b", expr or ""))
    hit = [c for c in callees if any(v in words for v in lhs_map.get(c, []))]
    rest = [c for c in callees if c not in hit]
    return hit + rest


_BUILD_HARNESS_SYS = (
    "You are a CBMC harness engineer doing COMPOSITIONAL verification. You output "
    "ONLY a self-contained C harness in a single fenced ```c block."
)

_BUILD_HARNESS_PROMPT = """\
Build a CBMC harness that proves the `//@ assert` clauses of the entry function
`{entry}` COMPOSITIONALLY — i.e. each call to a contracted callee is replaced by
its CONTRACT, not its body.

For every call `lhs = {callee}(args);` to a contracted function, replace it with:
    /* the caller must ESTABLISH the callee's precondition */
    __CPROVER_assert(<the callee's PRECONDITION, parameters := actual args>, "pre: <P>");
    lhs = <nondet of lhs's type>;
    /* then the caller may ASSUME the callee's postcondition */
    __CPROVER_assume(<the callee's postcondition, with `result` := lhs and the
                      callee's parameters := the actual argument expressions>);
(If the call has no lhs, just emit the assume with result unconstrained. If the
precondition is `true`/empty, the assert may be omitted.)

Translate each `//@ assert E;` in `{entry}` to `__CPROVER_assert(E, "assert: E");`.

Keep all of `{entry}`'s own concrete local setup (variable initialisers, etc.)
verbatim — only the contracted CALLS are replaced. Do NOT include the callee
bodies. Define `int main(void)` that runs `{entry}`'s logic (inline it if
`{entry}` is not already main).

ENTRY FUNCTION:
```c
{entry_src}
```

CONTRACTED CALLEES (name : signature : requires <precondition> : ensures <postcondition>):
{contracts}

Output ONLY the harness in one ```c block.
"""

_REFINE_SYS = (
    "You are a formal-methods engineer strengthening a function postcondition so "
    "a caller's assertion becomes provable. You output ONLY the new postcondition "
    "as a single DSL/boolean expression on one line — no prose, no code fences."
)

_REFINE_PROMPT = """\
The caller assertion `{failing}` is NOT provable from `{callee}`'s current
postcondition:
    {current_post}

Propose a STRONGER postcondition for `{callee}` that (a) makes `{failing}`
provable at the call site, and (b) is ACTUALLY IMPLIED BY THE BODY below (it must
be sound — only state what the code guarantees). Refer to the return value as
`result` and use the parameter names from the signature.

SIGNATURE: {signature}
BODY:
```c
{body}
```

Output ONLY the new postcondition expression on one line.
"""


def _nondet_decl() -> str:
    return ("int __VERIFIER_nondet_int(void);\n"
            "long __VERIFIER_nondet_long(void);\n")


def _extract_c(text: str) -> str:
    m = re.search(r"```(?:c|cpp)?\s*\n(.*?)```", text or "", re.DOTALL)
    return (m.group(1) if m else (text or "")).strip()


def _run(check_src: str, config, entry: str, unwind: int, timeout: int):
    with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as tf:
        tf.write(check_src)
        path = tf.name
    return run_cbmc(
        harness_path=path, function=entry, unwind=unwind, timeout=timeout,
        cbmc_path=getattr(config, "cbmc_path", "cbmc"),
        signed_overflow_check=False, bounds_check=True, pointer_check=True,
    )


def synthesize(
    source_file: str | Path,
    config,
    llm: LLMClient,
    entry: str = "main",
    max_iters: int = 5,
    unwind: int = 16,
    timeout: int = 120,
) -> SynthResult:
    """Run the assertion-driven spec-synthesis loop. Returns a SynthResult."""
    src = Path(source_file).read_text(encoding="utf-8", errors="replace")
    # Verification goals are INPUTS: assert / static_assert / __VERIFIER_assert /
    # //@ assert. They are what S must let the verifier prove, not synthesis targets.
    asserts = extract_goals(src)
    if not asserts:
        # No proof target → N/A, NOT a pass. Counting an assertion-free program as
        # SATISFIED is a vacuous pass (nothing was proved). ok=False keeps it out of
        # the pass bucket; no_goals lets the runner report N/A distinctly.
        return SynthResult(ok=False, iterations=0, no_goals=True,
                           note="no verification goal (no //@ assert / assert / "
                                "__VERIFIER_assert) — nothing to prove")

    from bmc_agent.source_parser import parse_source_file
    from bmc_agent.harness_generator import _c_expressible_postcondition as _cexpr
    parsed = parse_source_file(str(source_file), source_text=src)
    defined = list(parsed.functions.keys())
    entry_src = parsed.function_bodies.get(entry, "")
    callees = [c for c in called_functions(src, defined) if c != entry]
    logger.info("assert-synth: %d assert(s), entry=%s, callees=%s",
                len(asserts), entry, callees)

    # Initial postconditions from Phase-1 spec-gen.
    from bmc_agent.spec_generator_v2 import SpecGeneratorV2
    gen = SpecGeneratorV2(config, llm, _NullStore(), corpus_paths=[Path(source_file)])
    specs = gen.generate_specs(str(source_file), "assertsynth", only_functions=set(callees))
    post = {c: (specs[c].postcondition if c in specs else "true") for c in callees}
    pre = {c: (specs[c].precondition if c in specs else "true") for c in callees}
    sigs = {c: _signature_of(parsed, c) for c in callees}
    bodies = {c: parsed.function_bodies.get(c, "") for c in callees}
    pnames = {c: [n for _, n in (parsed.get_function_info(c).signature.parameters
                                 if parsed.get_function_info(c) else [])] for c in callees}
    lhs_map = callee_lhs_map(entry_src, callees)

    # Engine context for the FAST path: reuse the pipeline's compositional harness
    # (callee stubs) with assume_callee_postcondition=True, so a C-expressible
    # contract propagates to the caller. Best-effort; None → agentic-only.
    eng = _engine_context(source_file, src, config, entry, callees)

    def _cexpr_ok(c, p=None):
        return _cexpr(p if p is not None else post[c], pnames.get(c, [])) is not None

    def _result(ok, it, failing, note, backend=""):
        return SynthResult(ok=ok, iterations=it, postconditions=dict(post),
                           preconditions=dict(pre), failing_asserts=list(failing),
                           asserts=asserts, note=(note + (f" [{backend}]" if backend else "")))

    for it in range(1, max_iters + 1):
        # SUFFICIENCY: engine stub (fast, reuses pipeline) when every contract is
        # C-expressible; otherwise the agentic harness (handles prose/unrolled).
        use_engine = eng is not None and all(_cexpr_ok(c) for c in callees)
        backend = "engine" if use_engine else "agentic"
        if use_engine:
            verdict = _suff_engine(eng, callees, pre, post)
            failing, verified = _failing_asserts(verdict), bool(verdict and verdict.verified)
        else:
            harness = _build_sufficiency_harness(llm, config, entry, entry_src, pre, post, sigs)
            if not harness:
                return _result(False, it, asserts, "could not build sufficiency harness", backend)
            res = _run(_nondet_decl() + harness, config, "main", unwind, timeout)
            failing, verified = _failing_asserts(res), res.verified
        logger.info("assert-synth iter %d [%s]: verified=%s failing=%s", it, backend, verified, failing)
        if verified and not failing:
            return _result(True, it, [], "all //@ asserts provable from synthesized specs", backend)

        if not callees:
            return _result(False, it, failing, "no callee to refine", backend)
        # Attribute the failing assert to the callee whose return value flows into
        # it; try implicated callees first, then the rest. A refinement counts only
        # if it (a) changes the postcondition and (b) is SOUND (implied by the body).
        focus = failing[0] if failing else asserts[0]
        candidates = attribute_assert(focus, lhs_map, callees)
        progressed = False
        saw_changed_unsound = False   # a strictly-stronger proposal failed soundness
        for target in candidates:
            new_post = _refine_postcondition(
                llm, config, target, sigs[target], bodies[target], focus, post[target])
            if not new_post or new_post == post[target]:
                continue   # no new information from this callee — try the next
            # SOUNDNESS gate: body must imply the strengthened postcondition.
            if eng is not None and _cexpr_ok(target, new_post):
                sv = _sound_engine(eng, target, pre[target], new_post)
                sound = bool(sv and sv.verified)
            else:
                sound = _postcondition_sound(llm, config, target, sigs[target],
                                             bodies[target], new_post, unwind, timeout)
            if not sound:
                saw_changed_unsound = True
                logger.info("assert-synth: '%s' proposal unsound, trying next callee", target)
                continue   # unsound for this callee — another callee may carry the assert
            post[target] = new_post
            logger.info("assert-synth: refined '%s' (implicated by %r)", target, focus)
            progressed = True
            break
        if not progressed:
            note = ("no SOUND postcondition strengthening across the implicated callees makes "
                    "the assert provable → assert likely false / not implied by any callee body"
                    if saw_changed_unsound else
                    "refinement proposed no stronger postcondition (fixpoint) — assert unprovable")
            return _result(False, it, failing, note, backend)

    return _result(False, max_iters, asserts,
                   "max iterations reached without satisfying all asserts")


# --- engine backend (fast path: reuse the pipeline's compositional harness) --

_ENTRY_ALIAS = "__assert_entry"   # rename `main` so it doesn't clash with the engine harness's own main


def _engine_context(source_file, src, config, entry, callees):
    """Build a reusable engine context for the C-expressible fast path, or None.

    Translates //@ asserts → __CPROVER_assert, renames `main` (clashes with the
    harness's own main), parses, and wires the pipeline engine with inlining OFF
    + assume_callee_postcondition ON so callee stubs propagate functional
    contracts. Returns a dict, or None on any failure (caller falls back to agentic)."""
    try:
        from bmc_agent.standalone import translate_acsl_asserts
        from bmc_agent.source_parser import parse_source_file
        import re as _re, tempfile as _tf
        translated, _ = translate_acsl_asserts(src)
        # Make executable goal forms checkable by CBMC: __VERIFIER_assert has no
        # body (CBMC would otherwise treat the goal as an uninterpreted call and
        # skip it); map it to a CBMC assertion. `assert` is handled natively.
        if "__VERIFIER_assert" in translated and "#define __VERIFIER_assert" not in translated:
            translated = ('#define __VERIFIER_assert(c) __CPROVER_assert((c), "goal")\n'
                          + translated)
        entry_name = entry
        if entry == "main":
            translated = _re.sub(r"\b(int|void)(\s+)main(\s*\()", rf"\1\2{_ENTRY_ALIAS}\3", translated)
            entry_name = _ENTRY_ALIAS
        with _tf.NamedTemporaryFile("w", suffix=".c", delete=False) as tf:
            tf.write(translated)
            tu = tf.name
        parsed = parse_source_file(tu, source_text=translated)
        entry_func = parsed.get_function_info(entry_name)
        if entry_func is None:
            return None
        all_funcs = {n: parsed.get_function_info(n) for n in parsed.functions}
        config.inline_pure_callees = False
        config.enable_inlining_advisor = False
        config.assume_callee_postcondition = True
        from bmc_agent.pipeline import AMCPipeline
        engine = AMCPipeline(config).bmc_engine
        return {"engine": engine, "parsed": parsed, "all_funcs": all_funcs,
                "entry_name": entry_name, "entry_func": entry_func}
    except Exception as exc:
        logger.info("assert-synth: engine context unavailable (%r) — agentic-only", exc)
        return None


def _suff_engine(eng, callees, pre, post):
    """SUFFICIENCY via the engine: verify the entry with callees stubbed by their
    current contracts (functional postconditions propagated). Returns the verdict."""
    from bmc_agent.spec import Spec
    callee_specs = {c: Spec(function_name=c, precondition=pre[c], postcondition=post[c]) for c in callees}
    entry_spec = Spec(function_name=eng["entry_name"], precondition="true",
                      postcondition="true", callee_specs=callee_specs)
    return eng["engine"].check_function(eng["entry_func"], entry_spec, eng["parsed"],
                                        "assertsynth", all_funcs=eng["all_funcs"])


def _sound_engine(eng, callee, pre_c, new_post):
    """SOUNDNESS via the engine: does the callee body imply `new_post`?"""
    from bmc_agent.spec import Spec
    cf = eng["all_funcs"].get(callee)
    if cf is None:
        return None
    spec = Spec(function_name=callee, precondition=pre_c, postcondition=new_post)
    return eng["engine"].check_function(cf, spec, eng["parsed"], "assertsynth_sound",
                                        all_funcs=eng["all_funcs"])


# --- helpers -----------------------------------------------------------------

class _NullStore:
    def init_driver(self, *a, **k): pass
    def save_spec(self, *a, **k): pass


def _failing_asserts(res) -> list[str]:
    """The //@ assert expressions that CBMC could not prove (from cex). Our
    __CPROVER_assert messages are 'assert: <expr>', so recover <expr>."""
    out = []
    for ce in getattr(res, "counterexamples", []) or []:
        d = (ce.description or "").strip()
        prop = (ce.failing_property or "").lower()
        if d.startswith("assert:"):
            out.append(d[len("assert:"):].strip())
        elif "assertion" in prop:
            out.append(d or ce.failing_property)
    return out


def _signature_of(parsed, fn: str) -> str:
    fi = parsed.get_function_info(fn)
    if not fi:
        return fn
    sig = fi.signature
    params = ", ".join(f"{t} {n}" for t, n in sig.parameters) or "void"
    return f"{sig.return_type} {sig.name}({params})"


def _build_sufficiency_harness(llm, config, entry, entry_src, pre, post, sigs) -> str:
    contracts = "\n".join(
        f"  - {c} : {sigs.get(c, c)} : requires {pre.get(c, 'true')} : ensures {post[c]}"
        for c in post) or "  (none)"
    prompt = _BUILD_HARNESS_PROMPT.format(
        entry=entry, entry_src=entry_src, contracts=contracts, callee=next(iter(post), "f"))
    txt = llm.complete(
        agentic_system_prompt(config, "spec_gen", _BUILD_HARNESS_SYS),
        prompt, max_tokens=2048, role="spec_gen")
    return _extract_c(txt)


def _refine_postcondition(llm, config, callee, signature, body, failing, current) -> str:
    prompt = _REFINE_PROMPT.format(
        failing=failing, callee=callee, current_post=current,
        signature=signature, body=body)
    txt = llm.complete(
        agentic_system_prompt(config, "refinement", _REFINE_SYS),
        prompt, max_tokens=256, role="refinement")
    return (txt or "").strip().splitlines()[0].strip().strip("`").strip() if txt else ""


def _postcondition_sound(llm, config, callee, signature, body, post, unwind, timeout) -> bool:
    """CBMC-check that the callee body implies `post` for nondet inputs. The LLM
    writes a small harness (nondet args -> call body -> assert(post))."""
    sys = ("You write a CBMC harness that checks a postcondition holds for a "
           "function body on fully nondeterministic inputs. Output ONLY a ```c block.")
    prompt = (f"Write a CBMC harness: define the function below verbatim, then in "
              f"main() call it on FULLY NONDET inputs (nondet pointers backed by "
              f"nondet values) and `__CPROVER_assert(<postcondition>, \"sound\")` "
              f"with result bound to the call's return.\n\n"
              f"SIGNATURE: {signature}\nPOSTCONDITION: {post}\nBODY:\n```c\n{body}\n```\n"
              f"Output ONLY the harness in one ```c block.")
    txt = llm.complete(agentic_system_prompt(config, "refinement", sys),
                       prompt, max_tokens=1024, role="refinement")
    h = _extract_c(txt)
    if not h:
        return False
    res = _run(_nondet_decl() + h, config, "main", unwind, timeout)
    # sound iff the postcondition assertion is NOT violated
    return res.verified and not res.error