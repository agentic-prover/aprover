#!/usr/bin/env python3
"""Collect recorded results without inferring safety from an empty report list."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def collect(output: Path) -> dict:
    manifest = json.loads((output / "run_manifest.json").read_text())
    executions = json.loads((output / "execution.json").read_text()) if (output / "execution.json").exists() else []
    result = {"profile": manifest["profile"], "pilot": manifest["pilot"],
              "execution_kind": manifest["execution_kind"], "source_commit": manifest["source"]["commit"],
              "selected_cases": len(manifest["cases"]), "trials_per_case": manifest["trials"],
              "wall_s_sum": sum(e["wall_s"] for e in executions), "process_returncodes": [e["returncode"] for e in executions]}
    if manifest["settings"]["kind"] == "rq1":
        invocation_log = output / "cbmc_invocations.jsonl"
        observed = []
        for line in invocation_log.read_text().splitlines() if invocation_log.exists() else []:
            command = json.loads(line)["command"]
            if "--unwind" not in command:
                continue  # e.g. a version probe
            observed.append({"unwind": int(command[command.index("--unwind") + 1]),
                             "arch": "ILP32" if "--32" in command else "LP64"})
        expected = manifest["settings"]
        matches = bool(observed) and all(r["unwind"] == expected.get("unwind") and r["arch"] == expected.get("arch") for r in observed)
        result["solver_configuration"] = {"invocations": len(observed), "observed": observed,
                                          "matches_profile": matches}
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
            rows.append({"case": case["name"], "expected": case["expected_verdict"], "verdict": verdict,
                         "bounded_property_status": bounded_status,
                         "unwinding_complete": (None if manifest["settings"]["strategy"] != "faithful_main"
                                                else False if any(".unwind." in name for name in failed_properties)
                                                else True if (clean or failed_properties) and not errors else None),
                         "pipeline_report_count": report_count, "reach_error_properties": sorted(set(candidates)),
                         "solver_errors": errors, **execution})
        result.update(rows=rows, verdict_counts=dict(Counter(r["verdict"] for r in rows)))
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
