#!/usr/bin/env bash
# Sequential --agentic DEFAULT-config sweep across multiple VibeOS kernel modules,
# for judgment-based finding adjudication (no oracle). One module at a time.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=/tmp/judge_sweep.log
exec >> "$LOG" 2>&1
echo "=== judge_sweep START $(date -u) PID=$$ ==="
wait_idle(){ while pgrep -f "[.]venv/bin/bmc-agent verify" >/dev/null 2>&1; do sleep 30; done; }
for mod in "$@"; do
  wait_idle
  echo "[sweep] START $mod $(date -u)"
  PER_FUNC_BUDGET=240 OVERALL_TIMEOUT=5400 bash tools/judge_run.sh "def_$mod" "$mod" || echo "[sweep] $mod rc=$?"
  echo "[sweep] DONE $mod -> $(cat /tmp/judge_def_$mod.txt 2>/dev/null)"
done
echo "=== judge_sweep DONE $(date -u) ==="
