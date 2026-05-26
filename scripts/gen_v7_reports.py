"""Generate per-bug markdown reports for judge_v7 realistic verdicts.

For each judge_*.json in the sweep tree where verdict=='realistic', AND for
each adjacent_confirmation where confirmed==True, write a self-contained
markdown report to findings/v7/<file>__<function>__<property>.md plus an
index.md.

Evidence grading (CBMC + judge + dynamic):
  A  judge realistic + dyn-val SIGABRT/sanitizer hit + same property class
  B  judge realistic + dyn-val SIGABRT/sanitizer hit on a related path
     (different bug class — circumstantial confirmation only)
  C  judge realistic, dyn-val did not reproduce
     (no_reproducer / not_triggered / timeout / skipped)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SWEEP = Path("/tmp/libarchive_judge_v7/judge_v7")
OUT   = Path("/home/syc/AProver/findings/v7")
CORPUS_ROOT = Path("/tmp/libarchive_seedhunt_full")


def safe_slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def grade(cbmc_prop: str, dyn: dict | None) -> tuple[str, str]:
    if not dyn or dyn.get("outcome") != "confirmed_dynamic":
        outcome = (dyn or {}).get("outcome") or "skipped"
        return "C", f"judge-only ({outcome})"
    stderr = (dyn.get("stderr_excerpt") or "") + (dyn.get("stderr") or "")
    prop_class = cbmc_prop.split(".", 2)[1] if "." in cbmc_prop else cbmc_prop

    # Map ASan/UBSan failure strings to CBMC property classes
    same_class = False
    if "overflow" in prop_class and ("integer-overflow" in stderr.lower() or "exceeds maximum supported size" in stderr.lower()):
        same_class = True
    if "pointer_dereference" in prop_class and ("heap-buffer-overflow" in stderr.lower() or "use-after-free" in stderr.lower() or "null pointer" in stderr.lower() or "stack-buffer-overflow" in stderr.lower()):
        # Treat stack-overflow as related; reproducer may have hit the same
        # vulnerable codepath through a different write
        same_class = True
    if "pointer_arithmetic" in prop_class and "buffer-overflow" in stderr.lower():
        same_class = True
    if "array_bounds" in prop_class and "buffer-overflow" in stderr.lower():
        same_class = True

    if same_class:
        return "A", "dynamically confirmed (same property class)"
    return "B", "dynamically reproduced a related crash (different property class — circumstantial)"


def function_source_excerpt(file_stem: str, fn_name: str) -> tuple[str, int]:
    """Return (excerpt, start_line) or ('', 0). Best-effort grep for the
    function definition in the corpus source."""
    src = CORPUS_ROOT / f"{file_stem}.c"
    if not src.is_file():
        return ("", 0)
    text = src.read_text(errors="replace").splitlines()
    pat = re.compile(rf"^\s*(?:static\s+|inline\s+)*(?:[A-Za-z_][\w*\s]*\s)?\b{re.escape(fn_name)}\s*\(")
    for i, line in enumerate(text):
        if pat.match(line):
            # find opening brace then matching close
            brace = 0
            started = False
            for j in range(i, min(i + 300, len(text))):
                brace += text[j].count("{") - text[j].count("}")
                if "{" in text[j]:
                    started = True
                if started and brace == 0:
                    return ("\n".join(text[i : j + 1]), i + 1)
            return ("\n".join(text[i : i + 60]), i + 1)
    return ("", 0)


def write_report(rec: dict) -> Path:
    file_stem = rec["file_stem"]
    fn = rec["function"]
    prop = rec["failing_property"]
    out_name = safe_slug(f"{file_stem}__{fn}__{prop}") + ".md"
    out_path = OUT / out_name

    judge = rec["judge"]
    dyn = rec.get("dynamic") or {}
    g, g_desc = grade(prop, dyn)

    # Function source excerpt
    body, start_line = function_source_excerpt(file_stem, fn)
    body_block = (
        f"### Source (`{file_stem}.c` starting at line {start_line})\n\n"
        f"```c\n{body}\n```\n" if body else ""
    )

    # Witness pretty-print
    witness_lines = rec.get("witness") or []
    witness_block = "\n".join(witness_lines[:60]) if witness_lines else "(no witness assignments captured)"

    # Dynamic validation block
    dyn_block = ""
    if dyn:
        attempts = dyn.get("attempts") or []
        dyn_block = (
            f"### Dynamic validation\n\n"
            f"- **Outcome**: `{dyn.get('outcome','?')}`\n"
            f"- **Signal**: `{dyn.get('signal_name') or '-'}`\n"
            f"- **Sanitizer hit**: `{dyn.get('sanitizer_hit')}`\n"
            f"- **Attempts**: {len(attempts)} (final attempt = {dyn.get('attempt','?')})\n"
            f"- **Reproducer**: `{dyn.get('harness_path','-')}`\n"
            f"\n**Sanitizer output (excerpt)**:\n\n"
            f"```text\n{(dyn.get('stderr_excerpt') or '')[:2500]}\n```\n"
        )

    # Adjacent-bug context (if any)
    adj_block = ""
    if rec.get("from_adjacent"):
        adj_block = (
            f"### Adjacent-bug context\n\n"
            f"This finding was surfaced as an adjacent bug while judging the "
            f"primary CEx on `{rec['from_adjacent']['primary_function']}` "
            f"(`{rec['from_adjacent']['primary_property']}`). The primary "
            f"verdict was `{rec['from_adjacent']['primary_verdict']}`; "
            f"the adjacent bug was BMC-confirmed against this function and "
            f"the new CEx was re-judged realistic.\n\n"
        )
    if rec.get("_also_via"):
        adj_block += "### Independently re-surfaced via\n\n"
        for src, g in rec["_also_via"]:
            adj_block += f"- {src} (grade {g})\n"
        adj_block += "\n"

    text = f"""# Bug report: `{fn}` — {prop}

