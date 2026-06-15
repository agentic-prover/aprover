#!/usr/bin/env bash
# Per-component flat-vs-agent ablation on one module: default(all tools) vs
# flat vs reproducer-only vs spec_gen-flat. Sequential, judgment-adjudicated.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=/tmp/judge_ablate.log
exec >> "$LOG" 2>&1
MOD="${1:-dtb}"
echo "=== judge_ablate START $(date -u) module=$MOD PID=$$ ==="
wait_idle(){ while pgrep -f "[.]venv/bin/bmc-agent verify" >/dev/null 2>&1; do sleep 20; done; }
run(){ # label  extra_flags
  wait_idle
  echo "[ablate] START $1 ($2) $(date -u)"
  EXTRA_FLAGS="$2" PER_FUNC_BUDGET=180 OVERALL_TIMEOUT=5400 bash tools/judge_run.sh "ab_${MOD}_$1" "$MOD" || echo "[ablate] $1 rc=$?"
  echo "[ablate] DONE $1 -> $(cat /tmp/judge_ab_${MOD}_$1.txt 2>/dev/null)"
}
run deftools ""
run flat "--no-spec-gen-tools --no-reproducer-agent --no-bmc-config-agent"
run reproonly "--no-spec-gen-tools --no-bmc-config-agent"
echo "=== judge_ablate DONE $(date -u) ==="
