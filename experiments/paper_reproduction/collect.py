#!/usr/bin/env python3
"""Collect recorded results without inferring safety from an empty report list."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


LDV_TARGET = "reach_error.assertion.1"


def guarded_ldv_evidence(case_dir: Path, execution: dict, candidates: list, errors: list) -> dict:
    """Read the original LDV target's saved classification, not other assertions.

    Evidence flags are independent of process completion. A stopped case retains
    any evidence already saved, but is never counted as a completed miss.
    """
    main = case_dir / "artifacts/agdrv/main"
    classification_path = main / "classifications" / f"{LDV_TARGET}.json"
    report_path = main / "bug_reports" / f"{LDV_TARGET}.json"
    classification = json.loads(classification_path.read_text()) if classification_path.exists() else {}
    classification = classification.get("classification", classification)
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    report = report.get("report", report)
    candidate = (LDV_TARGET in candidates
                 or classification.get("counterexample", {}).get("failing_property") == LDV_TARGET
                 or report.get("violated_property") == LDV_TARGET)
    # These are the feasibility-specific fields persisted by the validator.
    # A real_bug label alone can also come from other validation paths.
    confirmed = (candidate and classification.get("outcome") == "real_bug"
                 and report.get("violated_property") == LDV_TARGET
                 and report.get("confidence") == "confirmed_system_entry"
                 and "Callee feasibility confirmed" in str(classification.get("reasoning", "")))
    unresolved = candidate and classification.get("outcome") == "unresolved"
    tool_errors = list(errors)
    for pattern in ("**/bug_report.json", "**/bug_reports/*.json"):
        for path in (case_dir / "artifacts").glob(pattern):
            payload = json.loads(path.read_text())
            body = payload.get("report", payload)
            if (isinstance(body, dict) and body.get("error")
                    and any(k in body for k in ("function_name", "harness_path", "verified"))):
                tool_errors.append(str(body["error"]))
    tool_errors = sorted(set(tool_errors))
    if not execution:
        verdict = "not_run"
    elif execution.get("completion") == "case_timeout" or execution.get("returncode") in (124, 137):
        verdict = "timeout"
    elif execution.get("returncode") is None or execution["returncode"] < 0:
        verdict = "censored"
    elif execution["returncode"] != 0:
        verdict = "tool_error"
    elif confirmed:
        verdict = "target_feasibility_confirmed"
    elif unresolved:
        verdict = "unresolved"
    elif tool_errors:
        verdict = "tool_error"
    elif candidate:
        verdict = "target_candidate"
    else:
        verdict = "no_target_candidate"
    return {"verdict": verdict, "target_property": LDV_TARGET,
            "target_candidate": candidate, "target_feasibility_confirmed": confirmed,
            "target_unresolved": unresolved, "target_outcome": classification.get("outcome"),
            "dynamic_outcome": (classification.get("dynamic_result") or {}).get("outcome"),
            "solver_errors": tool_errors}


def final_report_counts(log: str) -> tuple[int | None, int | None]:
    """Read the CLI's post-filter results, not the reporter's stale-file summary.

    Missing/truncated CLI output is unknown, never an inferred zero. Counting
    bug_report.json files would be wrong: they can survive realism rejection or
    reclassification, and multiple reports can be saved for one defect.
    """
    summaries = list(re.finditer(
        r"^=== Results ===\r?\n"
        r"(?:\(suppressed (?P<suppressed>\d+) stale finding\(s\):[^\r\n]*\)\r?\n)?"
        r"(?:Confirmed bugs: (?P<count>\d+)|No bugs confirmed\.)[ \t]*\r?$",
        log, flags=re.MULTILINE))
    if not summaries:
        return None, None
    summary = summaries[-1]
    return int(summary["count"] or 0), int(summary["suppressed"] or 0)


def collect(output: Path) -> dict:
    manifest = json.loads((output / "run_manifest.json").read_text())
    executions = json.loads((output / "execution.json").read_text()) if (output / "execution.json").exists() else []
    result = {"profile": manifest["profile"], "pilot": manifest["pilot"],
              "execution_kind": manifest["execution_kind"], "source_commit": manifest["source"]["commit"],
              "selected_cases": len(manifest["cases"]), "trials_per_case": manifest["trials"],
              "wall_s_sum": sum(e["wall_s"] for e in executions), "process_returncodes": [e["returncode"] for e in executions]}
    if manifest["settings"]["kind"] == "rq1":
        backend = manifest.get("c_backend", "cbmc")  # Historical manifests used CBMC exclusively.
        if backend not in ("cbmc", "esbmc"):
            raise ValueError(f"unknown RQ1 C backend: {backend}")
        invocation_log = output / f"{backend}_invocations.jsonl"
        observed = []
        for line in invocation_log.read_text().splitlines() if invocation_log.exists() else []:
            command = json.loads(line)["command"]
            if "--unwind" not in command:
                continue  # e.g. a version probe
            observed.append({"unwind": int(command[command.index("--unwind") + 1]),
                             "arch": "ILP32" if "--32" in command else "LP64"})
        expected = manifest["settings"]
        policy = expected.get("unwind_policy", "fixed")
        if policy == "agent_may_override":
            # LDV guarded mode: the configuration agent may raise the per-function
            # unwind, so a run at unwind=2 is NOT a configuration error. What must
            # still hold is the architecture, a non-empty invocation set, that no
            # cap/floor pulled any query BELOW the profile's default, AND that the
            # recorded environment itself carries no cap/floor: checking only the
            # observed values would accept a run whose environment set
            # BMC_UNWIND_CAP (at the default) or an SV-COMP floor, i.e. exactly the
            # regression this profile must catch.
            values = sorted({r["unwind"] for r in observed})
            default = int(expected.get("unwind", 1))
            env = manifest.get("environment") or {}
            leaks = []
            for key in ("BMC_UNWIND_CAP", "BMC_TRANSITIVE_INLINE"):
                if key in env:
                    leaks.append(f"{key}={env[key]!r}")
            for key in ("SVCOMP_UNWIND", "SVCOMP_TIMEOUT"):
                # ABSENT is a leak, not a pass: with SVCOMP_PROP=unreach the
                # production defaults are 64 (unwind floor) and 800 (timeout
                # floor) when the variable is missing (cbmc.py:122-131). Only an
                # explicit "0" disables the floor.
                if env.get(key) is None or str(env.get(key)).strip() != "0":
                    leaks.append(f"{key}={env.get(key)!r} (must be explicit '0')")
            if str(env.get("BMC_AGENT_CBMC_UNWIND", "")).strip() != str(default):
                leaks.append(f"BMC_AGENT_CBMC_UNWIND={env.get('BMC_AGENT_CBMC_UNWIND')!r} "
                             f"!= default {default}")
            matches = (bool(observed)
                       and all(r["arch"] == expected.get("arch") for r in observed)
                       and all(v >= default for v in values)
                       and not leaks)
            result["solver_configuration"] = {
                "invocations": len(observed), "observed": observed,
                "unwinds": values, "unwind_policy": policy,
                "unwind_default": default, "environment_leaks": leaks,
                "matches_profile": matches}
        else:
            matches = bool(observed) and all(r["unwind"] == expected.get("unwind") and r["arch"] == expected.get("arch") for r in observed)
            result["solver_configuration"] = {"invocations": len(observed), "observed": observed,
                                              "matches_profile": matches}
        result["solver_configuration"]["backend"] = backend
        rows = []
        for case in manifest["cases"]:
            case_dir = output / "cases" / case["name"]
            log = (case_dir / "run.log").read_text(errors="replace") if (case_dir / "run.log").exists() else ""
            execution = next((e for e in executions if e["name"] == case["name"]), {})
            candidates, clean, errors, failed_properties = [], False, [], []
            for path in (case_dir / "artifacts").rglob("*cbmc_result.json"):
                data = json.loads(path.read_text())
                data = data.get("result", data)
                if data.get("error"):
                    errors.append(str(data["error"]))
                if data.get("verified") is True and not data.get("error"):
                    clean = True
                for cex in data.get("counterexamples", []):
                    failed_properties.append(cex.get("failing_property", ""))
                    if "reach_error" in cex.get("failing_property", ""):
                        candidates.append(cex["failing_property"])
            counts = re.findall(r"Pipeline END: (\d+) real bug", log)
            report_count = int(counts[-1]) if counts else None
            final_count, suppressed_count = final_report_counts(log)
            if not execution:
                verdict = "not_run"
            elif execution["returncode"] in (124, 137):
                verdict = "timeout"
            elif candidates:
                verdict = ("abstract_bug_candidate" if manifest["settings"]["strategy"] in ("frame_havoc", "public_ldv_fallback")
                           else "reach_error_counterexample")
            elif errors:
                verdict = "solver_error"
            elif clean:
                verdict = ("abstract_no_violation" if manifest["settings"]["strategy"] in ("frame_havoc", "public_ldv_fallback")
                           else "bounded_no_violation")
            else:
                verdict = "unknown"
            bounded_status = None
            if manifest["settings"]["strategy"] == "faithful_main":
                if verdict == "reach_error_counterexample":
                    bounded_status = "reach_error_counterexample"
                elif verdict == "bounded_no_violation":
                    bounded_status = "no_reach_error_within_bound"
                elif (execution.get("returncode") == 0 and not errors and failed_properties
                      and all(".unwind." in name for name in failed_properties)):
                    bounded_status = "no_reach_error_within_bound_incomplete_unwinding"
            guarded = (guarded_ldv_evidence(case_dir, execution, candidates, errors)
                       if manifest["settings"]["strategy"] == "ldv_property_guard" else {})
            rows.append({"case": case["name"], "expected": case["expected_verdict"], "verdict": verdict,
                         "bounded_property_status": bounded_status,
                         "unwinding_complete": (None if manifest["settings"]["strategy"] != "faithful_main"
                                                else False if any(".unwind." in name for name in failed_properties)
                                                else True if (clean or failed_properties) and not errors else None),
                         # Keep the historical pre-CLI-filter count for compatibility.
                         "pipeline_report_count": report_count,
                         "final_report_count": final_count,
                         "final_filter_suppressed_report_count": suppressed_count,
                         "pipeline_completed": execution.get("returncode") == 0 and final_count is not None,
                         "reach_error_properties": sorted(set(candidates)),
                         "solver_errors": errors, **execution, **guarded})
        result.update(rows=rows, verdict_counts=dict(Counter(r["verdict"] for r in rows)))
        result["report_counts"] = {
            "cases_with_reach_error_witness": sum(bool(r["reach_error_properties"]) for r in rows),
            "pipeline_completed_cases": sum(r["pipeline_completed"] for r in rows),
            "cases_with_final_reports": sum((r["final_report_count"] or 0) > 0 for r in rows),
            "cases_missing_final_report_count": sum(r["final_report_count"] is None for r in rows),
            "observed_final_reports": sum(r["final_report_count"] or 0 for r in rows),
            "observed_final_filter_suppressed_reports": sum(r["final_filter_suppressed_report_count"] or 0 for r in rows),
        }
        if manifest["settings"]["strategy"] == "ldv_property_guard":
            result["target_evidence_counts"] = {
                "target_candidate": sum(r["target_candidate"] for r in rows),
                "target_feasibility_confirmed": sum(r["target_feasibility_confirmed"] for r in rows),
                "unresolved": sum(r["target_unresolved"] for r in rows)}
        if manifest["settings"]["strategy"] == "faithful_main":
            result["bounded_property_counts"] = dict(Counter(r["bounded_property_status"] or "unknown" for r in rows))
    else:
        report = output / "results/report.json"
        if report.exists():
            data = json.loads(report.read_text())
            result["summary"] = data.get("summary")
            result["reported_cases"] = len(data.get("rows", []))
    if manifest["execution_kind"] == "saved_response_replay":
        result["billed_cost_usd"] = 0.0
        log = Path(manifest.get("replay_log") or "")
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []
        prefix = urlsplit(manifest["environment"]["BMC_AGENT_LLM_DEFAULT_BASE_URL"]).path.rstrip("/") + "/"
        calls = [c for c in calls if c["path"].startswith(prefix)]
        result["replay_fidelity"] = {"calls": len(calls), "cache_hits": sum(c["status"] == 200 for c in calls),
                                     "cache_misses": sum(c["status"] != 200 for c in calls),
                                     "verified": bool(calls) and all(c["status"] == 200 for c in calls)}
    elif manifest.get("call_log") and Path(manifest["call_log"]).exists():
        url = manifest["environment"]["BMC_AGENT_LLM_DEFAULT_BASE_URL"]
        match = re.search(r"/openrouter/([^/]+)/", url)
        calls = [json.loads(line) for line in Path(manifest["call_log"]).read_text().splitlines()]
        calls = [c for c in calls if match and c["tag"] == match[1]]
        result["llm"] = {"calls": len(calls), "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in calls),
                         "completion_tokens": sum(c.get("completion_tokens") or 0 for c in calls),
                         "cached_tokens": sum(c.get("cached_tokens") or 0 for c in calls),
                         "billed_cost_usd": sum(c.get("cost_usd") or 0 for c in calls),
                         "usage_complete": all(c.get("cost_usd") is not None for c in calls),
                         "http_status_counts": dict(Counter(c["status"] for c in calls))}
    else:
        result["llm"] = {"usage_complete": False, "reason": "no recording proxy call log supplied"}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.output), indent=2))
