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
CODE_ROOT="$(cd "${ACTION_DIR}/../.." && pwd)"

if [[ ! -d "${CODE_ROOT}/bmc_agent" ]]; then
    echo "error: ${CODE_ROOT}/bmc_agent not found; run from inside bmcagent/" >&2
    exit 1
fi

rm -rf "${ACTION_DIR}/bundle"
mkdir -p "${ACTION_DIR}/bundle"

# Copy the runtime source bmc-agent needs to pip install -e.
cp -r "${CODE_ROOT}/bmc_agent"      "${ACTION_DIR}/bundle/bmc_agent"
cp    "${CODE_ROOT}/pyproject.toml" "${ACTION_DIR}/bundle/pyproject.toml"
[[ -f "${CODE_ROOT}/README.md"   ]] && cp "${CODE_ROOT}/README.md"   "${ACTION_DIR}/bundle/README.md"
[[ -f "${CODE_ROOT}/uv.lock"     ]] && cp "${CODE_ROOT}/uv.lock"     "${ACTION_DIR}/bundle/uv.lock"

# Strip caches that would balloon the image and the build context.
find "${ACTION_DIR}/bundle" -type d -name __pycache__ -prune -exec rm -rf {} +
find "${ACTION_DIR}/bundle" -type d -name '.pytest_cache' -prune -exec rm -rf {} +

bundle_size="$(du -sh "${ACTION_DIR}/bundle" | awk '{print $1}')"
echo "Bundled bmc-agent into ${ACTION_DIR}/bundle (${bundle_size})"
