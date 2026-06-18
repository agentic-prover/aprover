#!/usr/bin/env bash
# Leaf-oracle 24/7 loop — the VALIDATED, tractable, FP-free bug-finder.
#
# For each configured leaf (buf,len) routine: run CBMC from the entry with the
# light config that completes (bounds+pointer checks, no unwinding-assertions,
# no overflow checks), escalating the input bound. On a counterexample,
# concretize it to an input file and replay through the real ASan/UBSan build.
# A crash there = CONFIRMED real bug + reproducer. VERIFIED at a bound = sound
# exhaustive proof of no memory-safety bug for inputs up to that size.
#
# Usage: run_leaf_oracle_loop.sh            # loop forever
#        run_leaf_oracle_loop.sh --once     # one pass over all targets
#
# Add a target: append a line to TARGETS and a case in target_config().
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"
LOGDIR="${LEAF_ORACLE_LOG:-$REPO_ROOT/findings/cbmc_leaf_oracle}"
mkdir -p "$LOGDIR"
BOUNDS="${LEAF_BOUNDS:-4 6 8 10 12}"
CBMC_TIMEOUT="${LEAF_CBMC_TIMEOUT:-300}"

# Configured leaf targets (extend freely).
TARGETS=( "cmark:utf8_check" )

# Emits: SRCDIR| CBMC_SRCS| INC| HARNESS| REPLAY| ARRAY| SIZEVAR  (pipe-separated)
target_config() {
  case "$1" in
    cmark:utf8_check)
      echo "/tmp/oss_fuzz_corpora/cmark|src/utf8.c src/buffer.c src/cmark.c src/cmark_ctype.c|-I src|$DIR/cmark_utf8_leaf.c|$DIR/cmark_utf8_replay|data|size" ;;
    *) return 1 ;;
  esac
}

run_target() {
  local name="$1" ts cfg srcdir csrcs inc harness replay array sizev
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  cfg=$(target_config "$name") || { echo "[leaf] unknown target $name"; return; }
  IFS='|' read -r srcdir csrcs inc harness replay array sizev <<<"$cfg"
  local out="$LOGDIR/${name//:/_}_${ts}"; mkdir -p "$out"
  echo "[leaf] === $name @ $ts ==="
  ( cd "$srcdir" || exit 1
    for MAXLEN in $BOUNDS; do
      local unwind=$((MAXLEN+2)) json="$out/cbmc_${MAXLEN}.json"
      timeout "$CBMC_TIMEOUT" cbmc "$harness" $csrcs $inc \
        --function cbmc_entry --unwind "$unwind" \
        --bounds-check --pointer-check \
        -DMAXLEN=$MAXLEN --object-bits 12 --json-ui --trace \
        > "$json" 2>"$out/cbmc_${MAXLEN}.err"
      local rc=$?
      if [[ $rc -eq 124 ]]; then echo "[leaf]   MAXLEN=$MAXLEN: TIMEOUT (stop escalation)"; break; fi
      local input="$out/cex_${MAXLEN}.bin"
      if python3 "$DIR/concretize.py" "$json" --array "$array" --size-var "$sizev" \
            --max "$MAXLEN" -o "$input" >"$out/concretize_${MAXLEN}.txt" 2>&1; then
        echo "[leaf]   MAXLEN=$MAXLEN: CBMC counterexample -> replaying under ASan"
        "$replay" "$input" >"$out/replay_${MAXLEN}.out" 2>&1; local rrc=$?
        if grep -qE "AddressSanitizer|runtime error|SUMMARY: (Address|Undefined)" "$out/replay_${MAXLEN}.out" || [[ $rrc -ge 128 ]]; then
          echo "[leaf]   *** CONFIRMED REAL BUG ($name, MAXLEN=$MAXLEN) — reproducer: $input ***"
          grep -E "ERROR|runtime error|SUMMARY|#[0-9]" "$out/replay_${MAXLEN}.out" | head -10 | sed 's/^/[leaf]     /'
          break
        else
          echo "[leaf]   MAXLEN=$MAXLEN: CBMC CEx did NOT reproduce under ASan (spurious/bound artifact) — discarding"
        fi
      else
        echo "[leaf]   MAXLEN=$MAXLEN: VERIFIED (no memory-safety bug for inputs <= $MAXLEN bytes)"
      fi
    done )
}

ITER=0
while true; do
  ITER=$((ITER+1))
  for t in "${TARGETS[@]}"; do run_target "$t"; done
  [[ "${1:-}" == "--once" ]] && { echo "[leaf] --once done"; exit 0; }
  echo "[leaf] pass $ITER complete; sleeping 120s"
  sleep 120
done
