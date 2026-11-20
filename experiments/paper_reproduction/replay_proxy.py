#!/usr/bin/env python3
"""Replay recorded OpenRouter responses by exact request hash; no network calls."""
from __future__ import annotations
import argparse
from collections import defaultdict, deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--normalize-prefix", action="append", default=[],
                        help="Replace only these machine-local output path prefixes with <RUN_OUTPUT> before matching.")
    parser.add_argument("--normalize-diagnostic-identifiers", action="store_true",
                        help="Normalize ASan diagnostic PIDs and compiler diagnostic temporary C filenames; keep code, values and diagnostic text unchanged.")
    args = parser.parse_args()
    def normalize(value):
        if isinstance(value, str):
            for prefix in sorted(args.normalize_prefix, key=len, reverse=True):
                value = value.replace(prefix, "<RUN_OUTPUT>")
            if args.normalize_diagnostic_identifiers:
                value = re.sub(r"==\d+==(?=(?:ERROR: AddressSanitizer|ReserveShadowMemoryRange))", "==PID==", value)
                value = re.sub(r"/tmp/tmp[A-Za-z0-9_]+\.c(?=:\d+(?::\d+)?:)", "/tmp/<DIAGNOSTIC_SOURCE>.c", value)
            return value
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        return value

    def fingerprint(payload):
        return hashlib.sha256(json.dumps(normalize(payload), sort_keys=True).encode()).hexdigest()

    responses = defaultdict(deque)
    for line in (args.recording / "llm_calls.jsonl").read_text().splitlines():
        record = json.loads(line)
        cache = Path(record["cache_dir"])
        if record["status"] == 200:
            request = json.loads((cache / "request.json").read_text())
            responses[fingerprint(request)].append((record["status"], (cache / "response.json").read_bytes()))
    lock = threading.Lock()
    state = {"hits": 0, "misses": 0}
    args.log.parent.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body):
            if isinstance(body, dict):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            with lock:
                status = dict(state)
            self.reply(200, status)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            match_key = fingerprint(payload)
            requests = args.log.parent / "replay_requests"
            requests.mkdir(parents=True, exist_ok=True)
            (requests / (key + ".json")).write_text(json.dumps(payload, indent=2))
            with lock:
                if responses[match_key]:
                    status, body = responses[match_key].popleft()
                    state["hits"] += 1
                else:
                    status, body = 409, {"error": {"message": "Exact-request replay miss; no model request was sent"}}
                    state["misses"] += 1
                with args.log.open("a") as log:
                    log.write(json.dumps({"path": self.path, "request_sha256": key,
                                          "match_sha256": match_key, "normalized_output_paths": bool(args.normalize_prefix),
                                          "normalized_diagnostic_identifiers": args.normalize_diagnostic_identifiers,
                                          "status": status}) + "\n")
            self.reply(status, body)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"port": args.port, "recorded_requests": sum(map(len, responses.values()))}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
