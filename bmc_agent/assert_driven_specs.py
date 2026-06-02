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


@dataclass
class SynthResult:
    ok: bool
    iterations: int
    postconditions: dict = field(default_factory=dict)   # callee -> postcondition
    preconditions: dict = field(default_factory=dict)     # callee -> precondition
    failing_asserts: list = field(default_factory=list)   # asserts still unprovable
    asserts: list = field(default_factory=list)
    note: str = ""


def extract_asserts(source: str) -> list[str]:
    """Return the list of ``//@ assert`` expressions (in source order)."""
    return [m.group(1).strip() for m in _ACSL_ASSERT.finditer(source)]


def called_functions(source: str, defined: list[str]) -> list[str]:
    """Which defined functions are actually called in the source (call sites)."""
    return [fn for fn in defined if re.search(_CALL_RE_TMPL.format(name=re.escape(fn)), source)
            and re.search(rf"\b\w[\w\s\*]*\b{re.escape(fn)}\s*\([^;]*\)\s*\{{", source)]


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
    asserts = extract_asserts(src)
    if not asserts:
        return SynthResult(ok=True, iterations=0, note="no //@ assert clauses found")

    from bmc_agent.source_parser import parse_source_file
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

    def _result(ok, it, failing, note):
        return SynthResult(ok=ok, iterations=it, postconditions=dict(post),
                           preconditions=dict(pre), failing_asserts=list(failing),
                           asserts=asserts, note=note)

    for it in range(1, max_iters + 1):
        harness = _build_sufficiency_harness(llm, config, entry, entry_src, pre, post, sigs)
        if not harness:
            return _result(False, it, asserts, "could not build sufficiency harness")
        res = _run(_nondet_decl() + harness, config, "main", unwind, timeout)
        failing = _failing_asserts(res)
        logger.info("assert-synth iter %d: sufficiency verified=%s, failing=%s",
                    it, res.verified, failing)
        if res.verified and not failing:
            return _result(True, it, [], "all //@ asserts provable from synthesized specs")

        # Refine the postcondition of the (single) callee, grounded in its body.
        target = callees[0] if callees else None
        if not target:
            return _result(False, it, failing, "no callee to refine")
        new_post = _refine_postcondition(
            llm, config, target, sigs[target], bodies[target],
            failing[0] if failing else asserts[0], post[target])
        if not new_post or new_post == post[target]:
            return _result(False, it, failing,
                           "refinement did not change the postcondition — "
                           "assert likely false / not implied by the body")
        # SOUNDNESS gate: the body must actually guarantee the new postcondition.
        if not _postcondition_sound(llm, config, target, sigs[target],
                                    bodies[target], new_post, unwind, timeout):
            return _result(False, it, failing,
                           f"proposed postcondition for '{target}' is NOT implied "
                           f"by its body (would be unsound) → assert is false")
        post[target] = new_post

    return _result(False, max_iters, asserts,
                   "max iterations reached without satisfying all asserts")


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