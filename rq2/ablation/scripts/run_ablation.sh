#!/usr/bin/env bash
set -uo pipefail

: "${BMC_ABLATION_ROOT:?set BMC_ABLATION_ROOT to the extracted run directory}"
: "${BMC_AGENT_CODE:?set BMC_AGENT_CODE to a clean f2f31711e checkout}"
: "${VIBEOS_REPO:?set VIBEOS_REPO to the VibeOS checkout}"
: "${CBMC_BIN:?set CBMC_BIN to the directory containing CBMC 5.95.1}"

ROOT="$BMC_ABLATION_ROOT"
CODE="$BMC_AGENT_CODE"
PYTHON="${PYTHON:-$CODE/.venv/bin/python}"
KERNEL="$VIBEOS_REPO/kernel"
ARCHIVE_SOURCES="${ARCHIVE_SOURCES:-$KERNEL}"
PROXY="${PROXY:-$ROOT/openrouter_proxy.py}"

JOBS="${JOBS:-4}"
MAX_CALLS="${MAX_CALLS:-2000}"
BUDGET_PER_RUN_USD="${BUDGET_PER_RUN_USD:-40}"
BASE_PORT="${BASE_PORT:-9101}"
MODEL="${MODEL:-anthropic/claude-sonnet-4.6}"
UPSTREAM_PROVIDER="${UPSTREAM_PROVIDER:-Anthropic}"

MODULES=(elf fb memory string)
ARMS=(no_dynamic no_realism fixed_config)

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "OPENROUTER_API_KEY is not set" >&2
  exit 2
fi

for path in "$PYTHON" "$CBMC_BIN/cbmc" "$PROXY"; do
  if [ ! -e "$path" ]; then
    echo "missing required path: $path" >&2
    exit 3
  fi
done

mkdir -p "$ROOT/runs" "$ROOT/proxies"

{
  printf 'setting\tvalue\n'
  printf 'code_commit\t%s\n' "$(git -C "$CODE" rev-parse HEAD)"
  printf 'code_status\t%s\n' "$(git -C "$CODE" status --porcelain=v1 | tr '\n' ';')"
  printf 'vibeos_commit\t%s\n' "$(git -C "$VIBEOS_REPO" rev-parse HEAD)"
  printf 'cbmc_version\t%s\n' "$("$CBMC_BIN/cbmc" --version)"
  printf 'python\t%s\n' "$("$PYTHON" -V 2>&1)"
  printf 'provider\tOpenRouter Anthropic-native Messages API\n'
  printf 'upstream_provider\t%s\n' "$UPSTREAM_PROVIDER"
  printf 'model\t%s\n' "$MODEL"
  printf 'source_layout\tcomplete vibeos/kernel source tree\n'
  printf 'kernel_include_dir\t%s\n' "$KERNEL"
  printf 'libc_include_dir\t%s\n' "$KERNEL/libc"
  printf 'per_function_time_budget_s\t180\n'
  printf 'module_timeout_s\t9000\n'
  printf 'realism_thinking\toff\n'
  printf 'phase_3e_triage\toff\n'
  printf 'reproducer_agent\ton\n'
  printf 'feedback_loop\ton\n'
  printf 'spec_refiner\ton\n'
  printf 'spec_gen_tools\ton\n'
  printf 'realism_tools\ton\n'
  printf 'agentic_harness_repair\ton\n'
  printf 'concurrency\t%s\n' "$JOBS"
  printf 'per_module_llm_workers\t1\n'
  printf 'parallel_validation\t0\n'
  printf 'proxy_budget_per_run_usd\t%s\n' "$BUDGET_PER_RUN_USD"
  printf 'proxy_max_calls_per_run\t%s\n' "$MAX_CALLS"
} > "$ROOT/fixed_config.tsv"

printf 'script\tsha256\n%s\t%s\n' \
  "$ROOT/run_ablation.sh" \
  "$(sha256sum "$ROOT/run_ablation.sh" | awk '{print $1}')" \
  > "$ROOT/script_provenance.tsv"

arm_flags() {
  case "$1" in
    no_dynamic)
      printf '%s\n' "--no-dynamic-validation"
      ;;
    no_realism)
      printf '%s\n' "--no-realism-check"
      ;;
    fixed_config)
      printf '%s\n' \
        "--no-bmc-config-agent" \
        "--no-flag-selection" \
        "--no-inlining-advisor"
      ;;
    *)
      echo "unknown arm: $1" >&2
      return 2
      ;;
  esac
}

