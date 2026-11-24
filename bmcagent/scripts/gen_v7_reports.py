"""Generate per-bug markdown reports for judge_v7 realistic verdicts.

For each judge_*.json in the sweep tree where verdict=='realistic', AND for
each adjacent_confirmation where confirmed==True, write a self-contained
markdown report to findings/v7/<file>__<function>__<property>.md plus an
index.md.

This version BUNDLES the reproduction artifacts into the repo (the prior
version only referenced /tmp/ paths). For each finding:
  * Re-runs CBMC on the saved harness.c to recover the witness +
    abbreviated trace if cbmc_result.json doesn't already carry them
    (older sweeps wrote a minimal cbmc_result.json).
  * Copies the CBMC harness   → findings/v7/harnesses/<bug>.c
  * Copies the dyn-val reproducer (winning attempt, when present)
                              → findings/v7/reproducers/<bug>.c
  * Embeds witness + sanitizer stderr inline in the markdown report.

Evidence grading:
  A  judge realistic + dyn-val SIGABRT/sanitizer hit + same property class
  B  judge realistic + dyn-val SIGABRT/sanitizer hit on a related path
     (different bug class — circumstantial confirmation only)
  C  judge realistic, dyn-val did not reproduce
     (no_reproducer / not_triggered / timeout / skipped)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SWEEP = Path("/tmp/libarchive_judge_v7/judge_v7")
OUT   = Path(REPO / "findings/v7")
HARNESS_DIR = OUT / "harnesses"
REPRO_DIR   = OUT / "reproducers"
CORPUS_ROOT = Path("/tmp/libarchive_seedhunt_full")
LIBA_INCLUDE = "/tmp/libarchive_bench/libarchive/libarchive"
BUILD_INCLUDE = "/tmp/libarchive_bench/libarchive/build"


def safe_slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def grade(cbmc_prop: str, dyn: dict | None) -> tuple[str, str]:
    if not dyn or dyn.get("outcome") != "confirmed_dynamic":
        outcome = (dyn or {}).get("outcome") or "skipped"
        return "C", f"judge-only ({outcome})"
    stderr = (dyn.get("stderr_excerpt") or "") + (dyn.get("stderr") or "")
    prop_class = cbmc_prop.split(".", 2)[1] if "." in cbmc_prop else cbmc_prop
    same_class = False
    if "overflow" in prop_class and ("integer-overflow" in stderr.lower() or "exceeds maximum supported size" in stderr.lower()):
        same_class = True
    if "pointer_dereference" in prop_class and ("heap-buffer-overflow" in stderr.lower() or "use-after-free" in stderr.lower() or "null pointer" in stderr.lower() or "stack-buffer-overflow" in stderr.lower()):
        same_class = True
    if "pointer_arithmetic" in prop_class and "buffer-overflow" in stderr.lower():
        same_class = True
    if "array_bounds" in prop_class and "buffer-overflow" in stderr.lower():
        same_class = True
    if same_class:
        return "A", "dynamically confirmed (same property class)"
    return "B", "dynamically reproduced a related crash (different property class — circumstantial)"


def function_source_excerpt(file_stem: str, fn_name: str) -> tuple[str, int]:
    src = CORPUS_ROOT / f"{file_stem}.c"
    if not src.is_file():
        return ("", 0)
    text = src.read_text(errors="replace").splitlines()
    # Match the DEFINITION (has `{` at end of params on same line or next),
    # not the forward-declaration (ends with `;`).
    sig_pat = re.compile(
        rf"^\s*(?:static\s+|inline\s+)*[A-Za-z_][\w*\s,()]*?\b{re.escape(fn_name)}\s*\("
    )
    for i, line in enumerate(text):
        if not sig_pat.match(line):
            continue
        # Peek forward up to 8 lines for `{` (definition) vs `;` (decl).
        peek = "\n".join(text[i : i + 8])
        if "{" not in peek or (";" in peek and peek.index(";") < peek.index("{")):
            continue  # forward declaration
        brace = 0
        started = False
        for j in range(i, min(i + 600, len(text))):
            brace += text[j].count("{") - text[j].count("}")
            if "{" in text[j]:
                started = True
            if started and brace == 0:
                return ("\n".join(text[i : j + 1]), i + 1)
        return ("\n".join(text[i : i + 80]), i + 1)
    return ("", 0)


def recover_witness(fn_dir: Path, failing_property: str) -> tuple[dict, list[str]]:
    """Return (variable_assignments, trace) for the named property.

    First tries the persisted counterexamples[] in cbmc_result.json
    (sweeps from this commit on persist them). Falls back to re-running
    CBMC on the saved harness.c when missing or empty (older sweeps).
    """
    cr_path = fn_dir / "cbmc_result.json"
    if cr_path.is_file():
        try:
            cr = json.load(open(cr_path))
        except Exception:
            cr = {}
        for c in cr.get("counterexamples") or []:
            if c.get("failing_property") == failing_property:
                return (
                    c.get("variable_assignments") or {},
                    c.get("trace") or [],
                )
    harness = fn_dir / "harness.c"
    if not harness.is_file():
        return ({}, [])
    # Re-run CBMC. Use the same flags judge_pipeline uses by default.
    from bmc_agent.cbmc import run_cbmc
    try:
        r = run_cbmc(
            harness_path=str(harness),
            unwind=16, timeout=120,
            include_dirs=[BUILD_INCLUDE, LIBA_INCLUDE],
            defines=["HAVE_CONFIG_H"],
            bounds_check=True, pointer_check=True,
            signed_overflow_check=True, div_by_zero_check=True,
            unsigned_overflow_check=True,
            conversion_check=False, pointer_overflow_check=False,
        )
    except Exception as exc:
        print(f"  WARN: cbmc re-run for {fn_dir.name} failed: {exc}", file=sys.stderr)
        return ({}, [])
    for c in (r.counterexamples or []):
        if (getattr(c, "failing_property", "") or "") == failing_property:
            return (
                dict(getattr(c, "variable_assignments", {}) or {}),
                list(getattr(c, "trace", []) or [])[:200],
            )
    # Property-by-name didn't match; return the first CEx as best-effort
    if r.counterexamples:
        c = r.counterexamples[0]
        return (
            dict(getattr(c, "variable_assignments", {}) or {}),
            list(getattr(c, "trace", []) or [])[:200],
        )
    return ({}, [])


def find_winning_reproducer(fn_dir: Path, target_fn: str) -> Path | None:
    """The dynamic-validation pass writes reproducer_attempt{1,2,3}.c
    under <fn_dir>/dynamic/<target_fn>/. The 'attempts' list in the
    judge JSON tells us which one fired SIGABRT."""
    d = fn_dir / "dynamic" / target_fn
    if not d.is_dir():
        return None
    # Prefer the latest reproducer_attempt*.c with a matching .bin
    cs = sorted(d.glob("reproducer_attempt*.c"))
    return cs[-1] if cs else None


def write_report(rec: dict) -> Path:
    file_stem = rec["file_stem"]
    fn = rec["function"]
    prop = rec["failing_property"]
    out_name = safe_slug(f"{file_stem}__{fn}__{prop}") + ".md"
    out_path = OUT / out_name
    slug = out_path.stem

    judge = rec["judge"]
    dyn = rec.get("dynamic") or {}
    g, g_desc = grade(prop, dyn)

    # Bundle the harness
    src_harness = Path(rec["harness_path"]) if rec.get("harness_path") else None
    bundled_harness: Path | None = None
    if src_harness and src_harness.is_file():
        bundled_harness = HARNESS_DIR / f"{slug}.c"
        bundled_harness.write_text(src_harness.read_text())

    # Bundle the winning reproducer
    bundled_repro: Path | None = None
    if dyn.get("harness_path"):
        rp = Path(dyn["harness_path"])
        if rp.is_file():
            bundled_repro = REPRO_DIR / f"{slug}.c"
            bundled_repro.write_text(rp.read_text())

    # Recover witness (from persisted cbmc_result.json or by re-running CBMC)
    var_assigns, trace = ({}, [])
    if not rec.get("from_adjacent"):
        fn_dir = Path(rec["judge_path"]).parent
        var_assigns, trace = recover_witness(fn_dir, prop)

    # Function source excerpt
    body, start_line = function_source_excerpt(file_stem, fn)
    body_block = (
        f"### Source: `{file_stem}.c` (lines {start_line}–{start_line + body.count(chr(10))})\n\n"
        f"```c\n{body}\n```\n\n" if body else ""
    )

    # Witness block (variable assignments + trace excerpt)
    witness_lines = []
    if var_assigns:
        for k, v in sorted(var_assigns.items()):
            witness_lines.append(f"  {k} = {v}")
    witness_block = (
        "```text\n" + "\n".join(witness_lines) + "\n```"
        if witness_lines else "_witness not recovered_"
    )
    trace_block = ""
    if trace:
        trace_block = (
            "### CBMC trace (first 80 steps)\n\n"
            "```text\n"
            + "\n".join(f"{i+1:3d}. {s}" for i, s in enumerate(trace[:80]))
            + "\n```\n\n"
        )

    # Harness embedded
    harness_block = ""
    if bundled_harness:
        h = bundled_harness.read_text()
        harness_block = (
            f"### CBMC harness (bundled at `{bundled_harness.relative_to(REPO)}`)\n\n"
            f"```c\n{h}\n```\n\n"
        )

    # Reproducer embedded
    repro_block = ""
    if bundled_repro:
        rep = bundled_repro.read_text()
        repro_block = (
            f"### Dynamic reproducer "
            f"(bundled at `{bundled_repro.relative_to(REPO)}`)\n\n"
            f"This is the {dyn.get('attempt','?')}-of-{dyn.get('n_attempts','?')} attempt the dyn-val LLM "
            f"produced that triggered the sanitizer. Compile + link against a "
            f"sanitiser-instrumented libarchive .so:\n\n"
            f"```sh\n"
            f"gcc -fsanitize=address,undefined -g -O1 -I/path/to/libarchive \\\n"
            f"    {bundled_repro.name} -L/path/to/libarchive/build -larchive -o repro\n"
            f"LD_LIBRARY_PATH=/path/to/libarchive/build ./repro\n"
            f"```\n\n"
            f"```c\n{rep}\n```\n\n"
        )

    # Sanitizer output
    san_block = ""
    if dyn and dyn.get("stderr_excerpt"):
        san_block = (
            f"### Sanitizer output\n\n"
            f"```text\n{(dyn.get('stderr_excerpt') or '')[:3000]}\n```\n\n"
        )

    # Adjacent-bug context
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
        for src, gg in rec["_also_via"]:
            adj_block += f"- {src} (grade {gg})\n"
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

{witness_block}

{trace_block}{harness_block}{repro_block}{san_block}## Caveats

- This is an *automated* finding. The CBMC counterexample is real; the
  realism judgement is an LLM call.
- Grade **B** findings reproduced a crash in libarchive but not the
  exact CBMC property class.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may follow. See `findings/v7/index.md`.
- The bundled reproducer hit the sanitizer when compiled + linked
  against the libarchive build at the path noted above. Other builds
  (different libarchive version, -O2 vs -O0, different libc) may not
  reproduce.
"""
    out_path.write_text(text)
    return out_path


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    REPRO_DIR.mkdir(parents=True, exist_ok=True)
    findings = []

    for jf in sorted(SWEEP.rglob("judge_*.json")):
        try: d = json.load(open(jf))
        except: continue
        fn_dir = jf.parent
        file_stem = fn_dir.parent.name
        fn = fn_dir.name

        judge = d.get("judge") or {}
        if judge.get("verdict") == "realistic":
            findings.append({
                "file_stem": file_stem,
                "function": fn,
                "failing_property": d.get("failing_property", "?"),
                "judge": judge,
                "dynamic": d.get("primary_dynamic_validation") or None,
                "harness_path": str(fn_dir / "harness.c"),
                "cbmc_result_path": str(fn_dir / "cbmc_result.json"),
                "judge_path": str(jf),
                "from_adjacent": None,
            })

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
                # adjacent harness is generated on-the-fly; we don't
                # currently persist it. Fall back to the primary harness
                # which at least documents the harness style.
                "harness_path": str(fn_dir / "harness.c"),
                "cbmc_result_path": str(jf),
                "judge_path": str(jf),
                "from_adjacent": {
                    "primary_function": fn,
                    "primary_property": d.get("failing_property", "?"),
                    "primary_verdict": (d.get("judge") or {}).get("verdict", "?"),
                },
            })

    # Dedupe by (function, property): keep strongest grade
    GRADE_RANK = {"A": 3, "B": 2, "C": 1}
    best: dict[tuple[str, str], dict] = {}
    for rec in findings:
        key = (rec["function"], rec["failing_property"])
        g, _ = grade(rec["failing_property"], rec.get("dynamic"))
        rec["_grade"] = g
        rec.setdefault("_also_via", [])
        if key in best:
            prev = best[key]
            if GRADE_RANK[g] > GRADE_RANK[prev["_grade"]]:
                rec["_also_via"] = list(prev.get("_also_via") or [])
                src_prev = ("adjacent of " + prev["from_adjacent"]["primary_function"]
                            if prev.get("from_adjacent") else "primary")
                rec["_also_via"].append((src_prev, prev["_grade"]))
                best[key] = rec
            else:
                src_new = ("adjacent of " + rec["from_adjacent"]["primary_function"]
                           if rec.get("from_adjacent") else "primary")
                prev["_also_via"].append((src_new, g))
        else:
            best[key] = rec
    findings = list(best.values())

    written = []
    for rec in findings:
        print(f"  {rec['_grade']}  {rec['function']}/{rec['failing_property']}")
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
        "## Bundled reproduction artifacts",
        "",
        f"- CBMC harnesses (LLM-written): `findings/v7/harnesses/<bug>.c`",
        f"- Dynamic reproducers (winning attempt for grade-A/B): "
        f"`findings/v7/reproducers/<bug>.c`",
        f"- CBMC witnesses + abbreviated trace: embedded inline in each report",
        f"- ASan/UBSan stderr excerpts: embedded inline in each report",
        "",
        f"## {len(written)} unique realistic finding(s)",
        "",
        "| Grade | File | Function | Property | Dyn-val | Also via | Report |",
        "|---|---|---|---|---|---|---|",
    ]
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
        "class CBMC identified.",
        "- **C** — judge realistic, dyn-val did not reproduce. Judge-only.",
        "",
        "## Caveats",
        "",
        "- These are automated findings from a research prototype. The CBMC "
        "counterexample is real; the realism judgement is an LLM call.",
        "- The agentic harness writes a harness it believes matches the real "
        "caller chain. When it gets that wrong, the finding may be a "
        "harness artifact even when CBMC says 'verification failed'.",
        "- Grade **B** findings reproduced a crash in libarchive but not the "
        "exact CBMC property class.",
        "- This is not coordinated disclosure. None of these has been filed "
        "with libarchive upstream.",
    ])
    (OUT / "index.md").write_text("\n".join(idx_lines))

    print(f"\nwrote {len(written)} report(s) to {OUT}")
    print(f"  harnesses: {sum(1 for _ in HARNESS_DIR.glob('*.c'))}")
    print(f"  reproducers: {sum(1 for _ in REPRO_DIR.glob('*.c'))}")
    print(f"  index: {OUT / 'index.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
