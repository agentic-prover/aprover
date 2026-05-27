"""Run the ``TriageAgent`` over a sweep's per-CEx classifications.

For every classification under
``<sweep_dir>/<driver>/<fn>/classifications/<property>.json`` whose
outcome is UNRESOLVED (or REAL_BUG with low pipeline confidence),
build a triage prompt from the available evidence and ask the
TriageAgent for an independent verdict. Writes:

  * One ``triage.json`` per CEx alongside the classification it judged.
  * A summary at ``<sweep_dir>/triage_summary.md`` with the verdicts +
    fp_class breakdown.

Usage:
    source /tmp/aprover_or_keys.env
    .venv/bin/python scripts/triage_unresolved.py \\
        --sweep-dir /tmp/libarchive_postfix8 \\
        --driver libarchive_postfix8 \\
        --corpus-dir /tmp/libarchive_seedhunt_full \\
        [--only fn1,fn2 ...]
        [--include-real-bug]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bmc_agent.agents.triage import TriageAgent, TriageResult, TriageVerdict
from bmc_agent.agents.triage_tools import TriageToolsAgent
from bmc_agent.config import Config
from bmc_agent.llm import LLMClient


def _read_function_source(corpus_dir: Path, function_name: str) -> str:
    """Extract a function definition from any .c file in corpus_dir.
    Best-effort regex match — returns empty string when not found."""
    pat = re.compile(
        r"^\s*(?:static\s+|inline\s+)*[\w\*\s]+?\b"
        + re.escape(function_name)
        + r"\s*\([^)]*\)\s*\{",
        re.MULTILINE,
    )
    for c_file in corpus_dir.glob("*.c"):
        try:
            text = c_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = pat.search(text)
        if not m:
            continue
        start = m.start()
        # walk the body's matching brace
        i = m.end() - 1  # position of '{'
        depth = 0
        n = len(text)
        while i < n:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
            i += 1
    return ""


def _format_witness(variable_assignments: dict) -> str:
    """Filter the CBMC witness to function-relevant entries."""
    relevant: list[str] = []
    for k, v in variable_assignments.items():
        if k.startswith("__CPROVER_"):
            continue
        if k.startswith("nfsv4_acl_") or k.startswith("nfs4_acl_"):
            continue
        relevant.append(f"  {k} = {v}")
    return "\n".join(relevant)


def triage_one(
    *,
    agent: TriageAgent,
    function_name: str,
    function_source: str,
    classification_path: Path,
    harness_text: str,
) -> Optional[TriageResult]:
    """Build context + invoke the agent. Returns None on parse failure."""
    d = json.loads(classification_path.read_text())
    cls = d.get("classification", d)
    cex = cls.get("counterexample", {}) or {}
    failing_property = cex.get("failing_property", "")
    witness = _format_witness(cex.get("variable_assignments", {}) or {})
    caller_path = cls.get("caller_path", []) or []
    dyn = cls.get("dynamic_result") or {}
    dyn_outcome = dyn.get("outcome") if dyn else None
    dyn_reasoning = dyn.get("reasoning") if dyn else None
    reproducer = cls.get("system_entry_input") or ""
    # Realism verdict isn't in classification.json directly — pipeline
    # stores it on the BugReport. Pass empty for now; the standalone
    # triage path runs without it. (Pipeline integration as Phase 3e
    # would have full access.)
    result = agent.run(
        function_name=function_name,
        function_source=function_source,
        cbmc_property=failing_property,
        harness_source=harness_text,
        witness_text=witness,
        caller_path=caller_path,
        dyn_outcome=dyn_outcome,
        dyn_reasoning=dyn_reasoning,
        reproducer_source=reproducer,
        realism_verdict=None,
        realism_reasoning=None,
        pipeline_reasoning=cls.get("reasoning", ""),
        sys_entry_reached=bool(cls.get("system_entry_reached")),
    )
    if not result.ok:
        return None
    return result.output


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep-dir", required=True, type=Path)
    ap.add_argument("--driver", required=True)
    ap.add_argument("--corpus-dir", required=True, type=Path)
    ap.add_argument("--only", default="", help="comma-separated function names")
    ap.add_argument("--include-real-bug", action="store_true",
                    help="also triage outcomes already classified real_bug")
    ap.add_argument("--no-tools", action="store_true",
                    help="use the one-shot TriageAgent (v1) instead of the "
                         "tool-using TriageToolsAgent (v2). v2 walks the "
                         "call chain to catch upstream size/write mismatches.")
    args = ap.parse_args()

    sweep_dir = args.sweep_dir
    driver_dir = sweep_dir / args.driver
    if not driver_dir.is_dir():
        print(f"ERROR: driver dir not found: {driver_dir}", file=sys.stderr)
        return 2

    only = {s.strip() for s in args.only.split(",") if s.strip()}

    cfg = Config.from_env()
    llm = LLMClient(cfg)
    if args.no_tools:
        agent = TriageAgent(cfg, llm)
        print("# Using TriageAgent (v1, one-shot, function-source-only)",
              flush=True)
    else:
        # Build the parsed corpus once so the TriageToolsAgent can
        # walk the call graph via lookup_function / find_more_callers.
        from bmc_agent.parser import parse_c_file
        from bmc_agent.preprocessor import preprocess
        corpus_paths = sorted(args.corpus_dir.glob("*.c"))
        if not corpus_paths:
            print(f"ERROR: no .c files under {args.corpus_dir}", file=sys.stderr)
            return 2
        # Parse the LARGEST .c (heuristic: the one that contains the
        # function being triaged). The triage script processes one
        # function at a time so re-parsing per-function is feasible
        # but wasteful; for v1 of the script, parse the first .c
        # whose basename matches the file_stem in the driver path
        # (driver_dir / file_stem / fn_name / classifications).
        # If parsing fails, fall back to v1.
        parsed = None
        for cp in corpus_paths:
            try:
                expanded = preprocess(
                    cp,
                    include_dirs=[
                        "/tmp/libarchive_bench/libarchive/build",
                        "/tmp/libarchive_bench/libarchive/libarchive",
                    ],
                    defines=["HAVE_CONFIG_H"],
                )
                parsed = parse_c_file(cp, source_text=expanded)
                if parsed and parsed.functions:
                    print(f"# Parsed corpus root: {cp} ({len(parsed.functions)} fns)",
                          flush=True)
                    break
            except Exception as exc:
                print(f"# Parse failed for {cp}: {exc}", file=sys.stderr)
        if parsed is None:
            print("# WARN: corpus parse failed; falling back to v1 TriageAgent",
                  file=sys.stderr, flush=True)
            agent = TriageAgent(cfg, llm)
        else:
            agent = TriageToolsAgent(
                cfg, llm,
                parsed_file=parsed,
                corpus_paths=corpus_paths,
                all_specs={},
            )
            print(f"# Using TriageToolsAgent (v2, tool-use, "
                  f"max_iterations={agent.max_iterations_param}, "
                  f"max_tool_calls={agent.max_tool_calls_param})",
                  flush=True)

    results: list[dict] = []
    summary_counts: Counter = Counter()
    fp_class_counts: Counter = Counter()
    error_count = 0

    # The verify-dir driver layout is two levels deep:
    #   <driver>/<source_file_stem>/<function_name>/classifications/
    # Walk both levels so the script works for both single-file
    # (verify) and multi-file (verify-dir) sweeps.
    candidate_fn_dirs: list[Path] = []
    for first in sorted(driver_dir.iterdir()):
        if not first.is_dir():
            continue
        if (first / "classifications").is_dir():
            candidate_fn_dirs.append(first)  # single-file layout
        else:
            for second in sorted(first.iterdir()):
                if second.is_dir() and (second / "classifications").is_dir():
                    candidate_fn_dirs.append(second)

    for fn_dir in candidate_fn_dirs:
        fn_name = fn_dir.name
        if only and fn_name not in only:
            continue
        cls_dir = fn_dir / "classifications"
        harness_path = fn_dir / "harness.c"
        harness_text = harness_path.read_text() if harness_path.exists() else ""
        function_source = _read_function_source(args.corpus_dir, fn_name)
        for cls_file in sorted(cls_dir.glob("*.json")):
            try:
                d = json.loads(cls_file.read_text())
            except Exception:
                continue
            cls = d.get("classification", d)
            outcome = (cls.get("outcome") or "").lower()
            if outcome == "unresolved" or (
                args.include_real_bug and outcome == "real_bug"
            ):
                pass
            else:
                continue
            prop = (cls.get("counterexample") or {}).get("failing_property", "?")
            print(f"  triaging {fn_name}::{prop} (outcome={outcome}) …",
                  flush=True)
            tr = triage_one(
                agent=agent,
                function_name=fn_name,
                function_source=function_source,
                classification_path=cls_file,
                harness_text=harness_text,
            )
            if tr is None:
                error_count += 1
                print(f"    ✗ TriageAgent parse error", flush=True)
                continue
            summary_counts[tr.verdict.value] += 1
            if tr.fp_class:
                fp_class_counts[tr.fp_class] += 1
            tag = {
                TriageVerdict.REAL_BUG: "🔴 REAL_BUG",
                TriageVerdict.LIKELY_FP: "🟢 LIKELY_FP",
                TriageVerdict.NEEDS_HUMAN: "🟡 NEEDS_HUMAN",
            }[tr.verdict]
            print(f"    {tag} ({tr.confidence}) — {tr.reasoning[:200]}",
                  flush=True)
            out_path = cls_file.with_name(cls_file.stem + ".triage.json")
            out_path.write_text(json.dumps({
                "verdict": tr.verdict.value,
                "confidence": tr.confidence,
                "fp_class": tr.fp_class,
                "reasoning": tr.reasoning,
            }, indent=2))
            results.append({
                "function": fn_name,
                "property": prop,
                "pipeline_outcome": outcome,
                "triage_verdict": tr.verdict.value,
                "confidence": tr.confidence,
                "fp_class": tr.fp_class,
                "reasoning": tr.reasoning,
            })

    summary_path = sweep_dir / "triage_summary.md"
    lines: list[str] = []
    lines.append(f"# Triage summary — {args.driver}")
    lines.append("")
    lines.append(f"**Sweep dir**: `{sweep_dir}`")
    lines.append(f"**Total triaged**: {len(results)}")
    lines.append(f"**Parse errors**: {error_count}")
    lines.append("")
    lines.append("## Verdict breakdown")
    lines.append("")
    for v, n in summary_counts.most_common():
        lines.append(f"- **{v}**: {n}")
    lines.append("")
    if fp_class_counts:
        lines.append("## FP class breakdown (likely_fp only)")
        lines.append("")
        for cls_name, n in fp_class_counts.most_common():
            lines.append(f"- `{cls_name}`: {n}")
        lines.append("")
    lines.append("## Per-CEx verdicts")
    lines.append("")
    lines.append("| Function | Property | Pipeline | Triage | Confidence | FP class |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        lines.append(
            f"| `{r['function']}` | `{r['property']}` | "
            f"{r['pipeline_outcome']} | {r['triage_verdict']} | "
            f"{r['confidence']} | {r['fp_class'] or '—'} |"
        )
    lines.append("")
    lines.append("## Per-CEx reasoning")
    lines.append("")
    for r in results:
        lines.append(f"### {r['function']}::{r['property']}")
        lines.append(f"- pipeline: `{r['pipeline_outcome']}`  →  triage: **{r['triage_verdict']}** ({r['confidence']})")
        if r['fp_class']:
            lines.append(f"- fp_class: `{r['fp_class']}`")
        lines.append("")
        lines.append(f"> {r['reasoning']}")
        lines.append("")
    summary_path.write_text("\n".join(lines))
    print(f"\nSummary written to {summary_path}")
    print(f"Verdict breakdown: {dict(summary_counts)}")
    if fp_class_counts:
        print(f"FP-class breakdown: {dict(fp_class_counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
