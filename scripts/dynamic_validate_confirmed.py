"""
Retroactively run dynamic validation on every BMC-confirmed adjacent bug
in a judge-dir output directory.

For each confirmed bug:
  1. Re-parse the target function's source file (so we have FunctionInfo)
  2. Ask the LLM (gpt-5 by default) to produce a C reproducer using
     libarchive's public API + the attacker_scenario the judge produced
  3. Compile with gcc -fsanitize=address,undefined linked against the
     built libarchive .so
  4. Run with timeout; record signal / sanitizer output if any
  5. Persist the outcome alongside the original judge_*.json record

Usage:
    OPENROUTER_API_KEY=... \
    BMC_AGENT_LLM_MODEL=openai/gpt-5 \
      python scripts/dynamic_validate_confirmed.py <judge_output_dir>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bmc_agent.config import Config
from bmc_agent.llm import LLMClient
from bmc_agent.parser import parse_c_file
from bmc_agent.preprocessor import preprocess
from bmc_agent.judge_pipeline import (
    _dynamic_validate_bug,
    _DEFAULT_LIBARCHIVE_BUILD,
    _DEFAULT_LIBARCHIVE_INC,
)

CORPUS = Path("/tmp/libarchive_seedhunt_full")
BUILD_INC = "/tmp/libarchive_bench/libarchive/build"
LIBA_INC = "/tmp/libarchive_bench/libarchive/libarchive"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    out_root = Path(sys.argv[1])
    if not out_root.is_dir():
        print(f"ERROR: not a dir: {out_root}")
        return 1

    # OpenAI env wiring (route via OpenRouter)
    if os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("BMC_AGENT_LLM_API_KEY"):
        os.environ["BMC_AGENT_LLM_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
        os.environ.setdefault("BMC_AGENT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
        os.environ.setdefault("BMC_AGENT_LLM_PROVIDER", "openai")
        os.environ.setdefault("BMC_AGENT_LLM_MODEL", "openai/gpt-5")

    config = Config.from_env()
    config.llm_request_timeout_s = 600.0
    llm = LLMClient(config)

    # Cache parsed files
    parsed_cache: dict[str, "ParsedCFile"] = {}

    def _get_parsed(stem: str):
        path = CORPUS / f"{stem}.c"
        if not path.exists():
            return None
        if path.as_posix() not in parsed_cache:
            try:
                expanded = preprocess(
                    path, include_dirs=[BUILD_INC, LIBA_INC],
                    defines=["HAVE_CONFIG_H"],
                )
                parsed_cache[path.as_posix()] = parse_c_file(path, source_text=expanded)
            except Exception as exc:
                print(f"  parse failed for {path}: {exc}")
                return None
        return parsed_cache[path.as_posix()]

    # Walk judge_*.json, find confirmed adjacents
    summary = []
    for jf in sorted(out_root.rglob("judge_*.json")):
        try:
            d = json.load(open(jf))
        except Exception:
            continue
        src_fn = jf.parent.name
        stem = jf.parts[-3]  # file_stem
        confirmations = d.get("adjacent_confirmations") or []
        any_confirmed = any(c.get("confirmed") for c in confirmations)
        if not any_confirmed:
            continue

        parsed = _get_parsed(stem)
        if parsed is None:
            print(f"\n=== SKIP {jf} (no parsed file for stem {stem}) ===")
            continue

        for idx, cf in enumerate(confirmations):
            if not cf.get("confirmed"):
                continue
            tgt = cf.get("target_function")
            if not tgt or tgt not in parsed.functions:
                print(f"  skip: target {tgt!r} not in parsed file")
                continue
            target_func = parsed.get_function_info(tgt)
            adj = cf.get("adjacent") or {}
            scenario = (
                cf.get("attacker_scenario")
                or adj.get("attacker_scenario")
                or ""
            )
            print(f"\n=== Dynamic-validating {stem}/{src_fn} → {tgt} ===")
            print(f"    bug_type: {(adj.get('bug_type') or '')[:90]}")
            out_dir = jf.parent / "dynamic" / tgt
            try:
                result = _dynamic_validate_bug(
                    func=target_func,
                    attacker_scenario=scenario,
                    parsed_file=parsed,
                    libarchive_build_dir=_DEFAULT_LIBARCHIVE_BUILD,
                    libarchive_include_dir=_DEFAULT_LIBARCHIVE_INC,
                    llm=llm,
                    out_dir=out_dir,
                )
            except Exception as exc:
                result = {"outcome": "exception", "reason": str(exc)}
            print(f"    outcome={result.get('outcome')}  signal={result.get('signal_name')}")
            if result.get("stderr_excerpt"):
                # Show first ASan/UBSan summary line if present
                for line in result["stderr_excerpt"].splitlines():
                    if "Sanitizer" in line or "runtime error" in line:
                        print(f"    !! {line[:200]}")
                        break
            cf["dynamic_validation"] = result
            summary.append({
                "source_judge": str(jf),
                "src_fn": src_fn,
                "target_fn": tgt,
                "bug_type": adj.get("bug_type"),
                "outcome": result.get("outcome"),
                "signal": result.get("signal_name"),
            })

        # Persist back the updated record
        with open(jf, "w") as f:
            json.dump(d, f, indent=2)

    # Top-line
    print()
    print("=" * 80)
    print(f"Summary: {len(summary)} confirmed bugs dynamic-validated")
    print("=" * 80)
    print(f"{'src_fn':28s} {'target_fn':28s} {'outcome':22s} {'signal':10s}")
    print("-" * 90)
    counts = {}
    for s in summary:
        oc = s["outcome"]
        counts[oc] = counts.get(oc, 0) + 1
        print(f"{s['src_fn'][:28]:28s} {(s['target_fn'] or '?')[:28]:28s} "
              f"{oc:22s} {(s['signal'] or '-')[:10]:10s}")
    print()
    print(f"Outcome tallies: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