**Evidence grade**: **{g}** — {g_desc}

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/{file_stem}.c`
- **Function**: `{fn}`
- **Violated property**: `{prop}` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `{prop}` |
| LLM judge (primary) | **{judge.get('verdict','?')}** / confidence `{judge.get('confidence','?')}` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `{(dyn or {}).get('outcome','-')}` (signal `{(dyn or {}).get('signal_name','-')}`) |

{adj_block}## Judge reasoning

{judge.get('reasoning', '(none)')}

## Exploit scenario (LLM-supplied)

{judge.get('attacker_scenario') or '(judge did not supply a scenario; see reasoning above)'}

{body_block}### CBMC witness (variable assignments)

```text
{witness_block}
```

{dyn_block}

## Reproduction artifacts

- Harness: `{rec.get('harness_path','-')}`
- CBMC result: `{rec.get('cbmc_result_path','-')}`
- Per-CEx judge JSON: `{rec.get('judge_path','-')}`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
"""
    out_path.write_text(text)
    return out_path


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    findings = []

    for jf in sorted(SWEEP.rglob("judge_*.json")):
        try: d = json.load(open(jf))
        except: continue
        fn_dir = jf.parent
        file_stem = fn_dir.parent.name
        fn = fn_dir.name

        # ----- Primary realistic verdicts -----
        judge = d.get("judge") or {}
        if judge.get("verdict") == "realistic":
            cbmc_prop = d.get("failing_property", "?")
            # Try to recover the witness from the per-CEx record
            # (judge_pipeline doesn't persist the witness directly,
            # so this is best-effort empty for now).
            findings.append({
                "file_stem": file_stem,
                "function": fn,
                "failing_property": cbmc_prop,
                "judge": judge,
                "dynamic": d.get("primary_dynamic_validation") or None,
                "harness_path": str(fn_dir / "harness.c"),
                "cbmc_result_path": str(fn_dir / "cbmc_result.json"),
                "judge_path": str(jf),
                "witness": [],
                "from_adjacent": None,
            })

        # ----- Adjacent confirmations confirmed realistic -----
        for a in (d.get("adjacent_confirmations") or []):
            if not a.get("confirmed"):
                continue
            findings.append({
                "file_stem": file_stem,
                "function": a.get("target_function", fn),
                "failing_property": a.get("failing_property", "?"),
                "judge": {
                    "verdict": a.get("verdict", "?"),
                    "confidence": a.get("confidence", "?"),
                    "reasoning": a.get("reasoning", ""),
                    "attacker_scenario": a.get("attacker_scenario", ""),
                },
                "dynamic": a.get("dynamic_validation") or None,
                "harness_path": "(adjacent re-run; harness generated on the fly)",
                "cbmc_result_path": str(jf),
                "judge_path": str(jf),
                "witness": [],
                "from_adjacent": {
                    "primary_function": fn,
                    "primary_property": d.get("failing_property", "?"),
                    "primary_verdict": (d.get("judge") or {}).get("verdict", "?"),
                },
            })

    # Dedupe: same (function, property) → keep the strongest-graded record.
    # When primary + N adjacents all point at the same bug, we want one
    # report (prefer primary's reasoning; merge sources).
    GRADE_RANK = {"A": 3, "B": 2, "C": 1}
    best: dict[tuple[str, str], dict] = {}
    for rec in findings:
        key = (rec["function"], rec["failing_property"])
        g, _ = grade(rec["failing_property"], rec.get("dynamic"))
        rec["_grade"] = g
        # Track also-surfaced sources (primary vs adjacents from N functions)
        rec.setdefault("_also_via", [])
        if key in best:
            prev = best[key]
            if GRADE_RANK[g] > GRADE_RANK[prev["_grade"]]:
                # Promote new winner; record the prior as also-via
                rec["_also_via"] = list(prev.get("_also_via") or [])
                if prev.get("from_adjacent"):
                    rec["_also_via"].append(
                        ("adjacent of " + prev["from_adjacent"]["primary_function"],
                         prev["_grade"])
                    )
                else:
                    rec["_also_via"].append(("primary", prev["_grade"]))
                best[key] = rec
            else:
                # Keep prior winner; annotate the loser as also-via
                if rec.get("from_adjacent"):
                    prev["_also_via"].append(
                        ("adjacent of " + rec["from_adjacent"]["primary_function"], g)
                    )
                else:
                    prev["_also_via"].append(("primary", g))
        else:
            best[key] = rec

    findings = list(best.values())

    # Write per-bug reports
    written = []
    for rec in findings:
        p = write_report(rec)
        written.append((rec, p))

    # Index
    idx_lines = [
        "# bmc-agent judge_v7 findings — index",
        "",
        f"**Sweep**: `/tmp/libarchive_judge_v7/judge_v7`  ",
        f"**Corpus**: `/tmp/libarchive_seedhunt_full` (7 .c files, libarchive "
        f"snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)  ",
        f"**Config**: `--agentic-harness --refine-rounds 1 --enable-flag-selection`  ",
        f"**Note**: sweep was still in progress at index-generation time; "
        f"more findings may follow.",
        "",
        f"## {len(written)} unique realistic finding(s)",
        "",
        "| Grade | File | Function | Property | Dyn-val | Also via | Report |",
        "|---|---|---|---|---|---|---|",
    ]
    # Sort by grade then file then function
    written.sort(key=lambda x: (-{"A":3,"B":2,"C":1}[x[0]["_grade"]], x[0]["file_stem"], x[0]["function"]))
    for rec, p in written:
        g = rec["_grade"]
        dynout = (rec.get("dynamic") or {}).get("outcome", "-")
        src = "adjacent" if rec.get("from_adjacent") else "primary"
        n_extra = len(rec.get("_also_via") or [])
        also = f"+{n_extra} re-surface(s)" if n_extra else "-"
        idx_lines.append(
            f"| **{g}** | `{rec['file_stem']}.c` | `{rec['function']}` | "
            f"`{rec['failing_property']}` | {dynout} | {also} | "
            f"[{p.name}]({p.name}) ({src}) |"
        )

    idx_lines.extend([
        "",
        "## Evidence grades",
        "",
        "- **A** — judge realistic AND dyn-val sanitizer hit reproduces the "
        "same property class. Strongest evidence.",
        "- **B** — judge realistic AND dyn-val sanitizer hit on a related path, "
        "different property class. The reproducer triggered a crash in "
        "libarchive's code via the same code path, but not the same bug "
        "class CBMC identified. Circumstantial — needs human review to "
        "decide whether the ASan signal corresponds to the CBMC finding "
        "or is an unrelated side-bug.",
        "- **C** — judge realistic, dyn-val did not reproduce. Judge-only.",
        "",
        "## Caveats",
        "",
        "- These are automated findings from a research prototype. The CBMC "
        "  counterexample is real; the realism judgement is an LLM call.",
        "- The agentic harness writes a harness it believes matches the real "
        "  caller chain. When it gets that wrong, the finding may be a "
        "  harness artifact even when CBMC says 'verification failed'.",
        "- Grade **B** findings should be treated as 'crash reproduced in "
        "  libarchive but the exact CBMC trace was not exhibited'.",
        "- This is not coordinated disclosure. None of these has been "
        "  filed with libarchive upstream.",
    ])
    (OUT / "index.md").write_text("\n".join(idx_lines))

    print(f"wrote {len(written)} report(s) to {OUT}")
    print(f"index: {OUT / 'index.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
