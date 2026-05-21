#!/usr/bin/env python3
"""Walk bmc-agent sweep output dirs and surface real_bug findings whose
realism_check is null or uncertain — i.e., the realism filter didn't get
to weigh in. These are the candidates that need human triage.

Usage:
    python3 findings/find_unfiltered_real_bugs.py [extra-sweep-dir ...]

The script knows about today's sweep dirs by default. Pass any additional
/tmp/aprover_* sweep root as a CLI arg.
"""
import json
import sys
from pathlib import Path

DEFAULT_ROOTS = [
    "/tmp/aprover_neuron_or_sweep",
    "/tmp/aprover_neuron_hybrid_p2",
    "/tmp/aprover_llama_nghttp2_or",
]


def main(argv):
    roots = [Path(p) for p in (DEFAULT_ROOTS + argv[1:])]
    survivors = []
    for root in roots:
        if not root.exists():
            continue
        for stem_dir in sorted(root.iterdir()):
            if not stem_dir.is_dir():
                continue
            for driver in stem_dir.iterdir():
                if not driver.is_dir() or not driver.name.endswith(
                    ("_or", "_hybrid", "_p2", "_recheck")
                ):
                    continue
                for fn_dir in sorted(driver.iterdir()):
                    if not fn_dir.is_dir():
                        continue
                    cls = fn_dir / "classification.json"
                    bug = fn_dir / "bug_report.json"
                    if not cls.exists():
                        continue
                    try:
                        c = json.load(open(cls))
                        d = json.load(open(bug)) if bug.exists() else {}
                    except Exception:
                        continue
                    outcome = (c.get("classification") or {}).get("outcome")
                    if outcome != "real_bug":
                        continue
                    rc = c.get("realism_check") or {}
                    rc_b = (d.get("report") or {}).get("realism_check") or {}
                    rv = (
                        rc.get("verdict") if isinstance(rc, dict) else None
                    ) or (rc_b.get("verdict") if isinstance(rc_b, dict) else None)
                    if rv in ("unrealistic", "realistic"):
                        continue
                    survivors.append(
                        (
                            str(stem_dir.name),
                            fn_dir.name,
                            (d.get("report") or {}).get("violated_property") or "",
                            (d.get("report") or {}).get("confidence") or "",
                            rv or "null",
                        )
                    )
    if not survivors:
        print("(none)")
    else:
        print(
            f"{'FILE':<22} {'FUNCTION':<40} {'PROPERTY':<48} {'CONF':<24} {'REALISM'}"
        )
        for s in survivors:
            print(
                f"{s[0]:<22} {s[1]:<40} {s[2][:48]:<48} {s[3]:<24} {s[4]}"
            )


if __name__ == "__main__":
    main(sys.argv)
