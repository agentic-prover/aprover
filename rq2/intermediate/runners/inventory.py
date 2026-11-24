import re, sys, os, glob, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from norm import records

PAT = ["kernsweep_*.log", "resil_*.log", "anth_*.log", "strag_*.log", "ttf_rerun_*.log"]
ROOT = "/home/syc/AProver/findings"

rows = []
for p in PAT:
    for f in sorted(glob.glob(os.path.join(ROOT, p))):
        head = open(f, errors="replace").read(400)
        m = re.search(r"Full verification pipeline for: (\S+)", head)
        d = re.search(r"Driver: (\S+)", head)
        if not m or not d or "vibeos" not in d.group(1):
            continue
        nspec = nver = start = last = end = None
        for ts, lev, msg in records(f):
            if ts:
                if start is None:
                    start = ts
                last = ts
            if msg.startswith("Phase 1 complete:"):
                g = re.search(r"(\d+) specs", msg)
                nspec = int(g.group(1)) if g else None
            if msg.startswith("Phase 2 complete:"):
                g = re.search(r"(\d+) verdicts", msg)
                nver = int(g.group(1)) if g else None
            if "AMC Pipeline END" in msg:
                end = msg
        rows.append(dict(log=os.path.basename(f), src=m.group(1), driver=d.group(1),
                         specs=nspec, verdicts=nver, done=bool(end),
                         start=start, last=last, end=end))

json.dump(rows, open(os.path.join(os.environ.get("BQ6_OUT", os.path.dirname(os.path.abspath(__file__))), "inventory.json"), "w"), indent=1)
print("%d vibeos logs" % len(rows))
for r in rows:
    print("%-22s %-44s specs=%4s verdicts=%4s done=%d" %
          (r["driver"], r["log"], r["specs"], r["verdicts"], r["done"]))
