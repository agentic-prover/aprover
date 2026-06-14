#!/usr/bin/env bash
# Unattended overnight runner for the --agentic BUDGET-FREE hardening track.
#
# Repeatedly invokes headless `claude` on this box to do the SINGLE next
# incomplete budget-free step (registry wiring -> token plumbing -> output-
# contract validation -> test fidelity), validate it, and commit it — looping
# until the track is complete or a guardrail trips. Each iteration is a fresh
# headless session that re-derives state from docs/agentic_hardening_plan.md +
# git log, so it is resumable and bounded.
#
# It NEVER runs the budget-gated track (no `verify`/`--agentic` sweep, no live
# CBMC), so it spends no verification budget. Blast radius is a feature branch
# (unpushed); bad work is revert-on-failure + recoverable via git.
#
# Launch before you leave:
#   nohup ~/AProver/tools/overnight_agentic_hardening.sh \
#       > /tmp/overnight_hardening.out 2>&1 &
# Watch:  tail -f /tmp/overnight_hardening.out
# Stop:   rm -f /tmp/agentic_hardening.lock ; pkill -f overnight_agentic_hardening

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1            # -> ~/AProver
REPO="$PWD"
BRANCH="reproducer-agent-merge"
LOCK="/tmp/agentic_hardening.lock"
LOG_DIR="$REPO/findings/overnight_hardening"
MAX_ITERS="${MAX_ITERS:-8}"                  # safety cap (track is ~4 steps)
SENTINEL="BUDGET_FREE_TRACK_COMPLETE"
FAILMARK="STOP_VALIDATION_FAILED"

mkdir -p "$LOG_DIR"

# Single-instance lock.
exec 9>"$LOCK"
if ! flock -n 9; then echo "another run holds $LOCK — exiting"; exit 1; fi

git checkout -q "$BRANCH" || { echo "cannot checkout $BRANCH"; exit 1; }
echo "overnight hardening on $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"

read -r -d '' PROMPT <<'EOF'
You are running HEADLESS and UNATTENDED on the AProver box, cwd ~/AProver, git
branch reproducer-agent-merge. You have no memory of prior chats — derive all
state from files.

1. Read docs/agentic_hardening_plan.md (the resume anchor) and run
   `git log --oneline -14`.
2. Identify the SINGLE next INCOMPLETE step of the BUDGET-FREE track, in order:
   (a) wire bmc_agent/agent_registry.py into config.py + cli.py and add
       tests/test_agent_registry.py pinning the 11-role set;
   (b) plumb token usage out of LLMClient into bmc_agent/agent_telemetry.py;
   (c) centralize output-contract validation in BaseAgent;
   (d) test fidelity (randomized order + faithful agent doubles).
   Do NOT run the budget-gated track: no `verify`, no `--agentic` sweep, no live
   CBMC. If unsure whether a step is done, prefer the earliest not-yet-committed one.
3. Implement ONLY that one step. Then VALIDATE:
     python3 -m pytest tests/ -q -p no:cacheprovider
       -> baseline is 54 failures; your change is clean ONLY if the failure
          count does NOT rise above 54 (zero NEW failures).
     python3 -m pytest tests/test_phase3.py -q -p no:cacheprovider
       -> must stay at 3 failures (isolated check; full-suite ordering masks regressions).
     plus the targeted tests for your step (must pass).
4. If ANY new failure appears: REVERT your change (git checkout -- <files> /
   git reset), print exactly STOP_VALIDATION_FAILED, and end the turn. Do not commit.
5. Otherwise commit the one step:
     git -c user.name=syc commit -m "<clear message>"
   and end the turn.
6. If ALL budget-free steps are already complete (registry wired +
   test_agent_registry passing; token plumbing present; output-contract
   validation in BaseAgent; test fidelity in place), make NO changes and print
   exactly BUDGET_FREE_TRACK_COMPLETE.

Work autonomously; do not ask questions. One step per invocation, then stop.
EOF

for i in $(seq 1 "$MAX_ITERS"); do
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  LOG="$LOG_DIR/iter_${i}_${TS}.log"
  echo "=== iter ${i}/${MAX_ITERS} @ ${TS} -> ${LOG} ==="
  claude -p "$PROMPT" \
      --dangerously-skip-permissions \
      --permission-mode bypassPermissions \
      --output-format text \
      --max-turns 150 \
      >"$LOG" 2>&1 || echo "  (claude exited non-zero; see log)"

  if grep -q "$SENTINEL" "$LOG"; then
    echo "=== budget-free track COMPLETE (iter $i) ==="; break
  fi
  if grep -q "$FAILMARK" "$LOG"; then
    echo "=== STOPPED: a step failed validation (iter $i) — see $LOG ==="; break
  fi
  echo "  committed: $(git log --oneline -1)"
done

echo "=== overnight run finished @ $(date -u +%Y%m%dT%H%M%SZ); branch tip: $(git log --oneline -1) ==="
echo "logs: $LOG_DIR ; commits since start: review with 'git log --oneline'"
