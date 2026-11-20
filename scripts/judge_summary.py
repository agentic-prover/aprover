"""
Summarize a judge-dir output directory into a human-readable table.

Walks <output>/<driver>/<file_stem>/<function>/judge_*.json files and emits:
  - Per-function primary verdicts
  - Adjacent-bug hypotheses (with their BMC-confirmation status if attempted)
  - Top-line counts: realistic / unrealistic / uncertain / adjacent-confirmed

Usage:
    python scripts/judge_summary.py <output_dir>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def walk(root: Path) -> list[dict]:
    """Return a flat list of per-CEx records with function + file context."""
    out = []
    for p in sorted(root.rglob("judge_*.json")):
        if "/__pycache__/" in p.as_posix():
            continue
        try:
            d = json.load(open(p))
        except Exception:
            continue
        # path: .../<driver>/<file_stem>/<function>/judge_<prop>.json
        parts = p.parts
        out.append({
            "path": p,
            "function": parts[-2],
            "file_stem": parts[-3],
            "driver": parts[-4],
            "record": d,
        })
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"ERROR: not a dir: {root}")
        return 1

    rows = walk(root)
    if not rows:
        print(f"(no judge_*.json files under {root})")
        return 0

    verdicts = Counter()
    confidences = Counter()
    adj_count = 0
    adj_by_conf = Counter()
    adj_confirmed = 0
    adj_attempted_confirm = 0

    print(f"=== Per-CEx verdicts (under {root}) ===")
    print(f"{'file':28s} {'function':32s} {'property':36s} {'verdict':12s} {'conf':6s} {'adj':>4s} {'cnf':>3s}")
    print("-" * 130)
    for r in rows:
        rec = r["record"]
        j = (rec.get("judge") or {})
        v = j.get("verdict") or "?"
        c = j.get("confidence") or "?"
        verdicts[v] += 1
        confidences[c] += 1
        adj_list = j.get("adjacent_bugs") or []
        adj_count += len(adj_list)
        for ab in adj_list:
            if isinstance(ab, dict):
                adj_by_conf[(ab.get("confidence") or "").lower() or "?"] += 1
        confirms = rec.get("adjacent_confirmations") or []
        n_attempted = sum(1 for c_ in confirms if c_.get("verdict") is not None)
        n_confirmed = sum(1 for c_ in confirms if c_.get("confirmed"))
        adj_attempted_confirm += n_attempted
        adj_confirmed += n_confirmed
        print(
            f"{r['file_stem'][:28]:28s} "
            f"{r['function'][:32]:32s} "
            f"{(rec.get('failing_property') or '?')[:36]:36s} "
            f"{v[:12]:12s} {c[:6]:6s} "
            f"{len(adj_list):>4d} {n_confirmed:>3d}"
        )

    print()
    print(f"=== Top-line ===")
    print(f"  rows         : {len(rows)}")
    print(f"  verdicts     : {dict(verdicts)}")
    print(f"  confidences  : {dict(confidences)}")
    print(f"  adjacent bugs (LLM hypotheses): {adj_count}")
    print(f"    by confidence: {dict(adj_by_conf)}")
    print(f"  adjacent BMC-confirmation attempts: {adj_attempted_confirm}")
    print(f"  adjacent BMC-CONFIRMED            : {adj_confirmed}")

    # Surface high-value findings:
    #   ★★ REALISTIC primary + BMC-CONFIRMED adjacent (both layers agree)
    #   ☆  HIGH-confidence adjacent the LLM flagged but BMC couldn't confirm
    #      (still worth manual review — usually needs a larger unwind or
    #       targeted harness to surface in CBMC; not a refutation)
    print()
    print(f"=== High-value findings ===")
    n_star = 0
    n_open = 0
    for r in rows:
        rec = r["record"]
        j = rec.get("judge") or {}
        if j.get("verdict") == "realistic":
            n_star += 1
            print(f"  ★★ REALISTIC PRIMARY: {r['file_stem']}/{r['function']} ({rec.get('failing_property')})")
            print(f"     reasoning: {(j.get('reasoning') or '')[:300]}")
        for cf in rec.get("adjacent_confirmations") or []:
            adj = cf.get("adjacent") or {}
            if cf.get("confirmed"):
                n_star += 1
                print(f"  ★★ BMC-CONFIRMED ADJACENT: {r['file_stem']}/{r['function']} → {cf.get('target_function')}")
                print(f"     bug_type: {adj.get('bug_type')}")
                print(f"     reasoning: {(cf.get('reasoning') or '')[:300]}")
            elif (adj.get('confidence') or '').lower() == 'high':
                n_open += 1
                print(f"  ☆  HIGH-CONF ADJACENT (BMC could not confirm): {r['file_stem']}/{r['function']} → {cf.get('target_function') or 'unknown'}")
                print(f"     bug_type: {adj.get('bug_type')}")
                print(f"     scenario: {(adj.get('attacker_scenario') or '')[:300]}")
                print(f"     bmc-confirm reason: {(cf.get('reason') or cf.get('reasoning') or '')[:250]}")
        # Also surface HIGH-conf adjacent bugs that we couldn't even attempt
        # to confirm (no target function extractable, e.g. location had only
        # a file:line string).
        attempted_locs = {c.get("adjacent", {}).get("location") for c in (rec.get("adjacent_confirmations") or [])}
        for adj in (j.get("adjacent_bugs") or []):
            if not isinstance(adj, dict): continue
            if (adj.get('confidence') or '').lower() != 'high': continue
            if adj.get('location') in attempted_locs: continue
            n_open += 1
            print(f"  ☆  HIGH-CONF ADJACENT (not BMC-attempted): {r['file_stem']}/{r['function']}")
            print(f"     location: {adj.get('location')}")
            print(f"     bug_type: {adj.get('bug_type')}")
            print(f"     scenario: {(adj.get('attacker_scenario') or '')[:300]}")
    if n_star == 0 and n_open == 0:
        print("  (none)")
    print()
    print(f"  ★★ {n_star} confirmed-by-both findings, ☆ {n_open} high-conf-LLM-only findings")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
