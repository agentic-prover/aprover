#!/usr/bin/env bash
# Empirical re-validation of the realism discipline rule (commit 05697b9).
# Plain --agentic now = agentic realism (default) + discipline prompt + adjacent-bug off.
# Expect: string strncpy FP -> demoted; memset (real, concrete elf caller) -> KEPT;
# dtb read_be64 (real OOB) -> KEPT.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=/tmp/disc_test.log
exec >> "$LOG" 2>&1
echo "=== disc_test START $(date -u) PID=$$ ==="
wait_idle(){ while pgrep -f "[.]venv/bin/bmc-agent verify" >/dev/null 2>&1; do sleep 20; done; }
for mod in string dtb; do
  wait_idle
  echo "[disc] START $mod $(date -u)"
  PER_FUNC_BUDGET=180 OVERALL_TIMEOUT=6000 bash tools/judge_run.sh "disc_$mod" "$mod" || echo "[disc] $mod rc=$?"
  echo "[disc] DONE $mod -> $(cat /tmp/judge_disc_$mod.txt 2>/dev/null)"
done
echo "=== disc_test DONE $(date -u) ==="
