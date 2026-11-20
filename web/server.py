"""
FastAPI server for AProver chat.

POST /chat takes a JSON body ``{"messages": [...]}`` and returns a Server-Sent
Events stream. The assistant is a Claude model with one tool, ``run_aprover``;
when the model decides to call the tool, the runner streams pipeline progress
back to the client as it happens.

Run locally:
    uv run uvicorn web.server:app --port 7860
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator

from anthropic import Anthropic
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from web.fetch import fetch_source
from web.runner import run_aprover_streaming


WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(title="AProver chat")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Brand assets live at the repo root in assets/ — mount only if present so the
# Docker image (which copies assets/) and local runs both work.
_ASSETS_DIR = WEB_DIR.parent / "assets"
if _ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")


SYSTEM_PROMPT = """You are AProver Assistant — the conversational front-end for AProver, an agentic prover for AI-generated code. AProver is a suite of LLM-driven formal-verification agents. The first agent — BMC-Agent — pairs a Claude-driven LLM agent with the CBMC bounded model checker to verify C programs end-to-end. Other languages and backends (e.g. Rust via Kani) are pluggable and on the roadmap.

Today, the live demo verifies C source via BMC-Agent. If a user asks about another language, say so honestly and offer to run on a C example, or to take their description / pseudocode and reason about it conversationally even if the verifier itself can't run on it yet.

Your job is to let visitors USE AProver without configuring it. They just chat.

When a user wants their code analyzed:
1. Make sure you have C source. The user can paste it, link to it, or accept a tiny example you offer.
   - If they paste a URL (GitHub blob, raw, gist, or any http(s) link to a text file), call the `fetch_source` tool first, then pass the returned content to `run_aprover`. github.com/<owner>/<repo>/blob/... links work — `fetch_source` rewrites them to raw automatically.
   - If they paste code directly, skip straight to `run_aprover`.
2. Optionally accept a target function name and any domain-knowledge hints.
3. Call the `run_aprover` tool. The pipeline takes 30s–3min — that's normal.
4. When results come back, summarize plainly: how many bugs were confirmed, the bug type and confidence tier of each, and one or two sentences on what each bug means in human terms.

Confidence tiers (highest first):
- confirmed_dynamic: a runtime reproducer crashed on a real source-level check
- confirmed_system_entry: CBMC traced the failing state back to a no-caller (entry) function
- confirmed_bmc: at least one caller can reach the failing state
- likely: bug pattern fits but the chain to entry was not fully traced
- unlikely: realism audit downgraded the finding

If a user just wants a demo, offer this minimal signed-overflow example and run it without further questions:
```c
#include <stdint.h>
int add(int a, int b) {
    return a + b;
}
```

Be concise. Don't show internal pipeline phases unless the user asks. Don't speculate about bugs the tool didn't report — only describe what the tool returned."""


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "fetch_source",
        "description": (
            "Fetch a text file from an http(s) URL — typically a C source file the "
            "user wants verified. Handles GitHub blob URLs by rewriting them to "
            "raw.githubusercontent.com automatically. Returns the file content as "
            "a string, or an error message. Capped at 64KB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public http(s) URL pointing at a text file (raw GitHub URL, github.com blob URL, gist raw, etc.).",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "run_aprover",
        "description": (
            "Run the AProver pipeline on a piece of C source code. Returns a JSON "
            "summary that includes any confirmed bugs with their bug type, "
            "confidence tier, and call chain. The call is slow (30s–3min)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_code": {
                    "type": "string",
                    "description": "Full C source code to verify. Must be a self-contained file (no missing headers or external linkage).",
                },
                "function": {
                    "type": "string",
                    "description": "Optional: limit the bug summary to a specific function name. Empty string verifies all functions.",
                },
                "domain_knowledge": {
                    "type": "string",
                    "description": "Optional: hints about intended behavior, invariants, or threat model.",
                },
            },
            "required": ["source_code"],
        },
    },
]


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "cbmc_installed": shutil.which("cbmc") is not None,
            "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "model": os.environ.get("BMC_AGENT_LLM_MODEL", "claude-sonnet-4-6"),
        }
    )


@app.post("/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return JSONResponse({"error": "messages must be a list"}, status_code=400)

    # Bring-your-own-key: the visitor's Anthropic key arrives per request via
    # the X-Anthropic-Key header (kept out of the JSON body so it doesn't end
    # up in request logs). Fall back to a server-side key for local dev.
    user_key = (
        request.headers.get("X-Anthropic-Key", "").strip()
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )

    async def gen() -> AsyncIterator[str]:
        if not user_key:
            yield _sse("error", {"message": "Enter your Anthropic API key to run AProver — it stays in your browser and is sent only with your own requests."})
            return

        client = Anthropic(api_key=user_key)
        model = os.environ.get("BMC_AGENT_LLM_MODEL", "claude-sonnet-4-6")
        convo = list(messages)

        try:
            for _turn in range(6):  # safety cap on tool-use loops
                response = client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_DEFINITIONS,
                    messages=convo,
                )

                assistant_blocks: list[dict[str, Any]] = []
                tool_uses: list[Any] = []
                for block in response.content:
                    if block.type == "text":
                        assistant_blocks.append({"type": "text", "text": block.text})
                        yield _sse("assistant_text", {"text": block.text})
                    elif block.type == "tool_use":
                        assistant_blocks.append(
                            {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            }
                        )
                        tool_uses.append(block)

                convo.append({"role": "assistant", "content": assistant_blocks})

                if response.stop_reason != "tool_use" or not tool_uses:
                    break

                tool_results: list[dict[str, Any]] = []
                for tu in tool_uses:
                    yield _sse("tool_call", {"name": tu.name, "input": tu.input})

                    if tu.name == "fetch_source":
                        url = (tu.input or {}).get("url", "")
                        ok, body = fetch_source(url)
                        yield _sse(
                            "tool_progress",
                            {
                                "type": "fetch_result",
                                "ok": ok,
                                "url": url,
                                "bytes": len(body) if ok else 0,
                                "error": None if ok else body,
                            },
                        )
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": (
                                    body
                                    if ok
                                    else json.dumps({"ok": False, "error": body})
                                ),
                                "is_error": not ok,
                            }
                        )
                        continue

                    if tu.name == "run_aprover":
                        final_payload: dict[str, Any] | None = None
                        for ev in run_aprover_streaming(
                            source_code=tu.input.get("source_code", ""),
                            function=(tu.input.get("function") or None),
                            domain_knowledge=tu.input.get("domain_knowledge", ""),
                            api_key=user_key,
                        ):
                            yield _sse("tool_progress", ev)
                            if ev.get("type") == "result":
                                final_payload = ev["result"]
                            elif ev.get("type") == "error" and final_payload is None:
                                final_payload = {"ok": False, "error": ev.get("message", "")}

                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": json.dumps(final_payload or {"ok": False, "error": "no result"}),
                            }
                        )
                        continue

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": f"Unknown tool: {tu.name}",
                            "is_error": True,
                        }
                    )

                convo.append({"role": "user", "content": tool_results})

            yield _sse("done", {})
        except Exception as exc:  # pragma: no cover
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
