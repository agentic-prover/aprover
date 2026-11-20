#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RQ1_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

: "${APROVER_ROOT:?set APROVER_ROOT to the bmc-agent source checkout}"
: "${SV_BENCHMARKS_ROOT:?set SV_BENCHMARKS_ROOT to an sv-benchmarks checkout}"
: "${BMC_AGENT_LLM_DEFAULT_API_KEY:?set BMC_AGENT_LLM_DEFAULT_API_KEY in the environment}"

cd "$APROVER_ROOT"
export BMC_AGENT_LLM_DEFAULT_BASE_URL="${BMC_AGENT_LLM_DEFAULT_BASE_URL:-https://openrouter.ai/api/v1}"
export BMC_AGENT_LLM_DEFAULT_PROVIDER="${BMC_AGENT_LLM_DEFAULT_PROVIDER:-openai}"
export BMC_AGENT_LLM_DEFAULT_MODEL="${BMC_AGENT_LLM_DEFAULT_MODEL:-anthropic/claude-sonnet-4.5}"
export SVCOMP_ARCH=LP64 SVCOMP_UNWIND=64 SVCOMP_TIMEOUT=400
export SVCOMP_PROP="${SVCOMP_PROP:-${SV_BENCHMARKS_ROOT}/c/properties/unreach-call.prp}"

TASK_LIST="${RQ1_TIER_A_TASK_LIST:-${RQ1_ROOT}/tier-a-aws/task_list.tsv}"
AWS_TASK_ROOT="${RQ1_AWS_TASK_ROOT:-${SV_BENCHMARKS_ROOT}/c/aws-c-common}"
TSV="${RQ1_TIER_A_BMC_AGENT_TSV:-/tmp/aws_comp.tsv}"
: > "$TSV"

while IFS=$'\t' read -r path exp _; do
  path="${path//\$\{SV_BENCHMARKS_ROOT\}\/c\/aws-c-common/${AWS_TASK_ROOT}}"
  path="${path//\$\{SV_BENCHMARKS_ROOT\}/${SV_BENCHMARKS_ROOT}}"
  name="$(basename "$path" .i)"
  out="${RQ1_TIER_A_OUTPUT_ROOT:-/tmp}/awscomp_$name"
  [ -f "$path" ] || { echo ">>> [$name] MISSING SRC"; continue; }
  echo ">>> [$name exp=$exp] $(date +%T)"
  (ulimit -v 28000000; timeout 800 python3 -m bmc_agent.cli verify --source "$path" --driver agdrv --agentic --entry main --scope-from-entry --output "$out") > "$out.log" 2>&1
  rc=$?
  if grep -qaE "Pipeline END: [1-9][0-9]* real bug" "$out.log"; then bmc=false
  elif grep -qaE "Pipeline END: 0 real bug|verified=True" "$out.log"; then bmc=true
  elif [ "$rc" -eq 124 ]; then bmc=timeout
  else bmc=unknown; fi
  if [ "$bmc" = "timeout" ] || [ "$bmc" = "unknown" ]; then score=unknown
  elif [ "$bmc" = "$exp" ]; then score=correct
  elif [ "$exp" = "true" ]; then score=FALSE-ALARM
  else score=MISSED-BUG; fi
  llm="$(grep -ac "LLM usage" "$out.log")"
  echo "    $name exp=$exp bmc=$bmc [$score] LLM=$llm rc=$rc"
  echo -e "$name\t$exp\t$bmc\t$score\t$llm" >> "$TSV"
done < "$TASK_LIST"

echo ">>> COMPOSITIONAL DONE"
