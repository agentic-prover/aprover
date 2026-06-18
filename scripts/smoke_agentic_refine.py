"""Smoke-test AgenticHarnessGen.refine() in isolation.

Setup: take ``append_entry`` (the FP source from the judge_v6 sweep) and
hand the refine() method:
  * the bad 5-byte deterministic harness (from /tmp/libarchive_judge_v6)
  * a fabricated judge reasoning that explains why it's an artifact
  * the failing property + witness from the original sweep

Expected: refine() produces a harness that (a) compiles, (b) DIFFERS from
the prior, (c) sizes the buffer realistically, (d) CBMC produces
substantially fewer CExes.

Run:
    # Configure the required BMC_AGENT_LLM_* environment variables first.
    .venv/bin/python scripts/smoke_agentic_refine.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bmc_agent.agentic_harness_gen import AgenticHarnessGen
from bmc_agent.cbmc import run_cbmc
from bmc_agent.config import Config
from bmc_agent.parser import parse_c_file
from bmc_agent.preprocessor import preprocess


CORPUS = Path("/tmp/libarchive_seedhunt_full")
BUILD_INCLUDE = "/tmp/libarchive_bench/libarchive/build"
LIBA_INCLUDE = "/tmp/libarchive_bench/libarchive/libarchive"

PRIOR_HARNESS = '''/* Auto-generated CBMC harness for: append_entry */
#include "/tmp/libarchive_seedhunt_full/archive_acl.c"

int main(void) {
    char _p_backing[5];
    char *_p_cursor = _p_backing;
    char** p = &_p_cursor;
    unsigned char _prefix_buf[5];
    const char* prefix = (const char*)_prefix_buf;
    __CPROVER_assume(prefix != NULL);
    int type;
    int tag;
    int flags;
    unsigned char _name_buf[5];
    const char* name = (const char*)_name_buf;
    __CPROVER_assume(name != NULL);
    int perm;
    int id;
    append_entry(p, prefix, type, tag, flags, name, perm, id);
    return 0;
}
'''

FAILING_PROPERTY = "append_entry.pointer_dereference.125"
WITNESS = {
    "*p": "&_p_backing[0]",
    "prefix": "&_prefix_buf[0]",
    "type": "2304",
    "tag": "272147",
    "flags": "0",
    "name": "&_name_buf[0]",
    "perm": "0",
    "id": "0",
}

JUDGE_REASONING = (
    "The CBMC counterexample is a harness artifact. The harness allocates "
    "a 5-byte backing buffer (_p_backing[5]) and provides 5-byte non-NUL- "
    "terminated buffers for prefix and name. In real usage, append_entry "
    "is called from archive_acl_to_text_l, which pre-calculates the buffer "
    "size via archive_acl_text_len (lines 545-650) — this scans the ACL "
    "entries and computes space for all tag strings, colons, permission "
    "characters, flag characters, and name lengths, then mallocs a buffer "
    "of exactly that size. prefix and name come from "
    "archive_mstring_get_mbs_l and are always NUL-terminated. Additionally "
    "the witness shows tag=272147 (not a valid ACL tag — real values are "
    "10001-10107) and type=2304 (a mix of mutually-exclusive DENY/AUDIT "
    "flags), neither of which can occur in real usage."
)


def main() -> int:
    if not CORPUS.is_dir():
        print(f"ERROR: corpus missing: {CORPUS}", file=sys.stderr)
        return 2

    config = Config.from_env()
    config.lite_mode = True
    config.preprocess = True
    config.include_dirs = [BUILD_INCLUDE, LIBA_INCLUDE]
    config.cbmc_defines = ["HAVE_CONFIG_H"]
    config.enable_agentic_harness = True

    parsed_files: dict = {}
    for f in sorted(CORPUS.glob("*.c")):
        try:
            expanded = preprocess(
                f, include_dirs=config.include_dirs, defines=config.cbmc_defines,
            )
            parsed_files[str(f)] = parse_c_file(f, source_text=expanded)
        except Exception as exc:
            print(f"  parse {f.name}: {exc}")

    src = CORPUS / "archive_acl.c"
    parsed = parsed_files[str(src)]
    func = parsed.get_function_info("append_entry")
    if func is None:
        print("ERROR: append_entry not parsed")
        return 1

    all_funcs_global: dict = {}
    for p in parsed_files.values():
        for n in p.functions:
            info = p.get_function_info(n)
            if info is not None:
                all_funcs_global.setdefault(n, info)

    # First, baseline: run CBMC on the bad prior harness to count CExes.
    print("=== Baseline (prior bad harness) ===")
    n_prior, prior_verified = _cbmc_count(PRIOR_HARNESS)
    print(f"  CBMC: verified={prior_verified}  n_cex={n_prior}")

    # Now call refine() with the fabricated unrealistic reasoning.
    print("\n=== refine() invocation ===")
    ag = AgenticHarnessGen(
        config=config, parsed_files=parsed_files, corpus_root=CORPUS,
    )
    t0 = time.time()
    res = ag.refine(
        func=func,
        all_funcs_global=all_funcs_global,
        prior_harness=PRIOR_HARNESS,
        failing_property=FAILING_PROPERTY,
        judge_verdict="unrealistic",
        judge_reasoning=JUDGE_REASONING,
        witness=WITNESS,
        include_dirs=[BUILD_INCLUDE, LIBA_INCLUDE],
        defines=["HAVE_CONFIG_H"],
    )
    dt = time.time() - t0
    print(f"  turns={res.turns_used} retries={res.retries} elapsed={dt:.1f}s "
          f"compile_err={'yes' if res.last_compile_error else 'no'}")
    if res.rationale:
        print(f"  rationale: {res.rationale[:300]}")
    if res.last_compile_error:
        print(f"  compile_error: {res.last_compile_error[:600]}")
        return 1

    changed = res.harness.strip() != PRIOR_HARNESS.strip()
    print(f"  harness_changed: {changed}")
    out_dir = Path(tempfile.mkdtemp(prefix="agentic_refine_"))
    (out_dir / "harness_refined.c").write_text(res.harness)
    print(f"  refined harness written: {out_dir / 'harness_refined.c'}")

    # CBMC on the refined harness
    print("\n=== CBMC on refined harness ===")
    n_after, after_verified = _cbmc_count(res.harness)
    print(f"  CBMC: verified={after_verified}  n_cex={n_after}")

    print("\n=== SUMMARY ===")
    print(f"  baseline n_cex:   {n_prior}")
    print(f"  refined  n_cex:   {n_after}")
    print(f"  reduction:        {n_prior - n_after}")
    success = changed and (after_verified or n_after < n_prior)
    print(f"  refinement {'EFFECTIVE' if success else 'NOT effective'}")
    return 0 if success else 1


def _cbmc_count(harness_text: str) -> tuple[int, bool]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
        f.write(harness_text)
        path = f.name
    try:
        r = run_cbmc(
            harness_path=path,
            unwind=4, timeout=120,
            include_dirs=[BUILD_INCLUDE, LIBA_INCLUDE],
            defines=["HAVE_CONFIG_H"],
            bounds_check=True, pointer_check=True,
            signed_overflow_check=True, div_by_zero_check=True,
        )
    finally:
        try: os.unlink(path)
        except OSError: pass
    if r.error:
        print(f"    CBMC error: {r.error[:200]}")
        return -1, False
    return len(r.counterexamples or []), bool(r.verified)


if __name__ == "__main__":
    raise SystemExit(main())
