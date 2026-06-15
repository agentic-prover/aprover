#!/usr/bin/env bash
# Server-side autonomous overnight orchestrator (independent of any local machine).
# 1) wait for disc_test (string+dtb) to finish
# 2) run the deterministic decider on the discipline-rule re-validation
# 3) PASS -> commit decision + fast-forward push branch->main; else record + HOLD (no push)
# 4) queue net+fat32 readiness runs (data for morning human/Claude adjudication)
# Everything logged; a STATUS file is the single resume anchor.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
LOG=/tmp/overnight.log
STATUS=/tmp/overnight_STATUS.txt
exec >> "$LOG" 2>&1
BR=reproducer-agent-merge
say(){ echo "[$(date -u +%H:%M:%SZ)] $*"; echo "[$(date -u +%H:%M:%SZ)] $*" >> "$STATUS"; }
: > "$STATUS"
say "overnight START pid=$$ branch=$BR head=$(git rev-parse --short HEAD)"

idle(){ while pgrep -f "[.]venv/bin/bmc-agent verify" >/dev/null 2>&1; do sleep 60; done; }

# 1) wait for the disc_test driver + its sweeps to finish
say "waiting for disc_test (string+dtb) to finish..."
while pgrep -f "tools/disc_test.sh" >/dev/null 2>&1; do sleep 60; done
idle
say "disc_test finished."

# 2) deterministic decision
python3 tools/overnight_decide.py > /tmp/overnight_decision.txt 2>&1
RC=$?
say "decider rc=$RC"
sed "s/^/[decider] /" /tmp/overnight_decision.txt >> "$STATUS"

# 3) act on the verdict
git add findings/JUDGMENT_NOTES.md 2>/dev/null
git -c user.name=syc -c user.email=daniel1988xyz@gmail.com commit -q -m "docs: overnight discipline-rule decision (auto)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" 2>/dev/null && say "committed decision note"
if [ "$RC" = "0" ]; then
  say "VERDICT=PASS -> pushing branch->main (fast-forward)"
  git fetch origin -q 2>/dev/null
  if git merge-base --is-ancestor origin/main "$BR" 2>/dev/null; then
    if git push origin "$BR:main" >> "$LOG" 2>&1; then
      say "PUSHED to origin/main: $(git rev-parse --short HEAD)"
    else
      say "PUSH FAILED — left for review (see log)"
    fi
  else
    say "origin/main is NOT an ancestor (diverged) — NOT pushing; left for review"
  fi
else
  say "VERDICT!=PASS (rc=$RC) -> NOT pushing the rule; left for human review. Discipline commits 05697b9/e43a2c5 remain local-only on $BR."
fi

# 4) queue readiness data for morning (best-effort; budget may run out -> partial is fine)
say "queueing net+fat32 readiness runs for morning adjudication"
for mod in net fat32; do
  idle
  say "START readiness $mod"
  PER_FUNC_BUDGET=180 OVERALL_TIMEOUT=9000 bash tools/judge_run.sh "morn_$mod" "$mod" >> "$LOG" 2>&1 || say "readiness $mod rc=$?"
  say "DONE readiness $mod -> $(cat /tmp/judge_morn_$mod.txt 2>/dev/null)"
done

say "overnight DONE head=$(git rev-parse --short HEAD)"
