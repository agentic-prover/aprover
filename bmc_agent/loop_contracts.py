"""Loop-contract verification path (loop-level compositional arm).

Two-phase + validated:
  A. GOAL-DIRECTED synthesis -> loop contracts -> cbmc.
       SUCCESSFUL            => proved safe (sound).            verdict=true
       property FAILURE      => bug candidate -> VALIDATE.
       invariant fail/unknown=> fall to B.
  B. GOAL-FREE synthesis (inductive loop SUMMARY, not proving the goal) -> contracts -> cbmc.
       property FAILURE      => bug candidate -> VALIDATE.
       else                  => unknown.
  VALIDATE(bug candidate): plain cbmc on the ORIGINAL program (no contracts) at a
       high unwind, no unwinding-assertions -> a genuine reach_error/assertion FAILURE
       is a REAL concrete witness (sound). Rules out over-approximation false positives.
Soundness: goal-directed SUCCESSFUL is a sound proof (goto-instrument re-checks base/step);
a bug verdict requires an independently-validated concrete counterexample.
"""
from __future__ import annotations
import os, subprocess, tempfile
from bmc_agent.loop_invariants import find_loops, brace_braceless_loops, _inv_to_cbmc
from bmc_agent.logger import get_logger
logger = get_logger("loop_contracts")


def insert_loop_contracts(source: str, annotations: dict) -> str:
    src = brace_braceless_loops(source)
    loops = find_loops(src)
    edits = []
    for lp in loops:
        invs = annotations.get(lp.ordinal, []) or []
        if not invs:
            continue
        combined = " && ".join(f"({_inv_to_cbmc(i)})" for i in invs)
        edits.append((lp.head_offset - 1, f"\n  __CPROVER_loop_invariant({combined})\n  "))
    out = src
    for off, stmt in sorted(edits, key=lambda e: -e[0]):
        out = out[:off] + stmt + out[off:]
    return out


def _arch_flag(arch): return "--32" if arch == "ILP32" else "--64"


def _classify(out: str) -> str:
    """true | prop_fail | inv_fail | unknown  (from a cbmc run on the abstracted goto)."""
    if "VERIFICATION SUCCESSFUL" in out:
        return "true"
    if "VERIFICATION FAILED" in out:
        fails = [ln for ln in out.splitlines() if ": FAILURE" in ln]
        prop = any(("loop_invariant" not in ln and "__CPROVER_contracts" not in ln
                    and ".unwind." not in ln) for ln in fails)
        if prop:
            return "prop_fail"
        if any("loop_invariant" in ln for ln in fails):
            return "inv_fail"
    return "unknown"


def _apply_and_check(source_path, ann, entry, goal, arch, wd, tag, cbmc_timeout, log):
    src = open(source_path).read()
    annotated = insert_loop_contracts(src, ann)
    cpath = os.path.join(wd, f"annotated_{tag}.c"); open(cpath, "w").write(annotated)
    A = _arch_flag(arch)
    g1, g2 = os.path.join(wd, f"{tag}.goto"), os.path.join(wd, f"{tag}.i.goto")
    def _r(cmd, t):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
        log.append(f"$ {' '.join(cmd)}\n{p.stdout[-1500:]}\n{p.stderr[-300:]}"); return p
    _r(["goto-cc", A, cpath, "-o", g1], 120)
    _r(["goto-instrument", "--dfcc", entry, "--apply-loop-contracts", g1, g2], 120)
    cmd = ["cbmc", g2, "--unwind", "1", "--unwinding-assertions"]
    if goal == "memsafety":
        cmd += ["--pointer-check", "--bounds-check"]
    p = _r(cmd, cbmc_timeout)
    return _classify(p.stdout), cpath


