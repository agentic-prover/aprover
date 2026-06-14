#!/usr/bin/env bash
# Phase-3 ENFORCEMENT SAFETY-GATE fixture test (realism-enforcement plan).
#
# WHY: the live VibeOS source has been PATCHED for both gate-anchor bugs
# (vfs_open_handle unbounded strcpy -> bounded loop, Jun 12; ip_handle total_len
# OOB -> bounds guard at net.c:342). So the live tree can no longer prove the
# safety gate "a REAL confirmed_dynamic bug is NOT demoted by enforcement".
# This fixture (/tmp/p3_buggy/kernel) restores both bugs verbatim so we can test
# the gate against genuine reals with the working native-Anthropic LLM.
#
# GATE: with enforcement default-ON, vfs_open_handle (heap overflow) and
# ip_handle (OOB read) must stay confirmed/likely -- realism must judge them
# REALISTIC (not high/med UNREALISTIC), so enforcement KEEPS them. If realism
# wrongly demotes either to 'unlikely' -> gate FAILS -> revert the default.
set -u
ROOT=/home/syc/AProver
cd "$ROOT" || exit 1
ENVF="$HOME/.config/bmc-agent/env"
[ -f "$ENVF" ] && . "$ENVF"

FIX=/tmp/p3_buggy/kernel
out=findings/phase3_gate_fixture
mkdir -p "$out"

# VibeOS threat model so realism is grounded in the real attack surface
# (de-anchored from libarchive; cf. project_libarchive_anchored_realism).
TMC="VibeOS is a freestanding aarch64 hobby kernel. Attacker-controlled inputs: \
(1) inbound network frames parsed by net.c (ip_handle receives raw attacker \
packets; ip->total_len is attacker-chosen); (2) file contents read back through \
the VFS (vfs_open_handle copies temp->data, which is file data sized by vfs_write \
and can exceed VFS_MAX_PATH=256). A heap/buffer overflow or OOB read driven by \
those inputs is a genuine, attacker-reachable kernel bug."

python -m bmc_agent.cli verify-dir \
  --source-dir "$FIX" \
  --driver vibeos_p3gate \
  --output "$out" \
  --include-dir "$FIX" \
  --include-dir "$FIX/libc" \
  --functions vfs_open_handle,ip_handle \
  --threat-model-context "$TMC" \
  --agentic \
  >"$out/run.log" 2>&1
echo "DONE gate-fixture rc=$?" >>"$out/run.log"
