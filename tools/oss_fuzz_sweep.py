#!/usr/bin/env python3
"""Single-project OSS-Fuzz sweep with K2 routing + embargoed-repo auto-upload.

Per project (e.g. libpng), this script:

1. Resolves project metadata from google/oss-fuzz/projects/<name>/project.yaml
   (only ``main_repo`` is required; everything else is metadata for the report).
2. Clones / fast-forwards the source repo under ``--corpus-root``.
3. Invokes ``python -m bmc_agent.cli verify-dir`` on the source directory.
   Env vars sourced from ``~/.config/bmc-agent/env`` route cheap roles to K2.
4. Walks the produced ``bug_report.json`` files, keeps the ones that pass the
   full triage gate (confidence ∈ {confirmed_dynamic, confirmed_system_entry,
   confirmed_bmc} AND realism_check.verdict == "realistic"), renders a
   per-bug markdown report, and (unless ``--dry-run``) stages + commits +
   pushes them to the embargoed repo under
   ``findings/oss-fuzz/<project>/sweeps/<run-id>/audit/``.

This script does NOT touch CBMC config — it relies on the project's source
being self-contained enough for the existing pipeline. Multi-file projects
that need ``-I`` paths (libxml2, large codebases) will need to wire those
through ``--include-dir`` flags; for the libpng / expat / zstd starter
rotation a single source tree is usually enough.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

PROJECT_YAML_URL = (
    "https://raw.githubusercontent.com/google/oss-fuzz/master/projects/{name}/project.yaml"
)
DEFAULT_CORPUS_ROOT = Path("/tmp/oss_fuzz_corpora")
DEFAULT_EMBARGOED_ROOT = Path("/tmp/aprover-findings-embargoed")
DEFAULT_ARTIFACT_ROOT = Path("/tmp/oss_fuzz_artifacts")

CONFIRMED_TIERS = {"confirmed_dynamic", "confirmed_system_entry", "confirmed_bmc"}


@dataclass
class ProjectMeta:
    name: str
    main_repo: str
    language: str
    homepage: str
    primary_contact: str

    @classmethod
    def fetch(cls, name: str) -> "ProjectMeta":
        url = PROJECT_YAML_URL.format(name=name)
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read().decode("utf-8")
        meta = {"language": "", "homepage": "", "primary_contact": "", "main_repo": ""}
        for line in body.splitlines():
            line = line.strip()
            for key in meta:
                prefix = f"{key}:"
                if line.startswith(prefix):
                    value = line[len(prefix):].strip().strip("'\"")
                    meta[key] = value
        if not meta["main_repo"]:
            raise SystemExit(f"OSS-Fuzz {name}: project.yaml is missing main_repo")
        return cls(name=name, **meta)


def _run(cmd: list[str], *, cwd: Optional[Path] = None, check: bool = True,
         env: Optional[dict] = None, capture: bool = False) -> subprocess.CompletedProcess:
    """Wrapper around subprocess.run with sane defaults."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )


def clone_or_pull(meta: ProjectMeta, corpus_root: Path) -> Path:
    """Clone main_repo (shallow) or fast-forward an existing checkout.

    Returns the path to the working tree.
    """
    target = corpus_root / meta.name
    if target.exists() and (target / ".git").exists():
        try:
            _run(["git", "fetch", "--depth=1", "origin"], cwd=target)
            _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=target)
        except subprocess.CalledProcessError:
            # If fast-forward fails, fall back to a fresh clone next run.
            pass
        return target
    corpus_root.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--depth=1", meta.main_repo, str(target)])
    return target


def discover_sources(project_root: Path) -> Path:
    """Pick the directory that holds the .c sources we want to sweep.

    libpng: png*.c sits in the repo root.
    libtiff: libtiff/tif_*.c
    expat: expat/lib/*.c
    zstd: lib/**/*.c (multi-dir; this script's first cut points at lib/)

    Returns the directory bmc-agent should sweep.
    """
    candidates = [
        project_root,                  # libpng layout
        project_root / "libtiff",      # libtiff layout
        project_root / "expat" / "lib",
        project_root / "lib",          # zstd, generic
        project_root / "src",
    ]
    for cand in candidates:
        if cand.is_dir() and any(cand.glob("*.c")):
            return cand
    raise SystemExit(
        f"OSS-Fuzz {project_root.name}: no .c sources discovered in known layouts"
    )


