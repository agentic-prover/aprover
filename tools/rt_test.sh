#!/usr/bin/env bash
# Test agentic realism (--enable-realism-tools) on the FP module (string) and a
# REAL-bug module (dtb): does the tool-use realism pass DEMOTE the string FPs
# while PRESERVING the dtb reals? Decides whether to make it default under --agentic.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=/tmp/rt_test.log
exec >> "$LOG" 2>&1
echo "=== rt_test START $(date -u) PID=$$ ==="
wait_idle(){ while pgrep -f "[.]venv/bin/bmc-agent verify" >/dev/null 2>&1; do sleep 20; done; }
for mod in dtb string; do
  wait_idle
  echo "[rt] START $mod (--enable-realism-tools) $(date -u)"
  EXTRA_FLAGS="--enable-realism-tools" PER_FUNC_BUDGET=180 OVERALL_TIMEOUT=6000 \
      bash tools/judge_run.sh "rt_$mod" "$mod" || echo "[rt] $mod rc=$?"
  echo "[rt] DONE $mod -> $(cat /tmp/judge_rt_$mod.txt 2>/dev/null)"
done
echo "=== rt_test DONE $(date -u) ==="
