#!/usr/bin/env bash
# Build a Hugging Face Spaces–ready directory layout from this repo.
#
# Usage:
#   ./web/deploy_to_space.sh /path/to/aprover-space
#
# The destination must be (or will be initialised as) a clone of your
# Hugging Face Space repo. Run `git push` from there afterwards.
#
# Why this exists: HF Spaces requires Dockerfile and README.md (with the
# `sdk: docker` frontmatter) at the *root* of the Space repo. The AProver
# repo already has a root README.md describing the research project, so we
# stage a separate tree instead of overwriting it.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <space-repo-dir>" >&2
  exit 2
fi

DEST="$1"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$DEST"
echo "Staging Space tree in $DEST (source: $SRC)"

cp "$SRC/web/Dockerfile" "$DEST/Dockerfile"
cp "$SRC/web/README.md"  "$DEST/README.md"
cp "$SRC/pyproject.toml" "$DEST/pyproject.toml"
[[ -f "$SRC/uv.lock" ]] && cp "$SRC/uv.lock" "$DEST/uv.lock"

rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC/bmc_agent/" "$DEST/bmc_agent/"
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC/aprover/" "$DEST/aprover/"
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC/web/" "$DEST/web/"

# Drop the staged-files-only Dockerfile/README from the inner web/ copy so
# the Space root copies remain authoritative.
rm -f "$DEST/web/Dockerfile" "$DEST/web/README.md" "$DEST/web/deploy_to_space.sh"

echo "Done. Next:"
echo "  cd $DEST"
echo "  git add -A && git commit -m 'Update AProver Space' && git push"
