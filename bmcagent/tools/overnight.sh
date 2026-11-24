#!/usr/bin/env bash
# Server-side autonomous overnight (independent of any local machine).
# A: finish the realism discipline-rule decision (string+dtb+elf) -> push/hold.
# C: BUDGET-GUARDED agentic verification on attacker-facing parsers (adjudicated
#    findings, pre-run so morning triage is immediate). Pre-pings the API before
#    each module; HALTS cleanly on budget exhaustion (no contaminated runs).
# B: budget-free WHOLE-KERNEL CBMC coverage (--no-agentic) across all modules.
# STATUS file (/tmp/overnight_STATUS.txt) is the single resume anchor.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=/tmp/overnight.log; STATUS=/tmp/overnight_STATUS.txt
exec >> "$LOG" 2>&1
BR=reproducer-agent-merge; KDIR=examples/vibeos/repo/kernel
say(){ echo "[$(date -u +%H:%M:%SZ)] $*"; echo "[$(date -u +%H:%M:%SZ)] $*" >> "$STATUS"; }
: > "$STATUS"; say "overnight START pid=$$ head=$(git rev-parse --short HEAD)"
idle(){ while pgrep -f "[.]venv/bin/bmc-agent verify" >/dev/null 2>&1; do sleep 60; done; }
[[ -r "$HOME/.config/bmc-agent/env" ]] && source "$HOME/.config/bmc-agent/env"
source .venv/bin/activate 2>/dev/null || true

# ---------- A: discipline-rule decision ----------
say "PHASE A: waiting for disc_test (string+dtb)..."
while pgrep -f "tools/disc_test.sh" >/dev/null 2>&1; do sleep 60; done
idle; say "disc_test done; disc_elf (over-tighten guard)"
PER_FUNC_BUDGET=180 OVERALL_TIMEOUT=9000 bash tools/judge_run.sh "disc_elf" "elf" >> "$LOG" 2>&1 || say "disc_elf rc=$?"
idle
python3 tools/overnight_decide.py > /tmp/overnight_decision.txt 2>&1; RC=$?
say "decider rc=$RC"; sed "s/^/[decider] /" /tmp/overnight_decision.txt >> "$STATUS"
git add findings/JUDGMENT_NOTES.md 2>/dev/null
git -c user.name=syc -c user.email=daniel1988xyz@gmail.com commit -q -m "docs: overnight discipline-rule decision (auto)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" 2>/dev/null && say "committed decision note"
if [ "$RC" = "0" ]; then
  git fetch origin -q 2>/dev/null
  if git merge-base --is-ancestor origin/main "$BR" 2>/dev/null; then
    git push origin "$BR:main" >> "$LOG" 2>&1 && say "PASS -> PUSHED origin/main $(git rev-parse --short HEAD)" || say "PASS push FAILED"
  else say "PASS but origin/main diverged -- not pushing"; fi
else say "verdict!=PASS (rc=$RC) -> HOLD (discipline commits stay local for review)"; fi

# ---------- C: budget-guarded agentic parser verification ----------
say "PHASE C: budget-guarded agentic verification (attacker-facing parsers)"
for m in net fat32 ttf virtio_net keyboard; do
  if ! python3 tools/budget_ping.py >/dev/null 2>&1; then
    say "  BUDGET EXHAUSTED -> halting Phase C (skipping $m and rest; no contaminated runs)"; break
  fi
  idle; say "  agentic verify: $m"
  PER_FUNC_BUDGET=180 OVERALL_TIMEOUT=9000 bash tools/judge_run.sh "morn_$m" "$m" >> "$LOG" 2>&1 || say "  $m rc=$?"
  d=$(cat /tmp/judge_morn_$m.txt 2>/dev/null)
  bad=$(grep -ciE "workspace API usage|Error code: 400|Realism check LLM call failed" "$d/run.log" 2>/dev/null || echo 0)
  cex=$(grep "CBMC verdict for" "$d/run.log" 2>/dev/null | grep -c "verified=False" || echo 0)
  say "  done $m -> $d (CBMC cex=$cex; contamination_hits=$bad)"
  if [ "$bad" != "0" ]; then say "  CONTAMINATION during $m -> halting Phase C"; break; fi
done
say "PHASE C done."

# ---------- B: budget-free whole-kernel CBMC coverage ----------
say "PHASE B: whole-kernel CBMC coverage (--no-agentic, no LLM budget)"
COV=findings/kernel_coverage_$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$COV"
for f in "$KDIR"/*.c; do
  m=$(basename "$f" .c); idle; say "  cov: $m"
  timeout 1800 ./.venv/bin/bmc-agent verify --source "$f" --driver "cov_$m" --no-agentic \
      --include-dir "$KDIR" --include-dir "$KDIR/libc" --per-function-time-budget 30 \
      --output "$COV/$m" > "$COV/$m.log" 2>&1 || true
  tot=$(grep -c "CBMC verdict for" "$COV/$m.log" 2>/dev/null || echo 0)
  cex=$(grep "CBMC verdict for" "$COV/$m.log" 2>/dev/null | grep -c "verified=False" || echo 0)
  echo "$m  functions=$tot  counterexamples=$cex" >> "$COV/MAP.txt"
  say "    $m: functions=$tot cex=$cex"
done
say "PHASE B done. Map: $COV/MAP.txt"; cat "$COV/MAP.txt" >> "$STATUS" 2>/dev/null
say "overnight DONE head=$(git rev-parse --short HEAD)"
