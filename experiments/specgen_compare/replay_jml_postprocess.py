#!/usr/bin/env python3
"""Replay existing Java/JML artifacts through the current post-processor.

This adapter is intentionally LLM-free.  It reads a prior ``run_bmc_jml_specgen``
report, reuses each generated annotated Java file, applies the current JML
normalization/pruning logic, and reruns OpenJML.  Use it to measure whether a
post-processing change improves already-generated specs before spending another
full batch of LLM calls.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import sys
import threading
import time
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bmc_agent.jml_specs import (  # noqa: E402
    complete_standard_imports,
    count_jml_clauses,
    default_openjml_path,
    drop_generated_jml_assertions,
    java_verification_filename,
    kill_active_openjml_process_groups,
    normalize_jml_annotation_placement,
    repair_java_source_for_openjml,
    run_openjml,
    source_code_preserved_with_standard_imports,
    transplant_jml_annotations,
    write_openjml_support_files,
)
from experiments.specgen_compare.run_bmc_jml_specgen import sanitize_report_payload  # noqa: E402
from bmc_agent import jml_specs  # noqa: E402
from experiments.specgen_compare.run_bmc_jml_specgen import (  # noqa: E402
    CaseRow,
    attach_failure_classes,
    attach_oracle_source_metadata,
    failure_actionability,
    source_preflight_status,
    write_summary_md,
)


_STOP_REPLAY = threading.Event()


def load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows", data if isinstance(data, list) else [])
    if not isinstance(rows, list):
        raise SystemExit(f"no rows in report: {path}")
    return [row for row in rows if isinstance(row, dict)]


def select_rows(rows: list[dict[str, Any]], cases: list[str] | None) -> list[dict[str, Any]]:
    if not cases:
        return rows
    wanted = set(cases)
    return [row for row in rows if str(row.get("case") or "") in wanted]


def select_trials(trials: list[dict[str, Any]], max_trials: int | None) -> list[dict[str, Any]]:
    if max_trials is None or max_trials <= 0:
        return trials
    return trials[:max_trials]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_failure_class: dict[str, int] = {}
    by_failure_reason: dict[str, int] = {}
    by_actionability: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        failure_class = str(row.get("failure_class") or "not_classified")
        by_failure_class[failure_class] = by_failure_class.get(failure_class, 0) + 1
        actionability = failure_actionability(
            SimpleNamespace(passed=bool(row.get("passed")), failure_class=failure_class)
        )
        by_actionability[actionability] = by_actionability.get(actionability, 0) + 1
        if not bool(row.get("passed")):
            failure_reason = str(row.get("failure_reason") or "")
            if failure_reason:
                by_failure_reason[failure_reason] = by_failure_reason.get(failure_reason, 0) + 1
    trial_total = sum(int(row.get("trials") or 0) for row in rows if int(row.get("attempts", 1) or 0) > 0)
    trial_passes = sum(int(row.get("trial_passes") or 0) for row in rows)
    passed_with_zero_trial_passes = [
        str(row.get("case") or "")
        for row in rows
        if bool(row.get("passed"))
        and row.get("trial_passes") is not None
        and int(row.get("trial_passes") or 0) == 0
        and int(row.get("trials") or 0) > 0
    ]
    passed_without_trial_stats = [
        str(row.get("case") or "")
        for row in rows
        if bool(row.get("passed"))
        and row.get("trial_passes") is not None
        and int(row.get("trial_passes") or 0) == 0
        and int(row.get("trials") or 0) == 0
    ]
    return {
        "total": len(rows),
        "passed": sum(1 for row in rows if bool(row.get("passed"))),
        "generated_spec_issue_count": by_actionability.get("generated_spec_issue", 0),
        "source_or_tool_boundary_count": by_actionability.get("source_or_tool_boundary", 0),
        "llm_or_runner_issue_count": by_actionability.get("llm_or_runner_issue", 0),
        "passed_with_zero_trial_passes_count": len(passed_with_zero_trial_passes),
        "passed_with_zero_trial_passes_cases": passed_with_zero_trial_passes,
        "passed_without_trial_stats_count": len(passed_without_trial_stats),
        "passed_without_trial_stats_cases": passed_without_trial_stats,
        "trial_total": trial_total,
        "trial_passes": trial_passes,
        "mean_success_probability": (trial_passes / trial_total) if trial_total else 0.0,
        "by_status": dict(sorted(by_status.items())),
        "by_failure_class": dict(sorted(by_failure_class.items())),
        "by_failure_reason": dict(sorted(by_failure_reason.items())),
        "by_actionability": dict(sorted(by_actionability.items())),
    }


def openjml_text(result: Any) -> str:
    text = ((getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or ""))
    error = getattr(result, "error", "") or ""
    if error:
        text = (text + "\n" + error).strip()
    return text


def replay_generated_source(
    *,
    source_path: Path,
    generated_path: Path,
    output_java: Path,
    output_log_prefix: Path,
    openjml_path: str,
    timeout_s: int,
    max_prune_rounds: int,
) -> tuple[str, bool, float, int, str, Path]:
    original = repair_java_source_for_openjml(source_path.read_text(encoding="utf-8"))
    generated = repair_java_source_for_openjml(generated_path.read_text(encoding="utf-8"))
    preserved, source_error = source_code_preserved_with_standard_imports(original, generated)
    current = complete_standard_imports(
        normalize_jml_annotation_placement(drop_generated_jml_assertions(original, generated))
    )
    current = jml_specs.abstract_java_verifier_only_effects_for_openjml(current)
    if not preserved:
        transplanted = transplant_jml_annotations(original, current)
        if transplanted and count_jml_clauses(transplanted)["total"] > 0:
            transplanted = complete_standard_imports(
                normalize_jml_annotation_placement(drop_generated_jml_assertions(original, transplanted))
            )
            transplanted = jml_specs.abstract_java_verifier_only_effects_for_openjml(transplanted)
            transplanted_preserved, transplanted_error = source_code_preserved_with_standard_imports(
                original, transplanted
            )
            if transplanted_preserved:
                current = transplanted
                preserved = True
                source_error = ""
            else:
                source_error = f"{source_error}\ntransplanted JML was not source-preserving: {transplanted_error}"

    output_java = output_java.parent / java_verification_filename(current, output_java.name)
    if not preserved:
        output_java.parent.mkdir(parents=True, exist_ok=True)
        output_java.write_text(current, encoding="utf-8")
        (output_log_prefix.parent / f"{output_log_prefix.name}_round_0.out").write_text(
            source_error + ("\n" if source_error and not source_error.endswith("\n") else ""),
            encoding="utf-8",
        )
        return "source_changed", False, 0.0, 0, source_error[:1000], output_java

    runtime_s = 0.0
    prune_rounds = 0
    status = "runner_error"
    passed = False
    last_output = ""

    for round_idx in range(max_prune_rounds + 1):
        output_java.parent.mkdir(parents=True, exist_ok=True)
        output_java.write_text(current, encoding="utf-8")
        write_openjml_support_files(current, output_java.parent)
        result = run_openjml(output_java, openjml_path=openjml_path, timeout_s=timeout_s, cwd=output_java.parent)
        runtime_s += float(getattr(result, "runtime_s", 0.0) or 0.0)
        status = str(getattr(result, "status", "runner_error") or "runner_error")
        passed = bool(getattr(result, "passed", False))
        last_output = openjml_text(result)
        (output_log_prefix.parent / f"{output_log_prefix.name}_round_{round_idx}.out").write_text(
            last_output + ("\n" if last_output and not last_output.endswith("\n") else ""),
            encoding="utf-8",
        )
        recoverable_internal_error = status == "tool_error" and jml_specs._is_openjml_internal_error(last_output)
        if passed or (
            status not in {"verification_failed", "annotation_error", "source_invalid"}
            and not recoverable_internal_error
        ):
            if not (status == "timeout" and jml_specs._has_reported_nullable_failure(last_output)):
                break
        if round_idx >= max_prune_rounds:
            break

        if status == "timeout" and jml_specs._has_reported_nullable_failure(last_output):
            pruned, changed = jml_specs._annotate_reported_nullable(current, last_output)
        elif status == "verification_failed":
            pruned, changed = jml_specs._annotate_reported_nullable(current, last_output)
            if not changed:
                pruned, changed = jml_specs._prune_reported_precondition(current, last_output)
            if not changed:
                pruned, changed = jml_specs._prune_reported_assignable(current, last_output)
            if not changed:
                pruned, changed = jml_specs._prune_reported_postcondition(current, last_output)
            if not changed:
                pruned, changed = jml_specs._prune_reported_diverges(current, last_output)
            if not changed:
                pruned, changed = jml_specs._prune_reported_loop_decreases(current, last_output)
            if not changed:
                pruned, changed = jml_specs._prune_reported_loop_invariant(current, last_output)
            if not changed:
                pruned, changed = jml_specs._prune_reported_object_invariant(current, last_output)
        elif status == "annotation_error":
            pruned, changed = jml_specs._prune_reported_annotation_error(current, last_output)
        elif status == "source_invalid" and jml_specs._has_reported_jml_annotation_error(current, last_output):
            pruned, changed = jml_specs._prune_reported_annotation_error(current, last_output)
        elif status == "tool_error" and jml_specs._is_openjml_internal_error(last_output):
            pruned, changed = jml_specs._prune_enclosing_loop_specs_for_internal_error(current, last_output)
        else:
            break
        if not changed:
            break
        current = complete_standard_imports(normalize_jml_annotation_placement(pruned))
        current = jml_specs.abstract_java_verifier_only_effects_for_openjml(current)
        prune_rounds += 1

    return status, passed, runtime_s, prune_rounds, last_output[:1000], output_java


def preflight_case(row: dict[str, Any], output_case_dir: Path, args: argparse.Namespace) -> dict[str, Any] | None:
    source = row.get("source")
    if not source:
        return None
    source_path = Path(str(source))
    preflight_dir = output_case_dir / "source_preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    source_text = repair_java_source_for_openjml(source_path.read_text(encoding="utf-8"))
    preflight_source = preflight_dir / java_verification_filename(source_text, source_path.name)
    preflight_source.write_text(source_text, encoding="utf-8")
    write_openjml_support_files(source_text, preflight_dir)
    result = run_openjml(
        preflight_source,
        openjml_path=args.openjml_path,
        timeout_s=args.preflight_timeout,
        cwd=preflight_dir,
    )
    classified = source_preflight_status(result)
    if classified is None:
        return None

    status, text = classified
    output_case_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_case_dir / "source_preflight.out"
    output_path.write_text(text + ("\n" if text and not text.endswith("\n") else ""), encoding="utf-8")
    new_row = deepcopy(row)
    new_row.update(
        {
            "status": status,
            "passed": False,
            "iterations": 0,
            "runtime_s": float(getattr(result, "runtime_s", 0.0) or 0.0),
            "final_annotated_path": "",
            "openjml_output_path": str(output_path),
            "error": text[:1000],
            "attempts": 0,
            "trials": 0,
            "trial_passes": 0,
            "trial_status_counts": {status: 1},
            "trial_rows": [],
            "replay_note": "source preflight failed before generated JML was considered",
        }
    )
    return new_row


def replay_trial(row: dict[str, Any], trial: dict[str, Any], case_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    new_trial = deepcopy(trial)
    if bool(trial.get("passed")) and not args.replay_passed:
        new_trial["replay_note"] = "kept prior passing trial without rerunning"
        return new_trial
    if str(trial.get("status")) == "timeout" and not args.include_timeouts:
        new_trial["replay_note"] = "kept prior timeout without rerunning"
        return new_trial

    source = Path(str(trial.get("source") or row.get("source") or ""))
    generated = Path(str(trial.get("final_annotated_path") or ""))
    if not source.is_file() or not generated.is_file():
        new_trial["replay_note"] = "missing source or generated artifact"
        return new_trial

    trial_name = Path(str(trial.get("report_path") or generated.parent)).parent.name
    output_dir = case_dir / trial_name
    output_java = output_dir / generated.name
    status, passed, runtime_s, prune_rounds, error, output_java = replay_generated_source(
        source_path=source,
        generated_path=generated,
        output_java=output_java,
        output_log_prefix=output_dir / "openjml_replay",
        openjml_path=args.openjml_path,
        timeout_s=args.openjml_timeout,
        max_prune_rounds=args.max_prune_rounds,
    )
    new_trial.update(
        {
            "old_status": trial.get("status"),
            "old_passed": bool(trial.get("passed")),
            "status": status,
            "passed": passed,
            "runtime_s": runtime_s,
            "final_annotated_path": str(output_java),
            "openjml_output_path": str(output_dir / f"openjml_replay_round_{prune_rounds}.out"),
            "error": "" if passed else error,
            "replay_prune_rounds": prune_rounds,
            "replay_note": "current postprocess replay",
        }
    )
    return new_trial


def aggregate_case(row: dict[str, Any], trials: list[dict[str, Any]], case_dir: Path) -> dict[str, Any]:
    passed_trials = [trial for trial in trials if bool(trial.get("passed"))]
    representative = passed_trials[0] if passed_trials else (trials[0] if trials else row)
    status_counts: dict[str, int] = {}
    for trial in trials:
        status = str(trial.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    new_row = deepcopy(row)
    new_row.update(
        {
            "old_status": row.get("status"),
            "old_passed": bool(row.get("passed")),
            "status": "passed" if passed_trials else str(representative.get("status") or row.get("status") or "unknown"),
            "passed": bool(passed_trials),
            "iterations": sum(int(trial.get("iterations") or 0) for trial in trials),
            "runtime_s": sum(float(trial.get("runtime_s") or 0.0) for trial in trials),
            "final_annotated_path": representative.get("final_annotated_path", ""),
            "openjml_output_path": representative.get("openjml_output_path", ""),
            "error": "" if passed_trials else representative.get("error", ""),
            "attempts": 1,
            "trials": len(trials),
            "trial_passes": len(passed_trials),
            "trial_status_counts": dict(sorted(status_counts.items())),
            "trial_rows": trials,
            "replay_case_dir": str(case_dir),
        }
    )
    return new_row


def write_report(output: Path, rows: list[dict[str, Any]], source_report: Path) -> None:
    annotate_rows(output, rows)
    rows = sorted(rows, key=lambda row: str(row.get("case", "")))
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_report": str(source_report),
        "mode": "postprocess_replay",
        "summary": summarize(rows),
        "rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    payload = sanitize_report_payload(payload)
    (output / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_summary_md(output / "summary.md", payload)


def annotate_rows(output: Path, rows: list[dict[str, Any]]) -> None:
    valid_fields = {field.name for field in fields(CaseRow)}
    converted: list[tuple[dict[str, Any], CaseRow]] = []
    for row in rows:
        try:
            case_row = CaseRow(**{key: value for key, value in row.items() if key in valid_fields})
        except Exception:
            row.setdefault("failure_class", "not_classified")
            continue
        converted.append((row, case_row))
    case_rows = [case_row for _, case_row in converted]
    attach_oracle_source_metadata(output, case_rows)
    attach_failure_classes(case_rows)
    for row, case_row in converted:
        row["oracle_source_status"] = case_row.oracle_source_status
        row["oracle_source_diff_path"] = case_row.oracle_source_diff_path
        row["failure_class"] = case_row.failure_class
        row["failure_reason"] = case_row.failure_reason


def _ordered_partial_rows(rows_by_index: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [rows_by_index[index] for index in sorted(rows_by_index)]


def _write_partial_report(output: Path, rows_by_index: dict[int, dict[str, Any]], source_report: Path) -> None:
    write_report(output, _ordered_partial_rows(rows_by_index), source_report)


def process_row(index: int, total: int, row: dict[str, Any], output: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any], str]:
    case = str(row.get("case") or f"case_{index}")
    case_dir = output / "cases" / case
    if args.preflight_source:
        preflight = preflight_case(row, case_dir, args)
        if preflight is not None:
            return index, preflight, f"[{index}/{total}] {case}: {preflight['status']} preflight-skip"

    trials = row.get("trial_rows")
    if not isinstance(trials, list) or not trials:
        trials = [row]
    if getattr(args, "only_passed_trials", False):
        passed_trials = [trial for trial in trials if isinstance(trial, dict) and bool(trial.get("passed"))]
        if passed_trials:
            trials = passed_trials
    trials = select_trials(
        [trial for trial in trials if isinstance(trial, dict)],
        getattr(args, "max_trials_per_case", None),
    )
    replayed: list[dict[str, Any]] = []
    for trial in trials:
        if _STOP_REPLAY.is_set():
            break
        replayed.append(replay_trial(row, trial, case_dir, args))
        if _STOP_REPLAY.is_set():
            break
    new_row = aggregate_case(row, replayed, case_dir)
    message = (
        f"[{index}/{total}] {case}: {new_row['status']} "
        f"{new_row.get('trial_passes', 0)}/{new_row.get('trials', 0)}"
    )
    return index, new_row, message


def cmd_replay(args: argparse.Namespace) -> int:
    _STOP_REPLAY.clear()
    rows = select_rows(load_rows(Path(args.input_report)), getattr(args, "cases", None))
    source_report = Path(args.input_report)
    output = Path(args.output).resolve()
    out_rows_by_index: dict[int, dict[str, Any]] = {}
    total = len(rows)
    max_workers = max(1, int(args.workers))

    try:
        if max_workers == 1:
            for index, row in enumerate(rows, start=1):
                _, new_row, message = process_row(index, total, row, output, args)
                out_rows_by_index[index] = new_row
                write_report(output, _ordered_partial_rows(out_rows_by_index), source_report)
                print(message, flush=True)
        else:
            pool = futures.ThreadPoolExecutor(max_workers=max_workers)
            future_map = {
                pool.submit(process_row, index, total, row, output, args): index
                for index, row in enumerate(rows, start=1)
            }
            try:
                for fut in futures.as_completed(future_map):
                    index, new_row, message = fut.result()
                    out_rows_by_index[index] = new_row
                    write_report(output, _ordered_partial_rows(out_rows_by_index), source_report)
                    print(message, flush=True)
            except KeyboardInterrupt:
                _STOP_REPLAY.set()
                for fut in future_map:
                    fut.cancel()
                kill_active_openjml_process_groups()
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)
    except KeyboardInterrupt:
        _STOP_REPLAY.set()
        kill_active_openjml_process_groups()
        _write_partial_report(output, out_rows_by_index, source_report)
        print(f"interrupted: wrote partial report with {len(out_rows_by_index)}/{total} completed cases", flush=True)
        return 130

    out_rows = _ordered_partial_rows(out_rows_by_index)
    write_report(output, out_rows, Path(args.input_report))
    print(f"summary: {summarize(out_rows)}")
    print(f"report: {output / 'report.json'}")
    print(f"summary_md: {output / 'summary.md'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-report", required=True, help="Prior run_bmc_jml_specgen report.json.")
    parser.add_argument("--output", required=True, help="Output directory for replay report and artifacts.")
    parser.add_argument("--openjml-path", default=default_openjml_path(), help="Path to OpenJML.")
    parser.add_argument("--openjml-timeout", type=int, default=60, help="OpenJML timeout for replayed trials.")
    parser.add_argument(
        "--max-prune-rounds",
        type=int,
        default=5,
        help="Current postprocess prune rounds; defaults to the production JML checker setting.",
    )
    parser.add_argument("--workers", type=int, default=1, help="Parallel cases to replay.")
    parser.add_argument("--cases", nargs="*", default=None, help="Optional case names to replay.")
    parser.add_argument("--max-trials-per-case", type=int, default=None, help="Replay at most this many trials per case.")
    parser.add_argument("--only-passed-trials", action="store_true", help="When available, replay only prior passing trials.")
    parser.add_argument("--replay-passed", action="store_true", help="Also rerun previously passing trials.")
    parser.add_argument("--include-timeouts", action="store_true", help="Rerun prior timeout trials.")
    parser.add_argument(
        "--preflight-source",
        action="store_true",
        help="Classify cases whose unannotated Java source already fails OpenJML frontend/tool checks.",
    )
    parser.add_argument("--preflight-timeout", type=int, default=5, help="OpenJML timeout for source preflight.")
    return parser


def main(argv: list[str] | None = None) -> int:
    return cmd_replay(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
