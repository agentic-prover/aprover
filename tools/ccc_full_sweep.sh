#!/usr/bin/env bash
# FULL CCC codebase sweep (entire ~/claudes-c-compiler/src), CARGO-MODE
# (kani_real_crate=true → resolves crate types/E0433 + in-crate). Anthropic
# backend. Resilient: serial, backoff on rate-limit, resumable (.ccc_cm_done),
# and KILLS lingering kani per file (kani hangs on some fns → orphan leak).
# Covers free fns + static methods (self-methods pending the extension).
# Realism-filtered. Detached → survives SSH/internet loss. WEEKS-scale.
set -u
cd ~/AProver
export ANTHROPIC_API_KEY="$(cat ~/.config_bmc_anthropic_key)"
export BMC_AGENT_LLM_PROVIDER=anthropic
export BMC_AGENT_KANI_REAL_CRATE=true
export PATH="$HOME/.cargo/bin:$PATH"
CCC="$HOME/claudes-c-compiler"
LOG=/tmp/ccc_full.log; STATUS=/tmp/ccc_full_STATUS.txt; SUM=/tmp/ccc_full_SUMMARY.txt
DONEDIR=findings/.ccc_cm_done; mkdir -p "$DONEDIR"
exec >> "$LOG" 2>&1
say(){ echo "[$(date -u +%FT%TZ)] $*" >> "$STATUS"; }
CONTAM="call failed|Error code: 429|rate_limit|overloaded|Error code: 5[0-9][0-9]|ANTHROPIC_API_KEY is not set"
BACKOFF=300; RETRIES=5
# Priority order: untrusted-input frontend + common first, then the rest of src.
ORDER=""
for d in common ir passes frontend/preprocessor frontend/lexer frontend/parser frontend/sema backend driver bin; do
  for f in $(cd "$CCC" && find "src/$d" -name "*.rs" 2>/dev/null | sort); do ORDER="$ORDER $f"; done
done
# any src/*.rs not under the above (lib.rs, main.rs, etc.)
for f in $(cd "$CCC" && find src -maxdepth 1 -name "*.rs" | sort); do ORDER="$ORDER $f"; done
NFILES=$(echo $ORDER | wc -w)
say "ccc FULL sweep START pid=$$ cargo-mode anthropic files=$NFILES kani=$(cargo kani --version 2>&1|head -1)"
for rel in $ORDER; do
  name=$(echo "$rel" | sed -E "s#^src/##; s#/#__#g; s#\.rs##")
  [ -e "$DONEDIR/$name" ] && continue
  attempt=0
  while :; do
    attempt=$((attempt+1))
    TS=$(date -u +%Y%m%dT%H%M%SZ); OUT=findings/cccf_${name}_${TS}; ML=${OUT}.log; mkdir -p "$OUT"
    say "BEGIN $name (attempt $attempt/$RETRIES)"
    timeout 18000 ./.venv/bin/bmc-agent verify --source "$CCC/$rel" \
      --driver "cccf_${name}" --agentic --per-function-time-budget 180 --output "$OUT" > "$ML" 2>&1
    rc=$?
    # CLEANUP: kill any kani/cbmc processes left hanging from THIS file (orphan leak guard).
    # cbmc/kani-driver grandchildren do NOT carry the output-dir or cccf_ name in their
    # cmdline, so name-based pkill misses them -> they orphan at 100% CPU for hours.
    # Files run serially, so killing by exact comm between files is safe.
    pkill -9 -f "$OUT/" 2>/dev/null; pkill -9 -f "cccf_${name}" 2>/dev/null
    pkill -9 -x cbmc 2>/dev/null; pkill -9 -x kani-driver 2>/dev/null
    pkill -9 -x cargo-kani 2>/dev/null; pkill -9 -x kani-compiler 2>/dev/null
    contam=$(grep -cE "$CONTAM" "$ML" 2>/dev/null)
    realbugs=$(python3 - "$OUT" <<PY 2>/dev/null
import json,glob,sys
seen=set()
for j in glob.glob(sys.argv[1]+"/**/bug_reports/*.json",recursive=True):
    try: r=json.load(open(j)).get("report",{})
    except: continue
    rc=r.get("realism_check") or {}
    if (rc.get("verdict") if isinstance(rc,dict) else None)=="realistic": seen.add((r.get("function_name"),r.get("violated_property")))
print(len(seen))
PY
)
    if [ "$rc" -ne 124 ] && [ "${contam:-0}" -eq 0 ]; then
      say "END   $name rc=$rc CLEAN realism_real=${realbugs:-0}"
      echo "$name | clean | realism_real=${realbugs:-0}" >> "$SUM"; touch "$DONEDIR/$name"; break
    fi
    say "RETRY $name rc=$rc contam=$contam (124=timeout) attempt $attempt"
    [ "$attempt" -ge "$RETRIES" ] && { say "DEFER $name"; echo "$name | DEFERRED rc=$rc" >> "$SUM"; touch "$DONEDIR/$name"; break; }
    sleep "$BACKOFF"
  done
done
say "ccc FULL sweep DONE"
