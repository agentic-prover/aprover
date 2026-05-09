# Container image for the AProver chat demo.
#
# Designed for Hugging Face Spaces (sdk: docker, app_port: 7860). Installs
# CBMC + uv + the project, then launches uvicorn against web.server:app.
#
# Required runtime secrets (set in the Space settings):
#   ANTHROPIC_API_KEY    — Claude key for both chat agent and AProver pipeline
# Optional:
#   BMC_AGENT_LLM_MODEL  — defaults to claude-sonnet-4-6

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_SYSTEM_PYTHON=1 \
    HOME=/home/user

# CBMC + a C compiler (needed by the parser preprocessor and any future
# dynamic-validator path). gcc is pulled in by build-essential.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cbmc \
        build-essential \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs of the project deps.
RUN pip install --no-cache-dir uv

# HF Spaces runs containers as a non-root user (uid 1000). Match that so
# the workdir + temp dirs stay writable.
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

# Install Python deps first for better layer caching.
COPY --chown=user:user pyproject.toml uv.lock* /home/user/app/
RUN uv pip install --system \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.27" \
        "anthropic>=0.40.0" \
        "tree-sitter>=0.21.0" \
        "tree-sitter-c>=0.21.0" \
        "rich>=13.0.0" \
        "pydantic>=2.0.0"

# Project source.
COPY --chown=user:user bmc_agent /home/user/app/bmc_agent
COPY --chown=user:user aprover /home/user/app/aprover
COPY --chown=user:user assets /home/user/app/assets
COPY --chown=user:user web /home/user/app/web

USER user
EXPOSE 7860
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "7860"]
