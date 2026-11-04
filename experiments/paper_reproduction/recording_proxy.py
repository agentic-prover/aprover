#!/usr/bin/env python3
"""Loopback-only OpenRouter recorder with a conservative pilot spending limit.

Example client URL: http://127.0.0.1:8794/openrouter/my-run/api/v1
Stores request/response bodies without authorization headers for controlled
replay. It never changes model, temperature, token cap or prompt content.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

import httpx

UPSTREAM = "https://openrouter.ai/api/v1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8794)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--budget-usd", type=float, default=8.0)
    parser.add_argument("--max-calls", type=int, default=300)
    args = parser.parse_args()
    if args.budget_usd <= 0 or args.max_calls < 1:
        parser.error("budget and max-calls must be positive")
    key = args.key_file.read_text().strip() if args.key_file else os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        parser.error("set OPENROUTER_API_KEY or --key-file")
    args.output.mkdir(parents=True, exist_ok=True)
    call_log = args.output / "llm_calls.jsonl"
    if call_log.exists():
        parser.error("use a fresh output directory to avoid resetting the spending meter")
    client = httpx.Client(timeout=httpx.Timeout(240.0, connect=15.0))
    response = client.get(UPSTREAM + "/models")
    response.raise_for_status()
    models = {m["id"]: m for m in response.json()["data"]}
    (args.output / "model_prices.json").write_text(json.dumps({
        k: {"pricing": m.get("pricing"), "context_length": m.get("context_length")}
        for k, m in models.items() if k in {
            "anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.6", "openai/gpt-3.5-turbo-1106"}
    }, indent=2))
    lock = threading.Lock()
    state = {"charged_usd": 0.0, "reserved_usd": 0.0, "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status: int, data: bytes | dict):
            body = json.dumps(data).encode() if isinstance(data, dict) else data
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path != "/health":
                self.reply(404, {"error": {"message": "unknown endpoint"}})
                return
            with lock:
                data = {**state, "budget_usd": args.budget_usd}
            self.reply(200, data)

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if len(parts) != 6 or parts[0] != "openrouter" or parts[2:] != ["api", "v1", "chat", "completions"]:
                self.reply(404, {"error": {"message": "expected /openrouter/<tag>/api/v1/chat/completions"}})
                return
            tag = parts[1]
            if not tag.replace("_", "").replace("-", "").isalnum():
                self.reply(400, {"error": {"message": "tag must contain letters, digits, underscores or hyphens"}})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 16 * 1024 * 1024:
                    raise ValueError("request size out of range")
                body = self.rfile.read(size)
                payload = json.loads(body)
                if payload.get("stream"):
                    raise ValueError("this recorder supports non-streaming calls only")
                model = payload["model"]
                if model not in models:
                    raise ValueError(f"model unavailable in OpenRouter catalog: {model}")
                price = models[model]["pricing"]
                # One input token per UTF-8 byte is deliberately conservative.
                maximum = len(body) * float(price["prompt"]) + int(payload["max_tokens"]) * float(price["completion"])
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {"error": {"message": str(exc)}})
                return
            with lock:
                if state["calls"] >= args.max_calls or state["charged_usd"] + state["reserved_usd"] + maximum > args.budget_usd:
                    self.reply(402, {"error": {"message": "Local pilot budget reached; no upstream request sent"}})
                    return
                state["reserved_usd"] += maximum
                state["calls"] += 1
                index = state["calls"]
            start = time.monotonic()
            original = dict(payload)
            payload["usage"] = {"include": True}
            request_hash = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
            usage = {}
            try:
                upstream = client.post(UPSTREAM + "/chat/completions", json=payload,
                                       headers={"Authorization": "Bearer " + key,
                                                "Content-Type": "application/json"})
                status, data = upstream.status_code, upstream.content
                parsed = upstream.json()
                usage = parsed.get("usage") or {}
            except (httpx.HTTPError, ValueError) as exc:
                status = 502
                data = json.dumps({"error": {"message": str(exc).replace(key, "[REDACTED]")}}).encode()
            cost = usage.get("cost")
            # Unmetered successes reserve their conservative upper bound.
            charged = float(cost) if cost is not None else (maximum if status == 200 else 0.0)
            record = {"index": index, "tag": tag, "model": model, "status": status,
                      "latency_s": round(time.monotonic() - start, 4), "request_sha256": request_hash,
                      "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                      "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                      "cost_usd": cost, "meter_charge_usd": charged,
                      "timestamp": time.time()}
            cache = args.output / "calls" / tag / f"{index:05d}"
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "request.json").write_text(json.dumps(original, indent=2))
            (cache / "response.json").write_bytes(data)
            record["cache_dir"] = str(cache)
            with lock:
                state["reserved_usd"] -= maximum
                state["charged_usd"] += charged
                with call_log.open("a") as log:
                    log.write(json.dumps(record) + "\n")
            self.reply(status, data)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"listening": f"127.0.0.1:{args.port}", "budget_usd": args.budget_usd,
                      "models_available": {m: m in models for m in [
                          "anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.6", "openai/gpt-3.5-turbo-1106"]}}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        client.close()


if __name__ == "__main__":
    main()
