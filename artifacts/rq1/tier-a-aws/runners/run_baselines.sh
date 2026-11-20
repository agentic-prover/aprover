#!/usr/bin/env bash
set -euo pipefail

# AWS Tier-A baselines: CBMC/ESBMC/CPAchecker on 27 tasks (7 false/20 true),
# LP64. Verdicts are scored against the expected labels in task_list.tsv.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RQ1_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

: "${SV_BENCHMARKS_ROOT:?set SV_BENCHMARKS_ROOT to an sv-benchmarks checkout}"
: "${ESBMC_ROOT:?set ESBMC_ROOT to the ESBMC installation root}"
: "${CPACHECKER_ROOT:?set CPACHECKER_ROOT to the CPAchecker installation root}"

ESBMC="${ESBMC:-${ESBMC_ROOT}/bin/esbmc}"
CPA="${CPA:-${CPACHECKER_ROOT}/bin/cpachecker}"
CPACFG="${CPACFG:-${CPACHECKER_ROOT}/config/svcomp26.properties}"
PRP="${SVCOMP_PROP:-${SV_BENCHMARKS_ROOT}/c/properties/unreach-call.prp}"
TASK_LIST="${RQ1_TIER_A_TASK_LIST:-${RQ1_ROOT}/tier-a-aws/task_list.tsv}"
AWS_TASK_ROOT="${RQ1_AWS_TASK_ROOT:-${SV_BENCHMARKS_ROOT}/c/aws-c-common}"
RAW="${RQ1_TIER_A_BASELINE_RAW:-/tmp/aws_base_raw}"
TSV="${RQ1_TIER_A_BASELINE_TSV:-/tmp/aws_base.tsv}"

mkdir -p "$RAW"
echo -e "task\texpected\tCBMC\tESBMC\tCPA" > "$TSV"

vc() {
  if grep -qa "VERIFICATION FAILED" "$1" || grep -qa "Verification result: FALSE" "$1"; then
    echo false
  elif grep -qa "VERIFICATION SUCCESSFUL" "$1" || grep -qa "Verification result: TRUE" "$1"; then
    echo true
  else
    echo unknown
  fi
}

while IFS=$'\t' read -r path exp _; do
  path="${path//\$\{SV_BENCHMARKS_ROOT\}\/c\/aws-c-common/${AWS_TASK_ROOT}}"
  path="${path//\$\{SV_BENCHMARKS_ROOT\}/${SV_BENCHMARKS_ROOT}}"
  [ -f "$path" ] || continue
  name="$(basename "$path" .i)"
  (ulimit -v 24000000; timeout 400 cbmc --64 --unwind 64 "$path") > "$RAW/$name.cbmc" 2>&1
  cb="$(vc "$RAW/$name.cbmc")"
  (ulimit -v 24000000; timeout 400 "$ESBMC" --64 --k-induction --unwind 64 --no-pointer-check --no-bounds-check --no-div-by-zero-check --no-align-check --no-pointer-relation-check "$path") > "$RAW/$name.esbmc" 2>&1
  es="$(vc "$RAW/$name.esbmc")"
  d="$(mktemp -d)"
  (cd "$d"; ulimit -v 24000000; timeout 400 "$CPA" -c "$CPACFG" --spec "$PRP" --64 "$path") > "$RAW/$name.cpa" 2>&1
  rm -rf "$d"
  cp="$(vc "$RAW/$name.cpa")"
  echo -e "$name\t$exp\t$cb\t$es\t$cp" >> "$TSV"
  echo ">>> $name exp=$exp CBMC=$cb ESBMC=$es CPA=$cp"
done < "$TASK_LIST"

echo "=== AWS_BASE DONE ==="