def load_env_file(path: Path) -> dict[str, str]:
    """Source a bash-style ``export FOO=bar`` file into a dict."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export "):]
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        if (v.startswith("'") and v.endswith("'")) or (
            v.startswith('"') and v.endswith('"')
        ):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def run_verify_dir(
    source_dir: Path,
    artifact_dir: Path,
    extra_env: dict[str, str],
    log_path: Path,
    *,
    minimal: bool = False,
    extra_args: Optional[list[str]] = None,
) -> int:
    """Invoke ``python -m bmc_agent.cli verify-dir`` on ``source_dir``.

    Returns the process exit code. Output is streamed to ``log_path``.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(extra_env)

    cmd = [
        sys.executable, "-m", "bmc_agent.cli", "verify-dir",
        "--source-dir", str(source_dir),
        "--driver", "ossfz",
        "--output", str(artifact_dir),
        "--enable-realism-check",
        "--enable-dynamic-validation",
        "--enable-phase-3e-triage",
    ]
    if minimal:
        cmd.append("--minimal")
    if extra_args:
        cmd.extend(extra_args)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[oss_fuzz_sweep] launching: {' '.join(cmd)}")
    print(f"[oss_fuzz_sweep] log: {log_path}")
    with log_path.open("w") as fh:
        return subprocess.call(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT)


