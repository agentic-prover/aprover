---
title: AProver
emoji: 🛡️
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Chat with an agentic model checker for C
---

# AProver — chat demo

Web front-end that lets visitors run [AProver](https://github.com/agentic-prover/aprover) by chatting, without configuring the tool.

The Space packages:

- the `bmc_agent` pipeline (Phase 1: spec gen → Phase 2: CBMC → Phase 3: CEx classify + refine → bug report),
- a FastAPI server (`web/server.py`) exposing `POST /chat` as a Server-Sent Events stream,
- a single-page chat UI (`web/static/`) that streams pipeline progress live.

## How the chat works

The assistant is a Claude model with one tool, `run_aprover(source_code, function?, domain_knowledge?)`. When you paste C code, the model decides whether to call the tool, the server runs the pipeline in a worker thread, and every log line + final bug summary streams back into the chat as it happens.

Defaults are tuned for a public demo (no dynamic validation, short refinement loop, 60s CBMC timeout, 64KB source cap). Heavier configs are available via the CLI.

## Running it locally

```bash
# from the repo root
uv pip install fastapi "uvicorn[standard]"
ANTHROPIC_API_KEY=sk-... uv run uvicorn web.server:app --port 7860
# then open http://localhost:7860
```

CBMC must be on `PATH` (`apt install cbmc` on Debian/Ubuntu).

## Deploying to Hugging Face Spaces

HF Spaces wants `Dockerfile` and `README.md` (with `sdk: docker` frontmatter) at the repo root, but the AProver repo already has a project README. Use the staging script:

```bash
# 1. create an empty Space (SDK: Docker) on huggingface.co/spaces
# 2. clone it locally
git clone https://huggingface.co/spaces/<you>/aprover ~/aprover-space

# 3. stage a Space-ready tree from this repo
./web/deploy_to_space.sh ~/aprover-space

# 4. push
cd ~/aprover-space && git add -A && git commit -m "Update AProver Space" && git push
```

In **Space Settings → Secrets**, add `ANTHROPIC_API_KEY`. Optionally set `BMC_AGENT_LLM_MODEL` (defaults to `claude-sonnet-4-6`). The container listens on port 7860, which Spaces routes automatically.

## Files

| Path | Purpose |
|------|---------|
| `web/server.py`  | FastAPI app, `/chat` SSE endpoint, Claude tool-use loop |
| `web/runner.py`  | Threaded wrapper around `AMCPipeline` that yields progress events |
| `web/static/`    | Single-page chat UI (HTML + CSS + JS, no build step) |
| `web/Dockerfile` | Container image: python + cbmc + uv + project |
