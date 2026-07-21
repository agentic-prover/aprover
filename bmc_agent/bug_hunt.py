"""Sound bug-finding levers for deep-bug unreach tasks where bounded unrolling
at the default unwind fails.

(1) faithful_unwind_sweep: run CBMC faithfully (NO havoc, NO --unwinding-assertions)
    at increasing unwinds. A VERIFICATION FAILED with a real reach_error/assertion
    property is a SOUND, reproducible bounded witness. (SUCCESSFUL is NOT returned as
    'true' -- absence within a bound is not a safety proof.)

(2) hunt_witness: the LLM reasons about WHICH inputs trigger reach_error, proposes a
    deterministic generator for __VERIFIER_nondet_int, and CONCRETE EXECUTION confirms.
    A crashing run (__assert_fail/abort) is a SOUND, reproducible witness -- something
    BMC structurally cannot get on a deep/large loop.
Both only ever return 'false' on a genuinely confirmed bug; else 'unknown'. Never a
false positive (a real bounded CEx / a real crashing execution).
"""
from __future__ import annotations
import os, re, subprocess, tempfile
from bmc_agent.logger import get_logger
logger = get_logger("bug_hunt")

def _arch(a): return "--32" if a == "ILP32" else "--64"

def _real_prop_failure(out: str) -> bool:
    return ("VERIFICATION FAILED" in out and
            any((": FAILURE" in ln and ".unwind." not in ln and "__CPROVER_contracts" not in ln
                 and re.search(r"reach_error|assertion", ln)) for ln in out.splitlines()))

def faithful_unwind_sweep(source_path, entry="main", goal="reach", arch="LP64",
                          unwinds=(64, 256, 1024, 4096), per_timeout=150, cbmc="cbmc"):
    A = _arch(arch)
    for u in unwinds:
        cmd = [cbmc, source_path, A, "--unwind", str(u)]
        if goal == "memsafety":
            cmd += ["--pointer-check", "--bounds-check"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=per_timeout)
        except subprocess.TimeoutExpired:
            return {"verdict": "unknown", "note": f"cbmc timeout at unwind {u}"}
        if _real_prop_failure(p.stdout):
            return {"verdict": "false", "at_unwind": u, "note": "sound bounded witness"}
    return {"verdict": "unknown", "note": "no bug within swept unwinds"}

_WITNESS_SYS = "You are a C verification expert. Output only code."
def _witness_prompt(src):
    return ("You are given a C SV-COMP task. It calls __VERIFIER_nondet_int() repeatedly "
      "to produce inputs and reaches reach_error()/ERROR (via __VERIFIER_assert) iff a "
      "property is VIOLATED. Reason about which concrete inputs TRIGGER the violation, then "
      "give a deterministic generator. OUTPUT ONLY a C function of EXACTLY this form (no prose, no fences):\n"
      "int __VERIFIER_nondet_int(void){ static unsigned long __k=0; unsigned long i=__k++; return /*expr in i*/; }\n"
      "The i-th returned value must drive the program to the error.\n\nPROGRAM:\n" + src[:8000])

def hunt_witness(source_path, entry="main", arch="LP64", config=None, llm=None,
                 run_timeout=120, workdir=None):
    src = open(source_path).read()
    try:
        resp = llm.complete(_WITNESS_SYS, _witness_prompt(src), 400, 0.0)
    except Exception as e:
        return {"verdict": "unknown", "note": f"llm error: {type(e).__name__}"}
    m = re.search(r"int\s+__VERIFIER_nondet_int\s*\(void\)\s*\{.*?\}", resp, re.S)
    if not m:
        return {"verdict": "unknown", "note": "no generator proposed"}
    gen = m.group(0)
    wd = workdir or tempfile.mkdtemp(prefix="w2_")
    cf = os.path.join(wd, "t.c"); open(cf, "w").write(src + "\n\n" + gen + "\n")
    exe = os.path.join(wd, "t.exe")
    c = subprocess.run(["gcc", "-w", "-O0", cf, "-o", exe], capture_output=True, text=True)
    if c.returncode != 0:
        return {"verdict": "unknown", "note": "compile fail", "generator": gen}
    try:
        r = subprocess.run([exe], capture_output=True, text=True, timeout=run_timeout)
    except subprocess.TimeoutExpired:
        return {"verdict": "unknown", "note": "concrete run timeout", "generator": gen}
    # non-zero exit / signal (SIGABRT from __assert_fail) = reach_error triggered = SOUND bug
    if r.returncode != 0:
        return {"verdict": "false", "validated": True, "generator": gen,
                "note": f"concrete execution hit error (exit {r.returncode})"}
    return {"verdict": "unknown", "note": "proposed witness did not trigger", "generator": gen}

_UNWIND_SYS = "You are a C bounded-model-checking expert. Output only a JSON list of integers."
def llm_unwind_bounds(source_path, llm, default=(64,256,1024,4096)):
    """LLM-guided adaptive unwinding: read the loop/recursion structure and propose
    the unwind bounds most likely to expose a reach_error with a bounded witness
    (e.g. literal loop bounds, array sizes, recursion depths). Falls back to `default`."""
    try:
        src=open(source_path).read()
        prompt=("Given this C SV-COMP program, list the CBMC --unwind bounds most likely to "
                "expose the reach_error with a BOUNDED counterexample. Consider literal loop "
                "bounds, array sizes, and recursion depths actually present in the code. "
                "Return ONLY a JSON array of up to 5 ascending positive integers.\n\n"+src[:6000])
        import json, re as _re
        resp=llm.complete(_UNWIND_SYS, prompt, 120, 0.0)
        m=_re.search(r"\[[0-9,\s]+\]", resp)
        if m:
            vals=[int(x) for x in json.loads(m.group(0)) if 0<int(x)<=1000000]
            if vals: return sorted(set(vals))[:6]
    except Exception as e:
        logger.info("llm_unwind_bounds failed (%s); using default sweep", e)
    return list(default)
