"""
Seed-bug regression suite for bmc-agent-lite.

For each documented seed bug that bmc-agent-lite currently detects, this
script regenerates the harness with the current code, runs CBMC, and
asserts CBMC still produces a counterexample on the expected property
class.

This is the safety net: every code change to harness_generator,
realism_checker, or pipeline must keep these assertions passing. A
regression here means the change has broken a previously-matched seed
bug; the change must be reverted before sweeping is worth doing.

Usage:
    .venv/bin/python -m pytest tests/test_seed_bug_regression.py -v -s

Run-time budget: each test runs CBMC standalone on one function. Target
per-function budget: ≤ 60s. Total suite budget: ≤ 10 minutes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure bmc_agent is importable
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bmc_agent.config import Config
from bmc_agent.parser import parse_c_file
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.preprocessor import preprocess
from bmc_agent.spec import Spec, SpecStatus


# Corpus root — the snapshot bmc-agent-lite has been validated against
CORPUS = Path("/tmp/libarchive_seedhunt_full")
BUILD_INCLUDE = "/tmp/libarchive_bench/libarchive/build"
LIBA_INCLUDE = "/tmp/libarchive_bench/libarchive/libarchive"


# Currently-matched seed bugs: (file_stem, function_name, expected
# failing-property prefix-or-keyword, source-commit). A test passes
# when CBMC produces a verification failure (verified=False) with at
# least one counterexample whose failing_property contains the keyword.
MATCHED_SEEDS = [
    # archive_acl.c
    ("archive_acl", "next_field", "pointer_dereference", "8308b61c"),
    ("archive_acl", "next_field_w", "pointer_dereference", "8308b61c-companion"),
    # archive_read_support_format_cab.c
    ("archive_read_support_format_cab", "cab_checksum_finish",
     "pointer_dereference", "32b62cf7"),
    # archive_read_support_format_cpio.c
    ("archive_read_support_format_cpio", "find_newc_header",
     "pointer_arithmetic", "1f2da75f"),
    ("archive_read_support_format_cpio", "record_hardlink",
     "precondition_instance", "16ad9310"),
    # archive_read_support_format_rar5.c
    ("archive_read_support_format_rar5", "rar5_cleanup",
     "precondition_instance", "35877523"),
]


def _require_corpus():
    if not CORPUS.is_dir():
        pytest.skip(f"corpus dir missing: {CORPUS}")
    if not Path(BUILD_INCLUDE).is_dir():
        pytest.skip(f"libarchive build dir missing: {BUILD_INCLUDE}")


def _make_config() -> Config:
    config = Config.from_env()
    config.lite_mode = True
    config.preprocess = True
    config.include_dirs = [BUILD_INCLUDE, LIBA_INCLUDE]
    config.cbmc_defines = ["HAVE_CONFIG_H"]
    return config


def _generate_harness_text(
    config: Config, source_path: Path, function_name: str
) -> tuple[str, list]:
    """Parse source, build a minimal precondition=true spec, emit harness.

    Returns (harness_source_text, parser_errors).
    """
    # Preprocess to inline all #include'd headers so struct_definitions
    # populates with archive_read, archive_format_descriptor, etc.
    expanded = preprocess(
        source_path,
        include_dirs=config.include_dirs,
        defines=config.cbmc_defines,
    )
    parsed = parse_c_file(source_path, source_text=expanded)
    func = parsed.get_function_info(function_name)
    if func is None:
        raise RuntimeError(
            f"function '{function_name}' not parsed from {source_path}"
        )
    # Lite-mode trivial spec (PRE = POST = true)
    spec = Spec(
        function_name=function_name,
        precondition="true",
        postcondition="true",
        status=SpecStatus.GENERATED,
    )
    all_specs = {function_name: spec}
    all_funcs = {n: parsed.get_function_info(n) for n in parsed.functions}
    all_funcs = {n: fi for n, fi in all_funcs.items() if fi is not None}
    gen = HarnessGenerator(config)
    text = gen.generate_harness(
        func=func,
        spec=spec,
        parsed_file=parsed,
        all_funcs=all_funcs,
    )
    return text, []


def _run_cbmc(harness_text: str, function_name: str, timeout_s: int = 90) -> dict:
    """Compile + run CBMC on the harness. Returns dict with
    {verified, counterexamples, raw_output, return_code, error}.
    """
    from bmc_agent.cbmc import run_cbmc

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".c", delete=False
    ) as f:
        f.write(harness_text)
        harness_path = f.name
    try:
        result = run_cbmc(
            harness_path=harness_path,
            unwind=4,
            timeout=timeout_s,
            include_dirs=[BUILD_INCLUDE, LIBA_INCLUDE],
            defines=["HAVE_CONFIG_H"],
            bounds_check=True,
            pointer_check=True,
            div_by_zero_check=True,
            signed_overflow_check=True,
        )
    finally:
        try:
            os.unlink(harness_path)
        except OSError:
            pass
    return {
        "verified": result.verified,
        "counterexamples": [
            {
                "failing_property": ce.failing_property,
                "description": ce.description,
            }
            for ce in (result.counterexamples or [])
        ],
        "error": result.error,
    }


@pytest.mark.parametrize(
    "file_stem,function_name,expected_property_kw,commit",
    MATCHED_SEEDS,
    ids=[f"{c}_{fn}" for _, fn, _, c in MATCHED_SEEDS],
)
def test_seed_bug_still_detected(
    file_stem: str,
    function_name: str,
    expected_property_kw: str,
    commit: str,
):
    """A previously-matched seed bug MUST still produce a CBMC
    counterexample matching the expected property class.

    If this fails, the most recent code change has regressed a
    seed-bug match. Revert and re-test.
    """
    _require_corpus()
    source_path = CORPUS / f"{file_stem}.c"
    if not source_path.exists():
        pytest.skip(f"source missing: {source_path}")

    config = _make_config()
    try:
        harness_text, _ = _generate_harness_text(config, source_path, function_name)
    except Exception as exc:
        pytest.fail(
            f"harness generation failed for '{function_name}' (commit {commit}): {exc}"
        )

    result = _run_cbmc(harness_text, function_name, timeout_s=90)
    if result["error"]:
        pytest.fail(
            f"CBMC error for '{function_name}' (commit {commit}): {result['error']}"
        )

    assert not result["verified"], (
        f"REGRESSION: '{function_name}' (commit {commit}) now VERIFIES clean. "
        f"This used to fail with property '{expected_property_kw}'."
    )

    # At least one counterexample must mention the expected property class.
    matching = [
        ce for ce in result["counterexamples"]
        if expected_property_kw in (ce.get("failing_property") or "")
    ]
    assert matching, (
        f"REGRESSION: '{function_name}' (commit {commit}) failed but none "
        f"of the {len(result['counterexamples'])} counterexamples match the "
        f"expected property class '{expected_property_kw}'. "
        f"Got: {[ce['failing_property'] for ce in result['counterexamples'][:5]]}"
    )


if __name__ == "__main__":
    # Direct invocation as a script (not via pytest) for quick checks.
    print("Seed-bug regression suite — running standalone")
    failures = 0
    for file_stem, fn, prop_kw, commit in MATCHED_SEEDS:
        print(f"\n=== {commit}  {fn}  (expect: {prop_kw}) ===")
        try:
            config = _make_config()
            source_path = CORPUS / f"{file_stem}.c"
            harness_text, _ = _generate_harness_text(config, source_path, fn)
            result = _run_cbmc(harness_text, fn, timeout_s=90)
            if result["error"]:
                print(f"  CBMC error: {result['error'][:120]}")
                failures += 1
                continue
            if result["verified"]:
                print(f"  REGRESSION: now verifies clean")
                failures += 1
                continue
            matching = [
                ce for ce in result["counterexamples"]
                if prop_kw in (ce.get("failing_property") or "")
            ]
            if matching:
                print(f"  OK — {len(matching)} matching CEx, first: {matching[0]['failing_property']}")
            else:
                print(f"  REGRESSION: no CEx matches '{prop_kw}'")
                print(f"  got: {[ce['failing_property'] for ce in result['counterexamples'][:5]]}")
                failures += 1
        except Exception as exc:
            print(f"  EXCEPTION: {exc}")
            failures += 1
    print(f"\n=== {len(MATCHED_SEEDS) - failures}/{len(MATCHED_SEEDS)} passing ===")
    raise SystemExit(1 if failures else 0)
