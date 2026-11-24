#!/usr/bin/env bash
# Continuously rotate bmc-agent through a curated OSS-Fuzz starter list.
#
# Usage:
#   tools/run_continuous_sweep.sh                # live mode (uploads to embargoed)
#   tools/run_continuous_sweep.sh --dry-run      # preview reports without commits
#
# Each iteration: pick next project, full verify-dir sweep, auto-upload
# confirmed+realistic findings. Sleeps briefly between iterations so
# rate limits + git pushes don't pile up.
#
# Logs land under findings/oss_fuzz/. Run under tmux/nohup for true
# continuous operation.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

# Load K2 routing + API key (gitignored, chmod 600).
if [[ -r "$HOME/.config/bmc-agent/env" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.config/bmc-agent/env"
else
    echo "FATAL: $HOME/.config/bmc-agent/env not found." >&2
    echo "Run: mkdir -p ~/.config/bmc-agent && create env with K2THINK_API_KEY=..." >&2
    exit 1
fi

# Parser-focused rotation. Buffer-oriented parsers (cmark/brotli/bzip2/zstd/
# expat/libpng) take (bytes,len) as the attack surface, so generated harnesses
# model reality closely → far fewer precondition-FPs than handle-heavy formats.
# libtiff dir-read is intentionally dropped from the active rotation: it needs a
# fully-initialized TIFF* (mmap/readproc/flags), so permissive harnesses
# manufacture impossible states (all 12 of its "confirmed+realistic" candidates
# were adjudicated FALSE_POSITIVE on 2026-05-29).
PROJECTS=(libtiff expat libpng zstd cmark bzip2)
DRY_RUN_FLAG=""
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN_FLAG="--dry-run"
    echo "[continuous-sweep] DRY RUN — no embargoed-repo commits will be made"
fi

mkdir -p findings/oss_fuzz
ITER=0
while true; do
    ITER=$((ITER + 1))
    for proj in "${PROJECTS[@]}"; do
        ts=$(date -u +%Y%m%dT%H%M%SZ)
        echo "[continuous-sweep] iter=$ITER project=$proj ts=$ts"
        python3 tools/oss_fuzz_sweep.py \
            --project "$proj" \
            $DRY_RUN_FLAG \
            2>&1 | tee "findings/oss_fuzz/${proj}_iter${ITER}_${ts}.log"
        echo "[continuous-sweep] sleeping 60s before next project"
        sleep 60
    done
    echo "[continuous-sweep] rotation complete; sleeping 300s before re-rotating"
    sleep 300
done
