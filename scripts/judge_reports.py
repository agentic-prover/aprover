"""
Generate per-bug markdown reports from a judge-dir output, for manual
verification of plausibly-real bugs.

For every BMC-confirmed adjacent bug (the ★★ findings), produces one
self-contained markdown file containing:
  - LLM bug type + attacker scenario
  - Source of the confirmed target function (with line numbers)
  - CBMC failing property + witness + abbreviated trace
  - Source of the originating function (the one whose primary CEx led
    GPT-5 to find this adjacent bug)
  - Primary verdict reasoning + adjacent verdict reasoning
  - Public-API call chain
  - Suggested manual-verification checklist

Usage:
    python scripts/judge_reports.py <judge_output_dir> <reports_dir>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORPUS = Path("/tmp/libarchive_seedhunt_full")


def _read_function_source(fn_name: str, corpus_dir: Path) -> tuple[str, str, int]:
    """Best-effort: grep for the function definition in the corpus and return
    (file_path, body_with_line_numbers, start_line). Returns ('', '', 0) on miss.
    """
    for c_file in corpus_dir.glob("*.c"):
        try:
            text = c_file.read_text(errors="replace")
        except Exception:
            continue
        # Try to find "fn_name(...)" at the start of a line (declarator)
        pattern = re.compile(
            r"^(?:static\s+)?\w[\w\s\*]*?\b" + re.escape(fn_name) + r"\s*\([^;]*\)\s*\n?\s*\{",
            re.MULTILINE,
        )
        m = pattern.search(text)
        if not m:
            continue
        start_offset = m.start()
        # Find brace-balanced end
        depth = 0
        i = text.index("{", start_offset)
        end = i
        while end < len(text):
            ch = text[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end += 1
                    break
            end += 1
        body = text[start_offset:end]
        start_line = text[:start_offset].count("\n") + 1
        # Add line numbers
        lines = body.split("\n")
        numbered = "\n".join(f"{start_line + i:5d}  {line}" for i, line in enumerate(lines))
        return c_file.name, numbered, start_line
    return "", "", 0


def _read_judge_file(p: Path) -> dict:
    try:
        return json.load(open(p))
    except Exception:
        return {}


def _render_report(
    *,
    bug_id: int,
    src_judge_fn: str,
    src_judge_property: str,
    src_judge_reasoning: str,
    adjacent: dict,
    confirmation: dict,
    seed_match_hint: str,
) -> str:
    target_fn = confirmation.get("target_function") or "(unknown)"
    bug_type = adjacent.get("bug_type") or "(unspecified)"
    scenario = adjacent.get("attacker_scenario") or "(none supplied)"
    adj_loc = adjacent.get("location") or "(none)"
    adj_conf = adjacent.get("confidence") or "(unspecified)"
    adj_evidence = adjacent.get("evidence") or ""
    bmc_failing = confirmation.get("failing_property") or "(none)"
    bmc_reasoning = confirmation.get("reasoning") or ""
    bmc_attacker = confirmation.get("attacker_scenario") or ""

    # Target source
    tgt_file, tgt_src, tgt_start = _read_function_source(target_fn, CORPUS)
    src_file, src_src, src_start = _read_function_source(src_judge_fn, CORPUS)

    md = []
    md.append(f"# Bug #{bug_id}: {target_fn} — {bug_type}")
    md.append("")
    md.append(f"**LLM confidence (hypothesis):** {adj_conf}")
    md.append(f"**BMC re-confirmation verdict:** {confirmation.get('verdict')} "
              f"(confirmed={confirmation.get('confirmed')})")
    md.append(f"**Originating judge:** {src_judge_fn} / {src_judge_property}")
    md.append(f"**Seed-bug match heuristic:** {seed_match_hint}")
    md.append("")

    md.append("## What the LLM thinks the bug is")
    md.append("")
    md.append(f"**Location:** {adj_loc}")
    md.append("")
    md.append(f"**Bug type:** {bug_type}")
    md.append("")
    md.append("**Attacker scenario (public-API trigger):**")
    md.append("")
    for line in scenario.split("\n"):
        md.append(f"> {line}")
    md.append("")
    if adj_evidence:
        md.append("**LLM-cited evidence (from initial adjacent-bug hunt):**")
        md.append("")
        for line in adj_evidence.split("\n"):
            md.append(f"> {line}")
        md.append("")

    md.append("## BMC re-verification")
    md.append("")
    md.append(f"After GPT-5 emitted this adjacent-bug hypothesis from the "
              f"`{src_judge_fn}` primary judge, the pipeline generated a "
              f"fresh CBMC harness focused on `{target_fn}` (unwind=16) and "
              f"re-ran CBMC. CBMC produced this failing property:")
    md.append("")
    md.append(f"  `{bmc_failing}`")
    md.append("")
    md.append("A second LLM call (with no memory of the first) judged this "
              "new CEx with the hypothesis as context. Its reasoning:")
    md.append("")
    for line in bmc_reasoning.split("\n"):
        md.append(f"> {line}")
    md.append("")
    if bmc_attacker:
        md.append("**Attacker scenario (from BMC-confirmation judge):**")
        md.append("")
        for line in bmc_attacker.split("\n"):
            md.append(f"> {line}")
        md.append("")

    md.append("## Target function source (file: " + (tgt_file or "?") + ")")
    md.append("")
    if tgt_src:
        md.append("```c")
        md.append(tgt_src[:6000])
        if len(tgt_src) > 6000:
            md.append(f"... [truncated; original {len(tgt_src)} chars]")
        md.append("```")
    else:
        md.append(f"_(could not locate source of `{target_fn}` in {CORPUS})_")
    md.append("")

    md.append(f"## Originating judge ({src_judge_fn})")
    md.append("")
    md.append(f"The primary CBMC counterexample that led GPT-5 to surface this "
              f"hypothesis was on `{src_judge_fn}` / `{src_judge_property}`. "
              f"GPT-5 voted UNREALISTIC on that primary CEx with the reasoning:")
    md.append("")
    for line in src_judge_reasoning.split("\n"):
        md.append(f"> {line}")
    md.append("")

    md.append("## Manual verification checklist")
    md.append("")
    md.append(f"- [ ] Read `{target_fn}` in `{tgt_file or CORPUS}` and confirm "
             "the cited line / condition matches the LLM's claim.")
    md.append(f"- [ ] Trace from the public API entry to `{target_fn}` and "
             "check whether ANY code path leaves the cited input in the "
             "unsafe state (NULL pointer, length=0, etc.) the LLM claims.")
    md.append(f"- [ ] Check upstream libarchive history for an already-landed "
             f"fix near `{target_fn}` matching this pattern (`git log -p "
             f"libarchive/archive_acl.c | grep -A20 \"{target_fn}\"`).")
    md.append(f"- [ ] If not patched: construct a minimal PAX tar / ACL "
             "input matching the attacker scenario; build with "
             "AddressSanitizer + UBSan; verify the crash.")
    md.append(f"- [ ] If reproducible: file as a defensive-coding gap upstream "
             "(no CVE class unless trivially exploitable).")
    md.append("")

    return "\n".join(md)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    out_root = Path(sys.argv[1])
    reports_dir = Path(sys.argv[2])
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Seed-bug match heuristics (best-effort string matching)
    def _seed_hint(target_fn: str, bug_type: str) -> str:
        t = bug_type.lower()
        tf = target_fn.lower()
        if "next_field" in tf and "out-of-bounds" in t:
            return "MATCH documented seed 8308b61c (ACL parser OOB read)"
        if "archive_acl_from_text_nl" in tf and "null" in t:
            return "MATCH documented seed 4b3ba035 (NULL deref in archive_acl_from_text_nl)"
        if any(s in tf for s in ("isint", "ismode", "is_nfs4")) and "null" in t:
            return ("Related to 4b3ba035 family (sibling helper that receives "
                    "the same potentially-NULL field pointers; the upstream "
                    "commit only patched ONE call site)")
        if "archive_acl_text_len" in tf or "archive_acl_to_text" in tf:
            return ("Related to d45b5b4b (ACL buffer overrun + NULL-name "
                    "handling); upstream fix patched the length estimator + "
                    "wide-char serializer")
        return "(no obvious match to a documented seed bug)"

    bug_id = 0
    written = []
    for jf in sorted(out_root.rglob("judge_*.json")):
        d = _read_judge_file(jf)
        if not d:
            continue
        src_fn = jf.parent.name
        src_property = d.get("failing_property", "?")
        primary_reasoning = (d.get("judge") or {}).get("reasoning") or ""
        for cf in d.get("adjacent_confirmations") or []:
            if not cf.get("confirmed"):
                continue
            bug_id += 1
            adj = cf.get("adjacent") or {}
            target_fn = cf.get("target_function") or "unknown"
            bug_type = adj.get("bug_type") or ""
            seed_hint = _seed_hint(target_fn, bug_type)
            md = _render_report(
                bug_id=bug_id,
                src_judge_fn=src_fn,
                src_judge_property=src_property,
                src_judge_reasoning=primary_reasoning,
                adjacent=adj,
                confirmation=cf,
                seed_match_hint=seed_hint,
            )
            slug = f"{bug_id:02d}_{target_fn}__{re.sub(r'[^A-Za-z0-9_]+', '_', bug_type)[:40]}".strip("_")
            out_path = reports_dir / f"{slug}.md"
            out_path.write_text(md)
            written.append((bug_id, out_path, target_fn, bug_type, seed_hint))
            print(f"wrote {out_path}")

    # Index
    index = ["# Bug reports — BMC-confirmed adjacent findings", "",
             f"Generated from {out_root}", "",
             f"{len(written)} bugs total.", ""]
    index.append("| # | Function | Bug type | Seed-bug map |")
    index.append("|---|---|---|---|")
    for bid, path, target, bug_type, hint in written:
        index.append(f"| [{bid}](./{path.name}) | `{target}` | {bug_type[:60]} | {hint[:60]} |")
    (reports_dir / "INDEX.md").write_text("\n".join(index))
    print(f"\nwrote {reports_dir/'INDEX.md'}")
    print(f"\n=== {len(written)} reports in {reports_dir} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
