"""Diff two TriageAgent runs on the same sweep artifact.

After the analyst-review-driven prompt update (reachability gates +
confirmation-not-exploit reframe) landed, we re-triaged the existing
postfix8 CExes. The v3 outputs got copied to ``*.triage.v3.json``
sidecars before the re-run overwrote ``*.triage.json``. This script
diffs the two and prints:

  * per-CEx verdict changes (v3 → v4)
  * net verdict-count delta
  * the new FP classes the v4 prompt is finding

Usage:
    .venv/bin/python scripts/compare_triage_versions.py \\
        --sweep-dir /tmp/libarchive_postfix8 \\
        --driver libarchive_postfix8
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def load_pair(triage_json: Path) -> tuple[dict | None, dict | None]:
    """Return (v3 sidecar, v4 current). Either may be None."""
    v4 = None
    try:
        v4 = json.loads(triage_json.read_text())
    except Exception:
        pass
    v3_path = triage_json.with_name(triage_json.stem + ".v3.json")
    v3 = None
    try:
        v3 = json.loads(v3_path.read_text())
    except Exception:
        pass
    return v3, v4


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep-dir", required=True, type=Path)
    ap.add_argument("--driver", required=True)
    args = ap.parse_args()

    driver_dir = args.sweep_dir / args.driver
    if not driver_dir.is_dir():
        print(f"ERROR: {driver_dir} missing", file=sys.stderr)
        return 2

    # Walk: <driver>/<file_stem>/<fn>/classifications/<prop>.triage.json
    triages: list[Path] = []
    for first in driver_dir.iterdir():
        if not first.is_dir():
            continue
        if (first / "classifications").is_dir():
            triages.extend(first.glob("classifications/*.triage.json"))
        else:
            for second in first.iterdir():
                if second.is_dir() and (second / "classifications").is_dir():
                    triages.extend(second.glob("classifications/*.triage.json"))
    # Exclude the .v3 sidecars from the walk
    triages = [t for t in triages if not t.stem.endswith(".triage.v3")
               and not t.name.endswith(".triage.v3.json")]

    changes: list[dict] = []
    v3_counter: Counter = Counter()
    v4_counter: Counter = Counter()
    new_fp_classes: Counter = Counter()
    only_v4: list[Path] = []
    only_v3: list[Path] = []
    for t in sorted(triages):
        v3, v4 = load_pair(t)
        if v3 is None and v4 is None:
            continue
        if v3 is None:
            only_v4.append(t)
            if v4:
                v4_counter[v4.get("verdict", "?")] += 1
            continue
        if v4 is None:
            only_v3.append(t)
            v3_counter[v3.get("verdict", "?")] += 1
            continue
        v3_v = v3.get("verdict", "?")
        v4_v = v4.get("verdict", "?")
        v3_counter[v3_v] += 1
        v4_counter[v4_v] += 1
        if v3_v != v4_v:
            changes.append({
                "path": str(t.relative_to(args.sweep_dir)),
                "function": t.parent.parent.name,
                "property": t.stem.replace(".triage", ""),
                "v3": v3_v,
                "v4": v4_v,
                "v4_confidence": v4.get("confidence", "?"),
                "v4_fp_class": v4.get("fp_class"),
                "v4_reasoning": (v4.get("reasoning") or "")[:300],
            })
        if v4_v == "likely_fp" and v4.get("fp_class"):
            new_fp_classes[v4["fp_class"]] += 1

    print(f"# TriageAgent v3 vs v4 diff — {args.driver}")
    print()
    print(f"Total pairs compared: {sum(v3_counter.values())}")
    print(f"Only-v4 (new triages): {len(only_v4)}")
    print(f"Only-v3 (missing v4): {len(only_v3)}")
    print()
    print("## Verdict counts")
    print()
    print(f"| Verdict | v3 | v4 | Δ |")
    print(f"|---|---:|---:|---:|")
    for verdict in ("real_bug", "likely_fp", "needs_human"):
        v3n = v3_counter.get(verdict, 0)
        v4n = v4_counter.get(verdict, 0)
        delta = v4n - v3n
        sign = "+" if delta > 0 else ""
        print(f"| {verdict} | {v3n} | {v4n} | {sign}{delta} |")
    print()
    print(f"## Per-CEx verdict changes ({len(changes)} total)")
    print()
    flips = Counter()
    for ch in changes:
        flips[(ch["v3"], ch["v4"])] += 1
    print("| v3 → v4 | count |")
    print("|---|---:|")
    for (a, b), n in flips.most_common():
        print(f"| {a} → {b} | {n} |")
    print()
    print("## Change details")
    print()
    for ch in changes:
        fp_tag = f" [{ch['v4_fp_class']}]" if ch.get("v4_fp_class") else ""
        print(f"### {ch['function']}::{ch['property']}")
        print(f"  v3: **{ch['v3']}**  →  v4: **{ch['v4']}**{fp_tag} ({ch['v4_confidence']})")
        print(f"  > {ch['v4_reasoning']}")
        print()
    print("## New FP classes from v4 gates")
    print()
    for cls, n in new_fp_classes.most_common():
        print(f"- `{cls}`: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
