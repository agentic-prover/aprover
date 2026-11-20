#!/usr/bin/env bash
# Clean --agentic run for JUDGMENT-based evaluation (no soundness-gate: true
# default behavior). Captures raw bug reports for manual adjudication.
# Usage: judge_run.sh LABEL [MODULE]   env: EXTRA_FLAGS, PER_FUNC_BUDGET, OVERALL_TIMEOUT
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
[[ -r "$HOME/.config/bmc-agent/env" ]] && source "$HOME/.config/bmc-agent/env"
LABEL="${1:?need label}"; MOD="${2:-vfs}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="findings/judge_${LABEL}_${TS}"; mkdir -p "$OUT"
echo "$OUT" > "/tmp/judge_${LABEL}.txt"
echo "[judge $LABEL] START $(date -u) module=$MOD out=$OUT extra='${EXTRA_FLAGS:-}'"
timeout "${OVERALL_TIMEOUT:-9000}" ./.venv/bin/bmc-agent verify \
    --source "examples/vibeos/repo/kernel/${MOD}.c" \
    --driver "vibeos_${MOD}" \
    --include-dir examples/vibeos/repo/kernel --include-dir examples/vibeos/repo/kernel/libc \
    --agentic ${EXTRA_FLAGS:-} \
    --threat-model security --threat-model-context examples/vibeos/threat_model_context.md \
    --per-function-time-budget "${PER_FUNC_BUDGET:-300}" \
    --output "$OUT" > "$OUT/run.log" 2>&1
rc=$?
echo "[judge $LABEL] DONE rc=$rc $(date -u)"
echo "rc=$rc out=$OUT" > "$OUT/DONE"