run_one() {
  local arm="$1"
  local module="$2"
  local port="$3"
  local run_root="$ROOT/runs/$arm/$module"
  local proxy_root="$ROOT/proxies/$arm-$module"
  local proxy_pid=""
  local rc=0
  local start_s end_s
  local source="$KERNEL/$module.c"
  local archive_source="$ARCHIVE_SOURCES/$module.c"
  local -a extra_flags

  mapfile -t extra_flags < <(arm_flags "$arm")

  if [ -e "$run_root" ] || [ -e "$proxy_root" ]; then
    echo "refusing to reuse existing run: $arm/$module" >&2
    return 2
  fi

  mkdir -p "$run_root" "$proxy_root"
  printf '%s\n' \
    $'setting\tvalue' \
    $'arm\t'"$arm" \
    $'module\t'"$module" \
    $'source\t'"$source" \
    $'source_sha256\t'"$(sha256sum "$source" | awk '{print $1}')" \
    $'archive_source_sha256\t'"$(sha256sum "$archive_source" | awk '{print $1}')" \
    $'extra_flags\t'"${extra_flags[*]}" \
    $'model\t'"$MODEL" \
    $'provider\tOpenRouter Anthropic-native Messages API' \
    $'upstream_provider\t'"$UPSTREAM_PROVIDER" \
    $'llm_base_url\thttp://127.0.0.1:'"$port" \
    $'per_function_time_budget_s\t180' \
    $'module_timeout_s\t9000' \
    $'proxy_budget_usd\t'"$BUDGET_PER_RUN_USD" \
    $'proxy_max_calls\t'"$MAX_CALLS" \
    > "$run_root/run_config.tsv"

  start_s="$(date +%s)"
  {
    echo "[RUN] arm=$arm module=$module"
    echo "[RUN] code=$(git -C "$CODE" rev-parse HEAD)"
    echo "[RUN] code_status=$(git -C "$CODE" status --porcelain=v1 | tr '\n' ';')"
    echo "[RUN] vibeos=$(git -C "$VIBEOS_REPO" rev-parse HEAD)"
    echo "[RUN] source=$source"
    echo "[RUN] source_sha256=$(sha256sum "$source" | awk '{print $1}')"
    echo "[RUN] archive_source_sha256=$(sha256sum "$archive_source" | awk '{print $1}')"
    echo "[RUN] cbmc=$("$CBMC_BIN/cbmc" --version)"
    echo "[RUN] python=$("$PYTHON" -V 2>&1)"
    echo "[RUN] model=$MODEL provider=openrouter upstream_provider=$UPSTREAM_PROVIDER"
    echo "[RUN] extra=${extra_flags[*]}"
    echo "[RUN] start=$(date -Iseconds)"
  } > "$run_root/run.log"

  env OPENROUTER_API_KEY="$OPENROUTER_API_KEY" PYTHONUNBUFFERED=1 \
    "$PYTHON" "$PROXY" \
      --port "$port" \
      --output "$proxy_root" \
      --budget-usd "$BUDGET_PER_RUN_USD" \
      --max-calls "$MAX_CALLS" \
      --model "$MODEL" \
      --provider "$UPSTREAM_PROVIDER" \
      > "$run_root/proxy.log" 2>&1 &
  proxy_pid=$!

  local healthy=0
  for _ in $(seq 1 30); do
    if curl --silent --show-error --fail --max-time 2 \
      "http://127.0.0.1:$port/health" \
      | jq -e --arg model "$MODEL" --arg provider "$UPSTREAM_PROVIDER" '
          .reserved_usd <= 0.000001
          and .denied == 0
          and .provider == $provider
          and .model == $model
        ' >/dev/null 2>&1
    then
      healthy=1
      break
    fi
    sleep 1
  done

  if [ "$healthy" -ne 1 ]; then
    echo "[RUN] INVALID: proxy failed health check" | tee -a "$run_root/run.log"
    rc=83
  else
    (
      cd "$CODE"
      ulimit -v 42000000
      env -i \
        HOME="$HOME" \
        PATH="$CBMC_BIN:/usr/local/bin:/usr/bin:/bin" \
        LANG="C.UTF-8" \
        LC_ALL="C.UTF-8" \
        TZ="Asia/Dubai" \
        TMPDIR="/tmp" \
        ANTHROPIC_API_KEY="loopback-proxy" \
        BMC_AGENT_LLM_DEFAULT_PROVIDER="anthropic" \
        BMC_AGENT_LLM_DEFAULT_MODEL="$MODEL" \
        BMC_AGENT_LLM_DEFAULT_BASE_URL="http://127.0.0.1:$port" \
        BMC_AGENT_LLM_TIMEOUT_S="240" \
        BMC_AGENT_MAX_WORKERS="1" \
        BMC_AGENT_BATCH_SIZE="1" \
        BMC_AGENT_PARALLEL_VALIDATION="0" \
        BMC_AGENT_ENABLE_PHASE_3E_TRIAGE="false" \
        timeout 9000 "$PYTHON" -m bmc_agent.cli verify \
          --source "$source" \
          --driver "vibeos_${module}" \
          --include-dir "$KERNEL" \
          --include-dir "$KERNEL/libc" \
          --agentic \
          --threat-model security \
          --threat-model-context examples/vibeos/threat_model_context.md \
          --per-function-time-budget 180 \
          --output "$run_root/artifacts" \
          "${extra_flags[@]}"
    ) >> "$run_root/run.log" 2>&1
    rc=$?
  fi

  local health_json=""
  if [ "$healthy" -eq 1 ]; then
    health_json="$(curl --silent --show-error --fail --max-time 5 \
      "http://127.0.0.1:$port/health" || true)"
  fi
  if [ -z "$health_json" ]; then
    health_json="{}"
  fi
  printf '%s\n' "$health_json" > "$run_root/proxy_health.json"

  if [ -n "$proxy_pid" ]; then
    kill "$proxy_pid" >/dev/null 2>&1 || true
    wait "$proxy_pid" >/dev/null 2>&1 || true
  fi

  if ! jq -e '.denied == 0 and .reserved_usd <= 0.000001' \
    "$run_root/proxy_health.json" >/dev/null 2>&1
  then
    echo "[RUN] INVALID: proxy denial or outstanding reservation" | tee -a "$run_root/run.log"
    [ "$rc" -ne 0 ] || rc=86
  fi

  local calls_log="$proxy_root/llm_calls.jsonl"
  if [ ! -f "$calls_log" ]; then
    echo "[RUN] INVALID: proxy call log is missing" | tee -a "$run_root/run.log"
    [ "$rc" -ne 0 ] || rc=87
  elif ! jq -e -s --arg model "$MODEL" --arg provider "$UPSTREAM_PROVIDER" '
      length > 0
      and all(.[]; .status == 200)
      and all(.[]; .response_model == $model)
      and all(.[]; .provider == $provider)
    ' "$calls_log" >/dev/null 2>&1
  then
    echo "[RUN] INVALID: non-200 call or unexpected model/provider" | tee -a "$run_root/run.log"
    [ "$rc" -ne 0 ] || rc=88
  fi

  if ! grep -q "v2 boundary detector: 222 public functions" "$run_root/run.log"; then
    echo "[RUN] INVALID: complete source-tree boundary context not observed" | tee -a "$run_root/run.log"
    [ "$rc" -ne 0 ] || rc=89
  fi

  end_s="$(date +%s)"
  {
    echo "[RUN] end=$(date -Iseconds) rc=$rc wall_s=$((end_s - start_s))"
    if [ -f "$calls_log" ]; then
      jq -s --arg arm "$arm" --arg mod "$module" --argjson rc "$rc" \
        '{
          arm: $arm,
          module_name: $mod,
          rc: $rc,
          calls: length,
          invalid_calls: ([.[] | select(.status != 200)] | length),
          model_mismatches: ([.[] | select(.response_model != "anthropic/claude-sonnet-4.6")] | length),
          provider_mismatches: ([.[] | select(.provider != "Anthropic")] | length),
          estimated_cost_usd: ([.[].estimated_cost_usd] | add // 0),
          input_tokens: ([.[].usage.input_tokens] | add // 0),
          output_tokens: ([.[].usage.output_tokens] | add // 0),
          cache_write_tokens: ([.[].usage.cache_creation_input_tokens] | add // 0),
          cache_read_tokens: ([.[].usage.cache_read_input_tokens] | add // 0)
        }' "$calls_log" > "$run_root/usage.json"
    fi
  } >> "$run_root/run.log"

  printf 'arm\tmodule\trc\twall_s\n%s\t%s\t%s\t%s\n' \
    "$arm" "$module" "$rc" "$((end_s - start_s))" \
    > "$run_root/status.tsv"
  return "$rc"
}

port="$BASE_PORT"
running=0
overall_rc=0

for arm in "${ARMS[@]}"; do
  for module in "${MODULES[@]}"; do
    run_one "$arm" "$module" "$port" &
    port=$((port + 1))
    running=$((running + 1))
    if [ "$running" -ge "$JOBS" ]; then
      wait || overall_rc=1
      running=0
    fi
  done
done

if [ "$running" -gt 0 ]; then
  wait || overall_rc=1
fi

exit "$overall_rc"