def iter_bug_reports(artifact_dir: Path) -> Iterable[tuple[Path, dict]]:
    """Walk all bug_report*.json files under ``artifact_dir``."""
    for path in artifact_dir.rglob("bug_report*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        yield path, data


def passes_triage(report: dict) -> bool:
    """Apply the full triage gate.

    Keep a bug iff:
      * confidence ∈ confirmed_* tiers (real_bug + reachable signal)
      * realism_check.verdict == "realistic"
    """
    confidence = report.get("confidence", "")
    if confidence not in CONFIRMED_TIERS:
        return False
    realism = report.get("realism_check") or {}
    verdict = (realism.get("verdict") or "").lower()
    return verdict == "realistic"


def render_finding(report: dict, source_path: Path, run_id: str, meta: ProjectMeta) -> str:
    """Render a minimal but human-reviewable markdown finding from a bug report."""
    fn = report.get("function", "<unknown>")
    cprop = report.get("cbmc_property", "<unknown>")
    conf = report.get("confidence", "<unknown>")
    realism = report.get("realism_check") or {}
    realism_verdict = realism.get("verdict", "")
    realism_reasoning = realism.get("reasoning", "")
    triage = report.get("triage", {}) or report.get("triage_result", {}) or {}
    triage_verdict = triage.get("verdict", "")
    triage_reasoning = triage.get("reasoning", "")

    return f"""# {meta.name}: {fn} — {cprop}

**Project**: {meta.name} ({meta.homepage})
**Upstream repo**: {meta.main_repo}
**Sweep run**: {run_id}
**Bug-report source**: `{source_path.name}`
**Confidence tier**: `{conf}`
**Realism verdict**: `{realism_verdict}`
**Triage verdict**: `{triage_verdict}`

## CBMC property
```
{cprop}
```

## Function under test
`{fn}`

## Realism check reasoning
{realism_reasoning or "_(not recorded)_"}

## Triage reasoning
{triage_reasoning or "_(not recorded)_"}

## Disclosure status
- ⏳ Upstream-status verification needed against shipping {meta.name} release.
- ⏳ Crafted reproducer / ASan witness pending.
- Primary OSS-Fuzz contact: {meta.primary_contact or "(see project.yaml)"}

## Raw report
See `{source_path}` in the artifact tree for the full JSON.
"""


def upload_findings(
    findings: list[tuple[Path, dict, str]],
    meta: ProjectMeta,
    run_id: str,
    embargoed_root: Path,
    dry_run: bool,
) -> Path:
    """Write per-bug markdown reports to the embargoed repo and (optionally) commit + push."""
    sweep_dir = embargoed_root / "findings" / "oss-fuzz" / meta.name / "sweeps" / run_id / "audit"
    sweep_dir.mkdir(parents=True, exist_ok=True)

    summary_lines = [
        f"# {meta.name} sweep {run_id} — summary",
        "",
        f"- Upstream: {meta.main_repo}",
        f"- Run UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Confirmed+realistic findings: **{len(findings)}**",
        "",
    ]
    if not findings:
        summary_lines.append("_No bugs passed the triage gate this run._")
    else:
        summary_lines.append("| # | Function | Property | Confidence | Realism |")
        summary_lines.append("|---|---|---|---|---|")

    for i, (src_path, report, md) in enumerate(findings, start=1):
        fn = report.get("function", "<unknown>")
        safe_fn = "".join(c if c.isalnum() or c in "_-" else "_" for c in fn)
        safe_prop = "".join(c if c.isalnum() or c in "_-" else "_" for c in report.get("cbmc_property", ""))
        out_path = sweep_dir / f"{i:02d}_{safe_fn}_{safe_prop[:40]}.md"
        out_path.write_text(md)
        summary_lines.append(
            f"| {i} | `{fn}` | `{report.get('cbmc_property','')}` | "
            f"`{report.get('confidence','')}` | "
            f"`{(report.get('realism_check') or {}).get('verdict','')}` |"
        )

    summary_path = sweep_dir.parent / "SWEEP_SUMMARY.md"
    summary_path.write_text("\n".join(summary_lines) + "\n")

    if dry_run:
        print(f"[oss_fuzz_sweep] DRY RUN — would have committed {len(findings)} finding(s) and the SWEEP_SUMMARY.md")
        return sweep_dir

    _run(["git", "add", "-A"], cwd=embargoed_root)
    status = _run(["git", "status", "--short"], cwd=embargoed_root, capture=True)
    if not status.stdout.strip():
        print("[oss_fuzz_sweep] no changes to commit")
        return sweep_dir
    commit_msg = (
        f"oss-fuzz/{meta.name}: sweep {run_id} — {len(findings)} confirmed+realistic finding(s)\n\n"
        f"Auto-uploaded by tools/oss_fuzz_sweep.py.\n"
        f"Triage gate: confidence ∈ confirmed_* AND realism=realistic.\n"
        f"Upstream-status verification pending for each finding.\n"
    )
    _run(["git", "commit", "-m", commit_msg], cwd=embargoed_root)
    try:
        _run(["git", "push"], cwd=embargoed_root)
    except subprocess.CalledProcessError as exc:
        print(f"[oss_fuzz_sweep] WARNING: push failed: {exc}; commit is local")
    return sweep_dir


def sweep_one_project(args: argparse.Namespace) -> int:
    meta = ProjectMeta.fetch(args.project)
    print(f"[oss_fuzz_sweep] project={meta.name} repo={meta.main_repo} lang={meta.language}")

    project_root = clone_or_pull(meta, args.corpus_root)
    source_dir = discover_sources(project_root)
    print(f"[oss_fuzz_sweep] source-dir: {source_dir}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = args.artifact_root / meta.name / run_id
    log_path = Path("/home/syc/AProver/findings") / "oss_fuzz" / f"{meta.name}_{run_id}.log"

    extra_env = load_env_file(Path("/home/syc/.config/bmc-agent/env"))
    extra_env.setdefault("BMC_AGENT_K2_NOTE", f"run_id={run_id}")

    if args.corpus_only:
        print("[oss_fuzz_sweep] --corpus-only: skipping verify-dir")
        return 0

    rc = run_verify_dir(
        source_dir, artifact_dir, extra_env, log_path,
        minimal=args.minimal,
        extra_args=args.extra,
    )
    print(f"[oss_fuzz_sweep] verify-dir exit={rc}")

    findings = []
    for src_path, report in iter_bug_reports(artifact_dir):
        if not passes_triage(report):
            continue
        md = render_finding(report, src_path, run_id, meta)
        findings.append((src_path, report, md))
    print(f"[oss_fuzz_sweep] {len(findings)} bug(s) passed triage gate")

    sweep_dir = upload_findings(
        findings, meta, run_id, args.embargoed_root, args.dry_run,
    )
    print(f"[oss_fuzz_sweep] report dir: {sweep_dir}")
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--project", required=True, help="OSS-Fuzz project name (e.g. libpng)")
    p.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    p.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    p.add_argument("--embargoed-root", type=Path, default=DEFAULT_EMBARGOED_ROOT)
    p.add_argument("--dry-run", action="store_true",
                   help="Render reports but don't commit/push to embargoed repo")
    p.add_argument("--corpus-only", action="store_true",
                   help="Clone/sync the project source then stop (smoke test)")
    p.add_argument("--minimal", action="store_true",
                   help="Pass --minimal to verify-dir (disable optional AI layers)")
    p.add_argument("--extra", nargs=argparse.REMAINDER,
                   help="Extra args forwarded to verify-dir verbatim")
    return p


def main() -> int:
    args = build_parser().parse_args()
    return sweep_one_project(args)


if __name__ == "__main__":
    sys.exit(main())
