#!/usr/bin/env python3
"""Loopback recorder/proxy for OpenRouter's Anthropic Messages endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


DEFAULT_UPSTREAM = "https://openrouter.ai/api/v1/messages"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_PROVIDER = "Anthropic"
PRICES_USD_PER_MTOK = {
    "input": 3.0,
    "output": 15.0,
    "cache_write": 3.75,
    "cache_read": 0.30,
}


def _usage_summary(usage: dict) -> dict:
    if not usage:
        return {}
    creation = usage.get("cache_creation") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_input_tokens": int(
            usage.get("cache_creation_input_tokens") or 0
        ),
        "cache_read_input_tokens": int(
            usage.get("cache_read_input_tokens") or 0
        ),
        "ephemeral_5m_input_tokens": int(
            creation.get("ephemeral_5m_input_tokens") or 0
        ),
        "ephemeral_1h_input_tokens": int(
            creation.get("ephemeral_1h_input_tokens") or 0
        ),
        "reported_cost_usd": float(usage.get("cost") or 0.0),
    }


def _estimated_cost(usage: dict) -> float:
    if not usage:
        return 0.0
    reported = float(usage.get("reported_cost_usd") or 0.0)
    if reported > 0:
        return reported
    return (
        usage["input_tokens"] * PRICES_USD_PER_MTOK["input"]
        + usage["output_tokens"] * PRICES_USD_PER_MTOK["output"]
        + usage["cache_creation_input_tokens"]
        * PRICES_USD_PER_MTOK["cache_write"]
        + usage["cache_read_input_tokens"] * PRICES_USD_PER_MTOK["cache_read"]
    ) / 1_000_000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--max-calls", type=int, default=2000)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    args = parser.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        parser.error("OPENROUTER_API_KEY is not set")
    if args.budget_usd <= 0 or args.max_calls < 1:
        parser.error("budget and max-calls must be positive")

    args.output.mkdir(parents=True, exist_ok=True)
    log_path = args.output / "llm_calls.jsonl"
    if log_path.exists():
        parser.error("use a fresh output directory")

    client = httpx.Client(timeout=httpx.Timeout(240.0, connect=15.0))
    state = {
        "calls": 0,
        "completed_calls": 0,
        "reserved_usd": 0.0,
        "estimated_cost_usd": 0.0,
        "denied": 0,
        "model_mismatches": 0,
        "provider_mismatches": 0,
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            return

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/health":
                self._reply(404, b'{"error":{"message":"unknown endpoint"}}')
                return
            with lock:
                data = {
                    **state,
                    "budget_usd": args.budget_usd,
                    "max_calls": args.max_calls,
                    "model": args.model,
                    "provider": args.provider,
                    "upstream": args.upstream,
                }
            self._reply(200, json.dumps(data).encode())

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/v1/messages":
                self._reply(404, b'{"error":{"message":"expected /v1/messages"}}')
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 32 * 1024 * 1024:
                    raise ValueError("request size out of range")
                original_body = self.rfile.read(size)
                payload = json.loads(original_body)
                if payload.get("stream"):
                    raise ValueError("streaming requests are not supported by recorder")
                if payload.get("model") != args.model:
                    raise ValueError(
                        f"unexpected request model {payload.get('model')!r}; "
                        f"expected {args.model!r}"
                    )
                payload["provider"] = {
                    "order": [args.provider],
                    "allow_fallbacks": False,
                }
                request_body = json.dumps(payload, separators=(",", ":")).encode()
                max_tokens = int(payload.get("max_tokens") or 4096)
                reservation = (
                    len(request_body) * PRICES_USD_PER_MTOK["input"]
                    + max_tokens * PRICES_USD_PER_MTOK["output"]
                ) / 1_000_000
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self._reply(
                    400, json.dumps({"error": {"message": str(exc)}}).encode()
                )
                return

            with lock:
                if (
                    state["calls"] >= args.max_calls
                    or state["estimated_cost_usd"]
                    + state["reserved_usd"]
                    + reservation
                    > args.budget_usd
                ):
                    state["denied"] += 1
                    with (args.output / "denials.jsonl").open("a") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "timestamp": time.time(),
                                    "reservation_usd": reservation,
                                    **state,
                                }
                            )
                            + "\n"
                        )
                    self._reply(
                        429,
                        b'{"error":{"message":"local OpenRouter run budget reached"}}',
                    )
                    return
                state["calls"] += 1
                state["reserved_usd"] += reservation
                index = state["calls"]

            call_dir = args.output / "calls" / f"{index:05d}"
            call_dir.mkdir(parents=True)
            (call_dir / "request.original.json").write_bytes(original_body)
            (call_dir / "request.json").write_bytes(request_body)

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "anthropic-version": self.headers.get(
                    "anthropic-version", "2023-06-01"
                ),
                "HTTP-Referer": "https://localhost",
                "X-OpenRouter-Title": "BMC Agent VibeOS RQ2",
            }
            beta = self.headers.get("anthropic-beta")
            if beta:
                headers["anthropic-beta"] = beta

            started = time.monotonic()
            try:
                upstream = client.post(
                    args.upstream,
                    content=request_body,
                    headers=headers,
                )
                status = upstream.status_code
                response_body = upstream.content
            except httpx.HTTPError as exc:
                status = 502
                response_body = json.dumps(
                    {"error": {"message": str(exc).replace(key, "[REDACTED]")}}
                ).encode()

            (call_dir / "response.json").write_bytes(response_body)
            try:
                parsed = json.loads(response_body)
            except (ValueError, json.JSONDecodeError):
                parsed = {}
            usage = _usage_summary(parsed.get("usage") or {})
            charged = _estimated_cost(usage) if usage else 0.0
            response_model = parsed.get("model")
            response_provider = parsed.get("provider")
            model_ok = status != 200 or response_model == args.model
            provider_ok = status != 200 or response_provider == args.provider
            record = {
                "index": index,
                "status": status,
                "requested_model": payload.get("model"),
                "response_model": response_model,
                "provider": response_provider,
                "provider_ok": provider_ok,
                "message_id": parsed.get("id"),
                "max_tokens": max_tokens,
                "usage": usage,
                "estimated_cost_usd": charged,
                "latency_s": round(time.monotonic() - started, 4),
                "request_sha256": hashlib.sha256(request_body).hexdigest(),
                "cache_dir": str(call_dir),
                "timestamp": time.time(),
            }
            with lock:
                state["reserved_usd"] = max(
                    0.0, round(state["reserved_usd"] - reservation, 12)
                )
                state["estimated_cost_usd"] += charged
                state["completed_calls"] += 1
                if not model_ok:
                    state["model_mismatches"] += 1
                if not provider_ok:
                    state["provider_mismatches"] += 1
                with log_path.open("a") as stream:
                    stream.write(json.dumps(record) + "\n")
            self._reply(status, response_body)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(
        json.dumps(
            {
                "listening": f"127.0.0.1:{args.port}",
                "provider": args.provider,
                "model": args.model,
                "upstream": args.upstream,
                "budget_usd": args.budget_usd,
                "max_calls": args.max_calls,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        client.close()


if __name__ == "__main__":
    main()
