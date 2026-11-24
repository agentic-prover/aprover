#!/usr/bin/env python3
"""Export fully revalidated archived proof inputs as a portable offline bundle.

The bundle reproduces verification of recorded successes, not LLM generation
rates. It includes only source inputs, flags, coverage and verifier provenance.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def encoded(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def gather(run: Path, kind: str) -> tuple[dict, list, dict]:
    plan = json.loads((run / "replay_plan.json").read_text())
    result = json.loads((run / "replay_results.json").read_text())
    if not result.get("complete") or result["passed"] != result["requested_inputs"]:
        raise ValueError(f"proof validation is incomplete or has failures: {run}")
    passed = {r["fingerprint"] for r in result["rows"] if r["passed"]}
    if passed != {r["fingerprint"] for r in plan["inputs"]}:
        raise ValueError("validated inputs do not match the planned input set")
    files, portable, coverage = {}, [], defaultdict(set)
    for row in plan["inputs"]:
        fingerprint = row["fingerprint"]
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("invalid input fingerprint")
        source_name = Path(row["source"]).name
        hashes = row["files_sha256"] if kind == "jml" else {source_name: row["sha256"]}
        prefix = f"inputs/{kind}/{fingerprint}"
        for name, expected in hashes.items():
            if Path(name).name != name:
                raise ValueError("expected a plain source filename")
            data = (run / "inputs" / fingerprint / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"validated source changed: {fingerprint}/{name}")
            files[prefix + "/" + name] = data
        portable.append({**row, "source": prefix + "/" + source_name})
        for item in row["covers"]:
            coverage[item.get("run", "acsl")].add((item["case"], item["trial"]))
    summary = {"unique_inputs": len(portable), "tool_version": plan["tool_version"],
               "validation_source_commit": plan["source_commit"],
               "coverage": {name: {"accepted_cases": len({case for case, _ in pairs}),
                                   "accepted_trials": len(pairs)} for name, pairs in coverage.items()}}
    return files, portable, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acsl-run", type=Path, required=True)
    parser.add_argument("--jml-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new bundle filename")
    files, summary = {}, {}
    for kind, run in (("acsl", args.acsl_run), ("jml", args.jml_run)):
        sources, manifest, details = gather(run, kind)
        files.update(sources)
        files[kind + "_manifest.json"] = encoded(manifest)
        summary[kind] = details
    summary.update(kind="archived_accepted_proof_bundle", model_calls=0, fresh_generation=False,
                   source_commit=subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
                   limitation="Verifies archived accepted specifications; does not reproduce model generation or historical failure outcomes.",
                   benchmark_sources={"AutoSpec": "https://zenodo.org/records/10912658",
                                      "SpecGen": "https://github.com/Lezhi-Ma/SpecGen-Artifact/tree/3b8355f60807dbdd9019c26b39bfe0bad2ec96a6"},
                   framac_image="framac/frama-c@sha256:bda451103345fdd1e2e57262bf3c6ff36090ce819a03c163b24f637d4dd8e053")
    files["validation_summary.json"] = encoded(summary)
    files["README.txt"] = b"""Archived BMC-Agent RQ3 accepted proof inputs

This bundle contains revalidated source inputs and flags, with hashes and original
trial coverage. It makes zero model calls. It does not rerun generation or verify
the original failed trials, and must not be presented as fresh generation scores.
Original benchmark copyright/license comments remain in the source files.

Extract to any directory. From the reproduction checkout, run:
python experiments/paper_reproduction/replay_verifiers.py jml \\
  --manifest /path/to/bundle/jml_manifest.json --tool /path/to/openjml \\
  --output artifacts/bundle-replay/jml --workers 4 --fail-fast --execute
python experiments/paper_reproduction/replay_verifiers.py acsl \\
  --manifest /path/to/bundle/acsl_manifest.json \\
  --tool experiments/paper_reproduction/tools/frama-c \\
  --output artifacts/bundle-replay/acsl --workers 4 --fail-fast --execute

Omit --execute to inspect only the selected-input plan.
"""
    files["files_sha256.json"] = encoded({name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as archive:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                archive.addfile(info, io.BytesIO(data))
    print(json.dumps({"bundle": str(args.output), "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                      "files": len(files), "bytes": args.output.stat().st_size}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
