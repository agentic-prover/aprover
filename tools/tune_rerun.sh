#!/usr/bin/env bash
# Re-run the all-agentic arm with the FIXED claude-code provider (d47f50c),
# then write the final 4-arm comparison. The original ccall arm is invalid
# (every claude -p call exited 1 on bad flags -> agents fell back to seed-only).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=/tmp/tune_rerun.log
exec >> "$LOG" 2>&1
echo "=== rerun driver START $(date -u) PID=$$ ==="
wait_idle(){ while pgrep -f "[.]venv/bin/bmc-agent verify" >/dev/null 2>&1; do sleep 30; done; }

wait_idle; echo "[rerun] idle -> START ccall2 (fixed claude-code) $(date -u)"
AGENTIC_FLAG="--agentic-claude-code" OVERALL_TIMEOUT=18000 PER_FUNC_BUDGET=180 \
    bash tools/tune_agentic.sh ccall2 || echo "[rerun] ccall2 rc=$?"
wait_idle; echo "[rerun] ccall2 done $(date -u)"

echo "=== FINAL COMPARISON $(date -u) ==="
for d in $(ls -dt findings/tune_baseline_*/ findings/tune_opusjudge_*/ findings/tune_haikumech_*/ findings/tune_ccall2_*/ 2>/dev/null); do
  [ -f "$d/DONE" ] || continue
  echo "--- $d"; cat "$d/DONE"; grep -E "GATE|REAL |FP " "$d/gate.txt" 2>/dev/null
done
echo "=== rerun driver DONE $(date -u) ==="
