#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from bmc_agent.acsl import contract_to_acsl
from bmc_agent.assert_driven_specs import _split_conjuncts
from bmc_agent.frama_c import (
    function_assigns_clause,
    function_frame_precondition,
    insert_contract_block,
    insert_loop_invariants_acsl,
    parse_wp_output,
)
from bmc_agent.loop_invariants import (
    _enclosing_function_source,
    _filter_in_scope,
    _loop_assigns,
    _prep_goals_acsl,
    brace_braceless_loops,
    find_loops,
)


REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "artifacts/autospec_251"

MODES = {
    "strict_rte": [
        "frama-c", "-wp", "-wp-rte",
        "-wp-prover", "Alt-Ergo,Z3",
        "-wp-timeout", "10",
    ],
    "autospec_compatible": [
        "frama-c", "-wp",
        "-wp-precond-weakening",
        "-wp-no-callee-precond",
        "-wp-prover", "Alt-Ergo,Z3",
        "-wp-print",
        "-wp-timeout", "8",
    ],
    "no_rte": [
        "frama-c", "-wp",
        "-wp-prover", "Alt-Ergo,Z3",
        "-wp-timeout", "10",
    ],
}

PROVED_RE = re.compile(r"\[wp\]\s+Proved goals:\s*(\d+)\s*/\s*(\d+)")


def autospec_result_type(raw: str) -> str:
    result_type = "UK"
    timeout_in_requires = 0
    for line in raw.splitlines():
        if (
            "[kernel] Frama-C aborted:" in line
            or "[kernel] Plug-in wp aborted" in line
            or "[wp] Warning: No goal generated" in line
            or "error: invalid preprocessing directive" in line
        ):
            return "Invalid"
        if "[wp] [Timeout] typed_" in line and ("_requires (" in line or "_requires_" in line):
            timeout_in_requires += 1
            continue
        if "[wp] Proved goals:" in line:
            m = PROVED_RE.search(line)
            if not m:
                return "UK"
            left, right = int(m.group(1)), int(m.group(2))
            if left + timeout_in_requires == right:
                return f"Pass_{left}_{right}"
            return f"Fail_{left}_{right}"
    return result_type


def build_annotated_source(case_dir: Path) -> Path | None:
    source = case_dir / "source.c"
    loop_jsons = sorted(case_dir.glob("synthesized_loop_invariants_*.json"))
    if source.exists() and loop_jsons:
        data = json.loads(loop_jsons[0].read_text())
        text = _prep_goals_acsl(brace_braceless_loops(source.read_text()))
        acsl_text = data.get("acsl") or ""
        prelude = acsl_text.split("/* loop #", 1)[0].strip()
        loops = find_loops(text)
        by_ord = {lp.ordinal: lp for lp in loops}
        annotations = {}
        for raw_ord, invs in (data.get("loop_invariants") or {}).items():
            ordn = int(raw_ord)
            lp = by_ord.get(ordn)
            if not lp:
                continue
            fn_src = (_enclosing_function_source(text, lp.start_offset) or text) + "\n" + prelude
            annotations[ordn] = _filter_in_scope(list(invs or []), fn_src)

        assigns = {}
        variants = {}
        cur_ord = None
        for line in acsl_text.splitlines():
            m = re.match(r"/\* loop #(\d+) \*/", line.strip())
            if m:
                cur_ord = int(m.group(1))
                continue
            if cur_ord is None:
                continue
            m = re.search(r"\bloop assigns\s+(.+);", line)
            if m:
                assigns[cur_ord] = m.group(1).strip()
            m = re.search(r"\bloop variant\s+(.+);", line)
            if m:
                variants[cur_ord] = m.group(1).strip()

        for lp in loops:
            if lp.ordinal in annotations and lp.ordinal not in assigns:
                assigns[lp.ordinal] = _loop_assigns(lp)

        if annotations:
            text = insert_loop_invariants_acsl(text, annotations, assigns, variants)
        else:
            raw_loop_lines = [
                line.strip()
                for line in acsl_text.splitlines()
                if re.match(r"loop\s+(?:invariant|assigns|variant)\b", line.strip())
            ]
            if raw_loop_lines and len(loops) == 1:
                block = "/*@\n  " + "\n  ".join(raw_loop_lines) + "\n*/\n"
                lp = loops[0]
                text = text[: lp.start_offset] + block + text[lp.start_offset :]
                prelude = ""

        if prelude and not re.match(r"loop\s+(?:invariant|assigns|variant)\b", prelude):
            text = prelude + "\n\n" + text
        out = case_dir / "postcheck_annotated.c"
        out.write_text(text)
        os.chmod(out, 0o644)
        return out

    jsons = sorted(case_dir.glob("synthesized_specs_*.json"))
    if not source.exists() or not jsons:
        instrumented = case_dir / "source_instrumented.c"
        if instrumented.exists():
            return instrumented
        return None

    data = json.loads(jsons[0].read_text())
    text = source.read_text()
    specs = data.get("synthesized_specs") or {}
    requires_by_fn = {}
    assigns_by_fn = {}
    posts_by_fn = {}
    for fn, spec in specs.items():
        assigns = spec.get("assigns", "") or function_assigns_clause(text, fn)
        requires = spec.get("requires", "true")
        frame_pre = function_frame_precondition(text, fn, assigns)
        if frame_pre:
            cur = (requires or "true").strip()
            requires = frame_pre if cur in ("", "true") else f"({cur}) && ({frame_pre})"
        requires_by_fn[fn] = requires
        assigns_by_fn[fn] = assigns
        posts_by_fn[fn] = spec.get("ensures", "true")

    def render_contract_source(posts: dict) -> str:
        rendered = text
        for fn, post in posts.items():
            block = contract_to_acsl(
                requires_by_fn.get(fn, "true"),
                post,
                assigns_by_fn.get(fn, ""),
            )
            rendered = insert_contract_block(rendered, fn, block.rstrip() + "\n")
        return rendered

    if posts_by_fn:
        posts_by_fn = prune_postconditions(case_dir, render_contract_source, posts_by_fn)

    acsl_by_fn = {}
    for fn, post in posts_by_fn.items():
        acsl_by_fn[fn] = contract_to_acsl(
            requires_by_fn.get(fn, "true"),
            post,
            assigns_by_fn.get(fn, ""),
        )
    if not acsl_by_fn:
        acsl_by_fn = data.get("acsl") or {}
    for fn, block in acsl_by_fn.items():
        text = insert_contract_block(text, fn, block.rstrip() + "\n")
    out = case_dir / "postcheck_annotated.c"
    out.write_text(text)
    os.chmod(out, 0o644)
    return out


