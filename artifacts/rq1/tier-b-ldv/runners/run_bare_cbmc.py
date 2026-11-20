import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

PROPERTY = "reach_error.assertion.1"
INPUT_LOCK = Path(__file__).resolve().parents[4] / "experiments/paper_reproduction/benchmark_inputs.json"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure(case, args, resources):
    source = Path(case["source"])
    peak_path = resources / (source.name + ".peak_kib.txt")
    command = [
        "/usr/bin/time", "-q", "-f", "%M", "-o", str(peak_path),
        "timeout", "--kill-after=5s", f"{args.timeout}s",
        "prlimit", f"--as={args.memory_kib * 1024}:{args.memory_kib * 1024}", "--",
        str(args.cbmc), "--32", "--function", "main", "--unwind", str(args.unwind),
        "--property", PROPERTY, "--json-ui", "--verbosity", "4", str(source),
    ]
    started = time.monotonic()
    completed = subprocess.run(command, capture_output=True, text=True, errors="replace")
    elapsed = time.monotonic() - started
    code = completed.returncode
    target_status = ""
    try:
        blocks = json.loads(completed.stdout)
    except json.JSONDecodeError:
        blocks = []
    for block in blocks:
        for result in block.get("result", ()):
            if result.get("property") == PROPERTY:
                if target_status:
                    raise ValueError(f"duplicate result for {PROPERTY}: {case['name']}")
                target_status = result["status"]

    messages = completed.stderr + completed.stdout
    if code == 124 or (code == 137 and elapsed >= args.timeout):
        outcome = "timeout"
    elif code != 0 and re.search(r"out of memory|bad_alloc|cannot allocate memory|memory exhausted", messages, re.I):
        outcome = "out_of_memory"
    elif code == 10 and target_status == "FAILURE":
        outcome = "target_violation"
    elif code == 0 and target_status == "SUCCESS":
        outcome = "bounded_no_target_violation"
    else:
        outcome = "tool_error"

    return {
        "task": case["name"], "input_sha256": case["sha256"],
        "property_status": target_status, "outcome": outcome,
        "returncode": code, "wall_seconds": round(elapsed, 3),
        "peak_rss_kib": peak_path.read_text().strip() if peak_path.exists() else "",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--cbmc", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--unwind", type=int, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--memory-kib", type=int, required=True)
    parser.add_argument("--workers", type=int, required=True)
    args = parser.parse_args()
    locked_hashes = json.loads(INPUT_LOCK.read_text())["rq1-ldv"]
    cases = [
        {"name": task, "source": args.inputs / f"{task}.i", "sha256": digest}
        for task, digest in locked_hashes.items()
    ]
    for case in cases:
        if sha256(case["source"]) != case["sha256"]:
            raise ValueError(f"LDV input does not match the selected benchmark: {case['name']}")
    version = subprocess.check_output([str(args.cbmc), "--version"], text=True).strip()
    if version.split()[0] != "5.95.1":
        raise ValueError(f"expected CBMC 5.95.1, got {version}")

    args.out.mkdir()
    resources = args.out / "resources"
    resources.mkdir()
    (args.out / "settings.json").write_text(json.dumps({
        "suite": "ldv-linux-3.4-simple", "property": PROPERTY, "data_model": "ILP32",
        "cbmc": str(args.cbmc), "cbmc_version": version, "cbmc_sha256": sha256(args.cbmc),
        "inputs": str(args.inputs), "unwind": args.unwind,
        "timeout_seconds": args.timeout, "address_space_limit_kib": args.memory_kib,
        "workers": args.workers, "start_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n")

    with (args.out / "results.tsv").open("w", newline="") as report:
        fields = ["task", "input_sha256", "property_status", "outcome", "returncode", "wall_seconds", "peak_rss_kib"]
        writer = csv.DictWriter(report, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        report.flush()
        print(f"Started {len(cases)} tasks with {args.workers} workers", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(measure, case, args, resources) for case in cases]
            for index, future in enumerate(as_completed(futures), 1):
                row = future.result()
                writer.writerow(row)
                report.flush()
                print(f"{index}/{len(cases)} {row['outcome']} {row['wall_seconds']}s", flush=True)


if __name__ == "__main__":
    main()
