#!/usr/bin/env python3
"""Recompute published RQ3 counts from per-trial evidence, without model calls.

This is an evidence audit, not a fresh generation experiment. It also emits a
deduplicated manifest of accepted verifier inputs for subsequent solver replay.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re

JML_RUNS = [
    ("specgenbench_bmc_jml_claude46_temp04_10trial_20260623", "specgenbench/claude_sonnet_4_6"),
    ("specgenbench_bmc_jml_gpt35_temp04_10trial_20260623", "specgenbench/gpt_3_5_turbo_1106"),
    ("svcomp_java_bmc_jml_claude46_temp04_10trial_20260623", "svcomp_java/claude_sonnet_4_6"),
    ("svcomp_java_bmc_jml_gpt35_temp04_10trial_20260623", "svcomp_java/gpt_3_5_turbo_1106"),
]


def read(path):
    return json.loads(path.read_text())


def evidence_path(recorded: str, run_root: Path) -> Path:
    """Resolve original absolute or repository-relative paths after an archive move."""
    path = Path(recorded)
    if run_root.name in path.parts:
        index = path.parts.index(run_root.name)
        return run_root.joinpath(*path.parts[index + 1:])
    if path.is_file():
        return path
    for parent in (run_root, *run_root.parents):
        candidate = parent / path
        if candidate.is_file():
            return candidate
    return path


def verifier_snapshot(path: Path, command: list[str]) -> dict:
    files = sorted(path.parent.glob("*.java"))
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    flags = [x for x in command[1:] if not x.endswith(".java")]
    digest = hashlib.sha256(json.dumps([path.name, hashes, flags], sort_keys=True).encode()).hexdigest()
    return {"fingerprint": digest, "source": str(path), "files_sha256": hashes, "flags": flags}


def audit_jml(run_root: Path, artifact: Path, leaf: str) -> tuple[dict, list]:
    rows = read(run_root / "report.json")["rows"]
    expected = read(artifact / leaf / "metadata.json")
    passing_cases, passing_trials, total = 0, 0, 0
    errors, missing_failure_records, accepted = [], [], []
    for row in rows:
        passes = 0
        trials = row.get("trial_rows", [])
        if len(trials) != 10:
            errors.append({"case": row["case"], "reason": "not ten recorded trial rows"})
        for number, trial in enumerate(trials, 1):
            total += 1
            report = evidence_path(trial.get("report_path") or "", run_root)
            if not report.is_file():
                missing_failure_records.append({"case": row["case"], "trial": number, "status": trial.get("status")})
                if trial.get("passed"):
                    errors.append({"case": row["case"], "trial": number, "reason": "missing passing trial evidence"})
                continue
            data = read(report)
            passed = data.get("passed") is True and data.get("status") == "passed"
            if passed != bool(trial.get("passed")):
                errors.append({"case": row["case"], "trial": number, "reason": "aggregate row contradicts trial JSON"})
            if passed:
                recorded_path = data["final_annotated_path"]
                path = evidence_path(recorded_path, run_root)
                final = next((x for x in reversed(data.get("iterations", [])) if x.get("annotated_path") == recorded_path), None)
                if not path.is_file() or not final or not final.get("source_preserved") or not final.get("openjml", {}).get("passed"):
                    errors.append({"case": row["case"], "trial": number, "reason": "accepted proof/source evidence incomplete"})
                    continue
                if final.get("annotated_source") != path.read_text():
                    errors.append({"case": row["case"], "trial": number, "reason": "saved verifier input differs from recorded successful source"})
                    continue
                accepted.append({"case": row["case"], "trial": number,
                                 **verifier_snapshot(path, final["openjml"]["command"])})
                passes += 1
        passing_trials += passes
        passing_cases += bool(passes)
    computed = {"cases_total": len(rows), "cases_passed": passing_cases, "trial_total": total, "trial_passes": passing_trials}
    target = {k: expected[k] for k in computed}
    return {"run": run_root.name, "computed": computed, "published": target, "counts_match": computed == target,
            "evidence_errors": errors, "missing_failure_records": missing_failure_records,
            "fresh_solver_replay_completed": False}, accepted


def audit_acsl(run_root: Path, artifact: Path) -> tuple[dict, list]:
    rows = read(run_root / "report.json")["rows"]
    expected = read(artifact / "metadata.json")
    total, passes, passing_cases = 0, 0, 0
    errors, accepted_inputs = [], []
    for row in rows:
        case_passes = 0
        attempts = row.get("trials", [])
        if len(attempts) != 5:
            errors.append({"case": row["case_id"], "reason": "not five recorded trials"})
        for attempt in attempts:
            total += 1
            pc = attempt.get("postcheck", {})
            log = evidence_path(pc.get("log") or "", run_root)
            if not log.is_file():
                errors.append({"case": row["case_id"], "trial": attempt.get("attempt"), "reason": "missing WP log"})
                continue
            raw = log.read_text(errors="replace")
            matches = re.findall(r"\[wp\]\s+Proved goals:\s+(\d+)\s*/\s*(\d+)", raw)
            complete = bool(matches and int(matches[-1][1]) > 0 and int(matches[-1][0]) == int(matches[-1][1]))
            accepted = pc.get("status") == "pass"
            if accepted and not complete:
                errors.append({"case": row["case_id"], "trial": attempt.get("attempt"), "reason": "pass lacks complete WP goal discharge"})
            if accepted and complete:
                source = evidence_path(attempt.get("annotated_or_instrumented") or "", run_root)
                if not source.is_file():
                    errors.append({"case": row["case_id"], "trial": attempt.get("attempt"), "reason": "missing accepted verification source"})
                    continue
                accepted_inputs.append({"case": row["case_id"], "trial": attempt.get("attempt"),
                                        "source": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                        "proved": int(matches[-1][0]), "goals": int(matches[-1][1]),
                                        "flags": ["-wp", "-wp-precond-weakening", "-wp-no-callee-precond",
                                                  "-wp-prover", "Alt-Ergo,Z3", "-wp-print", "-wp-timeout", "8"]})
            case_passes += accepted and complete
        passes += case_passes
        passing_cases += bool(case_passes)
    computed = {"cases_total": len(rows), "cases_passed": passing_cases, "trial_total": total, "trial_passes": passes}
    target = {k: expected[k] for k in computed}
    return {"computed": computed, "published": target, "counts_match": computed == target,
            "evidence_errors": errors, "fresh_solver_replay_completed": False}, accepted_inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jml-runs-root", type=Path, required=True)
    parser.add_argument("--acsl-run", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-scope", choices=["claude", "all"], default="claude",
                        help="GPT work is deferred by default; all also audits its historical files.")
    args = parser.parse_args()
    reports, unique = [], {}
    selected_runs = JML_RUNS if args.model_scope == "all" else [r for r in JML_RUNS if "claude46" in r[0]]
    for name, leaf in selected_runs:
        report, accepted = audit_jml(args.jml_runs_root / name,
                                     args.artifact_root / "rq3/jml/bmc_agent", leaf)
        reports.append(report)
        for row in accepted:
            key = row["fingerprint"]
            if key not in unique:
                unique[key] = {**row, "covers": []}
            unique[key]["covers"].append({"run": name, "case": row["case"], "trial": row["trial"]})
    acsl, acsl_inputs = audit_acsl(
        args.acsl_run,
        args.artifact_root / "rq3/acsl/bmc_agent/autospec_251/claude_sonnet_4_6",
    )
    acsl_unique = {}
    for row in acsl_inputs:
        key = row["sha256"]
        if key not in acsl_unique:
            acsl_unique[key] = {**row, "fingerprint": key, "covers": []}
        acsl_unique[key]["covers"].append({"case": row["case"], "trial": row["trial"]})
    result = {"kind": "recorded_evidence_audit", "model_scope": args.model_scope, "jml": reports, "acsl": acsl,
              "unique_accepted_jml_inputs": len(unique),
              "unique_accepted_acsl_inputs": len(acsl_unique),
              "all_recorded_counts_match": all(x["counts_match"] for x in reports + [acsl]),
              "no_evidence_errors": not any(x["evidence_errors"] for x in reports + [acsl]),
              "limitation": "Recorded-trial audit does not replace fresh model generation or solver execution."}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "published_counts_audit.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.output / "jml_accepted_replay_manifest.json").write_text(json.dumps(list(unique.values()), indent=2) + "\n")
    (args.output / "acsl_accepted_replay_manifest.json").write_text(json.dumps(list(acsl_unique.values()), indent=2) + "\n")
    print(json.dumps({"counts_match": result["all_recorded_counts_match"], "no_evidence_errors": result["no_evidence_errors"],
                      "unique_jml_inputs": len(unique), "jml": [r["computed"] for r in reports], "acsl": acsl["computed"]}, indent=2))
    return 0 if result["all_recorded_counts_match"] and result["no_evidence_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
