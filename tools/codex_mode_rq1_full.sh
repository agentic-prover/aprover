#!/usr/bin/env bash
# Full RQ1-shaped Codex-mode runner.
# Generates an AWS+LDV task manifest from the RQ1 artifact and runs each task
# through bmc-agent with --agentic-codex and --plan. Designed for detached
# overnight use.
set -u

cd "$HOME/AProver" || exit 1
PY="${PY:-$HOME/AProver/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_ROOT="${OUT_ROOT:-findings/codex_mode_rq1_full/$RUN_ID}"
SUMMARY="$OUT_ROOT/summary.tsv"
MANIFEST="$OUT_ROOT/task_manifest.tsv"
LOG="$OUT_ROOT/runner.log"
RQ1_ROOT="/home/syc/aprover-findings-embargoed/findings/rq1-svcomp"
AWS_TASKS="$RQ1_ROOT/tier-a-aws/task_list.tsv"
LDV_RESULTS="$RQ1_ROOT/tier-b-ldv/results/bmc_agent.tsv"
AWS_BASE="/home/syc/svcomp_exp/bench/sv-benchmarks/c/aws-c-common"
LDV_BASE="/home/syc/svcomp_exp/bench/sv-benchmarks/c/ldv-linux-3.4-simple"
TASK_TIMEOUT="${TASK_TIMEOUT:-1200}"
SUITES="${SUITES:-aws ldv}"
MAX_TASKS="${MAX_TASKS:-0}"
TASK_FILTER="${TASK_FILTER:-}"

mkdir -p "$OUT_ROOT"
printf "suite\ttask\texpected\tobserved\tscore\trc\twall_s\tvcc\tagent_tokens\tagent_roles\toutdir\n" > "$SUMMARY"
: > "$LOG"

log(){ echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }

setup_codex(){
  if [ -z "${BMC_AGENT_CODEX_BIN:-}" ]; then
    if command -v codex >/dev/null 2>&1; then BMC_AGENT_CODEX_BIN="$(command -v codex)"
    elif [ -x "$HOME/.local/bin/codex" ]; then BMC_AGENT_CODEX_BIN="$HOME/.local/bin/codex"
    else BMC_AGENT_CODEX_BIN=codex; fi
  fi
  export BMC_AGENT_CODEX_BIN
  export BMC_AGENT_CODEX_TIMEOUT_S="${BMC_AGENT_CODEX_TIMEOUT_S:-600}"
  export BMC_AGENT_MAX_WORKERS="${BMC_AGENT_CODEX_MAX_WORKERS:-1}"
  export BMC_AGENT_PARALLEL_VALIDATION="${BMC_AGENT_CODEX_PARALLEL_VALIDATION:-0}"
}

build_manifest(){
  "$PY" - "$MANIFEST" "$AWS_TASKS" "$LDV_RESULTS" "$LDV_BASE" <<'PY'
import csv, glob, os, sys
out, aws_tasks, ldv_results, ldv_base = sys.argv[1:]
rows = []
with open(aws_tasks, encoding="utf-8") as f:
    for raw in f:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        path, expected = parts[0], parts[1]
        task = os.path.basename(path)
        if task.endswith(".i"):
            task = task[:-2]
        rows.append(("aws", task, path, expected))
ldv_files = glob.glob(os.path.join(ldv_base, "*"))
with open(ldv_results, encoding="utf-8") as f:
    reader = csv.DictReader(f, delimiter="\t")
    for r in reader:
        task = (r.get("task") or "").strip()
        if not task:
            continue
        matches = [p for p in ldv_files if task in os.path.basename(p)]
        matches.sort(key=lambda p: (0 if p.endswith(".i") else 1, len(os.path.basename(p)), os.path.basename(p)))
        path = matches[0] if matches else ""
        rows.append(("ldv", task, path, "false"))
with open(out, "w", encoding="utf-8") as f:
    for row in rows:
        f.write("\t".join(row) + "\n")
PY
}

classify_run(){
  local rc="$1" log_file="$2"
  "$PY" - "$rc" "$log_file" <<'PY'
import re
import sys

rc = int(sys.argv[1])
path = sys.argv[2]
if rc == 124:
    print("timeout")
    raise SystemExit

state = None
try:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.search(r"Pipeline END:\s*([0-9]+)\s+real bug", line)
            if m:
                state = "false" if int(m.group(1)) else "true"
            elif re.search(r"\bConfirmed bugs:\s*[1-9][0-9]*\b", line):
                state = "false"
            elif re.search(r"\bTotal bugs found:\s*[1-9][0-9]*\b", line):
                state = "false"
            elif "No bugs confirmed." in line or "No bugs found for driver" in line:
                state = "true"
            elif "VERIFICATION FAILED" in line:
                state = "false"
            elif "VERIFICATION SUCCESSFUL" in line or "verified=True" in line:
                state = "true"
except OSError:
    state = None

print(state or "unknown")
PY
}

