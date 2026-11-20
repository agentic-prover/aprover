"""Smoke-test the agentic harness generator on two libarchive functions:

* ``next_field``   — documented seed bug commit 8308b61c (OOB read).
                     The harness MUST surface a CBMC counterexample on
                     a pointer_dereference / pointer_arithmetic property.
* ``append_entry`` — current FP source. The deterministic generator
                     emits a 5-byte buffer the function then overflows;
                     the agentic harness should size the buffer the way
                     ``archive_acl_to_text_l`` does via ``archive_acl_text_len``,
                     so CBMC should verify clean (or at least not produce
                     the trivial 5-byte overflow).

Run:
    .venv/bin/python scripts/smoke_agentic_harness.py
Env:
    configure the required BMC_AGENT_LLM_* environment variables first
"""

from __future__ import annotations

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

TARGETS = [
    # (file_stem, function, expected: "expect_fail"|"expect_clean"|"either")
    ("archive_acl", "next_field", "expect_fail"),
    ("archive_acl", "append_entry", "expect_clean"),
]


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

    # Parse every .c in the corpus once, so the agent can inspect callers
    # across the corpus, not just inside the target file.
    parsed_files: dict = {}
    for f in sorted(CORPUS.glob("*.c")):
        try:
            expanded = preprocess(
                f, include_dirs=config.include_dirs, defines=config.cbmc_defines,
            )
            parsed_files[str(f)] = parse_c_file(f, source_text=expanded)
        except Exception as exc:
            print(f"  parse {f.name}: {exc}")

    summary = []

    for file_stem, fn_name, expectation in TARGETS:
        src = CORPUS / f"{file_stem}.c"
        if str(src) not in parsed_files:
            print(f"  skip: {fn_name} ({src.name} not parsed)")
            continue
        parsed = parsed_files[str(src)]
        func = parsed.get_function_info(fn_name)
        if func is None:
            print(f"  skip: {fn_name} not in {src.name}")
            continue

        all_funcs_global: dict = {}
        for p in parsed_files.values():
            for n in p.functions:
                info = p.get_function_info(n)
                if info is not None:
                    all_funcs_global.setdefault(n, info)

        print(f"\n=== {fn_name} (expect: {expectation}) ===")
        ag = AgenticHarnessGen(
            config=config, parsed_files=parsed_files, corpus_root=CORPUS,
        )
        t0 = time.time()
        res = ag.generate(
            func=func,
            all_funcs_global=all_funcs_global,
            include_dirs=[BUILD_INCLUDE, LIBA_INCLUDE],
            defines=["HAVE_CONFIG_H"],
        )
        dt_gen = time.time() - t0
        print(
            f"  agentic gen: turns={res.turns_used} retries={res.retries} "
            f"elapsed={dt_gen:.1f}s compile_err={'yes' if res.last_compile_error else 'no'}"
        )
        if res.rationale:
            print(f"  rationale: {res.rationale[:240]}")
        if res.last_compile_error:
            print(f"  compile_error (final):\n{res.last_compile_error[:1200]}")
            summary.append((fn_name, expectation, "harness_gen_failed", dt_gen, 0.0, 0))
            continue

        # Persist for inspection
        artefact = Path(tempfile.mkdtemp(prefix=f"agentic_{fn_name}_"))
        (artefact / "harness.c").write_text(res.harness)
        print(f"  harness written: {artefact / 'harness.c'}")

        # Run CBMC on it (90s budget, unwind=4)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write(res.harness)
            path = f.name
        try:
            t0 = time.time()
            cbmc_res = run_cbmc(
                harness_path=path,
                unwind=4, timeout=120,
                include_dirs=[BUILD_INCLUDE, LIBA_INCLUDE],
                defines=["HAVE_CONFIG_H"],
                bounds_check=True, pointer_check=True,
                signed_overflow_check=True, div_by_zero_check=True,
            )
            dt_cbmc = time.time() - t0
        finally:
            try: os.unlink(path)
            except OSError: pass

        if cbmc_res.error:
            print(f"  CBMC error: {cbmc_res.error[:200]}")
            summary.append((fn_name, expectation, "cbmc_error", dt_gen, dt_cbmc, 0))
            continue

        n_cex = len(cbmc_res.counterexamples or [])
        verified = cbmc_res.verified
        outcome = "verified_clean" if verified else f"unverified_{n_cex}_cex"
        print(f"  CBMC: {outcome} (elapsed {dt_cbmc:.1f}s)")
        if not verified and n_cex:
            for c in (cbmc_res.counterexamples or [])[:5]:
                print(f"    - {c.failing_property}")
        summary.append((fn_name, expectation, outcome, dt_gen, dt_cbmc, n_cex))

    print("\n=== SUMMARY ===")
    for fn, exp, out, dt_g, dt_c, ncx in summary:
        match = (
            (exp == "expect_fail" and out.startswith("unverified") and ncx > 0)
            or (exp == "expect_clean" and out == "verified_clean")
            or (exp == "either" and out not in ("harness_gen_failed", "cbmc_error"))
        )
        verdict_emoji = "PASS" if match else "FAIL"
        print(f"  {verdict_emoji}  {fn}  expect={exp}  outcome={out}  gen={dt_g:.0f}s cbmc={dt_c:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
