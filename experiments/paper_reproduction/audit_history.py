#!/usr/bin/env python3
"""Read local Git history and experiment evidence; never run a solver or model."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def history(repo: Path, *args: str) -> list[dict]:
    rows = []
    text = git(repo, "log", "--all", "--format=%H%x09%aI%x09%cI%x09%s", *args)
    for line in text.splitlines():
        sha, author, committer, subject = line.split("\t", 3)
        files = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
        rows.append({"commit": sha, "author_date": author, "committer_date": committer,
                     "subject": subject, "changed_files": files})
    return rows


def functions(text: str) -> dict[str, str]:
    result = {}
    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                result[prefix + node.name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + ".")
    visit(ast.parse(text).body)
    return result


def compare(repo: Path, base: str, head: str, paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        a = git(repo, "show", base + ":" + path)
        b = git(repo, "show", head + ":" + path)
        fa, fb = functions(a), functions(b)
        rows.append({"path": path, "byte_identical": a == b,
                     "base_sha256": hashlib.sha256(a.encode()).hexdigest(),
                     "head_sha256": hashlib.sha256(b.encode()).hexdigest(),
                     "changed_functions": sorted(k for k in fa.keys() & fb.keys() if fa[k] != fb[k]),
                     "added_functions": sorted(fb.keys() - fa.keys()),
                     "removed_functions": sorted(fa.keys() - fb.keys())})
    return rows


def log_window(paths: list[Path]) -> dict:
    dates, markers = [], {}
    rx = re.compile(r"\[(\d{2}/\d{2}/\d{2}) (\d{2}:\d{2}:\d{2})\]")
    for path in paths:
        content = path.read_text(errors="replace")
        dates.extend(datetime.strptime(a + " " + b, "%m/%d/%y %H:%M:%S").isoformat()
                     for a, b in rx.findall(content))
        for marker in ["Scope-from-entry", "confirmed_system_entry", "INLINE lock-writer", "CEGAR-refine"]:
            if marker in content:
                markers[marker] = markers.get(marker, 0) + 1
    return {"log_count": len(paths), "first_log_time": min(dates) if dates else None,
            "last_log_time": max(dates) if dates else None,
            "timezone": "not encoded in logs; timestamps reported verbatim", "markers_in_logs": markers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aprover", type=Path, required=True)
    parser.add_argument("--private-artifacts", type=Path, required=True)
    parser.add_argument("--anonymous-artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = ["bmc_agent/jml_specs.py", "experiments/specgen_compare/run_bmc_jml_specgen.py",
             "bmc_agent/loop_invariants.py", "bmc_agent/assert_driven_specs.py", "bmc_agent/frama_c.py",
             "bmc_agent/acsl.py", "bmc_agent/dsl_to_cbmc.py", "bmc_agent/cli.py", "bmc_agent/llm.py"]
    private = args.private_artifacts
    rq1 = private / "findings/rq1-svcomp"
    data = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "scope": "BMC-Agent RQ1 and RQ3; archival commits are not execution versions",
        "repository_heads": {"aprover_local": git(args.aprover, "rev-parse", "HEAD").strip(),
                             "private_artifacts_local": git(private, "rev-parse", "HEAD").strip(),
                             "anonymous_artifacts_local": git(args.anonymous_artifacts, "rev-parse", "HEAD").strip()},
        "artifact_commit_timeline": history(private),
        "anonymous_commit_timeline": history(args.anonymous_artifacts),
        "aprover_relevant_commit_timeline": history(args.aprover, "--since=2026-06-15", "--until=2026-07-24", "--",
            "bmc_agent/pipeline.py", "bmc_agent/harness_generator.py", "bmc_agent/cli.py", "bmc_agent/loop_invariants.py",
            "bmc_agent/jml_specs.py", "bmc_agent/llm.py", "bmc_agent/config.py", "experiments/specgen_compare"),
        "acsl_1528d0d_to_candidate_base_149177d": compare(args.aprover, "1528d0d", "149177d", paths),
        "jml_fcad091_to_candidate_base_149177d": compare(args.aprover, "fcad091", "149177d", paths[:2] + ["bmc_agent/llm.py"]),
        "acsl_1528d0d_to_main_b9314c7": compare(args.aprover, "1528d0d", "b9314c7", paths),
        "rq1_aws": log_window(sorted((rq1 / "tier-a-aws/raw/bmc-agent").glob("*.log"))),
        "rq1_ldv": log_window(sorted((rq1 / "tier-b-ldv/raw/bmc-agent-examples").glob("*.log"))),
        "version_inferences": [
            {"experiment": "RQ1 AWS27", "candidate": "4569fa1-era scope-from-entry implementation, possibly with private CEGAR changes",
             "confidence": "medium for code family; exact SHA unrecorded",
             "archive_commit": "15e3d3424988d919f6413afba7e482ca63f1d3dc",
             "reason": "June 29 logs contain scope-from-entry; first reachable public implementation is June 24 4569fa1; original runner uses /home/syc/AProver_cegar"},
            {"experiment": "RQ1 LDV29", "candidate": "June 28 private /home/syc/AProver_cegar snapshot; public 44a3cbf/149177d are later compatibility candidates",
             "confidence": "medium for architecture; no exact public revision",
             "reason": "lock-writer CEGAR markers appear in archived logs; BMC_FRAME_HAVOC first appears in public reachable history on July 5; no matching lock-writer log string was found"},
            {"experiment": "RQ3 ACSL251", "candidate": "1528d0dcd9faecbc5b3fd40dc203aec38453af89 + recovered untracked experiment helpers",
             "confidence": "explicit artifact metadata", "archive_commit": "7210967598ef07a7f00e2da52c990451a51a71cb"},
            {"experiment": "RQ3 JML four settings", "candidate": "fcad091a344645f1e4930f31ce7b63507653187e + recovered local changes",
             "confidence": "high, from original worktree/cache/prompt evidence; not a pristine commit",
             "archive_commit": "9bc9a1c9f7ee36aafd2f4b0b64efb980008461c9"}
        ],
        "limits": ["Chronological proximity does not prove a tool revision.",
                   "AST equality of selected functions does not prove equality of the entire dynamic dependency closure.",
                   "Model routing, budgets, architecture and uncommitted changes remain part of the experiment configuration."]
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "private_commits": len(data["artifact_commit_timeline"]),
                      "aprover_commits": len(data["aprover_relevant_commit_timeline"]),
                      "rq1_aws": data["rq1_aws"], "rq1_ldv": data["rq1_ldv"]}, indent=2))


if __name__ == "__main__":
    main()
