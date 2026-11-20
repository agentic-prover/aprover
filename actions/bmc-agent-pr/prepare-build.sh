#!/usr/bin/env bash
# Bundle the bmc-agent source into the action's build context.
#
# GitHub Action Docker builds use the action directory as the build context;
# they cannot reach the AProver repo root (which sits above this directory)
# nor clone the private AProver repo without credentials. This script
# copies the minimum needed bmc-agent source into ./bundle/ so the
# Dockerfile can ``COPY ./bundle /opt/bmc-agent`` instead of trying to
# clone over the network.
#
# Run once before ``docker build`` (or before pushing a release). The
# bundle/ directory is git-ignored — it's a build artifact, not source.

set -euo pipefail

cd "$(dirname "$0")"
ACTION_DIR="$(pwd)"
REPO_ROOT="$(git -C "${ACTION_DIR}" rev-parse --show-toplevel)"

if [[ ! -d "${REPO_ROOT}/bmc_agent" ]]; then
    echo "error: ${REPO_ROOT}/bmc_agent not found; run from inside the AProver repo" >&2
    exit 1
fi

rm -rf "${ACTION_DIR}/bundle"
mkdir -p "${ACTION_DIR}/bundle"

# Copy the runtime source bmc-agent needs to pip install -e.
cp -r "${REPO_ROOT}/bmc_agent"      "${ACTION_DIR}/bundle/bmc_agent"
cp    "${REPO_ROOT}/pyproject.toml" "${ACTION_DIR}/bundle/pyproject.toml"
[[ -f "${REPO_ROOT}/README.md"   ]] && cp "${REPO_ROOT}/README.md"   "${ACTION_DIR}/bundle/README.md"
[[ -f "${REPO_ROOT}/uv.lock"     ]] && cp "${REPO_ROOT}/uv.lock"     "${ACTION_DIR}/bundle/uv.lock"

# Strip caches that would balloon the image and the build context.
find "${ACTION_DIR}/bundle" -type d -name __pycache__ -prune -exec rm -rf {} +
find "${ACTION_DIR}/bundle" -type d -name '.pytest_cache' -prune -exec rm -rf {} +

bundle_size="$(du -sh "${ACTION_DIR}/bundle" | awk '{print $1}')"
echo "Bundled bmc-agent into ${ACTION_DIR}/bundle (${bundle_size})"
