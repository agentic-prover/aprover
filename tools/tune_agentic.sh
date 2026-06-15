#!/usr/bin/env bash
# One configured --agentic tuning sweep on the VibeOS vfs oracle fixture.
# Captures per-role telemetry (incl. tokens) + soundness-gate verdict so the
# agentic-vs-flat / model-routing decision is made on numbers, not vibes.
#
# Usage: tune_agentic.sh LABEL
#   Per-role model routing is injected by the CALLER via env vars
#   (BMC_AGENT_LLM_<ROLE>_PROVIDER/_MODEL/_BASE_URL/_API_KEY). Baseline = none.
#   Knobs: PER_FUNC_BUDGET (default 180s), OVERALL_TIMEOUT (default 9000s),
#          EXTRA_FLAGS (e.g. "--no-realism-tools").
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
[[ -r "$HOME/.config/bmc-agent/env" ]] && source "$HOME/.config/bmc-agent/env"

LABEL="${1:?need a label}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
ROOT="findings/tune_${LABEL}_${TS}"
mkdir -p "$ROOT"
REALS="vfs_readdir,vfs_write"
FPS="vfs_append,vfs_delete_recursive"
BUDGET="${PER_FUNC_BUDGET:-180}"
OVERALL="${OVERALL_TIMEOUT:-9000}"
EXTRA_FLAGS="${EXTRA_FLAGS:-}"

echo "[tune $LABEL] START $(date -u) root=$ROOT budget=${BUDGET}s overall=${OVERALL}s extra="
timeout "$OVERALL" ./.venv/bin/bmc-agent verify \
    --source examples/vibeos/repo/kernel/vfs.c \
    --driver vibeos_vfs \
    --include-dir examples/vibeos/repo/kernel --include-dir examples/vibeos/repo/kernel/libc \
    ${AGENTIC_FLAG:---agentic} --enable-soundness-gate \
    --threat-model security --threat-model-context examples/vibeos/threat_model_context.md \
    --per-function-time-budget "$BUDGET" \
    $EXTRA_FLAGS \
    --output "$ROOT" > "$ROOT/run.log" 2>&1
rc=$?
echo "[tune $LABEL] verify rc=$rc $(date -u)"

python3 tools/check_soundness_gate.py "$ROOT" --reals "$REALS" --fps "$FPS" > "$ROOT/gate.txt" 2>&1
grc=$?
echo "[tune $LABEL] gate rc=$grc (0=GREEN, 1=BLOCKED real demoted, 2=usage)"

echo "label=$LABEL verify_rc=$rc gate_rc=$grc root=$ROOT ts=$TS" > "$ROOT/DONE"
echo "[tune $LABEL] DONE -> $ROOT"
