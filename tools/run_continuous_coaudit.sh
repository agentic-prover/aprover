#!/usr/bin/env bash
# Continuous coaudit over OSS-Fuzz projects — INCLUDING newly-added/updated ones.
#
# This is the MECHANIZABLE half of the coaudit loop (Direction B generator): for each
# project it derives build flags and runs bmc-agent's FP-hint net (oss_bmc.py), emitting a
# per-project WORKLIST. The LLM-driven half (Direction-A audit, adjudication, ASan
# confirmation, embargo push) runs in the scheduled coaudit agent passes that consume these
# worklists. See .claude/commands/coaudit.md and memory project_continuous_coaudit_setup.
#
# Each cycle:
#   1. refresh the OSS-Fuzz onboarding metadata (blobless clone) and detect NEW projects
#      since the last cycle (state file), plus keep a rotation of known-productive targets;
#   2. for each queued project: clone (shallow) if missing, run oss_bmc.py with real -I/-D,
#      log the worklist; explicitly LOG (never silently skip) projects whose build the net
#      can't integrate (autotools w/o compiledb, C++-only, etc.);
#   3. sleep between projects; re-detect new projects every cycle.
#
# Usage:  tools/run_continuous_coaudit.sh            # run forever (nohup/tmux/cron)
#         tools/run_continuous_coaudit.sh --once     # one cycle then exit
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# --- config / state ------------------------------------------------------------
META=/tmp/oss-fuzz-meta                       # blobless OSS-Fuzz clone (onboarding dates)
CORP=/tmp/oss_fuzz_corpora
STATE=tools/.coaudit_seen_projects            # projects we've already queued
OUT=findings/oss_fuzz_coaudit                 # worklists land here (gitignored)
ENVF="$HOME/.config/bmc-agent/env"
mkdir -p "$CORP" "$OUT"
[[ -r "$ENVF" ]] && source "$ENVF"

# Productive / cmake-buildable C targets that ARE real OSS-Fuzz projects (verified in projects/).
# (libmikmod/dumb are NOT OSS-Fuzz — excluded; they live under findings/open-source/ separately.)
ROTATION=(libredwg matio openjpeg libsndfile libucl)
ONCE=0; [[ "${1:-}" == "--once" ]] && ONCE=1

log(){ echo "[coaudit $(date -u +%H:%M:%SZ)] $*"; }

refresh_meta(){
  if [[ -d "$META/.git" ]]; then git -C "$META" fetch -q --filter=blob:none origin 2>/dev/null
  else git clone -q --filter=blob:none --no-checkout https://github.com/google/oss-fuzz.git "$META" 2>/dev/null; fi
}

# newest-onboarded C/C++ projects (skip the obviously memory-safe langs)
newest_projects(){
  git -C "$META" log --diff-filter=A --name-only --format='C %cs' -- 'projects/*/project.yaml' 2>/dev/null \
   | awk '/^C /{d=$2} /project.yaml/{n=$0;sub("projects/","",n);sub("/project.yaml","",n);print n}' \
   | head -40
}

detect_new(){
  touch "$STATE"
  newest_projects | while read -r p; do grep -qxF "$p" "$STATE" 2>/dev/null || echo "$p"; done
}

# META is a blobless --no-checkout clone, so read project.yaml via `git show` (fetches blob on demand)
yaml_of(){ git -C "$META" show "HEAD:projects/$1/project.yaml" 2>/dev/null; }
repo_of(){ yaml_of "$1" | grep -E '^main_repo' | sed -E "s/^main_repo: *//; s/^[\"']//; s/[\"'] *$//"; }
lang_of(){ yaml_of "$1" | grep -E '^language'  | sed -E 's/.*: *"?([a-z+]+).*/\1/'; }

run_one(){
  local p="$1" ts; ts=$(date -u +%Y%m%dT%H%M%SZ)
  local lang; lang=$(lang_of "$p")
  if [[ "$lang" != "c" && "$lang" != "c++" ]]; then
    log "SKIP $p (lang=$lang; bmc-agent/CBMC targets C/C++ only)"; grep -qxF "$p" "$STATE" || echo "$p" >> "$STATE"; return; fi
  # clone if needed
  if [[ ! -d "$CORP/$p" ]]; then
    local r; r=$(repo_of "$p"); [[ -z "$r" ]] && { log "SKIP $p (no main_repo)"; return; }
    log "clone $p <- $r"; git clone -q --depth 1 "$r" "$CORP/$p" 2>/dev/null || { log "SKIP $p (clone failed)"; return; }
  fi
  log "net $p (lang=$lang) -> $OUT/${p}_${ts}.log"
  # PYTHONUNBUFFERED + `python3 -u`: per-file worklist lines flush as produced, so they
  # SURVIVE the wall-clock SIGTERM. Targets like libredwg whose 8 files * 400s budget
  # exceeds 1800s always time out; without unbuffered output their block-buffered stdout
  # is discarded on kill and the worklist log is 0 bytes (every cycle). Now we keep the
  # partial worklist for the files that completed before the timeout.
  PYTHONUNBUFFERED=1 timeout 1800 python3 -u tools/cbmc_direct/oss_bmc.py "$p" --max-files 8 --timeout 400 \
      > "$OUT/${p}_${ts}.log" 2>&1
  if grep -qiE 'could not derive compile_commands' "$OUT/${p}_${ts}.log"; then
    log "  $p: build-integration FAILED (autotools/no compiledb) — flagged for manual -I/-D"; fi
  grep -qxF "$p" "$STATE" || echo "$p" >> "$STATE"
  # surface any FAILED/ADJUDICATE worklist lines into the cycle log
  grep -E 'ADJUDICATE|FAILED|HINT WORKLIST|hint\(s\)' "$OUT/${p}_${ts}.log" 2>/dev/null | sed 's/^/    /' | head -20
}

cycle(){
  refresh_meta
  log "=== cycle start ==="
  local newp; newp=$(detect_new)
  if [[ -n "$newp" ]]; then log "NEW OSS-Fuzz projects since last cycle: $(echo "$newp" | tr '\n' ' ')"; fi
  # queue: new projects first (highest value), then the productive rotation
  local queue; queue=$(printf '%s\n' $newp "${ROTATION[@]}" | awk '!seen[$0]++')
  for p in $queue; do run_one "$p"; sleep 30; done
  log "=== cycle complete ==="
}

if [[ "$ONCE" == "1" ]]; then cycle; else while true; do cycle; log "sleeping 1h before next cycle"; sleep 3600; done; fi
