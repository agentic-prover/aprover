#!/usr/bin/env python3
"""Quick status snapshot for a running autonomous sweep.

Usage: python3 /tmp/sweep_status.py /tmp/libarchive_auto libarchive_b_start_auto
"""

import json
import sys
from collections import Counter
from pathlib import Path


def main(artifact_root: str, driver: str) -> None:
    root = Path(artifact_root) / driver
    if not root.exists():
        print(f"No artifact tree at {root} yet")
        return

    # Per-file CBMC verdicts.
    file_dirs = sorted([p for p in root.iterdir() if p.is_dir() and p.name != "autonomous"])
    print(f"=== Sweep snapshot: {root} ===\n")

    total_funcs = 0
    total_verdicts = 0
    total_errors = 0
    files_with_verdicts = 0
    files_done = []
    for fd in file_dirs:
        cov = fd / "coverage_diagnostics.json"
        if not cov.exists():
            continue
        try:
            with cov.open() as f:
                d = json.load(f)
            runs = int(d.get("total_cbmc_runs", 0))
            verd = int(d.get("produced_verdict", 0))
            fail = int(d.get("failed_before_verdict", 0))
            total_funcs += runs
            total_verdicts += verd
            total_errors += fail
            if verd:
                files_with_verdicts += 1
            files_done.append((fd.name, verd, fail, runs))
        except Exception:
            pass

    print(f"Files with coverage_diagnostics: {len(files_done)}")
    print(f"Total functions seen: {total_funcs}")
    print(f"  CBMC verdicts:     {total_verdicts}")
    print(f"  CBMC errors:       {total_errors}")
    if total_funcs:
        print(f"  Coverage:          {100 * total_verdicts / total_funcs:.1f}%")
    print(f"  Files with verdicts: {files_with_verdicts}\n")

    # Auto-retry registry activity.
    retry_logs = sorted(root.glob("*/auto_retries.json"))
    if retry_logs:
        print(f"Auto-retry rounds (Phase 1+2b):")
        action_counts = Counter()
        for rl in retry_logs:
            try:
                with rl.open() as f:
                    entries = json.load(f)
                for e in entries:
                    if isinstance(e, dict) and "action" in e:
                        action_counts[e["action"]] += 1
            except Exception:
                pass
        for action, n in action_counts.most_common():
            print(f"  {action}: {n}")
        print()

    # Self-patch proposals (Phase 3).
    proposed = sorted(root.rglob("proposed_patches/*/*.meta.json"))
    if proposed:
        print(f"Self-patch proposals (Phase 3): {len(proposed)}")
        status_counts = Counter()
        for mf in proposed:
            try:
                with mf.open() as f:
                    meta = json.load(f)
                status_counts[meta.get("status", "unknown")] += 1
            except Exception:
                pass
        for status, n in status_counts.most_common():
            print(f"  {status}: {n}")
        print()

    # Confirmed bugs surfaced so far.
    bug_count = 0
    bug_files = Counter()
    for br in root.rglob("bug_report.json"):
        try:
            with br.open() as f:
                d = json.load(f)
            top = d.get("report") or d
            if isinstance(top, dict) and top.get("function_name"):
                # Only count those that aren't filtered as spurious.
                cls_path = br.parent / "classification.json"
                outcome = None
                if cls_path.exists():
                    try:
                        with cls_path.open() as fc:
                            cd = json.load(fc)
                        outcome = (cd.get("classification") or {}).get("outcome")
                    except Exception:
                        pass
                # Realism verdict.
                rc = top.get("realism_check") or {}
                rverdict = (rc.get("verdict") if isinstance(rc, dict) else None) or "n/a"
                if outcome != "spurious" and rverdict.lower() != "unrealistic":
                    bug_count += 1
                    parent_file = br.parents[-3].name if len(br.parents) >= 3 else br.parent.name
                    bug_files[parent_file] += 1
        except Exception:
            pass
    if bug_count:
        print(f"Confirmed bugs (post-realism, non-spurious): {bug_count}")
        for f, n in bug_files.most_common(10):
            print(f"  {f}: {n}")


if __name__ == "__main__":
    art = sys.argv[1] if len(sys.argv) > 1 else "/tmp/libarchive_auto"
    drv = sys.argv[2] if len(sys.argv) > 2 else "libarchive_b_start_auto"
    main(art, drv)