def _validate_bug(source_path, entry, goal, arch, wd, log, unwind=1000, timeout=300):
    """Confirm a bug candidate with a REAL concrete witness: plain cbmc on the
    ORIGINAL (no loop contracts) at a high unwind, WITHOUT unwinding-assertions.
    A genuine property FAILURE (not an unwinding assertion) is a sound real bug."""
    A = _arch_flag(arch)
    cmd = ["cbmc", source_path, A, "--unwind", str(unwind)]
    if goal == "memsafety":
        cmd += ["--pointer-check", "--bounds-check"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.append("validate: cbmc timeout -> unconfirmed"); return False
    out = p.stdout
    log.append(f"$ VALIDATE {' '.join(cmd)}\n{out[-1200:]}")
    if "VERIFICATION FAILED" not in out:
        return False
    real = [ln for ln in out.splitlines() if ": FAILURE" in ln and ".unwind." not in ln
            and "__CPROVER_contracts" not in ln]
    return bool(real)


def _synth(source_path, entry, config, llm, goal_free):
    from bmc_agent.loop_invariants import synthesize_loop_invariants
    try: config.goal_free = goal_free
    except Exception: pass
    # Bound synthesis churn (each refine iter is an LLM call): a hard multi-loop
    # task otherwise burns many minutes/calls trying an impossible invariant.
    r = synthesize_loop_invariants(source_path, config, llm, entry=entry, max_iters=3)
    return dict(getattr(r, "annotations", {}) or {}), getattr(r, "ok", None)


def verify_with_loop_contracts(source_path, entry="main", goal="reach", arch="LP64",
                               config=None, llm=None, workdir=None, cbmc_timeout=300,
                               validate_unwind=1000):
    wd = workdir or tempfile.mkdtemp(prefix="lc_"); os.makedirs(wd, exist_ok=True)
    log = []; mode = None
    def _finish(verdict, ann, validated=None, note=""):
        return {"verdict": verdict, "mode": mode, "validated": validated, "note": note,
                "n_invariants": sum(len(v or []) for v in ann.values()),
                "workdir": wd, "log": "\n".join(log)}
    try:
        # Phase A: goal-directed (proving)
        mode = "goal_directed"
        annA, okA = _synth(source_path, entry, config, llm, goal_free=False)
        vA, _ = _apply_and_check(source_path, annA, entry, goal, arch, wd, "A", cbmc_timeout, log)
        if vA == "true":
            return _finish("true", annA, note="proved safe (goal-directed)")
        if vA == "prop_fail":
            ok = _validate_bug(source_path, entry, goal, arch, wd, log, validate_unwind)
            return _finish("false" if ok else "unknown", annA, validated=ok,
                           note="bug candidate (goal-directed) " + ("VALIDATED" if ok else "unconfirmed"))
        # Phase B: goal-free (bug-finding)
        mode = "goal_free"
        annB, okB = _synth(source_path, entry, config, llm, goal_free=True)
        vB, _ = _apply_and_check(source_path, annB, entry, goal, arch, wd, "B", cbmc_timeout, log)
        if vB == "prop_fail":
            ok = _validate_bug(source_path, entry, goal, arch, wd, log, validate_unwind)
            return _finish("false" if ok else "unknown", annB, validated=ok,
                           note="bug candidate (goal-free) " + ("VALIDATED" if ok else "unconfirmed"))
        if vB == "true":
            # goal-free proved safe too (rare) -> report unknown; not a sound proof target
            return _finish("unknown", annB, note="goal-free SUCCESSFUL (weak-inv, not a sound proof)")
        return _finish("unknown", annB, note=f"goal-free {vB}")
    except subprocess.TimeoutExpired:
        logger.warning("loop_contracts: timed out -> unknown")
        return _finish("unknown", {}, note="loop_contracts timeout")
    except Exception as e:
        # Degrade gracefully: any failure (goto-cc/goto-instrument on a large or
        # unusual file, synthesis error, etc.) is inconclusive, not a crash.
        logger.warning("loop_contracts: aborted (%s: %s) -> unknown",
                       type(e).__name__, str(e)[:200])
        return _finish("unknown", {},
                       note="loop_contracts aborted: %s: %s" % (type(e).__name__, str(e)[:150]))
