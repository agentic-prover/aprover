#!/usr/bin/env bash
# Phase-3 cross-codebase enforcement check (realism-enforcement plan).
# Runs the SAME cli-verify path validated on VibeOS, but on a libredwg parser
# source, with --agentic (enforcement default-ON, no --keep-dynamic-immunity).
# Goal: empirically confirm enforcement does not demote a cross-codebase real.
# Mechanism expectation: libredwg parser bugs are confirmed_system_entry
# (attacker-entry-reachable), which the confirmed_dynamic immunity removal does
# NOT touch -> 0 demotions. This run provides the empirical data point.
set -u
ROOT="${APROVER_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" || exit 1
ENVF="$HOME/.config/bmc-agent/env"
[ -f "$ENVF" ] && . "$ENVF"

CORP=/tmp/oss_fuzz_corpora/libredwg
src="${1:-$CORP/src/reedsolomon.c}"
out=findings/phase3_enforce_libredwg
mkdir -p "$out"

timeout 1500 python -m bmc_agent.cli verify \
  --source "$src" \
  --driver ossfz_p3enforce \
  --output "$out" \
  --include-dir "$CORP/_cbmc_build/src" \
  --include-dir "$CORP/src" \
  --include-dir "$CORP/include" \
  --include-dir "$CORP/_cbmc_build" \
  -D DLL_EXPORT -D ENABLE_SHARED -D redwg_EXPORTS -D LTO -D NDEBUG \
  --agentic \
  >"$out/run.log" 2>&1
echo "DONE libredwg rc=$?" >>"$out/run.log"
