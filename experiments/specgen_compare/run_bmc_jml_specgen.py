#!/usr/bin/env python3
"""Run BMC-Agent Java/JML generation on SpecGen benchmark cases.

This is an experiment adapter, not a production pipeline entry point.  It keeps
SpecGen-specific paths, selection, and reporting outside ``bmc_agent verify``
while reusing the same JML/OpenJML implementation.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bmc_agent.config import Config  # noqa: E402
from bmc_agent.jml_specs import (  # noqa: E402
    default_openjml_path,
    run_jml_specs_bench,
    run_openjml,
)
from bmc_agent.llm import LLMClient  # noqa: E402


@dataclass(frozen=True)
class SpecGenCase:
    name: str
    source: str
    oracle: str


@dataclass
class CaseRow:
    case: str
    source: str
    oracle: str
    status: str
    passed: bool
    iterations: int
    runtime_s: float
    final_annotated_path: str
    report_path: str
    openjml_output_path: str
    oracle_status: str = "not_checked"
    error: str = ""
    jml_clause_counts: dict[str, int] | None = None
    attempts: int = 1
    trials: int = 1
    trial_passes: int | None = None
    trial_status_counts: dict[str, int] | None = None
    trial_rows: list[dict[str, Any]] | None = None
    model: str = ""
    provider: str = ""
    base_url: str = ""
    prompt_examples: str = "none"


def discover_cases(bench_root: Path, oracle_root: Path | None = None) -> list[SpecGenCase]:
    """Discover SpecGenBench cases in ``common`` layout.

    A normal case is ``common/<Case>/<Case>.java``.  Auxiliary driver files are
    intentionally ignored.
    """

    cases: list[SpecGenCase] = []
    for case_dir in sorted(p for p in bench_root.iterdir() if p.is_dir()):
        source = case_dir / f"{case_dir.name}.java"
        if not source.exists():
            java_files = sorted(p for p in case_dir.glob("*.java") if not p.name.endswith("Driver.java"))
            if not java_files:
                continue
            source = java_files[0]
        oracle = ""
        if oracle_root is not None:
            candidate = oracle_root / case_dir.name / source.name
            if candidate.exists():
                oracle = str(candidate)
        cases.append(SpecGenCase(case_dir.name, str(source), oracle))
    return cases


def select_cases(cases: list[SpecGenCase], names: list[str] | None, limit: int | None) -> list[SpecGenCase]:
    if names:
        wanted = set(names)
        selected = [c for c in cases if c.name in wanted]
        missing = sorted(wanted - {c.name for c in selected})
        if missing:
            raise SystemExit(f"unknown SpecGen case(s): {', '.join(missing)}")
    else:
        selected = list(cases)
    if limit is not None:
        selected = selected[:limit]
    return selected


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_manifest(path: Path, cases: list[SpecGenCase]) -> None:
    write_json(path, [asdict(c) for c in cases])


def _read_example(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"prompt example file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _format_example_pair(index: int, source: str, annotated: str) -> str:
    return (
        f"Example {index} input:\n"
        "```java\n"
        f"{source}\n"
        "```\n\n"
        f"Example {index} output:\n"
        "```java\n"
        f"{annotated}\n"
        "```"
    )


def load_prompt_examples(name: str, bench_root: Path) -> str:
    """Load optional few-shot prompt examples for experiment-only runs."""

    if not name or name == "none":
        return ""
    if name != "specgen-4shot":
        path = Path(name)
        if not path.exists():
            raise SystemExit(f"unknown prompt example set or file: {name}")
        return path.read_text(encoding="utf-8")

    # ``bench_root`` may point to either
    # SpecGen-Artifact/benchmark/SpecGenBench/common or
    # SpecGen-Artifact/benchmark/SVCOMP.  Infer the artifact root instead of
    # hard-coding a local workstation path.
    artifact_root = None
    for candidate in [bench_root.resolve(), *bench_root.resolve().parents]:
        if (candidate / "prompts").is_dir():
            artifact_root = candidate
            break
    if artifact_root is None:
        raise SystemExit(f"could not infer SpecGen artifact root from bench root: {bench_root}")
    pairs = [
        (
            artifact_root / "prompts" / "1" / "1",
            artifact_root / "prompts" / "1" / "1_reply",
        ),
        (
            artifact_root / "prompts" / "2" / "1",
            artifact_root / "prompts" / "2" / "2_reply",
        ),
        (
            artifact_root / "prompts" / "oracle_clean" / "AddLoop" / "AddLoop.java",
            artifact_root / "prompts" / "oracle" / "AddLoop" / "AddLoop.java",
        ),
        (
            artifact_root / "prompts" / "oracle_clean" / "LinearSearch" / "LinearSearch.java",
            artifact_root / "prompts" / "oracle" / "LinearSearch" / "LinearSearch.java",
        ),
    ]
    return "\n\n".join(
        _format_example_pair(i, _read_example(src), _read_example(annotated))
        for i, (src, annotated) in enumerate(pairs, start=1)
    )


def load_completed(report_path: Path) -> dict[str, CaseRow]:
    if not report_path.exists():
        return {}
    data = json.loads(report_path.read_text(encoding="utf-8"))
    rows = data.get("rows", data if isinstance(data, list) else [])
    completed: dict[str, CaseRow] = {}
    for row in rows:
        try:
            completed[row["case"]] = CaseRow(**row)
        except Exception:
            continue
    return completed


def summarize(rows: list[CaseRow]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    trial_total = sum(int(r.trials or 1) for r in rows)
    trial_passes = sum(int(r.trial_passes if r.trial_passes is not None else int(r.passed)) for r in rows)
    return {
        "total": len(rows),
        "passed": sum(1 for r in rows if r.passed),
        "trial_total": trial_total,
        "trial_passes": trial_passes,
        "mean_success_probability": (trial_passes / trial_total) if trial_total else 0.0,
        "by_status": dict(sorted(by_status.items())),
    }


def write_report(output: Path, rows: list[CaseRow]) -> None:
    rows_sorted = sorted(rows, key=lambda r: r.case)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": summarize(rows_sorted),
        "rows": [asdict(r) for r in rows_sorted],
    }
    write_json(output / "report.json", payload)
    write_summary_md(output / "summary.md", payload)


def write_summary_md(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# BMC-Agent Java/JML on SpecGen Benchmark",
        "",
        f"- Total cases: {payload['summary']['total']}",
        f"- Passed: {payload['summary']['passed']}",
        f"- Trial passes: {payload['summary'].get('trial_passes', payload['summary']['passed'])}/{payload['summary'].get('trial_total', payload['summary']['total'])}",
        f"- Mean success probability: {float(payload['summary'].get('mean_success_probability', 0.0)):.4f}",
        f"- Status counts: `{payload['summary']['by_status']}`",
        "",
        "| Case | Status | Pass | Trial passes | Attempts | Iters | Runtime(s) | OpenJML output |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in payload["rows"]:
        out = row.get("openjml_output_path") or ""
        trial_passes = row.get("trial_passes")
        trials = row.get("trials", 1)
        trial_cell = "" if trial_passes is None else f"{trial_passes}/{trials}"
        lines.append(
            "| {case} | {status} | {passed} | {trial_cell} | {attempts} | {iterations} | {runtime:.2f} | `{out}` |".format(
                case=row["case"],
                status=row["status"],
                passed="yes" if row["passed"] else "no",
                trial_cell=trial_cell,
                attempts=row.get("attempts", 1),
                iterations=row["iterations"],
                runtime=float(row["runtime_s"] or 0.0),
                out=out,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def configure(args: argparse.Namespace) -> Config:
    config = Config.from_env()
    if args.model:
        config.llm_model = args.model
    if args.provider:
        config.llm_provider = args.provider
    if args.base_url:
        config.llm_base_url = args.base_url
    if args.openjml_path:
        config.openjml_path = args.openjml_path
    else:
        config.openjml_path = default_openjml_path()
    config.openjml_timeout = args.openjml_timeout
    config.jml_max_iterations = args.max_iterations
    # Let the caller supply keys through the usual environment.  Do not read or
    # print secret files here.
    return config


def run_metadata(args: argparse.Namespace, config: Config) -> dict[str, str]:
    provider = getattr(config, "resolved_provider", lambda: getattr(config, "llm_provider", ""))()
    return {
        "model": str(getattr(config, "llm_model", "")),
        "provider": str(provider),
        "base_url": str(getattr(config, "llm_base_url", "")),
        "prompt_examples": str(getattr(args, "prompt_examples", "none") or "none"),
    }


def run_one(case: SpecGenCase, args: argparse.Namespace) -> CaseRow:
    config = configure(args)
    metadata = run_metadata(args, config)
    prompt_examples = load_prompt_examples(getattr(args, "prompt_examples", ""), Path(args.bench_root))
    case_driver = case.name
    case_output = Path(args.output) / "cases"
    oracle_status = "not_checked"
    if args.validate_oracle and case.oracle:
        oracle_result = run_openjml(
            case.oracle,
            openjml_path=config.openjml_path,
            timeout_s=args.openjml_timeout,
        )
        oracle_status = oracle_result.status

    attempts = max(1, int(getattr(args, "attempts", 1)))
    total_iterations = 0
    total_runtime = 0.0
    best_row: CaseRow | None = None

    for attempt in range(1, attempts + 1):
        driver = case_driver if attempts == 1 else f"{case_driver}/attempt_{attempt}"
        try:
            result = run_jml_specs_bench(
                case.source,
                driver=driver,
                config=config,
                llm=LLMClient(config),
                output_dir=case_output,
                openjml_path=config.openjml_path,
                openjml_timeout=args.openjml_timeout,
                max_iterations=args.max_iterations,
                prompt_examples=prompt_examples,
            )
            total_iterations += len(result.iterations)
            total_runtime += result.runtime_s
            last = result.iterations[-1] if result.iterations else None
            row = CaseRow(
                case=case.name,
                source=case.source,
                oracle=case.oracle,
                status=result.status,
                passed=result.passed,
                iterations=total_iterations,
                runtime_s=total_runtime,
                final_annotated_path=result.final_annotated_path,
                report_path=result.report_path,
                openjml_output_path=last.openjml_output_path if last else "",
                oracle_status=oracle_status,
                error=result.error,
                jml_clause_counts=result.jml_clause_counts,
                attempts=attempt,
                **metadata,
            )
            if row.passed:
                return row
            if best_row is None or best_row.status in {"runner_error", "source_changed", "annotation_error"}:
                best_row = row
        except Exception as exc:  # Keep batch reports partial and inspectable.
            row = CaseRow(
                case=case.name,
                source=case.source,
                oracle=case.oracle,
                status="runner_error",
                passed=False,
                iterations=total_iterations,
                runtime_s=total_runtime,
                final_annotated_path="",
                report_path="",
                openjml_output_path="",
                oracle_status=oracle_status,
                error=str(exc),
                jml_clause_counts={},
                attempts=attempt,
                **metadata,
            )
            if best_row is None:
                best_row = row

    if best_row is not None:
        best_row.iterations = total_iterations
        best_row.runtime_s = total_runtime
        best_row.attempts = attempts
        return best_row

    return CaseRow(
        case=case.name,
        source=case.source,
        oracle=case.oracle,
        status="runner_error",
        passed=False,
        iterations=0,
        runtime_s=0.0,
        final_annotated_path="",
        report_path="",
        openjml_output_path="",
        oracle_status=oracle_status,
        error="no attempts completed",
        jml_clause_counts={},
        attempts=attempts,
        **metadata,
    )


def run_one_trial(case: SpecGenCase, args: argparse.Namespace, trial: int) -> CaseRow:
    config = configure(args)
    metadata = run_metadata(args, config)
    prompt_examples = load_prompt_examples(getattr(args, "prompt_examples", ""), Path(args.bench_root))
    driver = f"{case.name}/trial_{trial}"
    case_output = Path(args.output) / "cases"
    try:
        result = run_jml_specs_bench(
            case.source,
            driver=driver,
            config=config,
            llm=LLMClient(config),
            output_dir=case_output,
            openjml_path=config.openjml_path,
            openjml_timeout=args.openjml_timeout,
            max_iterations=args.max_iterations,
            prompt_examples=prompt_examples,
        )
        last = result.iterations[-1] if result.iterations else None
        return CaseRow(
            case=case.name,
            source=case.source,
            oracle=case.oracle,
            status=result.status,
            passed=result.passed,
            iterations=len(result.iterations),
            runtime_s=result.runtime_s,
            final_annotated_path=result.final_annotated_path,
            report_path=result.report_path,
            openjml_output_path=last.openjml_output_path if last else "",
            error=result.error,
            jml_clause_counts=result.jml_clause_counts,
            attempts=1,
            trials=1,
            **metadata,
        )
    except Exception as exc:
        return CaseRow(
            case=case.name,
            source=case.source,
            oracle=case.oracle,
            status="runner_error",
            passed=False,
            iterations=0,
            runtime_s=0.0,
            final_annotated_path="",
            report_path="",
            openjml_output_path="",
            error=str(exc),
            jml_clause_counts={},
            attempts=1,
            trials=1,
            **metadata,
        )


def aggregate_trial_rows(case: SpecGenCase, trials: list[CaseRow]) -> CaseRow:
    status_counts: dict[str, int] = {}
    for row in trials:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
    passed_trials = [r for r in trials if r.passed]
    # Prefer a passing artifact as the representative row; otherwise keep the
    # first completed trial for a concrete failure artifact.
    representative = passed_trials[0] if passed_trials else (trials[0] if trials else None)
    if representative is None:
        return CaseRow(
            case=case.name,
            source=case.source,
            oracle=case.oracle,
            status="runner_error",
            passed=False,
            iterations=0,
            runtime_s=0.0,
            final_annotated_path="",
            report_path="",
            openjml_output_path="",
            error="no trials completed",
            jml_clause_counts={},
            attempts=1,
            trials=0,
            trial_passes=0,
            trial_status_counts={},
            trial_rows=[],
        )
    return CaseRow(
        case=case.name,
        source=case.source,
        oracle=case.oracle,
        status="passed" if passed_trials else representative.status,
        passed=bool(passed_trials),
        iterations=sum(r.iterations for r in trials),
        runtime_s=sum(r.runtime_s for r in trials),
        final_annotated_path=representative.final_annotated_path,
        report_path=representative.report_path,
        openjml_output_path=representative.openjml_output_path,
        oracle_status=representative.oracle_status,
        error="" if passed_trials else representative.error,
        jml_clause_counts=representative.jml_clause_counts,
        attempts=1,
        trials=len(trials),
        trial_passes=len(passed_trials),
        trial_status_counts=dict(sorted(status_counts.items())),
        trial_rows=[asdict(r) for r in sorted(trials, key=lambda x: x.report_path)],
        model=representative.model,
        provider=representative.provider,
        base_url=representative.base_url,
        prompt_examples=representative.prompt_examples,
    )


def cmd_discover(args: argparse.Namespace) -> int:
    bench_root = Path(args.bench_root)
    oracle_root = Path(args.oracle_root) if args.oracle_root else None
    cases = select_cases(discover_cases(bench_root, oracle_root), args.cases, args.limit)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    write_manifest(out / "manifest.json", cases)
    print(f"discovered {len(cases)} case(s)")
    print(f"manifest: {out / 'manifest.json'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    bench_root = Path(args.bench_root)
    oracle_root = Path(args.oracle_root) if args.oracle_root else None
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    cases = select_cases(discover_cases(bench_root, oracle_root), args.cases, args.limit)
    write_manifest(output / "manifest.json", cases)
    trial_count = max(1, int(getattr(args, "trials", 1)))
    completed = load_completed(output / "report.json") if args.resume else {}
    rows: dict[str, CaseRow] = dict(completed)

    if trial_count > 1:
        to_run = [
            c
            for c in cases
            if c.name not in rows or int(rows[c.name].trials or 1) < trial_count
        ]
    else:
        to_run = [c for c in cases if c.name not in rows]
    print(f"selected {len(cases)} case(s); running {len(to_run)}; output={output}")
    if not to_run:
        write_report(output, list(rows.values()))
        return 0

    max_workers = max(1, int(args.workers))
    if trial_count > 1:
        trial_rows: dict[str, list[CaseRow]] = {case.name: [] for case in to_run}
        jobs = [(case, trial) for case in to_run for trial in range(1, trial_count + 1)]
        print(f"trial mode: {trial_count} trial(s) per case; running {len(jobs)} trial job(s)")
        if max_workers == 1:
            for case, trial in jobs:
                row = run_one_trial(case, args, trial)
                trial_rows[case.name].append(row)
                print(f"{case.name} trial {trial}/{trial_count}: {row.status} pass={row.passed} iters={row.iterations}")
                if len(trial_rows[case.name]) == trial_count:
                    rows[case.name] = aggregate_trial_rows(case, trial_rows[case.name])
                    write_report(output, list(rows.values()))
        else:
            with futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_map = {
                    pool.submit(run_one_trial, case, args, trial): (case, trial)
                    for case, trial in jobs
                }
                for fut in futures.as_completed(future_map):
                    case, trial = future_map[fut]
                    row = fut.result()
                    trial_rows[case.name].append(row)
                    print(f"{case.name} trial {trial}/{trial_count}: {row.status} pass={row.passed} iters={row.iterations}")
                    if len(trial_rows[case.name]) == trial_count:
                        rows[case.name] = aggregate_trial_rows(case, trial_rows[case.name])
                        write_report(output, list(rows.values()))
        summary = summarize(list(rows.values()))
        print(f"summary: {summary}")
        print(f"report: {output / 'report.json'}")
        print(f"summary_md: {output / 'summary.md'}")
        return 0 if summary["passed"] > 0 else 1

    if max_workers == 1:
        for case in to_run:
            row = run_one(case, args)
            rows[row.case] = row
            write_report(output, list(rows.values()))
            print(f"{row.case}: {row.status} pass={row.passed} iters={row.iterations}")
    else:
        with futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {pool.submit(run_one, case, args): case for case in to_run}
            for fut in futures.as_completed(future_map):
                row = fut.result()
                rows[row.case] = row
                write_report(output, list(rows.values()))
                print(f"{row.case}: {row.status} pass={row.passed} iters={row.iterations}")

    summary = summarize(list(rows.values()))
    print(f"summary: {summary}")
    print(f"report: {output / 'report.json'}")
    print(f"summary_md: {output / 'summary.md'}")
    return 0 if summary["passed"] > 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--bench-root",
            default=os.environ.get("SPECGEN_BENCH_ROOT", "benchmark/SpecGenBench/common"),
            help="Path to SpecGenBench/common (or SPECGEN_BENCH_ROOT).",
        )
        p.add_argument(
            "--oracle-root",
            default=os.environ.get("SPECGEN_ORACLE_ROOT", "benchmark/SpecGenBench/oracle"),
            help="Path to SpecGenBench/oracle (or SPECGEN_ORACLE_ROOT); used only for metadata/oracle validation.",
        )
        p.add_argument("--cases", nargs="*", default=None, help="Case names to run.")
        p.add_argument("--limit", type=int, default=None, help="Limit after sorting/discovery.")
        p.add_argument("--output", default="artifacts/specgen_jml_pilot", help="Output directory.")

    d = sub.add_parser("discover", help="Build a manifest without running LLM/OpenJML.")
    add_common(d)
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("run", help="Run BMC-Agent JML generation on selected cases.")
    add_common(r)
    r.add_argument("--openjml-path", default="", help="Path to OpenJML binary.")
    r.add_argument("--openjml-timeout", type=int, default=200, help="OpenJML timeout per case.")
    r.add_argument("--max-iterations", type=int, default=3, help="LLM generate/refine iterations per case.")
    r.add_argument("--workers", type=int, default=1, help="Parallel case workers.")
    r.add_argument("--attempts", type=int, default=1, help="Independent attempts per case; stops after first pass.")
    r.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Independent trials per case; unlike --attempts, this never stops after first pass.",
    )
    r.add_argument("--resume", action="store_true", help="Reuse completed report rows.")
    r.add_argument("--validate-oracle", action="store_true", help="Run OpenJML on oracle files too.")
    r.add_argument(
        "--prompt-examples",
        default="none",
        help="Optional few-shot examples: 'none', 'specgen-4shot', or a text file to prepend.",
    )
    r.add_argument("--model", default="", help="Override BMC_AGENT_LLM_MODEL.")
    r.add_argument("--provider", default="", help="Override BMC_AGENT_LLM_PROVIDER.")
    r.add_argument("--base-url", default="", help="Override BMC_AGENT_LLM_BASE_URL.")
    r.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
