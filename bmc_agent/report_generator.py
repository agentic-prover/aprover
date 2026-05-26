"""
Per-bug report generator.

After a sweep finishes, write one human-readable markdown report per
realism-confirmed function to ``<output>/reports/<function>.md``. A
function counts as confirmed if ANY of its per-CEx records had
``realism.verdict == realistic AND confidence != unlikely`` — the latest
``bug_report.json`` alone is not enough, because realism's verdict can
flip across CExes and the latest one may be a spurious downgrade.

Reports include: target / property / call chain, layered verdict table,
realism reasoning, exploit scenario, per-CEx history list, reproduction
artifact paths, and explicit caveats about dynamic outcome and the gap
between realism-endorsed and independently-verified bugs.

Used by ``cli._cmd_verify_dir`` at the end of every sweep.
"""

from __future__ import annotations

import datetime
import glob
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _best_record(fn_dir: str):
    """Return the strongest (realism=realistic, confidence!=unlikely)
    per-CEx record for this function dir, or ``None`` if none exists.

    Prefers the latest realistic record by mtime so the reasoning text
    comes from an actually-confirmed run. Falls back to the top-level
    ``bug_report.json`` only when no per-CEx record qualifies.
    """
    history_dir = os.path.join(fn_dir, "bug_reports")
    candidates: list[tuple[float, str, dict, dict]] = []
    if os.path.isdir(history_dir):
        for hf in os.listdir(history_dir):
            p = os.path.join(history_dir, hf)
            try:
                d = json.load(open(p))["report"]
            except Exception:
                continue
            rc = d.get("realism_check") or {}
            if (rc.get("verdict") or "").lower() != "realistic":
                continue
            if d.get("confidence") == "unlikely":
                continue
            candidates.append((os.path.getmtime(p), p, d, rc))
    if not candidates:
        br = os.path.join(fn_dir, "bug_report.json")
        if not os.path.exists(br):
            return None
        try:
            d = json.load(open(br))["report"]
        except Exception:
            return None
        rc = d.get("realism_check") or {}
        if (rc.get("verdict") or "").lower() != "realistic":
            return None
        if d.get("confidence") == "unlikely":
            return None
        return (br, d, rc)
    candidates.sort(key=lambda t: t[0], reverse=True)
    _, p, d, rc = candidates[0]
    return (p, d, rc)


