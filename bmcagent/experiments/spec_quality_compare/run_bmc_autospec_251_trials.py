#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
DEFAULT_BASE_RUNNER = Path(__file__).with_name("_autospec_251_base.py")


def load_base_runner(path: Path):
    spec = importlib.util.spec_from_file_location("autospec251_base_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load base runner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def summarize_rows(rows: list[dict]) -> dict:
    status = Counter(r.get("status") for r in rows)
    by_benchmark: dict[str, Counter] = defaultdict(Counter)
    trial_total = 0
    trial_passes = 0
    for r in rows:
        by_benchmark[r.get("benchmark", "")][r.get("status")] += 1
        trial_total += int(r.get("trial_total") or 0)
        trial_passes += int(r.get("trial_passes") or 0)
    return {
        "total": len(rows),
        "status": dict(status),
        "trial_total": trial_total,
        "trial_passes": trial_passes,
        "mean_success_probability": (
            trial_passes / trial_total if trial_total else None
        ),
        "by_benchmark": {
            k: dict(v) for k, v in sorted(by_benchmark.items())
        },
    }


def write_report(out_dir: Path, metadata: dict, rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (r.get("benchmark", ""), r.get("relative_path", "")))
    summary = summarize_rows(rows)
    payload = {**metadata, "summary": summary, "rows": rows}
    (out_dir / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# BMC-Agent AutoSpec 251 Trials",
        "",
        f"- Progress: `{len(rows)}/251`",
        f"- Model: `{metadata.get('model')}`",
        f"- Prompt examples: `{metadata.get('prompt_examples')}`",
        f"- Trials per case: `{metadata.get('trials')}`",
        f"- Workers: `{metadata.get('workers')}`",
        f"- Case status: `{summary['status']}`",
        f"- Trial passes: `{summary['trial_passes']}/{summary['trial_total']}`",
        f"- Mean success probability: `{summary['mean_success_probability']}`",
        "",
        "## By Benchmark",
        "",
        "| Benchmark | Cases | Passed | Failed | Trial pass ratio |",
        "|---|---:|---:|---:|---:|",
    ]
    for bench, counts in summary["by_benchmark"].items():
        bench_rows = [r for r in rows if r.get("benchmark") == bench]
        b_trials = sum(int(r.get("trial_total") or 0) for r in bench_rows)
        b_passes = sum(int(r.get("trial_passes") or 0) for r in bench_rows)
        ratio = f"{b_passes}/{b_trials}" if b_trials else "-"
        lines.append(
            f"| `{bench}` | {len(bench_rows)} | {counts.get('pass', 0)} | "
            f"{len(bench_rows) - counts.get('pass', 0)} | {ratio} |"
        )
    lines += [
        "",
        "## Cases",
        "",
        "| Benchmark | Case | Status | Trial passes | Best WP | Runtime | Failure | Artifacts |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for r in rows:
        best = r.get("selected_trial") or {}
        pc = best.get("postcheck") or {}
        artifacts = []
        for key, label in (("log", "log"), ("postcheck_log", "wp"), ("json", "json"), ("annotated_or_instrumented", "c")):
            if r.get(key):
                artifacts.append(f"[{label}]({r[key]})")
        lines.append(
            f"| `{r.get('benchmark')}` | `{r.get('relative_path')}` | {r.get('status')} | "
            f"{r.get('trial_passes')}/{r.get('trial_total')} | "
            f"{pc.get('n_proved')}/{pc.get('n_total')} | {r.get('runtime_sec')}s | "
            f"{r.get('failure_mode', '')} | {' '.join(artifacts)} |"
        )
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_case(case: dict, out_dir: Path, trials: int, base) -> dict:
    case_dir = out_dir / "cases" / case["case_id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    attempts = []
    start = time.time()
    for trial in range(1, trials + 1):
        attempt = base.run_attempt(case, case_dir, trial)
        attempts.append(attempt)
        pc = attempt.get("postcheck") or {}
        print(
            f"{case['benchmark']}/{case['relative_path']} trial {trial}/{trials}: "
            f"agent={attempt.get('status')} pc={pc.get('status')} "
            f"wp={pc.get('n_proved')}/{pc.get('n_total')}",
            flush=True,
        )
    selected = max(attempts, key=base.attempt_score)
    base.promote_attempt(selected, case_dir)
    pc = selected.get("postcheck") or {}
    trial_passes = sum(1 for a in attempts if (a.get("postcheck") or {}).get("status") == "pass")
    status = "pass" if trial_passes else "fail"
    return {
        **case,
        "status": status,
        "trial_total": trials,
        "trial_passes": trial_passes,
        "selected_trial": selected,
        "runtime_sec": round(time.time() - start, 2),
        "postcheck_status": pc.get("status"),
        "postcheck_proved": pc.get("n_proved"),
        "postcheck_total": pc.get("n_total"),
        "failure_mode": "" if status == "pass" else (selected.get("status") or pc.get("status") or "fail"),
        "trials": attempts,
        "contains_secret_like_value": any(a.get("contains_secret_like_value") for a in attempts),
        **base.artifact_files(case_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--cases", nargs="+", help="Exact case_id values from manifest.json.")
    parser.add_argument("--benchmark-root", type=Path)
    parser.add_argument("--case-timeout", type=int, default=900)
    parser.add_argument("--postcheck-timeout", type=int, default=180)
    parser.add_argument("--base-runner", type=Path, default=DEFAULT_BASE_RUNNER)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.trials < 1 or args.workers < 1 or args.limit < 0:
        parser.error("trials/workers must be positive and limit must be nonnegative")
    if args.benchmark_root:
        os.environ["AUTOSPEC_BENCHMARK_ROOT"] = str(args.benchmark_root.resolve())

    args.out_dir.mkdir(parents=True, exist_ok=True)
    base = load_base_runner(args.base_runner)
    base.BASE = args.out_dir
    base.CASE_TIMEOUT = args.case_timeout
    base.POSTCHECK_TIMEOUT = args.postcheck_timeout
    base.MODEL = os.environ.get("BMC_AGENT_LLM_DEFAULT_MODEL", base.MODEL)

    cases = base.discover_cases()
    if args.cases:
        wanted = set(args.cases)
        missing = wanted - {c["case_id"] for c in cases}
        if missing:
            parser.error(f"unknown case(s): {', '.join(sorted(missing))}")
        cases = [c for c in cases if c["case_id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    (args.out_dir / "manifest.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")

    done: dict[str, dict] = {}
    report_path = args.out_dir / "report.json"
    if args.resume and report_path.exists():
        old = json.loads(report_path.read_text())
        done = {r["case_id"]: r for r in old.get("rows", []) if r.get("trial_total") == args.trials}

    metadata = {
        "label": args.out_dir.name,
        "model": base.MODEL,
        "provider": os.environ.get("BMC_AGENT_LLM_PROVIDER", "openai"),
        "base_url": os.environ.get("BMC_AGENT_LLM_BASE_URL") or os.environ.get("OPENAI_API_BASE") or "",
        "prompt_examples": os.environ.get("BMC_AGENT_C_SPEC_EXAMPLES") or os.environ.get("BMC_AGENT_C_PROMPT_EXAMPLES") or "",
        "trials": args.trials,
        "workers": args.workers,
        "case_timeout_sec": args.case_timeout,
        "postcheck_timeout_sec": args.postcheck_timeout,
        "base_runner": str(args.base_runner),
    }
    rows = list(done.values())
    write_report(args.out_dir, metadata, rows)

    pending = [c for c in cases if c["case_id"] not in done]
    print(f"Discovered {len(cases)} cases; pending={len(pending)}; workers={args.workers}; trials={args.trials}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_case, c, args.out_dir, args.trials, base): c for c in pending}
        for fut in concurrent.futures.as_completed(futs):
            case = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:
                row = {**case, "status": "error", "trial_total": args.trials, "trial_passes": 0, "failure_mode": repr(exc), "trials": []}
            rows.append(row)
            write_report(args.out_dir, metadata, rows)
            print(f"[{len(rows)}/{len(cases)}] {case['benchmark']}/{case['relative_path']} -> {row.get('status')} {row.get('trial_passes')}/{row.get('trial_total')}", flush=True)
    write_report(args.out_dir, metadata, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
