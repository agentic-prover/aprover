#!/usr/bin/env bash
# Unattended overnight --agentic tuning driver (v2: self-match-proof wait).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=/tmp/tune_overnight.log
exec >> "$LOG" 2>&1
echo "=== overnight tuning driver v2 START $(date -u) PID=$$ ==="

wait_idle() {  # block until the actual sweep binary is gone (regex avoids self-match)
  while pgrep -f "[.]venv/bin/bmc-agent verify" >/dev/null 2>&1; do sleep 30; done
}

wait_idle; echo "[driver] idle $(date -u)"

echo "[driver] START ccall $(date -u)"
AGENTIC_FLAG="--agentic-claude-code" OVERALL_TIMEOUT=18000 PER_FUNC_BUDGET=180 \
    bash tools/tune_agentic.sh ccall || echo "[driver] ccall rc=$?"
wait_idle; echo "[driver] ccall done $(date -u)"

echo "[driver] START haikumech $(date -u)"
BMC_AGENT_LLM_FEEDBACK_DISTILL_PROVIDER=anthropic \
BMC_AGENT_LLM_FEEDBACK_DISTILL_MODEL=claude-haiku-4-5 \
    bash tools/tune_agentic.sh haikumech || echo "[driver] haikumech rc=$?"
wait_idle; echo "[driver] haikumech done $(date -u)"

echo "=== COMPARISON $(date -u) ==="
for d in $(ls -dt findings/tune_*/ 2>/dev/null); do
  [ -f "$d/DONE" ] || continue
  echo "--- $d"; cat "$d/DONE"; grep -E "GATE|REAL |FP " "$d/gate.txt" 2>/dev/null
done
echo "=== overnight tuning driver DONE $(date -u) ==="
