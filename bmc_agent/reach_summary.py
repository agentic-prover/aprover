"""Compositional REACHABILITY (extends the DSL beyond memsafety) + assume-guarantee.

Each function gets a reaches_error SUMMARY (DSL condition on inputs under which it
reaches reach_error). Compositional analysis STUBS a callee by its summary --
`if (<cond>) reach_error();` (body NOT inlined) -- so a deep bug inside a callee is
exposed in the caller. A composed bug counts as SOUND only if each used summary is
VERIFIED by the assume-guarantee obligation: under assume(cond) the callee genuinely
reaches reach_error (faithful unroll if feasible; loop-contract abstraction otherwise).
"""
from __future__ import annotations
import os, re, subprocess, tempfile, json
from bmc_agent.logger import get_logger
logger = get_logger("reach_summary")

_SYS = "You are a C verification expert. Output only JSON."

def _prompt(src, callees):
    return ("For each listed callee of this program, give the DSL condition (a C boolean "
            "expression over its PARAMETERS) under which the callee reaches reach_error/ERROR. "
            'Use "false" if it never does. Return ONLY JSON: {"callee": "<cond>", ...}.\n'
            "Callees: " + str(callees) + "\n\nPROGRAM:\n" + src[:7000])

def _find_defs(src):
    defs = {}
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{", src):
        name = m.group(1)
        if name in ("if", "for", "while", "switch", "sizeof", "return"):
            continue
        b = src.index("{", m.start()); depth = 0; i = b
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    defs[name] = (m.start(), b, i); break
            i += 1
    return defs

def _verify_summary(src, callee, cond, arch, config, llm, timeout=200):
    """AG obligation: under assume(cond), does the callee genuinely reach reach_error?"""
    m = re.search(r"\b[A-Za-z_][\w \*]*\b\s+" + re.escape(callee) + r"\s*\(([^)]*)\)\s*\{", src)
    if not m:
        return "UNVERIFIED(no-sig)"
    decls, args = [], []
    for pp in m.group(1).split(","):
        pp = pp.strip()
        if not pp or pp == "void":
            continue
        if "*" in pp or "[" in pp:
            return "UNVERIFIED(nonscalar-param)"
        tm = re.match(r"(.+?)([A-Za-z_]\w*)\s*$", pp)
        if not tm:
            return "UNVERIFIED(param-parse)"
        typ, nm = tm.group(1).strip(), tm.group(2)
        decls.append(typ + " " + nm + " = (" + typ + ")__VERIFIER_nondet_int();")
        args.append(nm)
    hmain = ("int main(void){ " + " ".join(decls) + " __CPROVER_assume(" + cond + "); "
             + callee + "(" + ",".join(args) + "); return 0; }")
    defs = _find_defs(src); src2 = src
    if "main" in defs:
        ms, _b, me = defs["main"]; src2 = src[:ms] + src[me + 1:]
    src2 = src2 + chr(10) + hmain
    wd = tempfile.mkdtemp(prefix="ag_"); hf = os.path.join(wd, "ag.c"); open(hf, "w").write(src2)
    try:
        from bmc_agent.bug_hunt import faithful_unwind_sweep
        r = faithful_unwind_sweep(hf, entry="main", goal="reach", arch=arch,
                                  unwinds=(64, 256, 1024), per_timeout=max(60, timeout // 2))
        if r.get("verdict") == "false":
            return "VERIFIED(unroll@" + str(r.get("at_unwind")) + ")"
    except Exception:
        pass
    try:
        from bmc_agent.loop_contracts import verify_with_loop_contracts
        r2 = verify_with_loop_contracts(hf, entry="main", goal="reach", arch=arch,
                                        config=config, llm=llm, cbmc_timeout=timeout)
        if r2.get("verdict") == "false":
            return "VERIFIED(loop-contract)"
        return "UNVERIFIED(abstraction:" + str(r2.get("verdict")) + ")"
    except Exception as e:
        return "UNVERIFIED(err:" + type(e).__name__ + ")"

def compositional_reach(source_path, entry="main", arch="LP64", config=None, llm=None, cbmc_timeout=200):
    src = open(source_path).read()
    defs = _find_defs(src)
    if entry not in defs:
        return {"verdict": "unknown", "note": "entry " + entry + " not found"}
    _s, eb, ee = defs[entry]; ebody = src[eb:ee]
    callees = [n for n in defs if n != entry and n not in ("reach_error", "abort", "__assert_fail")
               and re.search(r"\b" + re.escape(n) + r"\s*\(", ebody)]
    if not callees:
        return {"verdict": "unknown", "note": "no stubbable callees in entry"}
    try:
        resp = llm.complete(_SYS, _prompt(src, callees), 300, 0.0)
        mm = re.search(r"\{.*\}", resp, re.S); summ = json.loads(mm.group(0)) if mm else {}
    except Exception as e:
        return {"verdict": "unknown", "note": "llm error " + type(e).__name__}
    out = src; edits = []
    for n in callees:
        cond = summ.get(n, "false")
        if not cond or cond == "false":
            continue
        _s2, b, e = defs[n]
        edits.append((b, e + 1, "{ if (" + cond + ") { reach_error(); } }"))
    if not edits:
        return {"verdict": "unknown", "note": "no callee reaches_error>false", "summaries": summ}
    for b, e, rep in sorted(edits, key=lambda x: -x[0]):
        out = out[:b] + rep + out[e:]
    wd = tempfile.mkdtemp(prefix="rs_"); cf = os.path.join(wd, "stubbed.c"); open(cf, "w").write(out)
    A = "--32" if arch == "ILP32" else "--64"
    p = subprocess.run(["cbmc", cf, A, "--unwind", "3"], capture_output=True, text=True, timeout=cbmc_timeout)
    o = p.stdout
    vf = ("false" if ("VERIFICATION FAILED" in o and any(": FAILURE" in l and ".unwind." not in l for l in o.splitlines()))
          else "true" if "VERIFICATION SUCCESSFUL" in o else "unknown")
    ag = {}
    if vf == "false":
        for n in callees:
            c = summ.get(n, "false")
            if c and c != "false":
                ag[n] = _verify_summary(src, n, c, arch, config, llm, timeout=cbmc_timeout)
    all_ok = bool(ag) and all(v.startswith("VERIFIED") for v in ag.values())
    final = "false" if (vf == "false" and all_ok) else ("candidate" if vf == "false" else vf)
    return {"verdict": final, "raw_stub_verdict": vf, "summaries": summ, "assume_guarantee": ag,
            "sound": all_ok, "note": "compositional-reach + AG: bug counts only if every used summary is verified"}
