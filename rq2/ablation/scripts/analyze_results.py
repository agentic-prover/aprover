#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import csv
from collections import Counter
from pathlib import Path
from typing import Any


def required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return Path(value).expanduser().resolve()


ROOT = required_path("BMC_RESULTS_ROOT")
MODEL = os.environ.get("BMC_RESULTS_MODEL", "claude-sonnet-4-6")
UPSTREAM_PROVIDER = os.environ.get("BMC_RESULTS_UPSTREAM_PROVIDER", "")
TEACHER_MANIFEST = required_path("BMC_TEACHER_MANIFEST")
MANUAL_LABELS = required_path("BMC_MANUAL_LABELS")
ARMS = tuple(
    os.environ.get(
        "BMC_RESULTS_ARMS", "full no_dynamic no_realism fixed_config"
    ).split()
)
MODULES = tuple(
    os.environ.get("BMC_RESULTS_MODULES", "elf fb memory string").split()
)


def property_class(prop: str) -> str:
    parts = prop.split(".")
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[-2]
    return parts[-1]


def load_reports(run_root: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in run_root.glob("artifacts/**/bug_reports/*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        report = data.get("report")
        if not isinstance(report, dict):
            continue
        prop = report.get("violated_property")
        func = report.get("function_name")
        confidence = report.get("confidence")
        if not prop or not func or not confidence:
            continue
        if str(prop).startswith("("):
            continue
        realism = report.get("realism_check")
        realism_verdict = (
            realism.get("verdict") if isinstance(realism, dict) else None
        )
        entry = {
            "path": str(path),
            "module": run_root.name,
            "function": str(func),
            "target_function": str(prop).split(".", 1)[0],
            "property": str(prop),
            "property_class": property_class(str(prop)),
            "confidence": str(confidence),
            "dynamic_outcome": report.get("dynamic_outcome"),
            "realism_verdict": realism_verdict,
            "realism_confidence": (
                realism.get("llm_confidence") if isinstance(realism, dict) else None
            ),
            "cex_outcome": report.get("cex_outcome"),
        }
        entry["survives"] = bool(
            confidence != "unlikely"
            and (
                realism_verdict is None
                or str(realism_verdict).lower() == "realistic"
            )
        )
        entry["meaningful"] = bool(
            entry["survives"] and ".unwind." not in str(prop).lower()
        )
        reports.append(entry)
    return reports


def parse_run_log(path: Path) -> dict[str, int]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""
    flat = re.sub(r"\s+", " ", text)
    specs = [int(x) for x in re.findall(r"Phase 1 complete: (\d+) specs generated", flat)]
    verdicts = [int(x) for x in re.findall(r"Phase 2 complete: (\d+) verdicts", flat)]
    end = re.findall(
        r"AMC Pipeline END: (\d+) real bug\(s\), (\d+) latent, (\d+) unresolved",
        flat,
    )
    return {
        "specs": specs[-1] if specs else 0,
        "verdicts": verdicts[-1] if verdicts else 0,
        "pipeline_end_real": int(end[-1][0]) if end else 0,
        "pipeline_end_latent": int(end[-1][1]) if end else 0,
        "pipeline_end_unresolved": int(end[-1][2]) if end else 0,
        "dynamic_confirmed": len(
            re.findall(
                r"(?:System-entry )?[Dd]ynamic validation for '[^']+':\s*confirmed",
                flat,
            )
        ),
        "dynamic_not_triggered": len(
            re.findall(
                r"(?:System-entry )?[Dd]ynamic validation for '[^']+':\s*not_triggered",
                flat,
            )
        ),
        "realism_realistic": len(re.findall(r"Realism check for '[^']+': verdict=realistic", flat)),
        "realism_unrealistic": len(re.findall(r"Realism check for '[^']+': verdict=unrealistic", flat)),
        "refinement_iterations": len(re.findall(r"Refinement iteration \d+/\d+ for", flat)),
        "harness_rebuilt": len(re.findall(r"agentic harness repair: rebuilding harness for", flat)),
        "harness_fixed": len(re.findall(r"agentic harness repair resolved the build error for", flat)),
    }


def ledger_metrics(proxy_root: Path) -> dict[str, Any]:
    log = proxy_root / "llm_calls.jsonl"
    records: list[dict[str, Any]] = []
    if log.exists():
        for line in log.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    statuses = Counter(str(r.get("status")) for r in records)
    models = Counter(str(r.get("response_model")) for r in records)
    providers = Counter(str(r.get("provider")) for r in records if r.get("provider"))
    return {
        "calls": len(records),
        "statuses": dict(statuses),
        "models": dict(models),
        "providers": dict(providers),
        "invalid": sum(1 for r in records if r.get("status") != 200),
        "denials": (proxy_root / "denials.jsonl").exists(),
        "cost_usd": sum(float(r.get("estimated_cost_usd") or 0) for r in records),
        "input_tokens": sum(
            int((r.get("usage") or {}).get("input_tokens") or 0) for r in records
        ),
        "output_tokens": sum(
            int((r.get("usage") or {}).get("output_tokens") or 0) for r in records
        ),
        "cache_write_tokens": sum(
            int((r.get("usage") or {}).get("cache_creation_input_tokens") or 0)
            for r in records
        ),
        "cache_read_tokens": sum(
            int((r.get("usage") or {}).get("cache_read_input_tokens") or 0)
            for r in records
        ),
    }


def coverage_metrics(run_root: Path, module: str) -> dict[str, int]:
    path = (
        run_root
        / "artifacts"
        / f"vibeos_{module}"
        / "coverage_diagnostics.json"
    )
    if not path.exists():
        return {"produced_verdict": 0, "failed_before_verdict": 0}
    data = json.loads(path.read_text())
    return {
        "produced_verdict": int(data.get("produced_verdict") or 0),
        "failed_before_verdict": int(data.get("failed_before_verdict") or 0),
    }


def load_teacher() -> tuple[list[dict[str, Any]], dict[tuple[str, str, str], list[dict[str, Any]]]]:
    manifest = json.loads(TEACHER_MANIFEST.read_text())
    findings = [
        f for f in manifest["findings"] if f.get("module") in MODULES
    ]
    by_loose: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for f in findings:
        key = (f["module"], f["function"], property_class(f["property"]))
        by_loose.setdefault(key, []).append(f)
    return findings, by_loose


def load_manual_labels() -> tuple[
    dict[tuple[str, str, str], dict[str, str]],
    dict[tuple[str, str, str], list[dict[str, str]]],
]:
    exact: dict[tuple[str, str, str], dict[str, str]] = {}
    loose: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    with MANUAL_LABELS.open(newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            if row["module"] not in MODULES:
                continue
            exact_key = (row["module"], row["function"], row["property"])
            loose_key = (
                row["module"],
                row["function"],
                property_class(row["property"]),
            )
            exact[exact_key] = row
            loose.setdefault(loose_key, []).append(row)
    return exact, loose


def main() -> None:
    teacher, teacher_loose = load_teacher()
    manual_exact, manual_loose = load_manual_labels()
    rows: list[dict[str, Any]] = []
    all_reports: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for arm in ARMS:
        for module in MODULES:
            run_root = ROOT / "runs" / arm / module
            reports = load_reports(run_root)
            all_reports[(arm, module)] = reports
            log_metrics = parse_run_log(run_root / "run.log")
            coverage = coverage_metrics(run_root, module)
            usage = ledger_metrics(ROOT / "proxies" / f"{arm}-{module}")
            survivors = [r for r in reports if r["survives"]]
            meaningful = [r for r in reports if r["meaningful"]]
            exact = {
                (r["module"], r["property"])
                for r in survivors
            }
            teacher_exact = {
                (f["module"], f["property"]) for f in teacher
            }
            teacher_loose_keys = set(teacher_loose)
            loose = {
                (r["module"], r["target_function"], r["property_class"])
                for r in survivors
            }
            teacher_label_matches = Counter()
            manual_confirmed_hits: set[tuple[str, str, str]] = set()
            manual_confirmed_exact_hits: set[tuple[str, str, str]] = set()
            manual_confirmed_loose_hits: set[tuple[str, str, str]] = set()
            manual_conditional_hits: set[tuple[str, str, str]] = set()
            manual_fp_hits: set[tuple[str, str, str]] = set()
            unlabeled: list[dict[str, Any]] = []
            for r in survivors:
                key = (r["module"], r["target_function"], r["property_class"])
                for f in teacher_loose.get(key, []):
                    teacher_label_matches[f.get("my_verdict", "UNKNOWN")] += 1
            for r in meaningful:
                exact_key = (r["module"], r["target_function"], r["property"])
                loose_key = (
                    r["module"],
                    r["target_function"],
                    r["property_class"],
                )
                exact_match = exact_key in manual_exact
                matches = (
                    [manual_exact[exact_key]]
                    if exact_match
                    else manual_loose.get(loose_key, [])
                )
                if not matches:
                    unlabeled.append(r)
                    continue
                labels = {m["label"] for m in matches}
                label_keys = {
                    (m["module"], m["function"], m["property"]) for m in matches
                }
                if "confirmed" in labels:
                    manual_confirmed_hits.update(label_keys)
                    if exact_match:
                        manual_confirmed_exact_hits.update(label_keys)
                    else:
                        manual_confirmed_loose_hits.update(label_keys)
                elif "conditional" in labels:
                    manual_conditional_hits.update(label_keys)
                elif "not_counted" in labels:
                    manual_fp_hits.update(label_keys)
            wall_s = 0
            status_file = run_root / "status.tsv"
            reported_status = None
            if status_file.exists():
                lines = status_file.read_text().splitlines()
                if len(lines) >= 2:
                    fields = lines[1].split("\t")
                    reported_status = int(fields[2])
                    wall_s = int(fields[3])
            pipeline_complete = bool(
                re.search(
                    r"AMC Pipeline END:",
                    (run_root / "run.log").read_text(errors="replace"),
                )
            )
            model_ok = set(usage["models"]) == {MODEL}
            provider_ok = (
                not UPSTREAM_PROVIDER
                or set(usage["providers"]) == {UPSTREAM_PROVIDER}
            )
            valid = bool(
                usage["invalid"] == 0
                and not usage["denials"]
                and model_ok
                and provider_ok
                and pipeline_complete
            )
            rows.append(
                {
                    "arm": arm,
                    "module": module,
                    "wall_s": wall_s,
                    "reported_status": reported_status,
                    "pipeline_complete": pipeline_complete,
                    "model_ok": model_ok,
                    "provider_ok": provider_ok,
                    "valid": valid,
                    "reports": len(reports),
                    "survivors": len(survivors),
                    "meaningful_survivors": len(meaningful),
                    "unwind_artifacts": len(survivors) - len(meaningful),
                    "unique_survivors": len(
                        {(r["function"], r["property"]) for r in survivors}
                    ),
                    "rejected_persisted": len(reports) - len(survivors),
                    "exact_teacher_matches": len(exact & teacher_exact),
                    "loose_teacher_matches": len(loose & teacher_loose_keys),
                    "teacher_label_matches": dict(teacher_label_matches),
                    "manual_confirmed_hits": len(manual_confirmed_hits),
                    "manual_confirmed_exact_hits": len(manual_confirmed_exact_hits),
                    "manual_confirmed_loose_hits": len(manual_confirmed_loose_hits),
                    "manual_conditional_hits": len(manual_conditional_hits),
                    "manual_fp_reemitted": len(manual_fp_hits),
                    "manual_unlabeled": len(
                        {
                            (r["function"], r["property"])
                            for r in unlabeled
                        }
                    ),
                    "final_dynamic": sum(
                        1
                        for r in meaningful
                        if str(r.get("dynamic_outcome") or "").lower() == "confirmed"
                    ),
                    "final_system_entry": sum(
                        1
                        for r in meaningful
                        if str(r.get("confidence") or "") == "confirmed_system_entry"
                    ),
                    "final_bmc": sum(
                        1
                        for r in meaningful
                        if str(r.get("confidence") or "") == "confirmed_bmc"
                    ),
                    **log_metrics,
                    **coverage,
                    **usage,
                }
            )
            (run_root / "status_corrected.tsv").write_text(
                "arm\tmodule\treported_status\tvalid\teffective_rc\t"
                "pipeline_complete\tvalid_llm_calls\tcost_usd\twall_s\n"
                f"{arm}\t{module}\t{reported_status}\t{str(valid).lower()}\t0\t"
                f"{str(pipeline_complete).lower()}\t{usage['calls']}\t"
                f"{usage['cost_usd']:.6f}\t{wall_s}\n"
            )

    (ROOT / "metrics.json").write_text(
        json.dumps(
            {
                "teacher": teacher,
                "rows": rows,
                "reports": {
                    f"{arm}/{module}": reports
                    for (arm, module), reports in all_reports.items()
                },
            },
            indent=2,
        )
    )
    with (ROOT / "validity.tsv").open("w", newline="") as stream:
        stream.write(
            "arm\tmodule\treported_status\tvalid\tpipeline_complete\t"
            "calls\tinvalid\tmodel_ok\tprovider_ok\tdenials\tcost_usd\twall_s\n"
        )
        for row in rows:
            stream.write(
                f"{row['arm']}\t{row['module']}\t{row['reported_status']}\t"
                f"{str(row['valid']).lower()}\t{str(row['pipeline_complete']).lower()}\t"
                f"{row['calls']}\t{row['invalid']}\t{str(row['model_ok']).lower()}\t"
                f"{str(row['provider_ok']).lower()}\t"
                f"{str(row['denials']).lower()}\t{row['cost_usd']:.6f}\t{row['wall_s']}\n"
            )
    with (ROOT / "final_findings.tsv").open("w", newline="") as stream:
        stream.write(
            "arm\tmodule\tfunction\tproperty\tconfidence\tdynamic_outcome\t"
            "realism\tmanual_label\tmatch_type\n"
        )
        for arm in ARMS:
            for module in MODULES:
                for report in all_reports[(arm, module)]:
                    if not report["meaningful"]:
                        continue
                    exact_key = (
                        report["module"],
                        report["target_function"],
                        report["property"],
                    )
                    loose_key = (
                        report["module"],
                        report["target_function"],
                        report["property_class"],
                    )
                    if exact_key in manual_exact:
                        match = manual_exact[exact_key]
                        match_type = "exact"
                    elif loose_key in manual_loose:
                        match = manual_loose[loose_key][0]
                        match_type = "loose"
                    else:
                        match = None
                        match_type = "unlabeled"
                    stream.write(
                        f"{arm}\t{module}\t{report['target_function']}\t"
                        f"{report['property']}\t{report['confidence']}\t"
                        f"{report['dynamic_outcome'] or ''}\t"
                        f"{report['realism_verdict'] or ''}\t"
                        f"{match['label'] if match else ''}\t{match_type}\n"
                    )

    print("arm\tmodule\twall_s\tcalls\tinvalid\tdenials\tcost_usd\tverdicts\tproduced\tfailed_before_verdict\tsurvivors\tmeaningful\trealistic\tunrealistic\tdynamic\tfinal_dynamic\tmanual_confirmed_exact\tmanual_confirmed_loose\tmanual_conditional\tmanual_fp\tunlabeled")
    for row in rows:
        print(
            f"{row['arm']}\t{row['module']}\t{row['wall_s']}\t{row['calls']}\t"
            f"{row['invalid']}\t{str(row['denials']).lower()}\t{row['cost_usd']:.4f}\t"
            f"{row['verdicts']}\t{row['produced_verdict']}\t"
            f"{row['failed_before_verdict']}\t"
            f"{row['survivors']}\t{row['meaningful_survivors']}\t"
            f"{row['realism_realistic']}\t"
            f"{row['realism_unrealistic']}\t{row['dynamic_confirmed']}\t"
            f"{row['final_dynamic']}\t"
            f"{row['manual_confirmed_exact_hits']}\t{row['manual_confirmed_loose_hits']}\t"
            f"{row['manual_conditional_hits']}\t"
            f"{row['manual_fp_reemitted']}\t{row['manual_unlabeled']}"
        )

    print("\nSURVIVING PROPERTIES")
    for arm in ARMS:
        props = sorted(
            {
                (r["module"], r["function"], r["property"], r["confidence"], r["dynamic_outcome"])
                for r in sum((all_reports[(arm, m)] for m in MODULES), [])
                if r["meaningful"]
            }
        )
        print(f"\n{arm} ({len(props)})")
        for prop in props:
            print("\t".join(str(x) for x in prop))


if __name__ == "__main__":
    main()