score_run(){
  local expected="$1" observed="$2"
  if [ "$observed" = timeout ] || [ "$observed" = unknown ]; then echo unknown
  elif [ "$observed" = "$expected" ]; then echo correct
  elif [ "$expected" = true ]; then echo FALSE-ALARM
  else echo MISSED-BUG; fi
}

cleanup_after_timeout(){
  local rc="$1" wd="$2"
  [ "$rc" -eq 124 ] || return 0
  pkill -TERM -f "codex exec --sandbox read-only" 2>/dev/null || true
  pkill -TERM -f "cbmc .*${wd}" 2>/dev/null || true
  sleep 2
  pkill -KILL -f "codex exec --sandbox read-only" 2>/dev/null || true
  pkill -KILL -f "cbmc .*${wd}" 2>/dev/null || true
}

telemetry_field(){
  local file="$1" mode="$2"
  "$PY" - "$file" "$mode" <<'PY'
import json, sys
path, mode = sys.argv[1], sys.argv[2]
try: data = json.load(open(path, encoding="utf-8"))
except Exception:
    print("0" if mode == "tokens" else ""); raise SystemExit
records = data.get("records") or []
if mode == "tokens": print(sum(int(r.get("tokens") or 0) for r in records))
else:
    summary = data.get("summary") or {}; parts = []
    for role in sorted(summary):
        calls = summary[role].get("calls", 0)
        toks = sum(int(r.get("tokens") or 0) for r in records if r.get("role") == role)
        parts.append(f"{role}:{calls}/{toks}")
    print(",".join(parts))
PY
}

should_run_task(){
  local suite="$1" task="$2"
  case " $SUITES " in *" $suite "*) ;; *) return 1 ;; esac
  if [ -n "$TASK_FILTER" ]; then
    case ",$TASK_FILTER," in *",$task,"*) ;; *) return 1 ;; esac
  fi
  return 0
}

run_one(){
  local suite="$1" task="$2" src="$3" expected="$4"
  [ -f "$src" ] || { log "missing source suite=$suite task=$task src=$src"; return 0; }
  setup_codex
  # PlanAgent owns BMC strategy/config. Keep this parent environment free of
  # legacy strategy overrides so every subprocess starts from the benchmark
  # source, entry, and property rather than suite labels.
  unset SVCOMP_PROP SVCOMP_ARCH SVCOMP_UNWIND SVCOMP_TIMEOUT
  unset BMC_FRAME_HAVOC BMC_BUGHUNT BMC_FAITHFUL_MAIN BMC_TRANSITIVE_INLINE
  unset BMC_CONE_SLICE BMC_CONE_TIGHT BMC_CONE_PROP BMC_ASSERT_BOUNDS_ONLY
  local wd="$OUT_ROOT/codex/$suite/$task"; mkdir -p "$wd"
  local t0=$(date +%s)
  log "start suite=$suite task=$task expected=$expected"
  (ulimit -v 42000000; timeout "$TASK_TIMEOUT" "$PY" -m bmc_agent.cli verify \
    --source "$src" --provider codex --agentic-codex --entry main --plan --svcomp \
    --driver "rq1codex_${suite}_${task}" --output "$wd") > "$wd/run.log" 2>&1
  local rc=$? t1=$(date +%s)
  cleanup_after_timeout "$rc" "$wd"
  local observed score vcc tokens roles
  observed="$(classify_run "$rc" "$wd/run.log")"
  score="$(score_run "$expected" "$observed")"
  vcc="$(grep -aoE "Generated [0-9]+ VCC" "$wd/run.log" | tail -1 | tr " " "_" || true)"
  tokens="$(telemetry_field "$wd/agent_telemetry.json" tokens)"
  roles="$(telemetry_field "$wd/agent_telemetry.json" roles)"
  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
    "$suite" "$task" "$expected" "$observed" "$score" "$rc" "$((t1-t0))" \
    "${vcc:-}" "${tokens:-0}" "${roles:-}" "$wd" >> "$SUMMARY"
  log "done suite=$suite task=$task observed=$observed score=$score rc=$rc wall=$((t1-t0))s tokens=${tokens:-0}"
}

build_manifest
log "Codex RQ1 full start run_id=$RUN_ID out=$OUT_ROOT suites=[$SUITES] max_tasks=$MAX_TASKS filter=[${TASK_FILTER:-all}]"
count=0
TAB=$(printf "\011")
while IFS="$TAB" read -r suite task src expected; do
  should_run_task "$suite" "$task" || continue
  count=$((count+1))
  if [ "$MAX_TASKS" -gt 0 ] && [ "$count" -gt "$MAX_TASKS" ]; then log "MAX_TASKS reached ($MAX_TASKS)"; break; fi
  run_one "$suite" "$task" "$src" "$expected"
done < "$MANIFEST"
log "Codex RQ1 full done summary=$SUMMARY"
