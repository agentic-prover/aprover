#!/usr/bin/env bash
# Overnight whole-kernel --agentic sweep (2026-06-12 night).
#
# Runs bmc-agent --agentic SEQUENTIALLY over every never-tested VibeOS kernel
# module, ordered most-structurally-novel first so failure modes surface early.
# Each module is bounded by a per-function time budget AND a hard per-module
# timeout so the whole night cannot hang. The driving session wakes periodically
# to triage failures, fix harness-gen bugs, commit, and let the sweep continue.
#
# Failure signals captured per module in SUMMARY.txt:
#   tracebacks (hard crash), archive-contamination (libarchive leak),
#   build/CONVERSION errors, budget-caps, END status + bug counts.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
[[ -r "$HOME/.config/bmc-agent/env" ]] && source "$HOME/.config/bmc-agent/env"

ROOT="${1:?usage: overnight_kernel_sweep.sh <root-dir>}"
mkdir -p "$ROOT"
INC=(--include-dir examples/vibeos/repo/kernel --include-dir examples/vibeos/repo/kernel/libc)
TMC=(--threat-model security --threat-model-context examples/vibeos/threat_model_context.md)
SUMMARY="$ROOT/SUMMARY.txt"
echo "VibeOS overnight whole-kernel --agentic sweep — started $(date -u)" > "$SUMMARY"

# Never-tested modules, most-novel-first. (Already covered today: string memory
# virtio_blk process vfs fat32 net elf ttf — skipped.)
MODULES=(virtio_sound irq tls keyboard kapi console mouse virtio_net fb font printf dtb kernel shell cursor rtc klog initramfs)

for f in "${MODULES[@]}"; do
  src="examples/vibeos/repo/kernel/$f.c"
  [[ -r "$src" ]] || { echo "[sweep $(date -u +%H:%M:%SZ)] SKIP $f (no source)" | tee -a "$SUMMARY"; continue; }
  OUT="$ROOT/$f"; mkdir -p "$OUT"
  echo "[sweep $(date -u +%H:%M:%SZ)] START $f ($(wc -l < "$src") lines)" | tee -a "$SUMMARY"
  timeout 2700 ./.venv/bin/bmc-agent verify \
      --source "$src" \
      --driver "vibeos_$f" \
      "${INC[@]}" --agentic --enable-soundness-gate "${TMC[@]}" \
      --per-function-time-budget 240 \
      --output "$OUT" > "$OUT/run.log" 2>&1
  rc=$?
  END=$(grep -oE 'AMC Pipeline END: .*' "$OUT/run.log" 2>/dev/null | tail -1)
  TB=$(grep -c 'Traceback' "$OUT/run.log" 2>/dev/null)
  ARCH=$(grep -cE 'archive_read_new|ARCHIVE_OK|archive\.h|archive_entry' "$OUT/run.log" 2>/dev/null)
  BUILD=$(grep -cE 'CONVERSION ERROR|incomplete type|preprocess.*fail|Failed to build|could not be parsed' "$OUT/run.log" 2>/dev/null)
  BUDGET=$(grep -c 'budget exhausted' "$OUT/run.log" 2>/dev/null)
  CONF=$(grep -c 'confirmed_dynamic' "$OUT/run.log" 2>/dev/null)
  echo "[sweep $(date -u +%H:%M:%SZ)] DONE $f rc=$rc | tracebacks=$TB archive=$ARCH build=$BUILD budget=$BUDGET confirmed_dyn=$CONF | ${END:-<no END>}" | tee -a "$SUMMARY"
done
echo "[sweep $(date -u)] ALL DONE" | tee -a "$SUMMARY"
