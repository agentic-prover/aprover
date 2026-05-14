#!/usr/bin/env python3
"""Triage helper for bmc-agent bounty runs.

Walks an artifact directory and prints, for each REALISTIC and UNCERTAIN
realism-check finding, the data needed for manual triage:
  - function name, failing property + description, source location
  - LLM realism key_concern and reasoning
  - claimed source-line guard (REQ-1)
  - claimed public-API call chain (REQ-2)
  - dynamic validation outcome (signal triggered?)

Usage:  python triage_helper.py <artifact_root>
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path


def find_findings(root: Path) -> list[tuple[Path, dict, dict]]:
    """Walk root, return (dir, classification, bug_report) tuples for every
    function with a classification.json."""
    out: list[tuple[Path, dict, dict]] = []
    for cls_path in root.rglob("classification.json"):
        d = cls_path.parent
        rep_path = d / "bug_report.json"
        if not rep_path.exists():
            continue
        try:
            cls = json.load(cls_path.open())
            rep = json.load(rep_path.open())
        except Exception:
            continue
        out.append((d, cls.get("classification") or {}, rep.get("report") or {}))
    return out


def format_finding(d: Path, classification: dict, report: dict) -> str:
    rc = report.get("realism_check") or {}
    verdict = rc.get("verdict", "")
    cex = classification.get("counterexample") or {}
    loc = (cex.get("failure_location") or {})

    bug_type = report.get("bug_type", "")
    confidence = report.get("confidence", "")
    prop = report.get("violated_property", "")
    desc = cex.get("description", "")
    chain = classification.get("caller_path", []) or []
    dyn = classification.get("dynamic_result") or {}

    parts: list[str] = []
    parts.append(f"=== {d.parent.name}::{d.name} ===")
    parts.append(f"  prop: {prop}")
    if desc:
        parts.append(f"  desc: {desc}")
    if loc:
        parts.append(f"  loc:  {loc.get('file','?')}:{loc.get('line','?')}")
    parts.append(f"  bug_type:  {bug_type}")
    parts.append(f"  confidence: {confidence}")
    parts.append(f"  realism:    {verdict}  (LLM-conf: {rc.get('llm_confidence','?')})")
    parts.append(f"  call_chain: {' → '.join(chain) if chain else '(none)'}")
    if dyn:
        parts.append(f"  dynamic:    outcome={dyn.get('outcome','?')} signal={dyn.get('signal_name','-')}")
    rc_reason = rc.get("reasoning", "")
    if rc_reason:
        parts.append(f"  REALISM REASONING:")
        for ln in rc_reason.splitlines()[:30]:
            parts.append(f"    {ln}")
    return "\n".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: triage_helper.py <artifact_root>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    findings = find_findings(root)
    realistic = []
    uncertain = []
    for d, c, r in findings:
        v = (r.get("realism_check") or {}).get("verdict", "")
        if v == "realistic":
            realistic.append((d, c, r))
        elif v == "uncertain":
            uncertain.append((d, c, r))
    print(f"Found {len(findings)} classifications; "
          f"{len(realistic)} REALISTIC, {len(uncertain)} UNCERTAIN")
    print()
    if realistic:
        print("### REALISTIC findings ###\n")
        for d, c, r in realistic:
            print(format_finding(d, c, r))
            print()
    if uncertain:
        print("### UNCERTAIN findings ###\n")
        for d, c, r in uncertain:
            print(format_finding(d, c, r))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
