"""B-Q6 sweep statistics, recomputed from the raw bmc-agent run logs.

Scope: the VibeOS kernel sweep.  One canonical run per module, defined as the
LAST run of that module that reached "AMC Pipeline END".  Re-runs and crashed
runs are excluded; see --show-runs.
"""
import re, sys, os, glob, json, csv, collections, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from norm import records

ROOT = os.environ.get("AMC_FINDINGS", "/home/syc/AProver/findings")
PAT = ["kernsweep_*.log", "resil_*.log", "anth_*.log", "strag_*.log", "ttf_rerun_*.log"]

R_SPECS   = re.compile(r"^Phase 1 complete: (\d+) specs generated")
R_VERD    = re.compile(r"^Phase 2 complete: (\d+) verdicts")
R_CBMC    = re.compile(r"^CBMC verdict for '([^']+)': verified=(True|False), counterexamples=(\d+)")
R_DEDUP   = re.compile(r"^Deduped (\d+) counterexamples . (\d+)")
R_CHECK   = re.compile(r"^Checking function '([^']+)'")
R_REBUILD = re.compile(r"^agentic harness repair: rebuilding harness for '([^']+)'")
R_FIXED   = re.compile(r"^agentic harness repair resolved the build error for '([^']+)'")
R_NOFIX   = re.compile(r"^agentic harness repair produced no clean harness for '([^']+)'")
R_RVAL    = re.compile(r"^Validating counterexample for '([^']+)'")
R_RREALM  = re.compile(r"^Realism check for '([^']+)' \(property: ([^)]+)\)")
R_RVERD   = re.compile(r"^Realism check for '([^']+)': verdict=(\w+)")
R_RITER   = re.compile(r"^Refinement iteration (\d+)/(\d+) for '([^']+)'")
R_RSTALL  = re.compile(r"^Refinement stalled at iteration")
R_GUARD   = re.compile(r"^Over-refinement check:")
R_GREJ    = re.compile(r"^Over-refinement detected at iteration")
R_USAGE   = re.compile(r"^LLM usage \((\w+)\): input_tokens=(\d+) output_tokens=(\d+) "
                       r"cache_creation=(\d+) cache_read=(\d+)")
R_END     = re.compile(r"AMC Pipeline END: (\d+) real bug")
R_ROLE    = re.compile(r"^([a-z_]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)$")

# Published list prices, US dollars per million tokens.  Stated here so that any
# cost figure derived from this artefact carries its price basis with it.
PRICES = {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}


def parse(path):
    d = dict(specs=None, verdicts=None, done=False,
             cbmc_cex=0, cbmc_fn_with_cex=set(), dedup_raw=0, dedup_out=0, dedup_events=0,
             checked=set(), rebuilt=set(), fixed=set(), nofix=set(),
             validated=0, realism_props=set(), realism_verdicts=collections.Counter(),
             refine_iters=0, refine_stall=0, guard_ran=0, guard_rejected=0,
             usage=collections.Counter(), usage_calls=0,
             roles=collections.Counter(), role_secs=collections.Counter(),
             t0=None, t1=None, real_bugs=None)
    for ts, lev, m in records(path):
        if ts:
            if d["t0"] is None:
                d["t0"] = ts
            d["t1"] = ts
        g = R_SPECS.match(m)
        if g: d["specs"] = int(g.group(1)); continue
        g = R_VERD.match(m)
        if g: d["verdicts"] = int(g.group(1)); continue
        g = R_CBMC.match(m)
        if g:
            n = int(g.group(3))
            d["cbmc_cex"] += n
            if n: d["cbmc_fn_with_cex"].add(g.group(1))
            continue
        g = R_DEDUP.match(m)
        if g:
            d["dedup_raw"] += int(g.group(1)); d["dedup_out"] += int(g.group(2))
            d["dedup_events"] += 1
            continue
        g = R_CHECK.match(m)
        if g: d["checked"].add(g.group(1)); continue
        g = R_REBUILD.match(m)
        if g: d["rebuilt"].add(g.group(1)); continue
        g = R_FIXED.match(m)
        if g: d["fixed"].add(g.group(1)); continue
        g = R_NOFIX.match(m)
        if g: d["nofix"].add(g.group(1)); continue
        if R_RVAL.match(m): d["validated"] += 1; continue
        g = R_RREALM.match(m)
        if g: d["realism_props"].add((g.group(1), g.group(2))); continue
        g = R_RVERD.match(m)
        if g: d["realism_verdicts"][g.group(2)] += 1; continue
        if R_RITER.match(m): d["refine_iters"] += 1; continue
        if R_RSTALL.match(m): d["refine_stall"] += 1; continue
        if R_GUARD.match(m): d["guard_ran"] += 1; continue
        if R_GREJ.match(m): d["guard_rejected"] += 1; continue
        g = R_USAGE.match(m)
        if g:
            d["usage"][g.group(1) + ".in"] += int(g.group(2))
            d["usage"][g.group(1) + ".out"] += int(g.group(3))
            d["usage"][g.group(1) + ".cache_w"] += int(g.group(4))
            d["usage"][g.group(1) + ".cache_r"] += int(g.group(5))
            d["usage_calls"] += 1
            continue
        g = R_ROLE.match(m)
        if g:
            d["roles"][g.group(1)] += int(g.group(2))
            d["role_secs"][g.group(1)] += float(g.group(7))
            continue
        g = R_END.search(m)
        if g: d["done"] = True; d["real_bugs"] = int(g.group(1))
    return d


