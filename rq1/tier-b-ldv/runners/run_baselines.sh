#!/usr/bin/env bash
set -euo pipefail

: "${SV_BENCHMARKS_ROOT:?set SV_BENCHMARKS_ROOT to an sv-benchmarks checkout}"
: "${ESBMC_ROOT:?set ESBMC_ROOT to the ESBMC installation root}"
: "${CPACHECKER_ROOT:?set CPACHECKER_ROOT to the CPAchecker installation root}"

LDV="${RQ1_LDV_TASK_ROOT:-${SV_BENCHMARKS_ROOT}/c/ldv-linux-3.4-simple}"
ESBMC="${ESBMC:-${ESBMC_ROOT}/bin/esbmc}"
CPA="${CPA:-${CPACHECKER_ROOT}/bin/cpachecker}"
CPACFG="${CPACFG:-${CPACHECKER_ROOT}/config/svcomp26.properties}"
PRP="${SVCOMP_PROP:-${SV_BENCHMARKS_ROOT}/c/properties/unreach-call.prp}"
RAW="${RQ1_TIER_B_BASELINE_RAW:-/tmp/escpa2_raw}"
TSV="${RQ1_TIER_B_BASELINE_TSV:-/tmp/escpa2.tsv}"

mkdir -p "$RAW"
echo -e "task\ttool\tverdict\tsecs" > "$TSV"

mapfile -t U < <(python3 - <<PY
import glob, os, re
for y in sorted(glob.glob("$LDV/*.yml")):
    t = open(y).read()
    for m in re.finditer(r"property_file:\s*\S*?([\w-]+)\.prp\s*\n\s*expected_verdict:\s*(\w+)", t):
        if m.group(1) == "unreach-call" and m.group(2) == "false":
            b = y[:-4]
            if os.path.exists(b + ".i"):
                print(b + ".i")
PY
)

echo "${#U[@]} LDV tasks"
for src in "${U[@]}"; do
  src="$(realpath "$src")"
  name="$(basename "$src" | sed -E "s/\.cil.*//;s/.*drivers--//;s/.ko.*//;s/^32_7_cpp_single_//")"
  t0=$SECONDS; rc=0
  (ulimit -v 32000000 && timeout 600 "$ESBMC" --32 --k-induction --no-pointer-check --no-bounds-check --no-div-by-zero-check --no-align-check --no-pointer-relation-check "$src") > "$RAW/$name.esbmc.txt" 2>&1 || rc=$?
  s=$((SECONDS-t0)); o="$RAW/$name.esbmc.txt"
  if grep -qa "VERIFICATION FAILED" "$o" && grep -qa "reach_error" "$o"; then v=CATCH
  elif grep -qa "VERIFICATION SUCCESSFUL" "$o"; then v=safe
  elif grep -qaiE "out of memory|Killed|bad_alloc" "$o"; then v=oom
  elif [ "$rc" -eq 124 ]; then v=timeout
  else v=unknown; fi
  echo -e "$name\tESBMC\t$v\t$s" >> "$TSV"; echo ">>> $name ESBMC=$v ${s}s"

  t0=$SECONDS; rc=0
  d="$(mktemp -d)"
  (cd "$d" && ulimit -v 32000000 && timeout 600 "$CPA" -c "$CPACFG" --spec "$PRP" --32 "$src") > "$RAW/$name.cpa.txt" 2>&1 || rc=$?
  s=$((SECONDS-t0)); rm -rf "$d"; o="$RAW/$name.cpa.txt"
  vr="$(grep -aoE "Verification result: (FALSE|TRUE|UNKNOWN)" "$o" | tail -1 || true)"
  if echo "$vr" | grep -q FALSE; then v=CATCH
  elif echo "$vr" | grep -q TRUE; then v=safe
  elif echo "$vr" | grep -q UNKNOWN; then v=unknown
  elif [ "$rc" -eq 124 ]; then v=timeout
  else v=unknown; fi
  echo -e "$name\tCPA\t$v\t$s" >> "$TSV"; echo ">>> $name CPA=$v ${s}s"
done

echo "=== ESCPA2 DONE ==="
