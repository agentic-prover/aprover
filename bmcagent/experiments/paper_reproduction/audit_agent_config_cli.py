"""Transparent instrumentation for the three-case agent-configuration experiment.

Invokes the complete production CLI. Saves agent decisions and every CBMC
input/output, without substituting any model response or solver result.
"""
from __future__ import annotations
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


def main():
    from bmc_agent.agents.bmc_config_tools import BmcConfigAgent
    from bmc_agent.cli import main as cli_main
    if os.environ.get("CONFIG_AUDIT_CONFIG_MAX_TOKENS"):
        BmcConfigAgent.max_tokens_per_turn_param = int(os.environ["CONFIG_AUDIT_CONFIG_MAX_TOKENS"])
    out = Path(os.environ["CONFIG_AUDIT_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    counter = 0
    original_select = BmcConfigAgent.select_all
    original_run = subprocess.run
    chosen = {}

    def save(name, data):
        (out / name).write_text(json.dumps(data, indent=2, default=str) + "\n")

    def select(self, *args, **kwargs):
        result = original_select(self, *args, **kwargs)
        chosen.update(result)
        save("agent_config.json", {name: dataclasses.asdict(config) for name, config in result.items()})
        save("config_defaults.json", {"cbmc_unwind": self.config.cbmc_unwind,
                                     "cbmc_timeout": self.config.cbmc_timeout,
                                     "config_max_tokens_per_turn": self.max_tokens_per_turn_param,
                                     "respect_agent_inline_decisions": self.config.respect_agent_inline_decisions})
        return result

    def run(command, *args, **kwargs):
        nonlocal counter
        if not isinstance(command, (list, tuple)) or Path(str(command[0])).name != "cbmc-record":
            return original_run(command, *args, **kwargs)
        with lock:
            counter += 1
            index = counter
        source = Path(command[1])
        text = source.read_text()
        copy = out / f"solver_{index:03d}.c"
        copy.write_text(text)
        inlined = set(re.findall(r"Inlined real callee body: (\w+)", text))
        main_config = chosen.get("main")
        explicit_stubs = {name for name, decision in main_config.inline_overrides.items()
                          if not decision.inline} if main_config else set()
        record = {"command": command, "timeout_s": kwargs.get("timeout"),
                  "harness_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "harness_copy": str(copy), "inlined_bodies": sorted(inlined),
                  "explicit_stub_bodies_inlined": sorted(explicit_stubs & inlined)}
        save(f"solver_{index:03d}.json", record)
        started = time.monotonic()
        try:
            result = original_run(command, *args, **kwargs)
        except subprocess.TimeoutExpired as exc:
            record.update(timeout=True, wall_s=time.monotonic() - started)
            for label, content in [("stdout", exc.stdout), ("stderr", exc.stderr)]:
                if content:
                    (out / f"solver_{index:03d}.{label}").write_bytes(content if isinstance(content, bytes) else content.encode())
            save(f"solver_{index:03d}.json", record)
            raise
        record.update(returncode=result.returncode, wall_s=time.monotonic() - started)
        for label, content in [("stdout", result.stdout), ("stderr", result.stderr)]:
            if content:
                (out / f"solver_{index:03d}.{label}").write_bytes(content if isinstance(content, bytes) else content.encode())
        save(f"solver_{index:03d}.json", record)
        return result

    BmcConfigAgent.select_all = select
    subprocess.run = run
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
