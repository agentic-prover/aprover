"""
Precision-baseline regression: every reported finding that bmc-agent
surfaces in the verify-dir summary (confidence != 'unlikely') is the
user-visible noise surface. SOTA quality means this set stays small
and dominated by known-real bugs.

This test pins the CURRENT set of user-visible findings on a small
subset of the corpus. After any Tier-3 code change, re-run this test.
If the set GREW (new finding outside the baseline), the change has
introduced a new FP and should be reverted (or the new finding
should be confirmed as real and added to the baseline).

Run-time budget: small subset (2-3 files) sweep, ≤ 15 min.

Usage:
    .venv/bin/python tests/test_precision_baseline.py [--update]

    --update : recompute the baseline from the current sweep output
               and overwrite the pinned baseline file. Use ONLY when
               you have manually confirmed that the new findings are
               real bugs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BASELINE_FILE = REPO_ROOT / "tests" / "precision_baseline.json"

# Small subset: the two files where the baseline N=1 sweep matched the
# most seeds. Sweep on these alone for the precision check (~10 min).
PRECISION_CORPUS_FILES = [
    "archive_read_support_format_cab.c",
    "archive_read_support_format_cpio.c",
]

CORPUS_ROOT = Path("/tmp/libarchive_seedhunt_full")


def _build_subset_corpus() -> Path:
    """Copy precision-subset files + headers into a fresh tmp dir."""
    if not CORPUS_ROOT.is_dir():
        print(f"ERROR: corpus dir missing: {CORPUS_ROOT}")
        sys.exit(2)
    d = Path(tempfile.mkdtemp(prefix="bmc_precision_"))
    for f in PRECISION_CORPUS_FILES:
        src = CORPUS_ROOT / f
        if not src.exists():
            print(f"ERROR: {src} missing")
            sys.exit(2)
        shutil.copy(src, d)
    for h in CORPUS_ROOT.glob("*.h"):
        shutil.copy(h, d)
    return d


def _run_sweep(corpus_dir: Path, output_dir: Path) -> int:
    """Run verify-dir lite-mode on the subset. Returns exit code."""
    key_path = Path("/tmp/.bmc_key")
    env = os.environ.copy()
    env["BMC_AGENT_DEDUP_MAX_PER_TYPE"] = "3"
    env["BMC_AGENT_CBMC_TIMEOUT"] = "60"
    if key_path.exists():
        # source the key file via subprocess shell
        cmd = (
            f". {key_path} && {REPO_ROOT}/.venv/bin/python -m bmc_agent.cli verify-dir "
            f"--source-dir {corpus_dir} --driver precision_check --output {output_dir} "
            f"--include-dir /tmp/libarchive_bench/libarchive/build "
            f"--include-dir /tmp/libarchive_bench/libarchive/libarchive "
            f"--lite-mode --enable-realism-check --skip-refinement "
            f"--exclude 'test_*' -D HAVE_CONFIG_H"
        )
        return subprocess.call(["bash", "-c", cmd], env=env)
    else:
        print("ERROR: /tmp/.bmc_key missing — needed for realism check")
        return 2


def _extract_userfacing(output_dir: Path) -> set[tuple[str, str]]:
    """Return the set of (function_name, property_kw) for findings that
    survive realism (confidence != 'unlikely' and != '?')."""
    surface: set[tuple[str, str]] = set()
    for br in output_dir.rglob("bug_report.json"):
        try:
            d = json.load(open(br))
        except Exception:
            continue
        report = d.get("report") or {}
        conf = (report.get("confidence") or "").strip()
        if not conf or conf in ("?", "unlikely"):
            continue
        fn = report.get("function_name") or ""
        prop = report.get("violated_property") or ""
        # Use just the property class (e.g. "pointer_dereference" from
        # "func.pointer_dereference.7") so dedup index drift doesn't
        # cause false regressions.
        prop_kw = ""
        for marker in ("pointer_dereference", "pointer_arithmetic",
                       "precondition_instance", "array_bounds",
                       "pointer.", "unwind", "overflow", "bounds"):
            if marker in prop:
                prop_kw = marker.rstrip(".")
                break
        surface.add((fn, prop_kw))
    return surface


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="recompute baseline (use only after confirming new findings are real)")
    args = ap.parse_args()

    output_dir = Path(tempfile.mkdtemp(prefix="bmc_precision_out_"))
    corpus_dir = _build_subset_corpus()
    print(f"Corpus subset: {corpus_dir}")
    print(f"Output dir:    {output_dir}")
    print(f"Files: {PRECISION_CORPUS_FILES}")

    rc = _run_sweep(corpus_dir, output_dir)
    if rc != 0:
        print(f"sweep failed (exit {rc})")
        return rc

    surface = _extract_userfacing(output_dir)
    print(f"\nUser-facing findings: {len(surface)}")
    for fn, kw in sorted(surface):
        print(f"  {fn}  [{kw}]")

    if args.update:
        BASELINE_FILE.write_text(json.dumps(
            sorted(list(surface)), indent=2,
        ))
        print(f"\nBaseline updated -> {BASELINE_FILE}")
        return 0

    if not BASELINE_FILE.exists():
        print(f"\nNo baseline file at {BASELINE_FILE}. Run --update first.")
        return 0  # not a failure on first run

    pinned = set(tuple(x) for x in json.loads(BASELINE_FILE.read_text()))
    added = surface - pinned
    removed = pinned - surface
    print(f"\n=== Precision diff vs baseline ===")
    print(f"  Added (NEW user-visible findings): {len(added)}")
    for fn, kw in sorted(added):
        print(f"    + {fn}  [{kw}]")
    print(f"  Removed (no longer surfaced): {len(removed)}")
    for fn, kw in sorted(removed):
        print(f"    - {fn}  [{kw}]")

    if added:
        print(f"\nFAIL: {len(added)} new finding(s) added vs baseline.")
        print("Either (a) revert the code change, OR (b) confirm new findings")
        print("are real bugs and run with --update.")
        return 1
    print("\nOK: no new findings introduced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
