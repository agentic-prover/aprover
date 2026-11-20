#!/usr/bin/env sh
set -eu

# Load secrets outside this artifact directory before running this script.
# Required:
#   BMC_AGENT_LLM_API_KEY
#   APROVER_ROOT
#   AUTOSPEC_BENCHMARK_ROOT
# Optional overrides:
#   BMC_AGENT_LLM_DEFAULT_MODEL
#   BMC_AGENT_LLM_PROVIDER
#   BMC_AGENT_LLM_BASE_URL
#   RQ3_ACSL_OUTPUT_DIR
#   CBMC_BIN_DIR
#   FRAMAC_WRAPPER_BIN_DIR
#   BMC_AGENT_WORKERS

: "${BMC_AGENT_LLM_API_KEY:?Set BMC_AGENT_LLM_API_KEY in the environment before running.}"
: "${APROVER_ROOT:?Set APROVER_ROOT to the BMC-Agent checkout.}"
: "${AUTOSPEC_BENCHMARK_ROOT:?Set AUTOSPEC_BENCHMARK_ROOT to the AutoSpec benchmark directory.}"

export BMC_AGENT_LLM_PROVIDER="${BMC_AGENT_LLM_PROVIDER:-openrouter}"
export BMC_AGENT_LLM_BASE_URL="${BMC_AGENT_LLM_BASE_URL:-https://openrouter.ai/api/v1}"
export BMC_AGENT_LLM_DEFAULT_MODEL="${BMC_AGENT_LLM_DEFAULT_MODEL:-anthropic/claude-sonnet-4.6}"
export BMC_AGENT_C_PROMPT_EXAMPLES="${BMC_AGENT_C_PROMPT_EXAMPLES:-4}"
export AUTOSPEC_DOCKER_CPUS="${AUTOSPEC_DOCKER_CPUS:-2}"
export AUTOSPEC_BENCHMARK_ROOT

if [ -n "${CBMC_BIN_DIR:-}" ]; then
  export PATH="${CBMC_BIN_DIR}:${PATH}"
fi
if [ -n "${FRAMAC_WRAPPER_BIN_DIR:-}" ]; then
  export PATH="${FRAMAC_WRAPPER_BIN_DIR}:${PATH}"
fi

cd "${APROVER_ROOT}"

RQ3_ACSL_OUTPUT_DIR="${RQ3_ACSL_OUTPUT_DIR:-artifacts/autospec_251_bmc_agent_acsl_rerun}"
BMC_AGENT_WORKERS="${BMC_AGENT_WORKERS:-4}"

uv run python experiments/spec_quality_compare/run_bmc_autospec_251_trials.py \
  --out-dir "${RQ3_ACSL_OUTPUT_DIR}" \
  --trials 5 \
  --workers "${BMC_AGENT_WORKERS}" \
  --case-timeout 900 \
  --postcheck-timeout 180 \
  --resume
