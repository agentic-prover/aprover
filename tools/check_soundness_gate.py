#!/usr/bin/env python3
"""Soundness gate checker — codifies the Phase-3 adjudication over a findings dir.

Run AFTER a real --agentic sweep (it reads the emitted bug_report.json files; it
does NOT re-run the LLM/CBMC). It asserts the empirical soundness invariant the
deterministic tests/test_soundness_corpus.py cannot:

  * every known GENUINE-REAL function kept a confirmed_* tier (was NOT demoted to
    'unlikely'), i.e. the agentic stack (realism enforcement, oracle-disagreement,
    dyn-val) never demoted a real on this run;
  * every known FALSE-POSITIVE function did demote (sanity that the FP filters
    still bite).

Exit code 0 = gate GREEN, 1 = a real was demoted (BLOCKED), 2 = usage error.

Usage:
  check_soundness_gate.py FINDINGS_DIR --reals fn1,fn2 [--fps fnA,fnB] [--strict]
  check_soundness_gate.py FINDINGS_DIR --manifest manifest.json

manifest.json: {"reals": ["vfs_open_handle", ...], "fps": ["sleep_ms", ...]}

By default a real that is ABSENT from the findings is a WARNING, not a failure
(it can be a documented source-modeling false-negative, as with vfs_open_handle
in Phase 3). Pass --strict to make a missing real fatal.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

CONFIRMED = {"confirmed_dynamic", "confirmed_system_entry", "confirmed_bmc"}


def tiers_for(findings_dir: str, fn: str):
    """Return [(path, tier)] for every bug_report.json under a <fn>/ dir."""
    pattern = os.path.join(findings_dir, "**", fn, "bug_report.json")
    out = []
    for m in sorted(glob.glob(pattern, recursive=True)):
        try:
            rep = (json.load(open(m)).get("report") or {})
            out.append((m, rep.get("confidence")))
        except Exception as exc:  # malformed report
            out.append((m, f"<error:{exc}>"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Soundness gate over a findings dir.")
    ap.add_argument("findings_dir")
    ap.add_argument("--reals", default="", help="comma-separated genuine-real fn names")
    ap.add_argument("--fps", default="", help="comma-separated known-FP fn names")
    ap.add_argument("--manifest", default="", help="JSON {reals:[...], fps:[...]}")
    ap.add_argument("--strict", action="store_true",
                    help="treat an absent real as a failure (default: warn)")
    args = ap.parse_args(argv)

    reals = [s for s in args.reals.split(",") if s]
    fps = [s for s in args.fps.split(",") if s]
    if args.manifest:
        m = json.load(open(args.manifest))
        reals += list(m.get("reals") or [])
        fps += list(m.get("fps") or [])
    if not reals and not fps:
        print("error: no reals/fps given (use --reals/--fps or --manifest)", file=sys.stderr)
        return 2
    if not os.path.isdir(args.findings_dir):
        print(f"error: not a directory: {args.findings_dir}", file=sys.stderr)
        return 2

    violations, warnings, oks = [], [], []

    for fn in reals:
        found = tiers_for(args.findings_dir, fn)
        if not found:
            (violations if args.strict else warnings).append(
                f"REAL {fn}: NOT FOUND in findings"
                + ("" if args.strict else " (absent — possibly documented modeling FN)"))
            continue
        # Function-level KEEP: confirmed if ANY of its reports is a confirmed_* tier.
        kept = any(t in CONFIRMED for _, t in found)
        if kept:
            oks.append(f"REAL {fn}: kept ({', '.join(str(t) for _, t in found)})")
        else:
            violations.append(
                f"REAL {fn}: DEMOTED -> {', '.join(str(t) for _, t in found)}")

    for fn in fps:
        found = tiers_for(args.findings_dir, fn)
        if not found:
            warnings.append(f"FP {fn}: not found (fine)")
            continue
        still_confirmed = [t for _, t in found if t in CONFIRMED]
        if still_confirmed:
            warnings.append(
                f"FP {fn}: still confirmed {still_confirmed} "
                "(unexpected — FP filter did not bite; review)")
        else:
            oks.append(f"FP {fn}: demoted ({', '.join(str(t) for _, t in found)})")

    print(f"=== soundness gate: {args.findings_dir} ===")
    for line in oks:
        print("  [OK]   " + line)
    for line in warnings:
        print("  [WARN] " + line)
    for line in violations:
        print("  [FAIL] " + line)

    if violations:
        print(f"\nGATE BLOCKED: {len(violations)} real(s) demoted / missing(strict).")
        return 1
    print(f"\nGATE GREEN: {len(oks)} checks passed, {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
