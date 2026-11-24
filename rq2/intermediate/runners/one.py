import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep_stats as S
for f in sys.argv[1:]:
    d = S.parse(f)
    print("%-62s done=%d specs=%s verd=%s raw=%d ded=%d realism=%d realistic=%d iters=%d stall=%d guard=%d rej=%d rebuilt=%d fixed=%d" %
          (os.path.basename(os.path.dirname(f)) + "/" + os.path.basename(f), d["done"], d["specs"], d["verdicts"],
           d["dedup_raw"], d["dedup_out"], len(d["realism_props"]), d["realism_verdicts"]["realistic"],
           d["refine_iters"], d["refine_stall"], d["guard_ran"], d["guard_rejected"],
           len(d["rebuilt"]), len(d["fixed"] & d["rebuilt"])))
