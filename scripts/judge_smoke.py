"""
Smoke test: run the JudgeAgent on the documented seed bug (next_field,
commit 8308b61c) in archive_acl.c and print the verdict.

This validates: parser → harness gen → CBMC → JudgeAgent → final_verdict.

Usage:
    OPENROUTER_API_KEY=... \
        uv run python scripts/judge_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bmc_agent.config import Config
from bmc_agent.parser import parse_c_file
from bmc_agent.preprocessor import preprocess
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.spec import Spec, SpecStatus
from bmc_agent.cbmc import run_cbmc
from bmc_agent.llm_judge import JudgeAgent

CORPUS = Path("/tmp/libarchive_seedhunt_full")
TARGET_FILE = CORPUS / "archive_acl.c"
TARGET_FN = "next_field"   # documented seed bug 8308b61c
BUILD_INC = "/tmp/libarchive_bench/libarchive/build"
LIBA_INC = "/tmp/libarchive_bench/libarchive/libarchive"


def main() -> int:
    if not TARGET_FILE.is_file():
        print(f"ERROR: corpus missing: {TARGET_FILE}")
        return 1
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set in environment")
        return 1

    # Wire env → Config (GPT-5 across all roles via OpenRouter)
    os.environ["BMC_AGENT_LLM_MODEL"] = "openai/gpt-5"
    os.environ["BMC_AGENT_LLM_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    os.environ["BMC_AGENT_LLM_BASE_URL"] = "https://openrouter.ai/api/v1"
    os.environ["BMC_AGENT_LLM_PROVIDER"] = "openai"
    os.environ["BMC_AGENT_LLM_REALISM_MODEL"] = "openai/gpt-5"
    os.environ["BMC_AGENT_LLM_REALISM_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    os.environ["BMC_AGENT_LLM_REALISM_BASE_URL"] = "https://openrouter.ai/api/v1"
    os.environ["BMC_AGENT_LLM_REALISM_PROVIDER"] = "openai"

    config = Config.from_env()
    config.lite_mode = True
    config.preprocess = True
    config.include_dirs = [BUILD_INC, LIBA_INC]
    config.cbmc_defines = ["HAVE_CONFIG_H"]
    config.llm_request_timeout_s = 900.0

    print(f"=== Parsing {TARGET_FILE} ===")
    expanded = preprocess(
        TARGET_FILE, include_dirs=config.include_dirs, defines=config.cbmc_defines,
    )
    parsed = parse_c_file(TARGET_FILE, source_text=expanded)
    print(f"  parsed: {len(parsed.functions)} functions; "
          f"{len(parsed.struct_definitions)} structs")

    func = parsed.get_function_info(TARGET_FN)
    if func is None:
        print(f"ERROR: function '{TARGET_FN}' not found in {TARGET_FILE}")
        return 1

    spec = Spec(
        function_name=TARGET_FN,
        precondition="true",
        postcondition="true",
        status=SpecStatus.GENERATED,
    )
    all_funcs = {n: parsed.get_function_info(n) for n in parsed.functions}
    all_funcs = {n: fi for n, fi in all_funcs.items() if fi is not None}

    print(f"=== Generating harness for '{TARGET_FN}' ===")
    gen = HarnessGenerator(config)
    harness_text = gen.generate_harness(
        func=func, spec=spec, parsed_file=parsed, all_funcs=all_funcs,
    )
    print(f"  harness: {len(harness_text)} chars")

    print("=== Running CBMC ===")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
        f.write(harness_text)
        harness_path = f.name
    t0 = time.time()
    cbmc_result = run_cbmc(
        harness_path=harness_path,
        unwind=4, timeout=120,
        include_dirs=[BUILD_INC, LIBA_INC], defines=["HAVE_CONFIG_H"],
        bounds_check=True, pointer_check=True,
        signed_overflow_check=True, div_by_zero_check=True,
    )
    print(f"  CBMC done in {time.time()-t0:.1f}s — verified={cbmc_result.verified} "
          f"counterexamples={len(cbmc_result.counterexamples or [])}")
    if cbmc_result.error:
        print(f"  CBMC error: {cbmc_result.error[:300]}")
    cexs = cbmc_result.counterexamples or []
    if not cexs:
        print("ERROR: no counterexamples — can't judge.")
        return 1

    # Pick the most interesting CEx: prefer pointer_dereference / OOB / overflow
    def _rank(c):
        prop = c.failing_property or ""
        for kw, score in [
            ("pointer_dereference", 5),
            ("pointer_arithmetic", 4),
            ("array_bounds", 4),
            ("overflow", 3),
        ]:
            if kw in prop:
                return score
        return 1
    cex = sorted(cexs, key=_rank, reverse=True)[0]
    print(f"  picked CEx: {cex.failing_property}")

    print(f"=== Judging with GPT-5 ===")
    judge = JudgeAgent(
        config=config,
        parsed_files={str(TARGET_FILE): parsed},
        corpus_root=CORPUS,
        harness_source=harness_text,
        cbmc_rerun_callback=None,   # rerun disabled for smoke test
    )
    t0 = time.time()
    verdict = judge.judge(func=func, counterexample=cex, cbmc_result=cbmc_result)
    dt = time.time() - t0

    print()
    print("=" * 70)
    print(f"JUDGE VERDICT after {dt:.1f}s, {verdict.turns_used} turn(s):")
    print(f"  verdict   : {verdict.verdict}")
    print(f"  confidence: {verdict.confidence}")
    print(f"  tools     : {verdict.tools_invoked}")
    print()
    print("  reasoning :")
    for line in verdict.reasoning.splitlines():
        print(f"    {line}")
    if verdict.attacker_scenario:
        print()
        print("  attacker_scenario:")
        for line in verdict.attacker_scenario.splitlines():
            print(f"    {line}")
    if verdict.adjacent_bugs:
        print()
        print(f"  adjacent_bugs: {len(verdict.adjacent_bugs)}")
        for ab in verdict.adjacent_bugs[:5]:
            print(f"    - {ab}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
