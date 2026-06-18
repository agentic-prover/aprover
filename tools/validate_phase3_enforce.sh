#!/usr/bin/env bash
# Phase-3 ENFORCEMENT validation launcher (realism-enforcement plan).
# Runs a VibeOS target with the CURRENT DEFAULT config, i.e. enforcement ON
# (config.enforce_realism_on_dynamic=True, commit cf569da). Under --agentic
# (now default) the realism verdict BITES on confirmed_dynamic findings:
# an UNREALISTIC (high/medium) verdict RE-TIERS a confirmed_dynamic finding to
# 'unlikely' (a re-tier, never a delete -> still reported -> sound).
#
# ABSOLUTE GATE (checked after the run, per target):
#   vfs : vfs_open_handle (strcpy heap overflow) MUST stay confirmed_dynamic / not 'unlikely'.
#         vfs_delete_recursive (callee-returns-NULL) SHOULD re-tier to 'unlikely' (expected-correct).
#   net : ip_handle MUST stay confirmed/likely (NOT 'unlikely') IF flagged.
#   irq : nondet-arg overflow wsod_* FPs SHOULD re-tier; NO real bug exists here to lose.
#
# Launch ONE target detached so it survives an iteration boundary:
#   setsid nohup bash tools/validate_phase3_enforce.sh vfs >/dev/null 2>&1 &
#   setsid nohup bash tools/validate_phase3_enforce.sh irq >/dev/null 2>&1 &
#   setsid nohup bash tools/validate_phase3_enforce.sh net >/dev/null 2>&1 &
set -u
ROOT="${APROVER_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" || exit 1

# Native-Anthropic LLM config (claude-sonnet-4-6). Required for realism to run.
ENVF="$HOME/.config/bmc-agent/env"
[ -f "$ENVF" ] && . "$ENVF"

target="${1:?usage: validate_phase3_enforce.sh <irq|vfs|net>}"
case "$target" in
  irq) src=examples/vibeos/repo/kernel/irq.c; drv=vibeos_irq_p3enforce;
       out=findings/phase3_enforce_irq ;;
  vfs) src=examples/vibeos/repo/kernel/vfs.c; drv=vibeos_vfs_p3enforce;
       out=findings/phase3_enforce_vfs ;;
  net) src=examples/vibeos/repo/kernel/net.c; drv=vibeos_net_p3enforce;
       out=findings/phase3_enforce_net ;;
  *) echo "unknown target '$target'"; exit 2 ;;
esac

mkdir -p "$out"
# --agentic is now the default; enforcement is default-ON. Run explicitly with
# --agentic for clarity. NO --keep-dynamic-immunity => enforcement bites.
python -m bmc_agent.cli verify \
  --source "$src" \
  --driver "$drv" \
  --output "$out" \
  --include-dir examples/vibeos/repo/kernel \
  --include-dir examples/vibeos/repo/kernel/libc \
  --agentic \
  >"$out/run.log" 2>&1
echo "DONE $target rc=$?" >>"$out/run.log"
