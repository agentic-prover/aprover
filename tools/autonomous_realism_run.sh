#!/usr/bin/env bash
# Unattended autonomous Claude Code runner. Survives SSH close (launch via setsid+nohup).
# Loops headless `claude -p`, resuming the same session so context carries across iterations.
# Stops cleanly when STATUS.md reports BLOCKED / PHASE4-REACHED / DONE, on no-progress, or at the cap.
#
# Launch (detached, survives logout):
#   setsid nohup bash /home/syc/AProver/tools/autonomous_realism_run.sh >/dev/null 2>&1 &
# Watch:
#   tail -f /home/syc/AProver/findings/autonomous_realism/run.log
#   cat     /home/syc/AProver/findings/autonomous_realism/STATUS.md
# Stop early:
#   touch   /home/syc/AProver/findings/autonomous_realism/STOP
set -u

ROOT=/home/syc/AProver
OUTDIR="$ROOT/findings/autonomous_realism"
PROMPT_FILE="$ROOT/tools/autonomous_realism_prompt.md"
STATUS="$OUTDIR/STATUS.md"
LOG="$OUTDIR/run.log"
SIDFILE="$OUTDIR/session_id"
STOPFILE="$OUTDIR/STOP"

MAX_ITERS=60            # hard cap on loop iterations
ITER_TIMEOUT=5400       # per-iteration wall-clock cap (seconds) = 90 min
SLEEP_BETWEEN=20        # pause between iterations (seconds)
NOPROGRESS_LIMIT=8      # stop only after this many iterations with NO progress signal
                        # progress = STATUS.md changed OR new git commit (waiting on a
                        # long background run is NOT no-progress; the worker also writes a
                        # heartbeat into STATUS.md each turn, so the hash changes when active)

mkdir -p "$OUTDIR"
cd "$ROOT" || exit 1
rm -f "$STOPFILE"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

log "=== autonomous run start (pid $$) ==="
PROMPT="$(cat "$PROMPT_FILE")"
SID=""
prev_hash=""
prev_head=""
noprogress=0

for i in $(seq 1 "$MAX_ITERS"); do
  if [ -f "$STOPFILE" ]; then log "STOP file present -> exiting"; break; fi
  log "--- iteration $i/$MAX_ITERS ---"

  if [ -z "$SID" ]; then
    MSG="$PROMPT"
    RESUME=()
  else
    MSG="Continue the autonomous plan from STATUS.md. Same hard constraints. Update STATUS.md."
    RESUME=(--resume "$SID")
  fi

  OUT=$(timeout "$ITER_TIMEOUT" claude -p "$MSG" \
          "${RESUME[@]}" \
          --permission-mode bypassPermissions \
          --output-format json </dev/null 2>>"$LOG")
  rc=$?
  if [ $rc -eq 124 ]; then log "iteration $i timed out after ${ITER_TIMEOUT}s"; fi

  # Capture/refresh session id from the result JSON (python3 is reliable; jq may be absent).
  NEWSID=$(printf '%s' "$OUT" | python3 -c \
      'import sys,json
try:
    d=json.load(sys.stdin); print(d.get("session_id",""))
except Exception:
    print("")' 2>/dev/null)
  if [ -n "$NEWSID" ]; then SID="$NEWSID"; printf '%s\n' "$SID" >"$SIDFILE"; fi

  # Append the assistant result text to the human log for visibility.
  printf '%s' "$OUT" | python3 -c \
      'import sys,json
try:
    d=json.load(sys.stdin); print(d.get("result","") or d.get("error",""))
except Exception:
    pass' 2>/dev/null >>"$LOG"

  # Sentinel: read STATE from STATUS.md.
  state=""
  if [ -f "$STATUS" ]; then
    state=$(grep -m1 '^STATE:' "$STATUS" | sed 's/^STATE:[[:space:]]*//' | tr -d '[:space:]')
  fi
  log "iteration $i state=${state:-<none>} rc=$rc"
  case "$state" in
    BLOCKED|PHASE4-REACHED|DONE) log "terminal state '$state' -> exiting loop"; break ;;
  esac

  # No-progress guard: progress = STATUS.md changed OR a new git commit landed.
  # Waiting on a long background run (STATUS heartbeat updated, no commit yet) still
  # counts as progress because the heartbeat changes the hash. Only a truly idle worker
  # (no STATUS change AND no new commit) for NOPROGRESS_LIMIT iterations stops the loop.
  cur_hash=$( [ -f "$STATUS" ] && md5sum "$STATUS" | cut -d' ' -f1 || echo none )
  cur_head=$(git rev-parse HEAD 2>/dev/null || echo none)
  if [ "$cur_hash" = "$prev_hash" ] && [ "$cur_head" = "$prev_head" ]; then
    noprogress=$((noprogress+1))
    log "no progress signal ($noprogress/$NOPROGRESS_LIMIT)"
    if [ "$noprogress" -ge "$NOPROGRESS_LIMIT" ]; then log "no progress -> exiting"; break; fi
  else
    noprogress=0
  fi
  prev_hash="$cur_hash"; prev_head="$cur_head"

  sleep "$SLEEP_BETWEEN"
done

log "=== autonomous run end (last state=${state:-<none>}) ==="
