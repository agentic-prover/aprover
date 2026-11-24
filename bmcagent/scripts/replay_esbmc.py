#!/usr/bin/env python3
"""Replay archived C harnesses on ESBMC without importing or calling an LLM.

Requires an original invocation whose harness SHA-256 matches the saved file.
Use --compare-cbmc to rerun that exact recorded CBMC command as a paired check.
Every result records source identity, budgets, commands and backend versions.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmc_agent.cbmc import CBMCResult, _parse_cbmc_output
from bmc_agent.esbmc import run_esbmc


def recorded_options(command: list[str]) -> dict:
    """Translate explicitly recorded query semantics, rejecting unknown switches."""
    options = {"arch": "LP64", "partial_loops": False, "apply_env_policy": False,
               "include_dirs": [], "defines": []}
    boolean = {
        "--unsigned-overflow-check": "unsigned_overflow_check",
        "--signed-overflow-check": "signed_overflow_check",
        "--conversion-check": "conversion_check",
        "--pointer-overflow-check": "pointer_overflow_check",
        "--undefined-shift-check": "undefined_shift_check",
        "--pointer-check": "pointer_check", "--bounds-check": "bounds_check",
        "--div-by-zero-check": "div_by_zero_check",
    }
    values = {"--unwind": "unwind", "--function": "function", "--object-bits": "object_bits"}
    # --refine/--slice-formula select CBMC's encoding strategy, not the query's
    # bounds, target property or checks. ESBMC uses its own SMT encoder/slicing.
    engine_only = {"--json-ui", "--refine", "--slice-formula", "--unwinding-assertions"}
    args = iter(command[2:])
    for arg in args:
        if arg in boolean:
            options[boolean[arg]] = True
        elif arg in values:
            value = next(args)
            options[values[arg]] = value if arg == "--function" else int(value)
        elif arg in ("-I", "-D"):
            value = next(args)
            if arg == "-I" and not Path(value).is_absolute():
                raise ValueError("relative include path needs the original working directory")
            options["include_dirs" if arg == "-I" else "defines"].append(value)
        elif arg == "--32":
            options["arch"] = "ILP32"
        elif arg == "--64":
            options["arch"] = "LP64"
        elif arg == "--partial-loops":
            options["partial_loops"] = True
        elif arg not in engine_only:
            raise ValueError(f"unmapped recorded option: {arg}")
    if "unwind" not in options:
        raise ValueError("recorded command has no explicit unwind bound")
    if not options["partial_loops"] and "--unwinding-assertions" not in command:
        raise ValueError("recorded command has no explicit supported unwinding policy")
    return options


def match_invocation(harness: Path, rows: list[dict]) -> tuple[int, dict]:
    digest = hashlib.sha256(harness.read_bytes()).hexdigest()
    candidates = [(i, row) for i, row in enumerate(rows) if row.get("harness_sha256") == digest]
    same_path = [(i, row) for i, row in candidates if Path(row["command"][1]).resolve() == harness.resolve()]
    if same_path:
        return same_path[-1]
    if candidates:
        # Permit a relocated copy, recording the exact original source below.
        return candidates[-1]
    raise ValueError("saved harness hash does not match any recorded invocation; refusing an unmatched comparison")


def verdict(result: CBMCResult) -> str:
    if result.error:
        return "error_or_inconclusive"
    if any(c.failure_location.get("function") == "reach_error" or
           c.failing_property.startswith("reach_error.assertion.") for c in result.counterexamples):
        return "target_counterexample"
    if result.counterexamples and all(".unwind." in c.failing_property for c in result.counterexamples):
        return "unwind_only"
    if result.counterexamples:
        return "other_counterexample"
    return "no_counterexample" if result.verified else "error_or_inconclusive"


def version(binary: str) -> str:
    proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    return (proc.stdout + proc.stderr).strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, action="append", type=Path)
    parser.add_argument("--invocations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--esbmc-path", default="esbmc")
    parser.add_argument("--compare-cbmc", action="store_true")
    parser.add_argument("--cbmc-path", help="Optional executable override for the paired CBMC run")
    parser.add_argument("--allow-object-encoding-change", action="store_true",
                        help="Explicitly drop CBMC-only object_bits for ESBMC and record the difference")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Same wall-clock cap per backend; recorded unwind/checks are preserved")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    # Never overwrite an earlier experiment's evidence.
    args.output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in args.invocations.read_text().splitlines() if line.strip()]
    root = Path(__file__).resolve().parents[1]
    code_files = ("scripts/replay_esbmc.py", "bmc_agent/esbmc.py", "bmc_agent/cbmc.py")
    summary = {"created_at": datetime.now(timezone.utc).isoformat(), "llm_calls": 0,
               "llm_cost_usd": 0, "invocations_file": str(args.invocations.resolve()),
               "code_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                               for name in code_files},
               "esbmc_version": version(args.esbmc_path), "cases": []}
    for number, source in enumerate(args.harness):
        source = source.resolve()
        case = {"harness": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        summary["cases"].append(case)
        dest = args.output / f"case-{number:03d}"
        dest.mkdir()
        try:
            index, row = match_invocation(source, rows)
            options = recorded_options(row["command"])
        except (ValueError, StopIteration) as exc:
            case["replay_error"] = str(exc)
            continue
        case.update({"original_invocation_index": index, "original_invocation": row,
                     "recorded_options": dict(options), "options": options, "timeout_s": args.timeout})
        if args.allow_object_encoding_change and "object_bits" in options:
            case["encoding_difference"] = {
                "cbmc_object_bits": options.pop("object_bits"),
                "esbmc": "native SMT object encoding; no equivalent object-bits option",
            }
        (dest / "source.c").write_bytes(source.read_bytes())
        started = time.monotonic()
        result = run_esbmc(source, esbmc_path=args.esbmc_path, timeout=args.timeout,
                           evidence_dir=dest / "esbmc", **options)
        case["esbmc"] = {"verdict": verdict(result), "elapsed_s": time.monotonic() - started,
                         "error": result.error, "properties": [c.failing_property for c in result.counterexamples]}
        (dest / "esbmc_result.json").write_text(json.dumps(asdict(result), indent=2))
        if args.compare_cbmc:
            command = list(row["command"])
            command[0] = args.cbmc_path or command[0]
            command[1] = str(source)
            case["cbmc_version"] = version(command[0])
            started = time.monotonic()
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
                result = _parse_cbmc_output(proc.stdout, proc.stderr, proc.returncode)
                (dest / "cbmc_stdout.json").write_text(proc.stdout)
                (dest / "cbmc_stderr.log").write_text(proc.stderr)
            except subprocess.TimeoutExpired:
                result = CBMCResult(False, error=f"cbmc timed out after {args.timeout}s")
            except OSError as exc:
                result = CBMCResult(False, error=f"cbmc OS error: {exc}")
            case["cbmc"] = {"verdict": verdict(result), "elapsed_s": time.monotonic() - started,
                            "error": result.error, "command": command,
                            "properties": [c.failing_property for c in result.counterexamples]}
            (dest / "cbmc_result.json").write_text(json.dumps(asdict(result), indent=2))
        if hashlib.sha256(source.read_bytes()).hexdigest() != case["sha256"]:
            case["replay_error"] = "source changed during replay; comparison is invalid"
        print(json.dumps({"source": str(source), "esbmc": case["esbmc"]["verdict"],
                          "cbmc": case.get("cbmc", {}).get("verdict")}))
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    return int(any("replay_error" in case or case.get("esbmc", {}).get("error") for case in summary["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