def prune_postconditions(case_dir: Path, render_fn, posts: dict) -> dict:
    """Greedily remove unproved top-level ensures conjuncts when WP gap improves."""
    def check(candidate: dict) -> tuple[int, int]:
        tmp = case_dir / "postcheck_prune_tmp.c"
        tmp.write_text(render_fn(candidate))
        os.chmod(tmp, 0o644)
        cmd = MODES["autospec_compatible"] + [str(tmp)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PATH": f"{BASE / 'bin'}:{os.environ.get('PATH', '')}"},
            )
            raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            raw = (exc.stdout or "") + "\n" + (exc.stderr or "")
        n_proved, n_total, _ = parse_wp_output(raw)
        return n_proved, n_total

    cur = dict(posts)
    proved, total = check(cur)
    if not total or proved == total:
        return cur
    best_gap = total - proved
    best_cur = dict(cur)
    changed = True
    trials = 0
    max_trials = 24

    def priority(item):
        _idx, clause = item
        clause = clause.strip()
        if re.fullmatch(r"\\old\s*\([^)]+\)\s*==\s*-?\d+\b", clause):
            return 0
        if re.fullmatch(r"[A-Za-z_]\w*\s*==\s*-?\d+\b", clause):
            return 1
        if "\\old" in clause:
            return 2
        return 3

    while changed and best_gap > 0:
        changed = False
        for fn in list(cur):
            clauses = _split_conjuncts(cur.get(fn, ""))
            if len(clauses) <= 1:
                continue
            ordered = sorted(enumerate(list(clauses)), key=priority)
            for idx, clause in ordered:
                if trials >= max_trials:
                    break
                trials += 1
                trial_clauses = clauses[:idx] + clauses[idx + 1:]
                trial = dict(cur)
                trial[fn] = " && ".join(trial_clauses) or "true"
                p, t = check(trial)
                if t and t - p < best_gap:
                    cur, best_gap = trial, t - p
                    best_cur = dict(trial)
                    print(f"prune {case_dir.name}: drop {fn}: {clause} -> {p}/{t}", flush=True)
                    changed = True
                    break
            if trials >= max_trials:
                break
            if changed:
                break
    return best_cur if best_gap == 0 else dict(posts)


def classify(raw: str, exit_code: int, timed_out: bool) -> dict:
    n_proved, n_total, unproved = parse_wp_output(raw)
    autospec_type = autospec_result_type(raw)
    syntax_or_annotation_error = any(
        marker in raw
        for marker in (
            "Frama-C aborted",
            "syntax error",
            "Invalid symbol",
            "unbound logic variable",
            "unbound variable",
            "annot-error",
        )
    )
    if timed_out:
        status = "timeout"
    elif syntax_or_annotation_error or autospec_type == "Invalid":
        status = "invalid"
    elif n_total > 0 and n_proved == n_total:
        status = "pass"
    elif n_total == 0:
        status = "zero_goals"
    else:
        status = "fail"
    return {
        "status": status,
        "n_proved": n_proved,
        "n_total": n_total,
        "unproved": unproved,
        "autospec_result_type": autospec_type,
        "autospec_pass": autospec_type.startswith("Pass_"),
        "exit_code": exit_code,
        "syntax_or_annotation_error": syntax_or_annotation_error,
    }


