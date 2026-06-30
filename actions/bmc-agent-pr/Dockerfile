# bmc-agent-pr GitHub Action runtime.
#
# Layers a Python + CBMC + bmc-agent install on a small Ubuntu base. Kept
# intentionally lean: the action runs once per PR, so cold-start cost
# matters more than image size optimisation.
#
# The bmc-agent source is taken from one of two sources, in order:
#
#   1. ./bundle/   — the AProver repo's bmc_agent/ + pyproject.toml staged
#                    locally by ``./prepare-build.sh``. Used when the source
#                    is private or you want to pin a specific working
#                    tree. THIS IS THE DEFAULT for self-hosted builds.
#
#   2. ``git clone $BMC_AGENT_REPO`` — used only if ./bundle/ is absent.
#                    Will be the path once AProver is public.

FROM ubuntu:24.04

ARG BMC_AGENT_REPO=https://github.com/anonymous/aprover.git
ARG BMC_AGENT_REF=main

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   gcc/binutils — preprocessing the project's C sources
#   cbmc — the conventional verification backend bmc-agent calls
#   git — diffing the PR, fetching base ref, cloning bmc-agent at build
#   curl/ca-certificates — talking to the LLM backend
#   jq — manipulating the action's output JSON
#   python3 + venv + pip — the bmc-agent runtime
#   gh — posting PR comments via GITHUB_TOKEN
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cbmc \
        ca-certificates \
        curl \
        git \
        gh \
        jq \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Stage bmc-agent source. Prefer ./bundle/ (set up by ./prepare-build.sh)
# over the network clone; the COPY always succeeds because the repo
# checks in ./bundle/.gitkeep, and the RUN script then decides whether
# to use the bundled source or fall back to git clone.
COPY ./bundle/ /opt/bmc-agent/
RUN set -e; \
    if [ -f /opt/bmc-agent/pyproject.toml ]; then \
        echo "Using ./bundle/ for bmc-agent source"; \
    else \
        echo "No ./bundle/; falling back to git clone ${BMC_AGENT_REPO}#${BMC_AGENT_REF}"; \
        rm -rf /opt/bmc-agent && \
        git clone --depth 1 --branch "${BMC_AGENT_REF}" "${BMC_AGENT_REPO}" /opt/bmc-agent; \
    fi

# pip install into a venv. The venv keeps Ubuntu 24.04's
# externally-managed-environment policy happy without --break-system-packages.
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -e /opt/bmc-agent

ENV PATH="/opt/venv/bin:${PATH}"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