def wall(d):
    if not d["t0"] or not d["t1"]:
        return None
    f = "%m/%d/%y %H:%M:%S"
    a = datetime.datetime.strptime(d["t0"], f)
    b = datetime.datetime.strptime(d["t1"], f)
    s = (b - a).total_seconds()
    return s + 86400 if s < 0 else s


def main():
    runs = []
    for p in PAT:
        for f in sorted(glob.glob(os.path.join(ROOT, p))):
            head = open(f, errors="replace").read(400)
            dr = re.search(r"Driver: (\S+)", head)
            if not dr or "vibeos" not in dr.group(1):
                continue
            d = parse(f)
            d["log"] = os.path.basename(f)
            d["module"] = dr.group(1).replace("vibeos_", "")
            runs.append(d)

    if "--show-runs" in sys.argv:
        for d in sorted(runs, key=lambda r: (r["module"], r["log"])):
            print("%-14s %-44s done=%d specs=%-4s verdicts=%-4s cex=%-5d" %
                  (d["module"], d["log"], d["done"], d["specs"], d["verdicts"], d["cbmc_cex"]))
        return

    canon = {}
    for d in runs:
        if d["done"]:
            canon[d["module"]] = d          # sorted order, so last wins
    sel = [canon[m] for m in sorted(canon)]

    T = collections.Counter()
    usage = collections.Counter()
    roles = collections.Counter()
    role_secs = collections.Counter()
    rows = []
    for d in sel:
        nrebuilt = len(d["rebuilt"]); nfixed = len(d["fixed"] & d["rebuilt"])
        rows.append(dict(module=d["module"], log=d["log"], specs=d["specs"],
                         verdicts=d["verdicts"], cbmc_cex=d["cbmc_cex"],
                         dedup_raw=d["dedup_raw"], dedup_out=d["dedup_out"],
                         harness_rebuilt=nrebuilt, harness_fixed=nfixed,
                         harness_nofix=len(d["nofix"]),
                         validated=d["validated"], realism_in=len(d["realism_props"]),
                         realistic=d["realism_verdicts"]["realistic"],
                         unrealistic=d["realism_verdicts"]["unrealistic"],
                         refine_iters=d["refine_iters"], refine_stall=d["refine_stall"],
                         guard_ran=d["guard_ran"], guard_rejected=d["guard_rejected"],
                         llm_calls=d["usage_calls"], wall_s=wall(d), real_bugs=d["real_bugs"]))
        for k in ("specs", "verdicts", "cbmc_cex", "dedup_raw", "dedup_out",
                  "validated", "refine_iters", "refine_stall", "guard_ran",
                  "guard_rejected", "usage_calls"):
            T[k] += d[k] or 0
        T["harness_rebuilt"] += nrebuilt
        T["harness_fixed"] += nfixed
        T["harness_nofix"] += len(d["nofix"])
        T["realism_in"] += len(d["realism_props"])
        T["realistic"] += d["realism_verdicts"]["realistic"]
        T["unrealistic"] += d["realism_verdicts"]["unrealistic"]
        usage += d["usage"]
        roles += d["roles"]
        role_secs += d["role_secs"]

    out = os.environ.get("BQ6_OUT", os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(out, "sweep_per_module.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

    n = T["verdicts"]
    print("VibeOS kernel sweep, %d modules, canonical runs only" % len(sel))
    print("  functions with specs generated  %d" % T["specs"])
    print("  functions with a BMC verdict    %d" % n)
    print()
    print("HARNESS COMPILATION")
    print("  needed agentic repair           %d" % T["harness_rebuilt"])
    print("  repair resolved the build error %d" % T["harness_fixed"])
    print("  repair produced no clean harness%d" % T["harness_nofix"])
    print("  compiled before repair          %.1f%%  (%d/%d)" %
          (100.0 * (n - T["harness_rebuilt"]) / n, n - T["harness_rebuilt"], n))
    print("  compiled after repair           %.1f%%  (%d/%d)" %
          (100.0 * (n - T["harness_rebuilt"] + T["harness_fixed"]) / n,
           n - T["harness_rebuilt"] + T["harness_fixed"], n))
    print()
    print("COUNTEREXAMPLE FUNNEL")
    print("  dedup input  (raw witnesses)    %d   [%d dedup events]" % (T["dedup_raw"], T["dedup_events"] if "dedup_events" in T else -1))
    print("  dedup output                    %d" % T["dedup_out"])
    print("  CBMC verdict cex total          %d" % T["cbmc_cex"])
    print("  entered validation              %d" % T["validated"])
    print("  reached the realism audit       %d" % T["realism_in"])
    print("  realistic                       %d" % T["realistic"])
    print("  unrealistic                     %d" % T["unrealistic"])
    print()
    print("REFINEMENT")
    print("  iterations                      %d" % T["refine_iters"])
    print("  ended without a proposal        %d" % T["refine_stall"])
    print("  reached the guard               %d" % T["guard_ran"])
    print("  guard rejected                  %d" % T["guard_rejected"])
    print()
    print("LLM USAGE  (%d recorded calls)" % T["usage_calls"])
    for k in sorted(usage):
        print("  %-22s %12d" % (k, usage[k]))
    print()
    print("AGENT TELEMETRY  (per-role call counts, from each run's end-of-run table)")
    tot_calls = 0
    for k in sorted(roles):
        tot_calls += roles[k]
        print("  %-22s calls=%-6d wall_s=%.0f" % (k, roles[k], role_secs[k]))
    print("  %-22s calls=%d" % ("TOTAL", tot_calls))
    print("  recorded-usage coverage %.1f%% (%d usage lines / %d telemetry calls)"
          % (100.0 * T["usage_calls"] / tot_calls if tot_calls else 0, T["usage_calls"], tot_calls))
    print()
    cost = (usage["anthropic.in"] * PRICES["input"]
            + usage["anthropic.out"] * PRICES["output"]
            + usage["anthropic.cache_w"] * PRICES["cache_write"]
            + usage["anthropic.cache_r"] * PRICES["cache_read"]) / 1e6
    print("COST FROM RECORDED TOKENS  $%.2f  at in/out/cw/cr = %s USD per Mtok" %
          (cost, "/".join("%.2f" % PRICES[k] for k in ("input", "output", "cache_write", "cache_read"))))
    print("  this is a LOWER BOUND: only %d of %d agent calls recorded a usage line" %
          (T["usage_calls"], tot_calls))
    extrap = cost * tot_calls / T["usage_calls"] if T["usage_calls"] else 0
    print("  EXTRAPOLATED to all telemetry calls  $%.2f  (assumes the unrecorded calls"
          % extrap)
    print("  have the same mean token cost as the recorded ones, which is unverified)")
    json.dump(dict(totals=dict(T), usage=dict(usage), roles=dict(roles),
                   prices=PRICES, cost_lower_bound_usd=round(cost, 2),
                   cost_extrapolated_usd=round(extrap, 2),
                   telemetry_calls=tot_calls,
                   modules=[r["module"] for r in rows]),
              open(os.path.join(out, "sweep_totals.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
