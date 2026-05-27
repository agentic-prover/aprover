"""Diff two TriageAgent runs on the same sweep artifact.

After each analyst-review-driven prompt update, we re-triage the
existing CExes. Before each re-run, the prior outputs get copied to
``*.triage.<old>.json`` sidecars; the re-run overwrites
``*.triage.json`` (the current/new verdicts). This script diffs the
two and prints:

  * per-CEx verdict changes (old → new)
  * net verdict-count delta
  * the new FP classes the new prompt is finding

Default suffix is ``v3`` (the original v3 vs v4 comparison this
script was first written for). Pass ``--old-suffix v4`` for v4 vs v5,
etc. The "new" side is always the current ``*.triage.json``.

Usage:
    # v3 vs v4 (the original)
    .venv/bin/python scripts/compare_triage_versions.py \\
        --sweep-dir /tmp/libarchive_postfix8 \\
        --driver libarchive_postfix8

    # v4 vs v5 (after G3 landed)
    .venv/bin/python scripts/compare_triage_versions.py \\
        --sweep-dir /tmp/libarchive_postfix8 \\
        --driver libarchive_postfix8 \\
        --old-suffix v4 --old-label v4 --new-label v5
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def make_load_pair(old_suffix: str):
    def load_pair(triage_json: Path) -> tuple[dict | None, dict | None]:
        """Return (old sidecar, new current). Either may be None."""
        new = None
        try:
            new = json.loads(triage_json.read_text())
        except Exception:
            pass
        old_path = triage_json.with_name(
            triage_json.stem + f".{old_suffix}.json"
        )
        old = None
        try:
            old = json.loads(old_path.read_text())
        except Exception:
            pass
        return old, new
    return load_pair


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep-dir", required=True, type=Path)
    ap.add_argument("--driver", required=True)
    ap.add_argument("--old-suffix", default="v3",
                    help="Sidecar suffix to compare against. Default v3.")
    ap.add_argument("--old-label", default=None,
                    help="Label for the old side in the report. Defaults to --old-suffix.")
    ap.add_argument("--new-label", default="v4",
                    help="Label for the current side in the report. Default v4.")
    args = ap.parse_args()
    old_label = args.old_label or args.old_suffix
    new_label = args.new_label
    load_pair = make_load_pair(args.old_suffix)

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
    # Exclude any .triage.<suffix>.json sidecars from the walk —
    # those are the "old" snapshots, never the current side.
    triages = [t for t in triages
               if not t.name.endswith(f".triage.{args.old_suffix}.json")]

    changes: list[dict] = []
    # Paired-only counters — both sides present. The verdict-count
    # table is on PAIRS so it reflects actual prompt-change deltas;
    # mixing in unpaired CExes inflates the deltas with CExes the
    # old run never saw.
    old_counter: Counter = Counter()
    new_counter: Counter = Counter()
    only_new_counter: Counter = Counter()  # reported separately
    only_old_counter: Counter = Counter()
    new_fp_classes: Counter = Counter()
    only_new: list[Path] = []
    only_old: list[Path] = []
    for t in sorted(triages):
        old, new = load_pair(t)
        if old is None and new is None:
            continue
        if old is None:
            only_new.append(t)
            if new:
                only_new_counter[new.get("verdict", "?")] += 1
            continue
        if new is None:
            only_old.append(t)
            only_old_counter[old.get("verdict", "?")] += 1
            continue
        old_v = old.get("verdict", "?")
        new_v = new.get("verdict", "?")
        old_counter[old_v] += 1
        new_counter[new_v] += 1
        if old_v != new_v:
            changes.append({
                "path": str(t.relative_to(args.sweep_dir)),
                "function": t.parent.parent.name,
                "property": t.stem.replace(".triage", ""),
                "old": old_v,
                "new": new_v,
                "new_confidence": new.get("confidence", "?"),
                "new_fp_class": new.get("fp_class"),
                "new_reasoning": (new.get("reasoning") or "")[:300],
            })
        if new_v == "likely_fp" and new.get("fp_class"):
            new_fp_classes[new["fp_class"]] += 1

    print(f"# TriageAgent {old_label} vs {new_label} diff — {args.driver}")
    print()
    print(f"Total pairs compared: {sum(old_counter.values())}")
    print(f"Only-{new_label} (no {old_label} sidecar): {len(only_new)}")
    print(f"Only-{old_label} (missing current): {len(only_old)}")
    print()
    print("## Verdict counts (paired CExes only)")
    print()
    print(f"| Verdict | {old_label} | {new_label} | Δ |")
    print(f"|---|---:|---:|---:|")
    for verdict in ("real_bug", "likely_fp", "needs_human"):
        old_n = old_counter.get(verdict, 0)
        new_n = new_counter.get(verdict, 0)
        delta = new_n - old_n
        sign = "+" if delta > 0 else ""
        print(f"| {verdict} | {old_n} | {new_n} | {sign}{delta} |")
    print()
    if only_new_counter:
        print(f"## Unpaired CExes (only-{new_label}, no {old_label} sidecar)")
        print()
        print(f"| Verdict | {new_label} |")
        print(f"|---|---:|")
        for verdict in ("real_bug", "likely_fp", "needs_human"):
            n = only_new_counter.get(verdict, 0)
            if n:
                print(f"| {verdict} | {n} |")
        print()
    print(f"## Per-CEx verdict changes ({len(changes)} total)")
    print()
    flips = Counter()
    for ch in changes:
        flips[(ch["old"], ch["new"])] += 1
    print(f"| {old_label} → {new_label} | count |")
    print("|---|---:|")
    for (a, b), n in flips.most_common():
        print(f"| {a} → {b} | {n} |")
    print()
    print("## Change details")
    print()
    for ch in changes:
        fp_tag = f" [{ch['new_fp_class']}]" if ch.get("new_fp_class") else ""
        print(f"### {ch['function']}::{ch['property']}")
        print(f"  {old_label}: **{ch['old']}**  →  {new_label}: **{ch['new']}**{fp_tag} ({ch['new_confidence']})")
        print(f"  > {ch['new_reasoning']}")
        print()
    print(f"## New FP classes from {new_label}")
    print()
    for cls, n in new_fp_classes.most_common():
        print(f"- `{cls}`: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