def _read_function_source(file_stem: str, fn: str, source_root: str | None = None) -> tuple[str, tuple[int, int] | None]:
    """Extract the body of ``fn`` from ``libarchive/{file_stem}.c``.

    Returns (source_excerpt, (start_line, end_line)) or ("(source not found)", None).
    Looks up libarchive on a few common paths; users without libarchive
    locally get a graceful fallback message.
    """
    candidates = []
    if source_root:
        candidates.append(os.path.join(source_root, f"{file_stem}.c"))
    candidates += [
        f"/tmp/libarchive_auto_corpus/{file_stem}.c",
        f"/tmp/libarchive_bench/libarchive/libarchive/{file_stem}.c",
        f"/tmp/libarchive_seedhunt_full/{file_stem}.c",
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            lines = open(path).readlines()
        except Exception:
            continue
        # Find a top-level function definition line. Heuristic: a line that
        # starts with the function name followed by '(' (after the return-type
        # line preceding it).
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith(fn + "(") or stripped.startswith(fn + " ("):
                # Walk back to capture return type (one prior non-blank line).
                start = i
                if start > 0 and lines[start - 1].strip() and not lines[start - 1].lstrip().startswith("//"):
                    start = i - 1
                # Walk forward to find matching closing brace.
                depth = 0
                end = start
                in_body = False
                for j in range(start, min(start + 600, len(lines))):
                    for ch in lines[j]:
                        if ch == "{":
                            depth += 1
                            in_body = True
                        elif ch == "}":
                            depth -= 1
                            if in_body and depth == 0:
                                end = j
                                break
                    if in_body and depth == 0:
                        break
                excerpt = "".join(lines[start : end + 1])
                # Cap to ~6000 chars; if longer, truncate mid-body with a marker.
                if len(excerpt) > 6000:
                    head = "".join(lines[start : start + 60])
                    excerpt = head + "\n    /* ... function body truncated for report ... */\n"
                return excerpt.rstrip(), (start + 1, end + 1)
    return "(libarchive source not available on common paths; see file_stem above)", None


def _format_cex_witness(report: dict) -> str:
    """Render the CBMC counterexample's variable_assignments as a fenced text
    block suitable for embedding in markdown. Keeps it small (≤ ~3000 chars).
    """
    cex = report.get("counterexample") or {}
    vars_ = cex.get("variable_assignments") or {}
    if not vars_:
        return "(no variable assignments recorded)"
    lines = []
    for k, v in sorted(vars_.items()):
        s = str(v)
        if len(s) > 200:
            s = s[:200] + "..."
        lines.append(f"  {k} = {s}")
        if sum(len(line) for line in lines) > 3000:
            lines.append("  ... (truncated)")
            break
    return "\n".join(lines)


def _format_report(
    br_path: str,
    report: dict,
    realism: dict,
    file_stem: str,
    fn_dir: str,
    rerun_cmd: str | None = None,
    source_root: str | None = None,
) -> str:
    fn = report.get("function_name", "unknown")
    cls_path = os.path.join(fn_dir, "classification.json")
    dyn: dict = {}
    if os.path.exists(cls_path):
        try:
            dyn = ((json.load(open(cls_path)).get("classification") or {}).get("dynamic_result") or {})
        except Exception:
            pass
    history_dir = os.path.join(fn_dir, "bug_reports")
    history_files = sorted(os.listdir(history_dir)) if os.path.isdir(history_dir) else []

    prop = report.get("violated_property") or (report.get("counterexample") or {}).get("failing_property", "")
    call_chain = " -> ".join(report.get("call_chain") or [])
    tier = report.get("confidence", "?")
    realism_verdict = realism.get("verdict", "?")
    realism_conf = realism.get("llm_confidence", "?")
    dyn_outcome = dyn.get("outcome", "no_record")
    dyn_signal = dyn.get("signal_name", "none")
    reasoning = realism.get("reasoning") or "(none)"
    scenario = realism.get("key_concern") or "(none)"

    dyn_caveat = (
        "STRONG evidence: the dynamic GCC+ASAN harness actually crashed at runtime, matching CBMC's predicted property."
        if dyn_outcome == "confirmed"
        else "WEAK evidence: the dynamic harness did NOT reproduce the crash with the concrete CBMC witness. The realism LLM's vote is the only evidence."
    )

    history_lines = "\n".join(f"- `bug_reports/{hf}`" for hf in history_files) or "- (none)"

    src_excerpt, src_lines = _read_function_source(file_stem, fn, source_root)
    src_lines_label = f"lines {src_lines[0]}-{src_lines[1]}" if src_lines else "(unknown lines)"
    cex_witness = _format_cex_witness(report)

    repro_cmd = f"""# 1. clone libarchive at the snapshot the sweep used
cd /tmp && git clone https://github.com/libarchive/libarchive
cd libarchive && git checkout 67830f7b9c27080c0170bcd71d94fb42316c47dd

# 2. apply CBMC bounds + pointer + signed-overflow checks
cbmc \\
    --bounds-check --pointer-check --div-by-zero-check \\
    --signed-overflow-check --unsigned-overflow-check --pointer-overflow-check \\
    --unwind 4 --timeout 60 \\
    -I /tmp/libarchive/libarchive -I /tmp/libarchive/libarchive/build \\
    -DHAVE_CONFIG_H \\
    --function main \\
    {os.path.basename(fn_dir)}/harness.c
# (paste the harness contents from the section below into harness.c first;
#  it is also committed alongside this report as harness.c.)
"""

    return f"""# bmc-agent-sec confirmed finding: `{fn}`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: {datetime.datetime.now(datetime.timezone.utc).isoformat()}

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/{file_stem}.c`
- **Function**: `{fn}` ({src_lines_label})
- **Violated property**: `{prop}` (CBMC-reported)
- **Call chain established**: `{call_chain}`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **{realism_verdict}** / confidence `{realism_conf}` |
| Dynamic harness (GCC + signal handlers) | **{dyn_outcome}**, signal=`{dyn_signal}` |
| Final tier | `{tier}` |

## Realism reasoning

{reasoning}

## Exploit scenario (LLM-supplied)

{scenario}

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
{cex_witness}
```

## Function source (from the snapshot)

```c
{src_excerpt}
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

{history_lines}

## Reproduction

The harness CBMC verified is committed alongside this report as `harness.c`. To re-verify just this finding:

```bash
{repro_cmd}
```

To re-run the full sweep end-to-end (re-derives this finding from scratch):

```bash
{rerun_cmd or "(no command provided)"}
```

## Honest caveats (read before upstream reporting)

- **Dynamic outcome was `{dyn_outcome}`.** {dyn_caveat}
- The realism LLM's attacker scenario may hypothesize an upstream condition (e.g. "some bug elsewhere creates the dangling pointer state"). **Independent code-level verification of that condition is required before reporting upstream.**
- Realism nondeterminism: the same CEx can flip between REALISTIC and UNREALISTIC across runs. Multiple per-CEx records in `bug_reports/` may show different verdicts; this report uses the strongest realistic record by mtime.
- The harness is auto-generated and uses CBMC's nondeterministic-input model. Reading `harness.c` shows exactly what input states CBMC was free to explore — verify those states are actually reachable from the real public API before declaring a vulnerability.
"""


def generate_reports(sweep_output: str | Path, driver: str, rerun_cmd: str | None = None) -> list[str]:
    """Walk ``<sweep_output>/<driver>/<file>/<function>/`` and write one
    markdown report per realism-confirmed function. Returns the list of
    written report paths (empty if none confirmed).
    """
    out = str(sweep_output)
    report_dir = os.path.join(out, "reports")
    os.makedirs(report_dir, exist_ok=True)

    fn_dirs = sorted({
        os.path.dirname(p)
        for p in glob.glob(f"{out}/{driver}/*/*/bug_report.json")
    })
    written: list[str] = []
    for fn_dir in fn_dirs:
        rec = _best_record(fn_dir)
        if rec is None:
            continue
        br_path, d, rc = rec
        # path: .../{driver}/{file_stem}/{function}/
        parts = fn_dir.split(f"/{driver}/")[-1].split("/")
        if len(parts) < 2:
            continue
        file_stem, fn = parts[0], parts[1]
        md = _format_report(br_path, d, rc, file_stem, fn_dir, rerun_cmd)
        out_path = os.path.join(report_dir, f"{fn}.md")
        try:
            with open(out_path, "w") as f:
                f.write(md)
            written.append(out_path)
        except OSError as e:
            logger.warning("Failed to write report for %s: %s", fn, e)
            continue
        # Copy the harness alongside the report so the report is
        # self-contained for external reproduction.
        src_h = os.path.join(fn_dir, "harness.c")
        if os.path.exists(src_h):
            try:
                with open(src_h, "rb") as src, open(os.path.join(report_dir, f"{fn}.harness.c"), "wb") as dst:
                    dst.write(src.read())
            except Exception as e:
                logger.warning("Failed to copy harness for %s: %s", fn, e)

    # Build a per-finding row for the index — needs the same data the
    # individual reports use, so re-walk fn_dirs once. Confirmed_fns is
    # the canonical set of currently-confirmed function names; everything
    # else gets pruned from report_dir to keep the published findings/
    # tree consistent.
    written_fns: set[str] = set()
    rows: list[dict] = []
    for fn_dir in fn_dirs:
        rec = _best_record(fn_dir)
        if rec is None:
            continue
        br_path, d, rc = rec
        fn = d.get("function_name", "unknown")
        written_fns.add(fn)
        parts = fn_dir.split(f"/{driver}/")[-1].split("/")
        file_stem = parts[0] if parts else "?"
        prop = d.get("violated_property") or (d.get("counterexample") or {}).get("failing_property", "")
        cls_path = os.path.join(fn_dir, "classification.json")
        dyn: dict = {}
        if os.path.exists(cls_path):
            try:
                dyn = ((json.load(open(cls_path)).get("classification") or {}).get("dynamic_result") or {})
            except Exception:
                pass
        rows.append({
            "fn": fn,
            "file": file_stem,
            "property": prop,
            "tier": d.get("confidence", "?"),
            "realism_verdict": rc.get("verdict", "?"),
            "realism_conf": rc.get("llm_confidence", "?"),
            "dyn_outcome": dyn.get("outcome", "no_record"),
            "scenario": (rc.get("key_concern") or "")[:200].replace("\n", " ").strip(),
        })

    if rows:
        gen_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        index_lines = [
            "# bmc-agent-sec confirmed findings",
            "",
            f"**Generated**: {gen_ts}  ",
            f"**Sweep output**: `{out}`  ",
            f"**Driver**: `{driver}`",
            "",
            f"## Summary ({len(rows)} realism-endorsed function(s))",
            "",
            "A function is counted as confirmed if AT LEAST ONE of its CBMC counterexamples passed the realism check (verdict=realistic, confidence!=unlikely). Realism nondeterminism is real: see each report's per-CEx history for the audit trail.",
            "",
            "| # | Function | File | Property | Tier | Realism | Dynamic |",
            "|---|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(sorted(rows, key=lambda r: r["fn"]), start=1):
            index_lines.append(
                f"| {i} | [`{r['fn']}`]({r['fn']}.md) | `{r['file']}.c` | `{r['property']}` | `{r['tier']}` | {r['realism_verdict']} / {r['realism_conf']} | {r['dyn_outcome']} |"
            )

        # Tally by dynamic outcome — useful at a glance.
        by_dyn: dict[str, int] = {}
        for r in rows:
            by_dyn[r["dyn_outcome"]] = by_dyn.get(r["dyn_outcome"], 0) + 1
        index_lines += [
            "",
            "## Evidence breakdown",
            "",
            "How strong is the runtime evidence behind each finding?",
            "",
            "| Dynamic outcome | Count | What it means |",
            "|---|---|---|",
            f"| `confirmed` | {by_dyn.get('confirmed', 0)} | GCC+ASAN harness actually crashed at runtime — PoC-grade evidence |",
            f"| `not_triggered` | {by_dyn.get('not_triggered', 0)} | Harness compiled and ran clean; the specific CBMC witness didn't reproduce. Bug may still be real with different inputs. |",
            f"| `inconclusive` | {by_dyn.get('inconclusive', 0)} | Harness compile failed (e.g. private headers); no runtime signal either way. |",
            f"| `no_record` / `skipped` | {by_dyn.get('no_record', 0) + by_dyn.get('skipped', 0)} | Dynamic didn't run (disabled, or both static checks errored). |",
            "",
            "## Per-finding scenarios",
            "",
        ]
        for i, r in enumerate(sorted(rows, key=lambda r: r["fn"]), start=1):
            index_lines.append(f"**{i}. [`{r['fn']}`]({r['fn']}.md)** — `{r['file']}.c`")
            if r["scenario"]:
                index_lines.append("")
                index_lines.append(f"> {r['scenario']}")
            index_lines.append("")

        index_lines += [
            "## How to read these findings",
            "",
            "1. The **realism check** is the audit. bmc-agent-sec counts a finding as a real bug when the LLM auditor (given full code context, callers, dynamic outcome) votes REALISTIC. The same LLM that finds bugs is told to be its own skeptic.",
            "2. The **dynamic outcome** column tells you whether the GCC+ASAN runtime check independently confirmed the crash. `confirmed` is the strongest evidence; `not_triggered` is the most ambiguous (could be a real bug with a different attacker input, could be an FP).",
            "3. The **tier** column is the pipeline's classification: `confirmed_dynamic` > `confirmed_system_entry` > `confirmed_bmc`. The tier guard ensures `confirmed_dynamic` is only assigned when dynamic actually crashed.",
            "4. Open the per-finding `<function>.md` for: full realism reasoning, attacker scenario, CBMC counterexample witness, function source, and concrete `cbmc` reproduction command.",
            "5. The exact harness CBMC verified is committed as `<function>.harness.c` alongside each report.",
            "",
            "## Caveats every reviewer should know",
            "",
            "- **Realism nondeterminism**: the LLM can flip on the same CEx across runs (~10% on borderline cases). We use the strongest realistic CEx per function; downgraded CExes for the same function are preserved in `bug_reports/<property>.json` in the sweep artifact tree.",
            "- **Pre-classifier disabled by default**: an earlier static filter was killing seed bugs before realism could see them. It's off now.",
            "- **Realism endorses ≠ verified**: realism's reasoning is plausible but the LLM may hypothesize an upstream condition that isn't actually reachable. Manual code audit or successful PoC reproduction is the gold standard before reporting upstream.",
            "- **Reports auto-generated** by `bmc_agent/report_generator.py`; reviewers should treat them as primary-source audit-trail dumps, not as polished disclosures.",
            "",
        ]

        with open(os.path.join(report_dir, "index.md"), "w") as f:
            f.write("\n".join(index_lines) + "\n")
        written.append(os.path.join(report_dir, "index.md"))

    # Stale-cleanup: any <fn>.md / <fn>.harness.c in report_dir whose <fn>
    # isn't in the current confirmed set is from a previous run where that
    # function was confirmed but has since been entirely downgraded. Remove
    # so the published findings tree always reflects the current sweep state.
    if os.path.isdir(report_dir):
        for f in os.listdir(report_dir):
            if f == "index.md":
                continue
            base, sep, ext = f.rpartition(".")
            if ext == "md" and base not in written_fns:
                logger.info("Removing stale report: %s", f)
                try:
                    os.remove(os.path.join(report_dir, f))
                except OSError:
                    pass
                continue
            if f.endswith(".harness.c"):
                fn_name = f[: -len(".harness.c")]
                if fn_name not in written_fns:
                    logger.info("Removing stale harness: %s", f)
                    try:
                        os.remove(os.path.join(report_dir, f))
                    except OSError:
                        pass

    return written
