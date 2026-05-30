#!/usr/bin/env bash
# Entry-point CBMC + ASan replay oracle.
#
# For a target, drive CBMC from the REAL fuzz entry (a byte buffer + length)
# whole-program, escalating the input bound. On a counterexample, concretize
# it to an input file and replay through the ASan/UBSan build. A crash there
# is a CONFIRMED, reproducible bug — no LLM realism guesswork.
#
# Usage: run_entry_oracle.sh <target>     (target = config key below)
#
# Why this beats per-function bmc-agent: every state CBMC explores from the
# entry is reachable by construction, so there is no caller-precondition /
# stubbed-callee false-positive class. The counterexample is literally the
# bytes a real fuzzer would feed in.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:?usage: run_entry_oracle.sh <target>}"
OUT="${CBMC_DIRECT_OUT:-/tmp/cbmc_direct_out}/$TARGET"
mkdir -p "$OUT"

# ---- per-target config -----------------------------------------------------
# SRC_DIR  : where the .c sources live
# SRCS     : library .c files to link whole-program (no main())
# INC       : -I include dirs
# HARNESS / DRIVER : the CBMC harness and the ASan replay driver (mirror each other)
# ARRAY/SIZEVAR : input-buffer + length variable names in the harness
case "$TARGET" in
  cmark)
    SRC_DIR=/tmp/oss_fuzz_corpora/cmark/src
    SRCS=$(ls "$SRC_DIR"/*.c | grep -v /main.c | tr '\n' ' ')
    INC="-I $SRC_DIR"
    HARNESS="$DIR/cmark_entry_harness.c"
    DRIVER="$DIR/cmark_replay_driver.c"
    ;;
  *)
    echo "unknown target '$TARGET'"; exit 2 ;;
esac
ARRAY=data; SIZEVAR=size

# ---- 1. compile the ASan replay binary once --------------------------------
REPLAY="$OUT/replay"
if [[ ! -x "$REPLAY" ]]; then
  echo "[oracle] building ASan replay binary..."
  gcc -fsanitize=address,undefined -g -O0 -w "$DRIVER" $SRCS $INC -o "$REPLAY" \
    || { echo "[oracle] replay build FAILED"; exit 1; }
fi

# ---- 2. escalate the input bound; CBMC from the entry ----------------------
CONFIRMED=0
for MAXLEN in ${CBMC_DIRECT_BOUNDS:-6 8 10 12 16}; do
  UNWIND=$((MAXLEN + 2))
  JSON="$OUT/cbmc_${MAXLEN}.json"
  echo "[oracle] CBMC MAXLEN=$MAXLEN unwind=$UNWIND (cap ${CBMC_TIMEOUT:-600}s)..."
  timeout "${CBMC_TIMEOUT:-600}" cbmc "$HARNESS" $SRCS $INC \
    --function cbmc_entry --unwind "$UNWIND" --unwinding-assertions \
    --bounds-check --pointer-check --signed-overflow-check \
    --unsigned-overflow-check --conversion-check --div-by-zero-check \
    -DMAXLEN=$MAXLEN --object-bits 12 --json-ui --trace > "$JSON" 2>"$OUT/cbmc_${MAXLEN}.err"
  rc=$?
  if [[ $rc -eq 124 ]]; then echo "[oracle]   timeout at MAXLEN=$MAXLEN; stopping escalation"; break; fi

  # ---- 3. concretize any counterexample --------------------------------
  INPUT="$OUT/cex_${MAXLEN}.bin"
  if python3 "$DIR/concretize.py" "$JSON" --array "$ARRAY" --size-var "$SIZEVAR" \
        --max "$MAXLEN" -o "$INPUT" > "$OUT/concretize_${MAXLEN}.txt" 2>&1; then
    echo "[oracle]   CBMC reports a violation at MAXLEN=$MAXLEN:"
    sed 's/^/[oracle]     /' "$OUT/concretize_${MAXLEN}.txt"

    # ---- 4. replay under ASan = ground truth -------------------------
    "$REPLAY" "$INPUT" > "$OUT/replay_${MAXLEN}.out" 2>&1
    rrc=$?
    if grep -qE "AddressSanitizer|runtime error|SUMMARY: (Address|Undefined)" "$OUT/replay_${MAXLEN}.out" \
       || [[ $rrc -ge 128 ]]; then
      echo "[oracle]   *** CONFIRMED REAL BUG — ASan replay crashed (rc=$rrc) ***"
      grep -E "ERROR|runtime error|SUMMARY|#0|#1|#2" "$OUT/replay_${MAXLEN}.out" | head -12 | sed 's/^/[oracle]     /'
      echo "[oracle]   reproducer: $INPUT"
      CONFIRMED=$((CONFIRMED+1))
    else
      echo "[oracle]   ASan replay did NOT crash (rc=$rrc) — CBMC CEx not reproducible on real build (likely harness/unwind artifact); discarding"
    fi
  else
    echo "[oracle]   MAXLEN=$MAXLEN: VERIFIED (no counterexample up to this bound)"
  fi
done

echo "[oracle] done: $CONFIRMED confirmed real bug(s) for target=$TARGET (artifacts in $OUT)"
exit 0
