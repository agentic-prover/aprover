"""
Head-to-head comparison: GPT-5 sweep vs prior Claude-Sonnet-4.5 sweep.

Reads bug_report.json files from both:
  /tmp/libarchive_gpt5_full_out/seedhunt_gpt5/   (new — GPT-5)
  /tmp/libarchive_n3_full_out/seedhunt_n3/       (prior — Claude 4.5)

Emits a per-(file, function, property) table showing each side's verdict,
plus a documented-seed-bug coverage diff. Writes markdown to
findings/libarchive_gpt5_vs_claude45_2026-05-25.md so the comparison is
reviewable without re-running the sweep.

Usage:
    python scripts/compare_gpt5_vs_claude.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.aggregate_results import SEED_FUNCTION_TO_COMMIT

GPT5_DIR = Path("/tmp/libarchive_gpt5_full_out/seedhunt_gpt5")
CLAUDE_DIR = Path("/tmp/libarchive_n3_full_out/seedhunt_n3")
OUT_MD = REPO / "findings/libarchive_gpt5_vs_claude45_2026-05-25.md"


_CONFIDENCE_RANK = {
    "confirmed_dynamic": 6,
    "confirmed_system_entry": 5,
    "confirmed_bmc": 4,
    "likely": 3,
    "uncertain": 2,
    "unlikely": 1,
    None: 0,
    "": 0,
    "?": 0,
}


def _walk(root: Path) -> dict[tuple[str, str], dict]:
    """Map (file_stem, function_name) -> {'verdict','confidence','prop','reasoning'}.
    Picks the STRONGEST per-CEx record per function: look at the latest
    ``bug_report.json`` *and* every ``bug_reports/*.json`` historical record
    (the per-CEx history landed in artifacts.py), then keep the one with the
    highest-rank confidence. Without this, the latest-CEx overwrite hides
    earlier confirmed findings — the dominant under-counting bug on Claude
    sweeps predating the per-CEx history change.
    """
    out: dict[tuple[str, str], dict] = {}
    if not root.is_dir():
        return out

    # Group every saved report under its (file_stem, function) key.
    grouped: dict[tuple[str, str], list[dict]] = {}
    for br in root.rglob("bug_report.json"):
        try:
            d = json.load(open(br))["report"]
        except Exception:
            continue
        file_stem = br.parent.parent.name
        fn = d.get("function_name", "?")
        grouped.setdefault((file_stem, fn), []).append(d)
        # Also fold in per-CEx history (artifacts.py >= 2026-05-25).
        history_dir = br.parent / "bug_reports"
        if history_dir.is_dir():
            for hf in history_dir.glob("*.json"):
                try:
                    hd = json.load(open(hf))["report"]
                except Exception:
                    continue
                grouped.setdefault((file_stem, fn), []).append(hd)

    for key, records in grouped.items():
        best = max(records, key=lambda r: _CONFIDENCE_RANK.get(r.get("confidence"), 0))
        rc = best.get("realism_check") or {}
        out[key] = {
            "verdict": rc.get("verdict", "?"),
            "confidence": best.get("confidence", "?"),
            "prop": best.get("violated_property", "?"),
            "reasoning": (rc.get("reasoning") or "")[:300],
            "n_records": len(records),
        }
    return out


def _confirmed(rec: dict) -> bool:
    return rec.get("confidence") not in (None, "", "?", "unlikely")


def _is_realistic(rec: dict) -> bool:
    return (rec.get("verdict") or "").lower() == "realistic"


def main() -> int:
    gpt = _walk(GPT5_DIR)
    cla = _walk(CLAUDE_DIR)

    if not gpt:
        print(f"GPT-5 sweep dir empty or missing: {GPT5_DIR}")
        return 1

    union = sorted(set(gpt) | set(cla))

    g_conf = sum(1 for k in gpt if _confirmed(gpt[k]))
    c_conf = sum(1 for k in cla if _confirmed(cla[k]))
    g_real = sum(1 for k in gpt if _is_realistic(gpt[k]))
    c_real = sum(1 for k in cla if _is_realistic(cla[k]))

    # Seed coverage
    g_seeds: set[str] = set()
    c_seeds: set[str] = set()
    for (stem, fn), rec in gpt.items():
        if fn in SEED_FUNCTION_TO_COMMIT and _confirmed(rec):
            g_seeds.add(SEED_FUNCTION_TO_COMMIT[fn])
    for (stem, fn), rec in cla.items():
        if fn in SEED_FUNCTION_TO_COMMIT and _confirmed(rec):
            c_seeds.add(SEED_FUNCTION_TO_COMMIT[fn])

    # Per-function disagreement: confirmed in one, not the other
    only_gpt: list[tuple[str, str, dict]] = []
    only_cla: list[tuple[str, str, dict]] = []
    both: list[tuple[str, str, dict, dict]] = []
    for key in union:
        g = gpt.get(key)
        c = cla.get(key)
        if g and _confirmed(g) and not (c and _confirmed(c)):
            only_gpt.append((key[0], key[1], g))
        elif c and _confirmed(c) and not (g and _confirmed(g)):
            only_cla.append((key[0], key[1], c))
        elif g and c and _confirmed(g) and _confirmed(c):
            both.append((key[0], key[1], g, c))

    # Render
    md = []
    md.append("# GPT-5 vs Claude-Sonnet-4.5 head-to-head — 2026-05-25")
    md.append("")
    md.append("Same 7-file libarchive corpus, same `verify-dir --lite-mode "
              "--enable-realism-check --threat-model security` flags, same "
              "`BMC_AGENT_DEDUP_MAX_PER_TYPE=3`. Only the LLM backend differs.")
    md.append("")
    md.append("| Metric | GPT-5 | Claude 4.5 |")
    md.append("|---|---:|---:|")
    md.append(f"| Total bug_reports | {len(gpt)} | {len(cla)} |")
    md.append(f"| Confidence != unlikely | {g_conf} | {c_conf} |")
    md.append(f"| Realism verdict = realistic | {g_real} | {c_real} |")
    md.append(f"| Documented seed-commits matched | {len(g_seeds)} | {len(c_seeds)} |")
    md.append("")

    # Seed-commit coverage
    md.append("## Documented seed-commit coverage")
    md.append("")
    md.append(f"- GPT-5 commits: {sorted(g_seeds)}")
    md.append(f"- Claude 4.5 commits: {sorted(c_seeds)}")
    md.append(f"- Only GPT-5: {sorted(g_seeds - c_seeds)}")
    md.append(f"- Only Claude 4.5: {sorted(c_seeds - g_seeds)}")
    md.append(f"- Both: {sorted(g_seeds & c_seeds)}")
    md.append("")

    # Per-function disagreement
    def _fmt_block(title: str, rows: list[tuple[str, str, dict]]):
        md.append(f"## {title} ({len(rows)})")
        if not rows:
            md.append("_none_")
            md.append("")
            return
        md.append("| File | Function | Confidence | Verdict | Property |")
        md.append("|---|---|---|---|---|")
        for stem, fn, rec in sorted(rows):
            seed = " ★" if fn in SEED_FUNCTION_TO_COMMIT else ""
            md.append(f"| {stem} | `{fn}`{seed} | {rec['confidence']} | "
                      f"{rec['verdict']} | `{rec['prop']}` |")
        md.append("")

    _fmt_block("Only GPT-5 confirmed (Claude downgraded or absent)", only_gpt)
    _fmt_block("Only Claude 4.5 confirmed (GPT-5 downgraded or absent)", only_cla)

    md.append(f"## Both confirmed ({len(both)})")
    md.append("")
    if both:
        md.append("| File | Function | GPT-5 verdict | Claude verdict |")
        md.append("|---|---|---|---|")
        for stem, fn, g, c in sorted(both):
            seed = " ★" if fn in SEED_FUNCTION_TO_COMMIT else ""
            md.append(f"| {stem} | `{fn}`{seed} | {g['verdict']} | {c['verdict']} |")
        md.append("")
    md.append("(★ = function matches a documented seed-bug commit)")
    md.append("")

    out = "\n".join(md)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(out)
    print(out)
    print(f"\n--- wrote {OUT_MD} ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