def run_one(row: dict, mode: str, timeout: int) -> dict:
    case_dir = Path(row["artifact_dir"])
    target = build_annotated_source(case_dir)
    out_dir = BASE / "postcheck_modes" / mode / row["case_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    if target is None:
        return {
            "case_id": row["case_id"],
            "relative_path": row["relative_path"],
            "category": row["category"],
            "mode": mode,
            "status": "missing_artifact",
            "target": "",
        }

    os.chmod(target, 0o644)
    cmd = MODES[mode] + [str(target)]
    start = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PATH": f"{BASE / 'bin'}:{os.environ.get('PATH', '')}"},
        )
        exit_code = proc.returncode
        raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        raw = (exc.stdout or "") + "\n" + (exc.stderr or "") + f"\nTIMEOUT after {timeout}s\n"
    runtime = round(time.time() - start, 2)
    log = out_dir / "framac.log"
    log.write_text(raw)
    parsed = classify(raw, exit_code, timed_out)
    return {
        "case_id": row["case_id"],
        "relative_path": row["relative_path"],
        "category": row["category"],
        "mode": mode,
        "target": str(target),
        "runtime_sec": runtime,
        "log": str(log),
        **parsed,
    }


def write_reports(rows: list[dict], label: str) -> None:
    out = BASE / "postcheck_modes"
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    by_category = {}
    for r in rows:
        counts.setdefault(r["mode"], {})
        counts[r["mode"]][r["status"]] = counts[r["mode"]].get(r["status"], 0) + 1
        by_category.setdefault(r["mode"], {})
        cat = by_category[r["mode"]].setdefault(r["category"], {})
        cat[r["status"]] = cat.get(r["status"], 0) + 1
        cat["total"] = cat.get("total", 0) + 1
    report = {
        "label": label,
        "modes": MODES,
        "counts": counts,
        "by_category": by_category,
        "rows": sorted(rows, key=lambda r: (r["mode"], r["relative_path"])),
    }
    (out / f"{label}.json").write_text(json.dumps(report, indent=2))

    lines = [
        f"# Frama-C Post-Check Modes: {label}",
        "",
        "| Mode | Pass | Fail | Invalid | Zero goals | Timeout | Missing |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        c = counts.get(mode, {})
        lines.append(
            f"| `{mode}` | {c.get('pass', 0)} | {c.get('fail', 0)} | "
            f"{c.get('invalid', 0)} | {c.get('zero_goals', 0)} | {c.get('timeout', 0)} | "
            f"{c.get('missing_artifact', 0)} |"
        )
    lines += ["", "## Cases", "", "| Case | Mode | Status | WP | AutoSpec type | Runtime |", "|---|---|---:|---:|---|---:|"]
    for r in sorted(rows, key=lambda r: (r["relative_path"], r["mode"])):
        wp = "" if r.get("n_total") is None else f"{r.get('n_proved', '')}/{r.get('n_total', '')}"
        lines.append(
            f"| `{r['relative_path']}` | `{r['mode']}` | {r['status']} | {wp} | "
            f"{r.get('autospec_result_type', '')} | {r.get('runtime_sec', '')}s |"
        )
    (out / f"{label}.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", action="append", default=[])
    ap.add_argument("--mode", action="append", choices=sorted(MODES), default=[])
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--label", default="all")
    args = ap.parse_args()

    original = json.loads((BASE / "report.json").read_text())["rows"]
    rows = [
        r for r in original
        if not args.category or r["category"] in set(args.category)
    ]
    modes = args.mode or list(MODES)
    # Dockerized Frama-C runs as the image user and must traverse these paths.
    for p in [BASE, BASE / "cases", BASE / "bin"]:
        os.chmod(p, 0o755)
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(run_one, r, mode, args.timeout) for mode in modes for r in rows]
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            results.append(res)
            print(
                f"[{len(results):03d}/{len(futs)}] {res['mode']} {res['relative_path']} -> "
                f"{res['status']} WP={res.get('n_proved')}/{res.get('n_total')} {res.get('autospec_result_type', '')}",
                flush=True,
            )
            write_reports(results, args.label)
    write_reports(results, args.label)
    print(BASE / "postcheck_modes" / f"{args.label}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
