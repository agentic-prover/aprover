#!/usr/bin/env python3
"""Recheck hash-locked historical verifier inputs, without any model requests.

The input manifest is emitted by audit_published.py. This replays verification,
not specification generation; its scores must not be reported as fresh RQ3 runs.
Without --execute, write only the selected-input plan.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_sources(row: dict, kind: str) -> list[Path]:
    source = Path(row["source"])
    hashes = row["files_sha256"] if kind == "jml" else {source.name: row["sha256"]}
    paths = []
    for name, expected in hashes.items():
        if Path(name).name != name:
            raise ValueError("expected a plain source filename")
        path = source.parent / name
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"historical verifier input missing or changed: {path}")
        paths.append(path)
    if source not in paths:
        raise ValueError("main verifier source is not included in input hashes")
    return paths


def wp_result(raw: str, returncode: int, timed_out: bool) -> dict:
    matches = re.findall(r"\[wp\]\s+Proved goals:\s+(\d+)\s*/\s*(\d+)", raw)
    proved, goals = map(int, matches[-1]) if matches else (0, 0)
    invalid = any(x in raw.lower() for x in ("annot-error", "user error", "syntax error"))
    passed = not timed_out and not invalid and goals > 0 and proved == goals
    return {"passed": passed, "proved": proved, "goals": goals,
            "status": "passed" if passed else "timeout" if timed_out else "not_proved",
            "returncode": returncode}


def run_framac(command: list[str], cwd: Path) -> tuple[str, int, bool]:
    proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            raw, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            raw, _ = proc.communicate()
    return raw, proc.returncode, timed_out


def replay(row: dict, kind: str, tool: str, output: Path) -> dict:
    sources = checked_sources(row, kind)
    case_dir = output / "inputs" / row["fingerprint"]
    case_dir.mkdir(parents=True, exist_ok=True)
    for path in sources:
        shutil.copyfile(path, case_dir / path.name)
    source = case_dir / Path(row["source"]).name
    start = time.monotonic()
    if kind == "jml":
        from bmc_agent.jml_specs import build_openjml_command, run_openjml
        # Refuse a historical flag set this backend would silently replace.
        expected = [x for x in build_openjml_command(tool, source, 200)[1:] if not x.endswith(".java")]
        if expected != row["flags"]:
            raise ValueError("historical OpenJML flags differ from the pinned backend")
        result = asdict(run_openjml(source, openjml_path=tool, timeout_s=200, cwd=case_dir))
        raw = result.pop("stdout") + "\n" + result.pop("stderr")
    else:
        command = [tool, *row["flags"], str(source.relative_to(ROOT))]
        raw, code, timed_out = run_framac(command, ROOT)
        result = {**wp_result(raw, code, timed_out), "command": command}
        result["goal_count_matches_recorded"] = result["goals"] == row["goals"]
        result["passed"] = result["passed"] and result["goal_count_matches_recorded"]
    (case_dir / "verifier.log").write_text(raw)
    result.update(fingerprint=row["fingerprint"], case=row["case"], covers=row["covers"],
                  wall_s=round(time.monotonic() - start, 3), log=str(case_dir / "verifier.log"))
    (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"case": row["case"], "passed": result["passed"], "wall_s": result["wall_s"]}), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["jml", "acsl"])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--run-contains", help="Filter JML rows by original run name, e.g. claude46")
    parser.add_argument("--one-per-case", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first batch with an unverified input.")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.workers < 1 or args.limit is not None and args.limit < 1:
        parser.error("workers and limit must be positive")
    rows = json.loads(args.manifest.read_text())
    if args.run_contains:
        rows = [{**r, "covers": [c for c in r["covers"] if args.run_contains in c.get("run", "")]}
                for r in rows if any(args.run_contains in c.get("run", "") for c in r["covers"])]
    if args.cases:
        missing = set(args.cases) - {r["case"] for r in rows}
        if missing:
            parser.error("unknown cases: " + ", ".join(sorted(missing)))
        rows = [r for r in rows if r["case"] in args.cases]
    if args.one_per_case:
        unique = {}
        for row in rows:
            unique.setdefault(row["case"], row)
        rows = list(unique.values())
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        parser.error("no selected inputs")
    # Check every selected input before launching any verification.
    for row in rows:
        checked_sources(row, args.kind)
    output = args.output.resolve()
    if args.kind == "acsl" and not output.is_relative_to(ROOT):
        parser.error("ACSL output must be inside this checkout for the pinned container mount")
    if (output / "replay_results.json").exists():
        parser.error("use a new output directory; existing replay results are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    tool = shutil.which(args.tool) or str(Path(args.tool).resolve())
    plan = {"kind": "historical_verifier_replay", "backend": args.kind,
            "model_calls": 0, "fresh_generation": False,
            "manifest_sha256": digest(args.manifest), "inputs": rows, "tool": tool,
            "tool_sha256": digest(Path(tool)) if Path(tool).is_file() else None,
            "source_commit": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()}
    (output / "replay_plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    if not args.execute:
        print(json.dumps({"planned_inputs": len(rows), "model_calls": 0, "execute": False}))
        return 0
    flag, expected = ("-version", "0.17.0-alpha-15") if args.kind == "jml" else ("--version", "26.0")
    version = subprocess.run([tool, flag], capture_output=True, text=True, timeout=40)
    version_output = (version.stdout + version.stderr).strip()
    if version.returncode or expected not in version_output:
        raise ValueError(f"expected verifier {expected}; got {version_output[:300]}")
    plan["tool_version"] = version_output
    (output / "replay_plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for start in range(0, len(rows), args.workers):
            batch = list(pool.map(lambda row: replay(row, args.kind, tool, output), rows[start:start + args.workers]))
            results.extend(batch)
            report = {"kind": "historical_verifier_replay", "model_calls": 0, "fresh_generation": False,
                      "requested_inputs": len(rows), "complete": len(results) == len(rows),
                      "inputs": len(results), "passed": sum(r["passed"] for r in results), "rows": results}
            checkpoint = output / "replay_results.json.tmp"
            checkpoint.write_text(json.dumps(report, indent=2) + "\n")
            checkpoint.replace(output / "replay_results.json")
            if args.fail_fast and any(not r["passed"] for r in batch):
                break
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    return 0 if report["complete"] and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
