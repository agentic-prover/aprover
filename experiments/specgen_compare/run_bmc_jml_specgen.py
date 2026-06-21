#!/usr/bin/env python3
"""Run BMC-Agent Java/JML generation on SpecGen benchmark cases.

This is an experiment adapter, not a production pipeline entry point.  It keeps
SpecGen-specific paths, selection, and reporting outside ``bmc_agent verify``
while reusing the same JML/OpenJML implementation.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import difflib
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bmc_agent.config import Config  # noqa: E402
from bmc_agent.jml_specs import (  # noqa: E402
    _is_source_frontend_error,
    default_openjml_path,
    drop_generated_jml_assertions,
    java_without_jml_fingerprint,
    java_verification_filename,
    kill_active_openjml_process_groups,
    repair_java_source_for_openjml,
    run_jml_specs_bench,
    run_openjml,
    strip_jml_comments,
    write_openjml_support_files,
)
from bmc_agent.llm import LLMClient  # noqa: E402


_STOP_RUN = threading.Event()


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
    oracle_source_status: str = "not_checked"
    oracle_source_diff_path: str = ""
    source_preflight_status: str = "not_checked"
    source_preflight_assert_failure: bool = False
    source_preflight_output_path: str = ""
    source_preflight_failure_reason: str = ""
    failure_class: str = "not_classified"
    failure_reason: str = ""
    generated_assert_assume_count: int | None = None
    clean_jml_artifact: bool | None = None


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


_REPORT_REDACTIONS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"sk-[A-Za-z0-9_*.-]{6,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"https://openrouter\.ai/workspaces/default/" r"keys/[A-Za-z0-9_-]+"), "[REDACTED_PROVIDER_KEY_URL]"),
    (re.compile(r"user_[A-Za-z0-9]+"), "user_[REDACTED]"),
    (re.compile(r"Key limit " r"exceeded \(total limit\)"), "Provider quota limit exceeded"),
)


def sanitize_report_payload(value: Any) -> Any:
    """Redact provider/API details before writing experiment artifacts."""

    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _REPORT_REDACTIONS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    if isinstance(value, list):
        return [sanitize_report_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_report_payload(item) for key, item in value.items()}
    return value


def classify_runner_exception(exc: Exception) -> tuple[str, str]:
    """Classify runner exceptions before they are recorded in experiment rows."""

    message = sanitize_report_payload(str(exc))
    lowered = message.lower()
    if "not a valid model id" in lowered:
        return "llm_config_error", message
    if (
        "key limit exceeded" in lowered
        or "http 401" in lowered
        or "http 403" in lowered
        or "insufficient_quota" in lowered
    ):
        return "llm_unavailable", message
    if "rate limit" in lowered or "http 429" in lowered:
        return "llm_rate_limited", message
    return "runner_error", message


def is_batch_level_llm_error(status: str) -> bool:
    return status in {"llm_config_error", "llm_unavailable", "llm_rate_limited"}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_report_payload(payload), indent=2), encoding="utf-8")


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


def _linked_structure_prompt_example(index: int) -> str:
    source = """class Node {
    public Node next;
    public int value;

    public void insertAfter(int data) {
        if (data > value) {
            next.insertAfter(data);
        } else {
            next = new Node();
            next.value = data;
        }
    }
}"""
    annotated = """class Node {
    public /*@ nullable @*/ Node next;
    public int value;

    //@ requires data > value ==> next != null;
    //@ assignable next;
    public void insertAfter(int data) {
        if (data > value) {
            next.insertAfter(data);
        } else {
            next = new Node();
            next.value = data;
        }
    }
}"""
    return _format_example_pair(index, source, annotated)


def load_prompt_examples(name: str, bench_root: Path) -> str:
    """Load optional few-shot prompt examples for experiment-only runs."""

    if not name or name == "none":
        return ""
    if name not in {"specgen-4shot", "specgen-4shot-linked"}:
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
    examples = [
        _format_example_pair(i, _read_example(src), _read_example(annotated))
        for i, (src, annotated) in enumerate(pairs, start=1)
    ]
    if name == "specgen-4shot-linked":
        examples.append(_linked_structure_prompt_example(len(examples) + 1))
    return "\n\n".join(examples)


def load_completed(report_path: Path) -> dict[str, CaseRow]:
    if not report_path.exists():
        return {}
    data = json.loads(report_path.read_text(encoding="utf-8"))
    rows = data.get("rows", data if isinstance(data, list) else [])
    completed: dict[str, CaseRow] = {}
    valid_fields = {field.name for field in fields(CaseRow)}
    for row in rows:
        try:
            completed[row["case"]] = CaseRow(**{key: value for key, value in row.items() if key in valid_fields})
        except Exception:
            continue
    return completed


def summarize(rows: list[CaseRow]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_failure_class: dict[str, int] = {}
    by_failure_reason: dict[str, int] = {}
    by_actionability: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        failure_class = row.failure_class or "not_classified"
        by_failure_class[failure_class] = by_failure_class.get(failure_class, 0) + 1
        actionability = failure_actionability(row)
        by_actionability[actionability] = by_actionability.get(actionability, 0) + 1
        if not row.passed and row.failure_reason:
            by_failure_reason[row.failure_reason] = by_failure_reason.get(row.failure_reason, 0) + 1
    trial_total = sum(row_trial_total(r) for r in rows)
    trial_passes = sum(row_trial_passes(r) for r in rows)
    generated_spec_issue_count = by_actionability.get("generated_spec_issue", 0)
    passed_with_zero_trial_passes = [
        r.case
        for r in rows
        if r.passed
        and r.trial_passes is not None
        and int(r.trial_passes or 0) == 0
        and int(r.trials or 0) > 0
    ]
    passed_without_trial_stats = [
        r.case
        for r in rows
        if r.passed
        and r.trial_passes is not None
        and int(r.trial_passes or 0) == 0
        and int(r.trials or 0) == 0
    ]
    return {
        "total": len(rows),
        "passed": sum(1 for r in rows if r.passed),
        "clean_jml_passed": sum(1 for r in rows if r.passed and r.clean_jml_artifact is True),
        "generated_spec_issue_count": generated_spec_issue_count,
        "source_or_tool_boundary_count": by_actionability.get("source_or_tool_boundary", 0),
        "llm_or_runner_issue_count": by_actionability.get("llm_or_runner_issue", 0),
        "passed_with_zero_trial_passes_count": len(passed_with_zero_trial_passes),
        "passed_with_zero_trial_passes_cases": passed_with_zero_trial_passes,
        "passed_without_trial_stats_count": len(passed_without_trial_stats),
        "passed_without_trial_stats_cases": passed_without_trial_stats,
        "rows_with_generated_assert_assume": sum(
            1 for r in rows if (r.generated_assert_assume_count or 0) > 0
        ),
        "trial_total": trial_total,
        "trial_passes": trial_passes,
        "mean_success_probability": (trial_passes / trial_total) if trial_total else 0.0,
        "by_status": dict(sorted(by_status.items())),
        "by_failure_class": dict(sorted(by_failure_class.items())),
        "by_failure_reason": dict(sorted(by_failure_reason.items())),
        "by_actionability": dict(sorted(by_actionability.items())),
    }


def failure_actionability(row: CaseRow) -> str:
    """Classify failures by whether further spec-generation work is likely useful."""

    if row.passed:
        return "passed"
    failure_class = row.failure_class or "not_classified"
    if failure_class in {"llm_config_error", "llm_rate_limited", "llm_unavailable", "runner_error"}:
        return "llm_or_runner_issue"
    if failure_class in {
        "library_precondition",
        "openjml_timeout",
        "oracle_source_mismatch",
        "source_assert_failure",
        "source_frontend_or_tool",
        "source_library_precondition",
        "source_openjml_timeout",
        "source_safety_obligation",
    }:
        return "source_or_tool_boundary"
    return "generated_spec_issue"


def row_trial_total(row: CaseRow) -> int:
    if int(row.attempts or 0) == 0:
        return 0
    return max(0, int(row.trials if row.trials is not None else 1))


def row_trial_passes(row: CaseRow) -> int:
    if row.trial_passes is not None:
        return max(0, int(row.trial_passes))
    if row_trial_total(row) == 0:
        return 0
    return int(row.passed)


def row_satisfies_requested_run(row: CaseRow, trial_count: int) -> bool:
    """Return whether a loaded report row should be skipped under --resume."""

    if int(row.attempts or 0) == 0:
        return True
    if trial_count > 1:
        return int(row.trials or 0) >= trial_count
    return True


def _oracle_source_diff(source_text: str, oracle_text: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            strip_jml_comments(source_text).splitlines(),
            strip_jml_comments(oracle_text).splitlines(),
            fromfile="input_source_without_jml",
            tofile="oracle_without_jml",
            lineterm="",
            n=3,
        )
    )


def oracle_source_metadata(row: CaseRow, output: Path) -> tuple[str, str]:
    """Classify whether the oracle changes Java code beyond JML annotations.

    The oracle is not used to generate specs.  This report-only diagnostic keeps
    benchmark interpretation honest: a failure on a case whose oracle also
    rewrites Java code is not the same kind of signal as a pure JML-generation
    failure on identical source.
    """

    if not row.oracle:
        return "not_available", ""
    source = Path(row.source)
    oracle = Path(row.oracle)
    if not source.exists() or not oracle.exists():
        return "missing_file", ""
    try:
        source_text = source.read_text(encoding="utf-8")
        oracle_text = oracle.read_text(encoding="utf-8")
    except OSError:
        return "unreadable", ""
    if java_without_jml_fingerprint(source_text) == java_without_jml_fingerprint(oracle_text):
        return "jml_only_or_same_source", ""

    diff_path = output / "cases" / row.case / "oracle_source_without_jml.diff"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(_oracle_source_diff(source_text, oracle_text) + "\n", encoding="utf-8")
    return "source_mismatch", str(diff_path)


def attach_oracle_source_metadata(output: Path, rows: list[CaseRow]) -> None:
    for row in rows:
        if row.oracle_source_status != "not_checked":
            continue
        row.oracle_source_status, row.oracle_source_diff_path = oracle_source_metadata(row, output)


_SOURCE_PREFLIGHT_STATUS_PRIORITY = {
    "unknown": 0,
    "passed": 0,
    "timeout": 1,
    "verification_failed": 2,
    "source_invalid": 4,
    "source_tool_error": 4,
}


def _is_concrete_source_preflight_reason(reason: str) -> bool:
    reason = (reason or "").strip()
    return bool(reason) and reason != "OpenJMLTimeout"


def _source_preflight_priority(item: dict[str, Any]) -> int:
    if item.get("source_preflight_assert_failure"):
        return 6
    if item.get("source_preflight_status") == "verification_failed" and item.get("source_preflight_failure_reason"):
        return 5
    if item.get("source_preflight_status") == "timeout" and _is_concrete_source_preflight_reason(
        str(item.get("source_preflight_failure_reason") or "")
    ):
        return 3
    status = str(item.get("source_preflight_status") or "unknown")
    return _SOURCE_PREFLIGHT_STATUS_PRIORITY.get(status, 1)


def _source_preflight_failure_reason_from_row(row: dict[str, Any]) -> str:
    explicit = str(row.get("failure_reason") or "")
    if explicit:
        return explicit
    first_line = str(row.get("first_line") or "")
    if first_line:
        reason = openjml_failure_reason_from_text(str(row.get("status") or "unknown"), first_line)
        if reason not in {"unknown", "verification_failed"}:
            return reason
    return ""


def _source_preflight_assert_failure_from_row(row: dict[str, Any], reason: str) -> bool:
    if reason:
        return reason == "Assert"
    return bool(row.get("has_assert_failure"))


def load_source_preflight_metadata(paths: Path | str | list[Path | str] | tuple[Path | str, ...] | None) -> dict[str, dict[str, Any]]:
    if paths is None:
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    if isinstance(paths, (str, Path)):
        paths = [paths]
    for path_item in paths:
        path = Path(path_item)
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("rows", data if isinstance(data, list) else [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not row.get("case"):
                continue
            reason = _source_preflight_failure_reason_from_row(row)
            item = {
                "source_preflight_status": str(row.get("status") or "unknown"),
                "source_preflight_assert_failure": _source_preflight_assert_failure_from_row(row, reason),
                "source_preflight_output_path": str(row.get("output_path") or ""),
                "source_preflight_failure_reason": reason,
            }
            case = str(row["case"])
            existing = metadata.get(case)
            if existing is None or _source_preflight_priority(item) >= _source_preflight_priority(existing):
                metadata[case] = item
            elif item["source_preflight_assert_failure"]:
                existing["source_preflight_assert_failure"] = True
                if item["source_preflight_output_path"]:
                    existing["source_preflight_output_path"] = item["source_preflight_output_path"]
                if item["source_preflight_failure_reason"]:
                    existing["source_preflight_failure_reason"] = item["source_preflight_failure_reason"]
    return metadata


def attach_source_preflight_metadata(rows: list[CaseRow], metadata: dict[str, dict[str, Any]]) -> None:
    for row in rows:
        item = metadata.get(row.case)
        if not item:
            continue
        row.source_preflight_status = item["source_preflight_status"]
        row.source_preflight_assert_failure = item["source_preflight_assert_failure"]
        row.source_preflight_output_path = item["source_preflight_output_path"]
        row.source_preflight_failure_reason = item.get("source_preflight_failure_reason", "")


def _row_failure_text(row: CaseRow) -> str:
    text = row.error or ""
    output_path = Path(row.openjml_output_path) if row.openjml_output_path else None
    if output_path and output_path.exists():
        try:
            text = (text + "\n" + output_path.read_text(encoding="utf-8", errors="replace")).strip()
        except OSError:
            pass
    return text


def _row_failure_points_to_java_assert(row: CaseRow) -> bool:
    text = _row_failure_text(row)
    if "(Assert)" not in text or not row.final_annotated_path:
        return False
    artifact = Path(row.final_annotated_path)
    if not artifact.exists() or not artifact.is_file():
        return False
    try:
        lines = artifact.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    try:
        artifact_resolved = artifact.resolve()
    except OSError:
        artifact_resolved = artifact
    for match in re.finditer(r"(?P<path>[^\n:]+\.java):(?P<line>\d+):\s+verify:.*?\(Assert\)", text):
        reported = Path(match.group("path"))
        try:
            reported_resolved = reported.resolve()
        except OSError:
            reported_resolved = reported
        if reported_resolved != artifact_resolved:
            continue
        line_no = int(match.group("line"))
        if line_no < 1 or line_no > len(lines):
            continue
        stripped = lines[line_no - 1].strip()
        if stripped.startswith("//@") or stripped.startswith("/*@"):
            continue
        if re.match(r"assert\b", stripped):
            return True
    return False


def _generated_assert_assume_count(original: str, annotated: str) -> int:
    cleaned = drop_generated_jml_assertions(original, annotated)
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    for line in annotated.splitlines():
        before[line] = before.get(line, 0) + 1
    for line in cleaned.splitlines():
        after[line] = after.get(line, 0) + 1
    return sum(max(0, count - after.get(line, 0)) for line, count in before.items())


def _artifact_generated_assert_assume_count(source_path: str, annotated_path: str) -> int | None:
    if not source_path or not annotated_path:
        return None
    source = Path(source_path)
    annotated = Path(annotated_path)
    if not source.is_file() or not annotated.is_file():
        return None
    try:
        return _generated_assert_assume_count(
            source.read_text(encoding="utf-8"),
            annotated.read_text(encoding="utf-8"),
        )
    except OSError:
        return None


def _annotate_trial_clean_jml(row: CaseRow, trial: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(trial)
    count = _artifact_generated_assert_assume_count(
        str(annotated.get("source") or row.source),
        str(annotated.get("final_annotated_path") or ""),
    )
    annotated["generated_assert_assume_count"] = count
    annotated["clean_jml_artifact"] = (count == 0) if count is not None else None
    return annotated


def canonicalize_representative_artifacts(rows: list[CaseRow]) -> None:
    """Prefer clean passing trial artifacts for top-level report pointers.

    Multi-trial reports can contain both clean passes and older passes that relied
    on generated ``//@ assert`` or ``//@ assume`` statements.  The summary count is
    case-level, but review packages and manual audits use the top-level artifact
    paths, so those pointers should identify a clean passing trial whenever one
    exists.
    """

    for row in rows:
        annotated_trials = [
            _annotate_trial_clean_jml(row, trial)
            for trial in (row.trial_rows or [])
            if isinstance(trial, dict)
        ]
        if annotated_trials:
            row.trial_rows = annotated_trials
        clean_passed = [
            trial
            for trial in annotated_trials
            if bool(trial.get("passed")) and trial.get("clean_jml_artifact") is True
        ]
        if clean_passed:
            representative = sorted(clean_passed, key=lambda trial: str(trial.get("report_path") or ""))[0]
            row.final_annotated_path = str(representative.get("final_annotated_path") or "")
            row.report_path = str(representative.get("report_path") or "")
            row.openjml_output_path = str(representative.get("openjml_output_path") or "")
            row.jml_clause_counts = representative.get("jml_clause_counts")
            row.generated_assert_assume_count = 0
            row.clean_jml_artifact = True
            if row.passed:
                row.status = "passed"
                row.error = ""
            continue

        count = _artifact_generated_assert_assume_count(row.source, row.final_annotated_path)
        row.generated_assert_assume_count = count
        row.clean_jml_artifact = (count == 0) if count is not None else None


def require_clean_passing_jml(rows: list[CaseRow]) -> None:
    """Downgrade pass rows that have no clean passing artifact."""

    canonicalize_representative_artifacts(rows)
    for row in rows:
        if not row.passed:
            continue
        if row.clean_jml_artifact is True:
            continue
        row.passed = False
        row.status = "verification_failed"
        row.error = (
            "no clean passing JML artifact remains after excluding generated "
            "JML assert/assume statements"
        )


def _is_library_precondition_text(text: str) -> bool:
    lowered = text.lower()
    if "/specs/java/" in lowered and (
        "precondition:" in lowered
        or "undefinedcalledmethodprecondition:" in lowered
        or "invariantleavecaller:" in lowered
        or "diverges:" in lowered
    ):
        return True
    return (
        "null precondition" in lowered
        and (
            " java.util." in lowered
            or " java.lang." in lowered
            or " java.io." in lowered
        )
    )


def _is_openjml_timeout_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "validity is unknown - time or memory limit reached" in lowered
        or "aborted proof: timeout" in lowered
        or "time or memory limit reached" in lowered
        or "openjml wall-clock timeout" in lowered
    )


def openjml_failure_reason_from_text(status: str, text: str) -> str:
    lowered = text.lower()
    if _is_library_precondition_text(text):
        return "LibraryPrecondition"
    if "null precondition" in lowered:
        return "NullPrecondition"
    if _is_source_frontend_error(lowered):
        if "unreachable statement" in lowered:
            return "SourceUnreachableStatement"
        if "cannot find symbol" in lowered:
            return "SourceMissingSymbol"
        if "is public, should be declared in a file named" in lowered:
            return "SourcePublicTypeFilename"
        if "package org.sosy_lab.sv_benchmarks does not exist" in lowered:
            return "SourceMissingVerifierPackage"
        return "SourceFrontendError"
    match = re.search(r"\(([^()\n]+)\)\s+in method", text)
    if not match:
        match = re.search(r"\((Precondition:[^)\n]+)\)", text)
    if match:
        reason = match.group(1)
        if reason.startswith("Precondition:"):
            if "/specs/java/" in reason:
                return "LibraryPrecondition"
            return "Precondition"
        if reason.startswith("UndefinedCalledMethodPrecondition:"):
            if "/specs/java/" in reason:
                return "LibraryPrecondition"
            return "UndefinedCalledMethodPrecondition"
        if reason.startswith("InvariantLeaveCaller:"):
            return "LibraryInvariantLeaveCaller" if "/specs/java/" in reason else "InvariantLeaveCaller"
        return reason
    if "an error while executing a proof script" in lowered:
        return "OpenJMLProofScriptError"
    if "double rewriting of ident" in lowered:
        return "OpenJMLDoubleRewriteIdent"
    if "catastrophic jml internal error" in lowered:
        return "OpenJMLInternalError"
    if _is_openjml_timeout_text(text):
        return "OpenJMLTimeout"
    if "error:" in lowered:
        return "AnnotationOrFrontendError"
    return status or "unknown"


def _is_source_library_precondition_reason(reason: str) -> bool:
    return (reason or "").strip().split(":", 1)[0] == "LibraryPrecondition"


def _has_concrete_source_preflight_reason(row: CaseRow) -> bool:
    return _is_concrete_source_preflight_reason(row.source_preflight_failure_reason)


def classify_failure(row: CaseRow) -> str:
    if row.passed:
        return "passed"
    if row.oracle_source_status == "source_mismatch":
        return "oracle_source_mismatch"
    status = row.status
    text = _row_failure_text(row).lower()
    if status in {"llm_config_error", "llm_unavailable", "llm_rate_limited"}:
        return status
    if status == "source_changed":
        return "source_changed"
    if status in {"source_invalid", "source_tool_error"} or _is_source_frontend_error(text):
        return "source_frontend_or_tool"
    if row.source_preflight_status in {"source_invalid", "source_tool_error"}:
        return "source_frontend_or_tool"
    if row.source_preflight_assert_failure:
        return "source_assert_failure"
    if (
        row.source_preflight_status == "verification_failed"
        and _is_source_library_precondition_reason(row.source_preflight_failure_reason)
    ):
        return "source_library_precondition"
    if (
        row.source_preflight_status == "verification_failed"
        and not row.source_preflight_assert_failure
    ):
        return "source_safety_obligation"
    if status == "verification_failed" and _row_failure_points_to_java_assert(row):
        return "source_assert_failure"
    if row.source_preflight_status == "timeout" and _has_concrete_source_preflight_reason(row):
        if _is_source_library_precondition_reason(row.source_preflight_failure_reason):
            return "source_library_precondition"
        return "source_safety_obligation"
    if row.source_preflight_status == "timeout":
        return "source_openjml_timeout"
    if _is_library_precondition_text(text):
        return "library_precondition"
    if (
        status == "tool_error"
        or "catastrophic jml internal error" in text
        or "double rewriting of ident" in text
        or "an error while executing a proof script" in text
    ):
        return "openjml_tool_error"
    if status == "timeout" or _is_openjml_timeout_text(text):
        return "openjml_timeout"
    if status == "annotation_error":
        return "invalid_generated_jml"
    if status == "verification_failed":
        return "spec_not_sufficient"
    if status == "interrupted":
        return "interrupted"
    return "other_failure"


def extract_failure_reason(row: CaseRow) -> str:
    if row.passed:
        return ""
    text = _row_failure_text(row)
    lowered = text.lower()
    if "generated jml assert/assume" in lowered:
        return "GeneratedAssertAssumeOnly"
    if row.status == "source_changed":
        return "SourceChanged"
    if row.oracle_source_status == "source_mismatch":
        return "oracle_source_mismatch"
    if row.source_preflight_assert_failure:
        return "SourceAssertFailure"
    if row.source_preflight_status == "source_tool_error":
        return "SourceOpenJMLToolError"
    if row.source_preflight_status == "source_invalid":
        return "SourceFrontendError"
    if (
        row.source_preflight_status == "verification_failed"
        and _is_source_library_precondition_reason(row.source_preflight_failure_reason)
    ):
        return "SourceLibraryPrecondition"
    if (
        row.source_preflight_status == "verification_failed"
        and not row.source_preflight_assert_failure
    ):
        return _source_safety_failure_reason(row.source_preflight_failure_reason)
    if row.status == "verification_failed" and _row_failure_points_to_java_assert(row):
        return "SourceAssertFailure"
    if row.source_preflight_status == "timeout" and _has_concrete_source_preflight_reason(row):
        if _is_source_library_precondition_reason(row.source_preflight_failure_reason):
            return "SourceLibraryPrecondition"
        return _source_safety_failure_reason(row.source_preflight_failure_reason)
    if row.source_preflight_status == "timeout":
        return "SourceOpenJMLTimeout"
    return openjml_failure_reason_from_text(row.status or "unknown", text)


def _source_safety_failure_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        return "SourceSafetyObligation"
    compact = reason.split(":", 1)[0].strip()
    if compact:
        return f"SourceSafety:{compact}"
    return "SourceSafetyObligation"


def attach_failure_classes(rows: list[CaseRow]) -> None:
    for row in rows:
        row.failure_class = classify_failure(row)
        row.failure_reason = extract_failure_reason(row)


def write_report(output: Path, rows: list[CaseRow], source_preflight_metadata: dict[str, dict[str, Any]] | None = None) -> None:
    canonicalize_representative_artifacts(rows)
    attach_oracle_source_metadata(output, rows)
    attach_source_preflight_metadata(rows, source_preflight_metadata or {})
    attach_failure_classes(rows)
    rows_sorted = sorted(rows, key=lambda r: r.case)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": summarize(rows_sorted),
        "rows": [asdict(r) for r in rows_sorted],
    }
    write_json(output / "report.json", payload)
    write_summary_md(output / "summary.md", payload)


def interrupted_row(case: SpecGenCase, args: argparse.Namespace) -> CaseRow:
    metadata = run_metadata(args, configure(args))
    return CaseRow(
        case=case.name,
        source=case.source,
        oracle=case.oracle,
        status="interrupted",
        passed=False,
        iterations=0,
        runtime_s=0.0,
        final_annotated_path="",
        report_path="",
        openjml_output_path="",
        error="run interrupted before this case completed",
        jml_clause_counts={},
        attempts=0,
        trials=0,
        trial_passes=0,
        trial_status_counts={"interrupted": 1},
        trial_rows=[],
        **metadata,
    )


def format_trial_cell(row: dict[str, Any]) -> str:
    """Render trial-pass stats without hiding replay/overlay provenance."""

    trial_passes = row.get("trial_passes")
    if trial_passes is None:
        return ""
    trials = int(row.get("trials") or 0)
    trial_passes_int = int(trial_passes or 0)
    if bool(row.get("passed")) and trial_passes_int == 0 and trials > 0:
        return f"0/{trials} (base; overlay pass)"
    if bool(row.get("passed")) and trial_passes_int == 0 and trials == 0:
        return "n/a (source-preflight pass)"
    if int(row.get("attempts", 1) or 0) == 0:
        return "skipped"
    return f"{trial_passes_int}/{trials}"


def write_summary_md(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# BMC-Agent Java/JML on SpecGen Benchmark",
        "",
        f"- Total cases: {payload['summary']['total']}",
        f"- Passed: {payload['summary']['passed']}",
        f"- Clean JML passed: {payload['summary'].get('clean_jml_passed', 0)}",
        f"- Generated-spec issue count: {payload['summary'].get('generated_spec_issue_count', 0)}",
        f"- Source/tool boundary count: {payload['summary'].get('source_or_tool_boundary_count', 0)}",
        f"- LLM/runner issue count: {payload['summary'].get('llm_or_runner_issue_count', 0)}",
        f"- Passed rows with zero preserved trial passes: {payload['summary'].get('passed_with_zero_trial_passes_count', 0)}",
        f"- Passed rows without trial stats: {payload['summary'].get('passed_without_trial_stats_count', 0)}",
        f"- Rows with generated assert/assume artifacts: {payload['summary'].get('rows_with_generated_assert_assume', 0)}",
        f"- Trial passes: {payload['summary'].get('trial_passes', payload['summary']['passed'])}/{payload['summary'].get('trial_total', payload['summary']['total'])}",
        f"- Mean success probability: {float(payload['summary'].get('mean_success_probability', 0.0)):.4f}",
        f"- Status counts: `{payload['summary']['by_status']}`",
        f"- Failure-class counts: `{payload['summary'].get('by_failure_class', {})}`",
        f"- Failure-reason counts: `{payload['summary'].get('by_failure_reason', {})}`",
        f"- Actionability counts: `{payload['summary'].get('by_actionability', {})}`",
        "",
    ]
    if payload["summary"].get("passed_with_zero_trial_passes_count", 0) or payload["summary"].get(
        "passed_without_trial_stats_count", 0
    ):
        lines.extend(
            [
                "Note: some case-level passes come from later replay/overlay checks while trial-pass counts are preserved from the base multi-trial run.",
                "Use the case-level `Pass` column for the current clean proof result and the trial-pass column only as base-run generation stability context.",
                "",
            ]
        )
        if payload["summary"].get("passed_with_zero_trial_passes_cases"):
            lines.extend(
                [
                    f"- Passed rows with zero preserved trial passes: `{payload['summary'].get('passed_with_zero_trial_passes_cases', [])}`",
                    "",
                ]
            )
        if payload["summary"].get("passed_without_trial_stats_cases"):
            lines.extend(
                [
                    f"- Passed rows without trial stats: `{payload['summary'].get('passed_without_trial_stats_cases', [])}`",
                    "",
                ]
            )
    lines.extend(
        [
            "| Case | Status | Failure class | Failure reason | Pass | Oracle source | Trial passes | Attempts | Iters | Runtime(s) | OpenJML output |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in payload["rows"]:
        out = row.get("openjml_output_path") or ""
        trial_cell = format_trial_cell(row)
        lines.append(
            "| {case} | {status} | {failure_class} | {failure_reason} | {passed} | {oracle_source} | {trial_cell} | {attempts} | {iterations} | {runtime:.2f} | `{out}` |".format(
                case=row["case"],
                status=row["status"],
                failure_class=row.get("failure_class", "not_classified"),
                failure_reason=row.get("failure_reason", ""),
                passed="yes" if row["passed"] else "no",
                oracle_source=row.get("oracle_source_status", "not_checked"),
                trial_cell=trial_cell,
                attempts=row.get("attempts", 1),
                iterations=row["iterations"],
                runtime=float(row["runtime_s"] or 0.0),
                out=out,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def residual_recommendation(row: CaseRow) -> str:
    if row.passed:
        return "passed"
    actionability = failure_actionability(row)
    if actionability == "generated_spec_issue":
        return "continue_spec_generation_optimization"
    if actionability == "llm_or_runner_issue":
        return "fix_runner_or_configuration"
    if actionability == "source_or_tool_boundary":
        return "stop_generation_optimization_source_or_tool_boundary"
    return "manual_inspection_needed"


def build_residual_audit_payload(rows: list[CaseRow], source_report: str) -> dict[str, Any]:
    attach_failure_classes(rows)
    rows_sorted = sorted(rows, key=lambda r: r.case)
    summary = summarize(rows_sorted)
    residual_rows: list[dict[str, Any]] = []
    by_failure_class: dict[str, int] = {}
    by_failure_reason: dict[str, int] = {}
    by_actionability: dict[str, int] = {}
    by_recommendation: dict[str, int] = {}
    for row in rows_sorted:
        if row.passed:
            continue
        actionability = failure_actionability(row)
        recommendation = residual_recommendation(row)
        by_failure_class[row.failure_class] = by_failure_class.get(row.failure_class, 0) + 1
        if row.failure_reason:
            by_failure_reason[row.failure_reason] = by_failure_reason.get(row.failure_reason, 0) + 1
        by_actionability[actionability] = by_actionability.get(actionability, 0) + 1
        by_recommendation[recommendation] = by_recommendation.get(recommendation, 0) + 1
        residual_rows.append(
            {
                "case": row.case,
                "status": row.status,
                "failure_class": row.failure_class,
                "failure_reason": row.failure_reason,
                "actionability": actionability,
                "recommendation": recommendation,
                "source_preflight_status": row.source_preflight_status,
                "source_preflight_failure_reason": row.source_preflight_failure_reason,
                "source_preflight_assert_failure": row.source_preflight_assert_failure,
                "oracle_source_status": row.oracle_source_status,
                "openjml_output_path": row.openjml_output_path,
                "source_preflight_output_path": row.source_preflight_output_path,
            }
        )

    if summary.get("generated_spec_issue_count", 0) > 0:
        decision = "continue_generation_optimization"
    elif summary.get("llm_or_runner_issue_count", 0) > 0:
        decision = "fix_runner_or_configuration_first"
    else:
        decision = "stop_generation_optimization_low_marginal_gain"

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_report": source_report,
        "decision": decision,
        "summary": {
            "total": summary["total"],
            "passed": summary["passed"],
            "residual": len(residual_rows),
            "generated_spec_issue_count": summary.get("generated_spec_issue_count", 0),
            "source_or_tool_boundary_count": summary.get("source_or_tool_boundary_count", 0),
            "llm_or_runner_issue_count": summary.get("llm_or_runner_issue_count", 0),
            "by_failure_class": dict(sorted(by_failure_class.items())),
            "by_failure_reason": dict(sorted(by_failure_reason.items())),
            "by_actionability": dict(sorted(by_actionability.items())),
            "by_recommendation": dict(sorted(by_recommendation.items())),
        },
        "rows": residual_rows,
    }


def write_residual_audit_md(path: Path, payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    lines = [
        "# Java/JML Residual Failure Audit",
        "",
        f"- Source report: `{payload['source_report']}`",
        f"- Decision: `{payload['decision']}`",
        f"- Total cases: `{summary['total']}`",
        f"- Passed: `{summary['passed']}`",
        f"- Residual failures: `{summary['residual']}`",
        f"- Generated-spec issue count: `{summary['generated_spec_issue_count']}`",
        f"- Source/tool boundary count: `{summary['source_or_tool_boundary_count']}`",
        f"- LLM/runner issue count: `{summary['llm_or_runner_issue_count']}`",
        f"- Recommendation counts: `{summary['by_recommendation']}`",
        f"- Failure-class counts: `{summary['by_failure_class']}`",
        f"- Failure-reason counts: `{summary['by_failure_reason']}`",
        "",
    ]
    if payload["decision"] == "stop_generation_optimization_low_marginal_gain":
        lines.extend(
            [
                "All residual failures are already classified as source/tool boundaries.",
                "Further JML-generation sweeps are unlikely to improve this checkpoint unless the verifier model, source benchmark contract, or evaluation scope changes.",
                "",
            ]
        )
    lines.extend(
        [
            "| Case | Failure class | Failure reason | Actionability | Recommendation | Source preflight | Source reason | OpenJML output |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in payload["rows"]:
        lines.append(
            "| {case} | {failure_class} | {failure_reason} | {actionability} | {recommendation} | {source_status} | {source_reason} | `{out}` |".format(
                case=row["case"],
                failure_class=row["failure_class"],
                failure_reason=row["failure_reason"],
                actionability=row["actionability"],
                recommendation=row["recommendation"],
                source_status=row["source_preflight_status"],
                source_reason=row["source_preflight_failure_reason"],
                out=row["openjml_output_path"] or row["source_preflight_output_path"],
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


def preflight_timeout(args: argparse.Namespace) -> int:
    value = getattr(args, "preflight_timeout", 0) or 0
    if value > 0:
        return int(value)
    return int(getattr(args, "openjml_timeout", 200))


def _openjml_text(result: Any) -> str:
    text = ((getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or ""))
    error = getattr(result, "error", "") or ""
    if error:
        text = (text + "\n" + error).strip()
    return text


def source_preflight_status(result: Any) -> tuple[str, str] | None:
    """Classify source-level OpenJML frontend blockers.

    The JML generator should not be asked to repair Java sources that OpenJML
    cannot parse/typecheck before any generated annotations are added.  Ordinary
    verification failures are not blockers because generated specs may help.
    """

    status = getattr(result, "status", "")
    text = _openjml_text(result)
    lowered = text.lower()
    if status == "tool_error":
        return "source_tool_error", text
    if "error:" in lowered and "verify:" not in lowered:
        return "source_invalid", text
    return None


def preflight_source_case(case: SpecGenCase, args: argparse.Namespace, config: Config) -> CaseRow | None:
    source_path = Path(case.source)
    case_dir = Path(args.output) / "cases" / case.name
    preflight_dir = case_dir / "source_preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    source_text = repair_java_source_for_openjml(source_path.read_text(encoding="utf-8"))
    preflight_source = preflight_dir / java_verification_filename(source_text, source_path.name)
    preflight_source.write_text(source_text, encoding="utf-8")
    write_openjml_support_files(source_text, preflight_dir)
    result = run_openjml(
        preflight_source,
        openjml_path=config.openjml_path,
        timeout_s=preflight_timeout(args),
        cwd=preflight_dir,
    )
    classified = source_preflight_status(result)
    if classified is None:
        return None

    status, text = classified
    metadata = run_metadata(args, config)
    output_path = case_dir / "source_preflight.out"
    output_path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")
    return CaseRow(
        case=case.name,
        source=case.source,
        oracle=case.oracle,
        status=status,
        passed=False,
        iterations=0,
        runtime_s=getattr(result, "runtime_s", 0.0) or 0.0,
        final_annotated_path="",
        report_path="",
        openjml_output_path=str(output_path),
        error=text[:1000],
        jml_clause_counts={},
        attempts=0,
        trials=0,
        trial_passes=0,
        **metadata,
    )


def source_preflight_feedback_context(
    case: SpecGenCase,
    args: argparse.Namespace,
    config: Config,
    driver: str,
) -> dict[str, Any]:
    """Run unannotated-source OpenJML and return prompt/report metadata.

    Unlike ``--preflight-source``, this is advisory: it does not skip a case.
    It gives the first generation prompt a compact view of source-level
    obligations so the model can focus on faithful contracts and loop facts.
    """

    if not getattr(args, "source_preflight_feedback", False):
        return {}
    source_path = Path(case.source)
    feedback_dir = Path(args.output) / "cases" / driver / "source_preflight_feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    source_text = repair_java_source_for_openjml(source_path.read_text(encoding="utf-8"))
    feedback_source = feedback_dir / java_verification_filename(source_text, source_path.name)
    feedback_source.write_text(source_text, encoding="utf-8")
    write_openjml_support_files(source_text, feedback_dir)
    result = run_openjml(
        feedback_source,
        openjml_path=config.openjml_path,
        timeout_s=preflight_timeout(args),
        cwd=feedback_dir,
    )
    text = _openjml_text(result)
    classified = source_preflight_status(result)
    status = classified[0] if classified is not None else str(getattr(result, "status", "unknown") or "unknown")
    output_path = feedback_dir / "openjml_source_preflight.out"
    output_path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")
    has_assert_failure = "(Assert)" in text
    reason = openjml_failure_reason_from_text(status, text)
    if status == "passed":
        context = ""
    else:
        context = (
            f"Unannotated-source OpenJML status: {status}\n"
            f"Failure reason: {reason}\n"
            f"Source assertion failure: {'yes' if has_assert_failure else 'no'}\n\n"
            f"{text[:3500]}"
        ).strip()
    return {
        "context": context,
        "status": status,
        "assert_failure": has_assert_failure,
        "output_path": str(output_path),
        "failure_reason": reason,
    }


def attach_source_preflight_feedback(row: CaseRow, feedback: dict[str, Any]) -> CaseRow:
    if not feedback:
        return row
    row.source_preflight_status = str(feedback.get("status") or "unknown")
    row.source_preflight_assert_failure = bool(feedback.get("assert_failure"))
    row.source_preflight_output_path = str(feedback.get("output_path") or "")
    row.source_preflight_failure_reason = str(feedback.get("failure_reason") or "")
    return row


def _row_matches_filters(row: CaseRow, args: argparse.Namespace) -> bool:
    cases = set(getattr(args, "cases", None) or [])
    statuses = set(getattr(args, "statuses", None) or [])
    failure_classes = set(getattr(args, "failure_classes", None) or [])
    failure_reasons = set(getattr(args, "failure_reasons", None) or [])
    if cases and row.case not in cases:
        return False
    if statuses and row.status not in statuses:
        return False
    if failure_classes and row.failure_class not in failure_classes:
        return False
    if failure_reasons and row.failure_reason not in failure_reasons:
        return False
    return True


def select_report_rows(rows: list[CaseRow], args: argparse.Namespace) -> list[CaseRow]:
    selected = [row for row in rows if _row_matches_filters(row, args)]
    limit = getattr(args, "limit", None)
    if limit is not None:
        selected = selected[: int(limit)]
    return selected


def source_preflight_output_row(
    row: CaseRow,
    *,
    output: Path,
    openjml_path: str,
    timeout_s: int,
) -> dict[str, Any]:
    source_path = Path(row.source)
    case_dir = output / "cases" / row.case
    case_dir.mkdir(parents=True, exist_ok=True)
    source_text = repair_java_source_for_openjml(source_path.read_text(encoding="utf-8"))
    preflight_source = case_dir / java_verification_filename(source_text, source_path.name)
    preflight_source.write_text(source_text, encoding="utf-8")
    write_openjml_support_files(source_text, case_dir)
    result = run_openjml(preflight_source, openjml_path=openjml_path, timeout_s=timeout_s, cwd=case_dir)
    text = _openjml_text(result)
    classified = source_preflight_status(result)
    source_status = classified[0] if classified is not None else str(getattr(result, "status", "unknown") or "unknown")
    output_path = case_dir / "openjml_source_preflight.out"
    output_path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")
    has_assert_failure = "(Assert)" in text
    return {
        "case": row.case,
        "source": row.source,
        "generated_status": row.status,
        "generated_failure_class": row.failure_class,
        "generated_failure_reason": row.failure_reason,
        "status": source_status,
        "passed": bool(getattr(result, "passed", False)),
        "returncode": getattr(result, "returncode", None),
        "runtime_s": float(getattr(result, "runtime_s", 0.0) or 0.0),
        "output_path": str(output_path),
        "has_assert_failure": has_assert_failure,
        "failure_reason": openjml_failure_reason_from_text(source_status, text),
        "first_line": text.splitlines()[0] if text else "",
    }


def summarize_source_preflight(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "total": len(rows),
        "passed": sum(1 for row in rows if bool(row.get("passed"))),
        "assert_failures": sum(1 for row in rows if bool(row.get("has_assert_failure"))),
        "source_timeouts": sum(1 for row in rows if row.get("status") == "timeout"),
        "by_status": dict(sorted(by_status.items())),
    }


def write_source_preflight_report(output: Path, payload: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "report.json", payload)
    lines = [
        "# Source Preflight Report",
        "",
        f"- Total: `{payload['summary']['total']}`",
        f"- Passed: `{payload['summary']['passed']}`",
        f"- Source assert failures: `{payload['summary']['assert_failures']}`",
        f"- Source timeouts: `{payload['summary']['source_timeouts']}`",
        f"- Status counts: `{payload['summary']['by_status']}`",
        "",
        "| Case | Source status | Generated class | Generated reason | Assert failure | Output |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["rows"]:
        lines.append(
            "| {case} | {status} | {failure_class} | {failure_reason} | {assert_failure} | `{out}` |".format(
                case=row["case"],
                status=row["status"],
                failure_class=row.get("generated_failure_class", ""),
                failure_reason=row.get("generated_failure_reason", ""),
                assert_failure="yes" if row.get("has_assert_failure") else "no",
                out=row.get("output_path", ""),
            )
        )
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def should_stop_attempts(statuses: list[str]) -> bool:
    """Return whether repeated attempts are no longer likely to add signal.

    Keep retrying ordinary proof failures because another LLM sample may produce
    a better spec.  Stop quickly for tool/backend failures where rerunning the
    same benchmark tends to only consume OpenJML/LLM time without changing the
    interpretation of the case.
    """

    if not statuses:
        return False
    if is_batch_level_llm_error(statuses[-1]):
        return True
    if statuses[-1] == "tool_error":
        return True
    if len(statuses) >= 2 and statuses[-2:] == ["timeout", "timeout"]:
        return True
    if len(statuses) >= 3 and statuses[-3:] == ["annotation_error", "annotation_error", "annotation_error"]:
        return True
    return False


def run_one(case: SpecGenCase, args: argparse.Namespace) -> CaseRow:
    if _STOP_RUN.is_set():
        return interrupted_row(case, args)
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
    attempt_statuses: list[str] = []
    feedback = source_preflight_feedback_context(case, args, config, case_driver)
    generation_context = str(feedback.get("context") or "")

    for attempt in range(1, attempts + 1):
        if _STOP_RUN.is_set():
            break
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
                generation_context=generation_context,
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
            row = attach_source_preflight_feedback(row, feedback)
            if row.passed:
                return row
            if best_row is None or best_row.status in {"runner_error", "source_changed", "annotation_error"}:
                best_row = row
            attempt_statuses.append(row.status)
            if should_stop_attempts(attempt_statuses):
                break
        except Exception as exc:  # Keep batch reports partial and inspectable.
            status, error = classify_runner_exception(exc)
            row = CaseRow(
                case=case.name,
                source=case.source,
                oracle=case.oracle,
                status=status,
                passed=False,
                iterations=total_iterations,
                runtime_s=total_runtime,
                final_annotated_path="",
                report_path="",
                openjml_output_path="",
                oracle_status=oracle_status,
                error=error,
                jml_clause_counts={},
                attempts=attempt,
                **metadata,
            )
            row = attach_source_preflight_feedback(row, feedback)
            if best_row is None:
                best_row = row
            attempt_statuses.append(row.status)
            if should_stop_attempts(attempt_statuses):
                break

    if best_row is not None:
        best_row.iterations = total_iterations
        best_row.runtime_s = total_runtime
        best_row.attempts = len(attempt_statuses) if attempt_statuses else best_row.attempts
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
    if _STOP_RUN.is_set():
        return interrupted_row(case, args)
    config = configure(args)
    metadata = run_metadata(args, config)
    prompt_examples = load_prompt_examples(getattr(args, "prompt_examples", ""), Path(args.bench_root))
    driver = f"{case.name}/trial_{trial}"
    case_output = Path(args.output) / "cases"
    feedback = source_preflight_feedback_context(case, args, config, driver)
    generation_context = str(feedback.get("context") or "")
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
            generation_context=generation_context,
        )
        last = result.iterations[-1] if result.iterations else None
        row = CaseRow(
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
        return attach_source_preflight_feedback(row, feedback)
    except Exception as exc:
        status, error = classify_runner_exception(exc)
        row = CaseRow(
            case=case.name,
            source=case.source,
            oracle=case.oracle,
            status=status,
            passed=False,
            iterations=0,
            runtime_s=0.0,
            final_annotated_path="",
            report_path="",
            openjml_output_path="",
            error=error,
            jml_clause_counts={},
            attempts=1,
            trials=1,
            **metadata,
        )
        return attach_source_preflight_feedback(row, feedback)


def skipped_llm_error_row(case: SpecGenCase, args: argparse.Namespace, status: str, error: str) -> CaseRow:
    metadata = run_metadata(args, configure(args))
    return CaseRow(
        case=case.name,
        source=case.source,
        oracle=case.oracle,
        status=status,
        passed=False,
        iterations=0,
        runtime_s=0.0,
        final_annotated_path="",
        report_path="",
        openjml_output_path="",
        error=error,
        jml_clause_counts={},
        attempts=0,
        trials=0,
        trial_passes=0,
        trial_status_counts={status: 1},
        trial_rows=[],
        **metadata,
    )


def mark_remaining_llm_error(
    rows: dict[str, CaseRow],
    cases: list[SpecGenCase],
    args: argparse.Namespace,
    status: str,
    error: str,
    trial_rows: dict[str, list[CaseRow]] | None = None,
) -> None:
    """Mark cases that were not worth running after a batch-level LLM failure."""

    for case in cases:
        if case.name in rows:
            continue
        partial_trials = trial_rows.get(case.name, []) if trial_rows is not None else []
        if partial_trials:
            rows[case.name] = aggregate_trial_rows(case, partial_trials)
        else:
            rows[case.name] = skipped_llm_error_row(case, args, status, error)


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
        source_preflight_status=representative.source_preflight_status,
        source_preflight_assert_failure=representative.source_preflight_assert_failure,
        source_preflight_output_path=representative.source_preflight_output_path,
        source_preflight_failure_reason=representative.source_preflight_failure_reason,
    )


def interrupted_run(output: Path, rows: dict[str, CaseRow], total: int) -> int:
    _STOP_RUN.set()
    kill_active_openjml_process_groups()
    write_report(output, list(rows.values()))
    print(f"interrupted: wrote partial report with {len(rows)}/{total} completed cases", flush=True)
    return 130


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


def cmd_annotate_report(args: argparse.Namespace) -> int:
    rows = list(load_completed(Path(args.input_report)).values())
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    source_preflight_reports = getattr(args, "source_preflight_report", [])
    if isinstance(source_preflight_reports, str):
        source_preflight_reports = [source_preflight_reports] if source_preflight_reports else []
    source_preflight_paths = [Path(path) for path in source_preflight_reports if path]
    write_report(output, rows, load_source_preflight_metadata(source_preflight_paths))
    summary = summarize(rows)
    print(f"annotated {len(rows)} row(s)")
    print(f"summary: {summary}")
    print(f"report: {output / 'report.json'}")
    print(f"summary_md: {output / 'summary.md'}")
    return 0


def cmd_overlay_report(args: argparse.Namespace) -> int:
    rows_by_case = load_completed(Path(args.input_report))
    replaced: dict[str, str] = {}
    preserve_base_trial_stats = bool(getattr(args, "preserve_base_trial_stats", False))
    for overlay_path_text in getattr(args, "overlay_report", []) or []:
        overlay_path = Path(overlay_path_text)
        overlay_rows = load_completed(overlay_path)
        for case, row in overlay_rows.items():
            if case in rows_by_case:
                if preserve_base_trial_stats:
                    base_row = rows_by_case[case]
                    row.attempts = base_row.attempts
                    row.trials = base_row.trials
                    row.trial_passes = base_row.trial_passes
                    row.trial_status_counts = base_row.trial_status_counts
                    row.iterations = base_row.iterations
                    row.runtime_s = base_row.runtime_s
                rows_by_case[case] = row
                replaced[case] = str(overlay_path)

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    source_preflight_reports = getattr(args, "source_preflight_report", [])
    if isinstance(source_preflight_reports, str):
        source_preflight_reports = [source_preflight_reports] if source_preflight_reports else []
    source_preflight_paths = [Path(path) for path in source_preflight_reports if path]
    rows = list(rows_by_case.values())
    if getattr(args, "require_clean_passing_jml", False):
        require_clean_passing_jml(rows)
    write_report(output, rows, load_source_preflight_metadata(source_preflight_paths))
    summary = summarize(rows)
    write_json(output / "overlay_sources.json", {"replaced": dict(sorted(replaced.items()))})
    print(f"overlayed {len(replaced)} row(s) from {len(getattr(args, 'overlay_report', []) or [])} report(s)")
    print(f"summary: {summary}")
    print(f"report: {output / 'report.json'}")
    print(f"summary_md: {output / 'summary.md'}")
    return 0


def cmd_audit_residuals(args: argparse.Namespace) -> int:
    rows = list(load_completed(Path(args.input_report)).values())
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    payload = build_residual_audit_payload(rows, str(args.input_report))
    write_json(output / "report.json", payload)
    write_residual_audit_md(output / "summary.md", payload)
    print(f"decision: {payload['decision']}")
    print(f"summary: {payload['summary']}")
    print(f"report: {output / 'report.json'}")
    print(f"summary_md: {output / 'summary.md'}")
    return 0


def cmd_source_preflight(args: argparse.Namespace) -> int:
    rows = select_report_rows(list(load_completed(Path(args.input_report)).values()), args)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    openjml_path = args.openjml_path or default_openjml_path()
    timeout_s = int(getattr(args, "openjml_timeout", 30) or 30)
    result_rows: list[dict[str, Any]] = []
    try:
        for index, row in enumerate(rows, 1):
            result = source_preflight_output_row(
                row,
                output=output,
                openjml_path=openjml_path,
                timeout_s=timeout_s,
            )
            result_rows.append(result)
            payload = {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "source_report": str(args.input_report),
                "mode": "source_preflight",
                "summary": summarize_source_preflight(result_rows),
                "rows": result_rows,
            }
            write_source_preflight_report(output, payload)
            print(
                "[{}/{}] {}: source={} passed={} assert={}".format(
                    index,
                    len(rows),
                    row.case,
                    result["status"],
                    result["passed"],
                    result["has_assert_failure"],
                ),
                flush=True,
            )
    except KeyboardInterrupt:
        kill_active_openjml_process_groups()
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source_report": str(args.input_report),
            "mode": "source_preflight",
            "interrupted": True,
            "summary": summarize_source_preflight(result_rows),
            "rows": result_rows,
        }
        write_source_preflight_report(output, payload)
        return 130

    summary = summarize_source_preflight(result_rows)
    print(f"summary: {summary}")
    print(f"report: {output / 'report.json'}")
    print(f"summary_md: {output / 'summary.md'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _STOP_RUN.clear()
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
            if c.name not in rows or not row_satisfies_requested_run(rows[c.name], trial_count)
        ]
    else:
        to_run = [c for c in cases if c.name not in rows]
    print(f"selected {len(cases)} case(s); running {len(to_run)}; output={output}")
    if not to_run:
        write_report(output, list(rows.values()))
        return 0

    if getattr(args, "preflight_source", False):
        config = configure(args)
        remaining: list[SpecGenCase] = []
        try:
            for case in to_run:
                row = preflight_source_case(case, args, config)
                if row is None:
                    remaining.append(case)
                    continue
                rows[row.case] = row
                write_report(output, list(rows.values()))
                print(f"{row.case}: {row.status} pass=False iters=0")
        except KeyboardInterrupt:
            return interrupted_run(output, rows, len(cases))
        to_run = remaining
        if not to_run:
            summary = summarize(list(rows.values()))
            print(f"summary: {summary}")
            print(f"report: {output / 'report.json'}")
            print(f"summary_md: {output / 'summary.md'}")
            return 0 if summary["passed"] > 0 else 1

    max_workers = max(1, int(args.workers))
    if trial_count > 1:
        trial_rows: dict[str, list[CaseRow]] = {case.name: [] for case in to_run}
        jobs = [(case, trial) for case in to_run for trial in range(1, trial_count + 1)]
        print(f"trial mode: {trial_count} trial(s) per case; running {len(jobs)} trial job(s)")
        if max_workers == 1:
            try:
                for case, trial in jobs:
                    row = run_one_trial(case, args, trial)
                    trial_rows[case.name].append(row)
                    print(f"{case.name} trial {trial}/{trial_count}: {row.status} pass={row.passed} iters={row.iterations}")
                    if len(trial_rows[case.name]) == trial_count:
                        rows[case.name] = aggregate_trial_rows(case, trial_rows[case.name])
                        write_report(output, list(rows.values()))
                    if is_batch_level_llm_error(row.status):
                        mark_remaining_llm_error(rows, to_run, args, row.status, row.error, trial_rows)
                        write_report(output, list(rows.values()))
                        break
            except KeyboardInterrupt:
                return interrupted_run(output, rows, len(cases))
        else:
            pool = futures.ThreadPoolExecutor(max_workers=max_workers)
            future_map = {
                pool.submit(run_one_trial, case, args, trial): (case, trial)
                for case, trial in jobs
            }
            try:
                for fut in futures.as_completed(future_map):
                    case, trial = future_map[fut]
                    row = fut.result()
                    trial_rows[case.name].append(row)
                    print(f"{case.name} trial {trial}/{trial_count}: {row.status} pass={row.passed} iters={row.iterations}")
                    if len(trial_rows[case.name]) == trial_count:
                        rows[case.name] = aggregate_trial_rows(case, trial_rows[case.name])
                        write_report(output, list(rows.values()))
                    if is_batch_level_llm_error(row.status):
                        for pending in future_map:
                            if not pending.done():
                                pending.cancel()
                        mark_remaining_llm_error(rows, to_run, args, row.status, row.error, trial_rows)
                        write_report(output, list(rows.values()))
                        break
            except KeyboardInterrupt:
                _STOP_RUN.set()
                for pending in future_map:
                    pending.cancel()
                kill_active_openjml_process_groups()
                pool.shutdown(wait=True, cancel_futures=True)
                return interrupted_run(output, rows, len(cases))
            else:
                pool.shutdown(wait=True)
        summary = summarize(list(rows.values()))
        print(f"summary: {summary}")
        print(f"report: {output / 'report.json'}")
        print(f"summary_md: {output / 'summary.md'}")
        return 0 if summary["passed"] > 0 else 1

    if max_workers == 1:
        try:
            for case in to_run:
                row = run_one(case, args)
                rows[row.case] = row
                write_report(output, list(rows.values()))
                print(f"{row.case}: {row.status} pass={row.passed} iters={row.iterations}")
                if is_batch_level_llm_error(row.status):
                    mark_remaining_llm_error(rows, to_run, args, row.status, row.error)
                    write_report(output, list(rows.values()))
                    break
        except KeyboardInterrupt:
            return interrupted_run(output, rows, len(cases))
    else:
        pool = futures.ThreadPoolExecutor(max_workers=max_workers)
        future_map = {pool.submit(run_one, case, args): case for case in to_run}
        try:
            for fut in futures.as_completed(future_map):
                row = fut.result()
                rows[row.case] = row
                write_report(output, list(rows.values()))
                print(f"{row.case}: {row.status} pass={row.passed} iters={row.iterations}")
                if is_batch_level_llm_error(row.status):
                    for pending in future_map:
                        if not pending.done():
                            pending.cancel()
                    mark_remaining_llm_error(rows, to_run, args, row.status, row.error)
                    write_report(output, list(rows.values()))
                    break
        except KeyboardInterrupt:
            _STOP_RUN.set()
            for pending in future_map:
                pending.cancel()
            kill_active_openjml_process_groups()
            pool.shutdown(wait=True, cancel_futures=True)
            return interrupted_run(output, rows, len(cases))
        else:
            pool.shutdown(wait=True)

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

    a = sub.add_parser("annotate-report", help="Add oracle/source diagnostics to an existing report without rerunning.")
    a.add_argument("--input-report", required=True, help="Existing report.json to annotate.")
    a.add_argument("--output", required=True, help="Output directory for the annotated report.")
    a.add_argument(
        "--source-preflight-report",
        action="append",
        default=[],
        help="Optional report.json from unannotated-source preflight; repeat to merge several diagnostics.",
    )
    a.set_defaults(func=cmd_annotate_report)

    o = sub.add_parser("overlay-report", help="Replace base rows with rows from one or more replay/updated reports.")
    o.add_argument("--input-report", required=True, help="Base report.json to update.")
    o.add_argument("--output", required=True, help="Output directory for the overlaid report.")
    o.add_argument(
        "--overlay-report",
        action="append",
        required=True,
        help="Report.json whose rows replace matching cases in the base report; repeat to apply several overlays.",
    )
    o.add_argument(
        "--source-preflight-report",
        action="append",
        default=[],
        help="Optional source-preflight report.json to merge before reclassification.",
    )
    o.add_argument(
        "--require-clean-passing-jml",
        action="store_true",
        help=(
            "Mark a passed case as failed unless it has a passing artifact with no "
            "generated JML assert/assume statements."
        ),
    )
    o.add_argument(
        "--preserve-base-trial-stats",
        action="store_true",
        help=(
            "When overlaying replay diagnostics, keep the base row's attempts, "
            "trial counts, iterations, and runtime while updating status/artifact fields."
        ),
    )
    o.set_defaults(func=cmd_overlay_report)

    audit = sub.add_parser(
        "audit-residuals",
        help="Summarize non-passing rows by actionability without rerunning LLM/OpenJML.",
    )
    audit.add_argument("--input-report", required=True, help="Existing report.json to audit.")
    audit.add_argument("--output", required=True, help="Output directory for the residual audit.")
    audit.set_defaults(func=cmd_audit_residuals)

    sp = sub.add_parser("source-preflight", help="Run OpenJML on unannotated sources from an existing report.")
    sp.add_argument("--input-report", required=True, help="Existing report.json whose rows provide source paths.")
    sp.add_argument("--output", required=True, help="Output directory for the preflight report.")
    sp.add_argument("--cases", nargs="*", default=None, help="Optional case names to include.")
    sp.add_argument("--statuses", nargs="*", default=None, help="Optional generated statuses to include.")
    sp.add_argument("--failure-classes", nargs="*", default=None, help="Optional generated failure classes to include.")
    sp.add_argument("--failure-reasons", nargs="*", default=None, help="Optional generated failure reasons to include.")
    sp.add_argument("--limit", type=int, default=None, help="Limit after filtering.")
    sp.add_argument("--openjml-path", default="", help="Path to OpenJML binary.")
    sp.add_argument("--openjml-timeout", type=int, default=30, help="OpenJML timeout per unannotated source.")
    sp.set_defaults(func=cmd_source_preflight)

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
        "--preflight-source",
        action="store_true",
        help="Run OpenJML on the unannotated source first and skip cases with source-level frontend/tool errors.",
    )
    r.add_argument(
        "--preflight-timeout",
        type=int,
        default=20,
        help="OpenJML timeout for --preflight-source; short by default so valid hard cases still proceed to generation.",
    )
    r.add_argument(
        "--source-preflight-feedback",
        action="store_true",
        help=(
            "Run OpenJML on the unannotated source and include compact source-level "
            "verifier feedback in the first generation prompt. This is advisory "
            "and does not skip cases."
        ),
    )
    r.add_argument(
        "--prompt-examples",
        default="none",
        help="Optional few-shot examples: 'none', 'specgen-4shot', 'specgen-4shot-linked', or a text file to prepend.",
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
