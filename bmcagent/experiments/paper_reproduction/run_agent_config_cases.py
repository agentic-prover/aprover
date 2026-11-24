"""Run three LDV cases end to end with effective agent configuration.

Input sources and all other experimental knobs come from the saved November
run. No original artifact is overwritten. Stop all cases if the local spending
gate or an upstream API failure would otherwise cause silent LLM fallbacks.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--budget-usd", type=float, default=5.5)
    parser.add_argument("--port", type=int, default=8817)
    parser.add_argument("--cases", nargs="+", choices=["mousedev", "mem2mem_testdev", "vivi"],
                        default=["mousedev", "mem2mem_testdev", "vivi"])
    parser.add_argument("--config-output-tokens", type=int, default=2048)
    parser.add_argument("--preserve-ldv-property", action="store_true",
                        help="Keep LDV checks and model-state call paths despite agent STUB choices.")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    original = json.loads(args.original_manifest.read_text())
    cases = [dict(next(c for c in original["cases"] if short in c["name"]), short_name=short)
             for short in args.cases]
    for case in cases:
        assert hashlib.sha256(Path(case["source"]).read_bytes()).hexdigest() == case["sha256"]
    (output / "recording").mkdir()
    proxy_log = (output / "proxy.log").open("w")
    proxy = subprocess.Popen([sys.executable, str(HERE / "recording_proxy.py"),
                              "--port", str(args.port), "--output", str(output / "recording"),
                              "--key-file", str(args.key_file.resolve()),
                              "--budget-usd", str(args.budget_usd), "--max-calls", "600"],
                             stdout=proxy_log, stderr=subprocess.STDOUT, start_new_session=True)
    active = {}
    executions = []
    stop_reason = None
    started = time.monotonic()

    def terminate(proc):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            except ProcessLookupError:
                pass

    def memory_limit():
        amount = original["settings"]["memory_kib"] * 1024
        resource.setrlimit(resource.RLIMIT_AS, (amount, amount))

    try:
        for _ in range(120):
            if proxy.poll() is not None:
                raise RuntimeError("Recorder failed; inspect proxy.log")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("Recorder did not become ready")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("BMC_", "AMC_", "SVCOMP_", "PAPER_REPRO_", "K2THINK_"))}
        settings = dict(original["environment"])
        for key in ["BMC_UNWIND_CAP", "BMC_AGENT_CBMC_UNWIND", "BMC_TRANSITIVE_INLINE"]:
            settings.pop(key, None)
        settings.update({
            "PYTHONPATH": str(ROOT), "PYTHONHASHSEED": "0",
            "BMC_AGENT_ENABLE_BMC_CONFIG_AGENT": "true",
            "BMC_AGENT_ENABLE_FLAG_SELECTION": "true",
            "BMC_AGENT_RESPECT_INLINE_DECISIONS": "true",
            # Zero disables the extra wrapper floors; default/agent values win.
            "SVCOMP_UNWIND": "0", "SVCOMP_TIMEOUT": "0",
            "BMC_AGENT_CBMC_TIMEOUT": "300",
            "BMC_AGENT_CBMC_PATH": str(HERE / "tools/cbmc-record"),
            "PAPER_REPRO_CBMC_LOG": str(output / "cbmc_invocations.jsonl"),
            "BMC_AGENT_LLM_DEFAULT_BASE_URL": f"http://127.0.0.1:{args.port}/openrouter/full_common/api/v1",
            "CONFIG_AUDIT_CONFIG_MAX_TOKENS": str(args.config_output_tokens),
        })
        if args.preserve_ldv_property:
            settings["BMC_AGENT_PRESERVE_LDV_PROPERTY"] = "true"
            # The guard keeps the property writable; strict grading keeps the
            # result honest. Without this the entry-point callability shortcut
            # still reports real_bug when the feasibility recheck errored.
            settings["BMC_AGENT_STRICT_EVIDENCE_GRADING"] = "true"
        env.update(settings)
        env.update(BMC_AGENT_LLM_DEFAULT_API_KEY="loopback-recorder",
                   BMC_AGENT_LLM_API_KEY="loopback-recorder", ANTHROPIC_API_KEY="")
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(),
                    "base_commit": original["source"]["commit"],
                    "experiment_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    "tracked_diff": subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT, text=True),
                    "environment": settings, "cases": cases,
                    "budget_usd": args.budget_usd, "case_timeout_s": 900,
                    "memory_kib_per_case": original["settings"]["memory_kib"],
                    "hypothesis": "Do effective agent-selected inlining/stubbing and unrestricted agent unwind recover archived CATCH cases?",
                    "policy": "Explicit agent inline and stub decisions win; no transitive expansion or heuristic timeout/OOM configuration mutation. Unspecified unwind uses repository default 4. No unwind cap or wrapper floor. Full CLI validation/refinement/reproducer phases enabled as in the original November profile.",
                    "limitations": "Frame-havoc permissive specs remain as in the control; this is not restoration of the missing private write-frame implementation. BMC candidates must not automatically be counted as concrete bugs."}
        write(output / "run_manifest.json", manifest)
        manifest["jobs"] = []
        for case in cases:
            name = case["short_name"]
            directory = output / "cases" / name
            directory.mkdir(parents=True)
            child_env = dict(env, CONFIG_AUDIT_DIR=str(directory / "audit"))
            child_env["BMC_AGENT_LLM_DEFAULT_BASE_URL"] = f"http://127.0.0.1:{args.port}/openrouter/full_{name}/api/v1"
            command = [sys.executable, str(HERE / "audit_agent_config_cli.py"), "verify",
                       "--source", case["source"], "--driver", "agdrv", "--entry", "main",
                       "--scope-from-entry", "--output", str(directory / "artifacts"),
                       "--no-agentic", "--legacy-spec-gen"]
            manifest["jobs"].append({"case": name, "command": command,
                                     "base_url": child_env["BMC_AGENT_LLM_DEFAULT_BASE_URL"],
                                     "audit_dir": child_env["CONFIG_AUDIT_DIR"]})
            log = (directory / "run.log").open("w")
            proc = subprocess.Popen(command, cwd=ROOT, env=child_env, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=memory_limit)
            active[name] = (proc, time.monotonic(), directory, log)
            print(json.dumps({"started": name, "pid": proc.pid}), flush=True)
        write(output / "run_manifest.json", manifest)
        last_status = 0
        while active:
            call_log = output / "recording/llm_calls.jsonl"
            rows = [json.loads(line) for line in call_log.read_text().splitlines()] if call_log.exists() else []
            if (output / "recording/budget_denials.jsonl").exists():
                stop_reason = "local_budget_censored"
            elif any(row["status"] >= 400 for row in rows):
                stop_reason = "api_failure_censored"
            elif proxy.poll() is not None:
                stop_reason = "recorder_failure_censored"
            for name, (proc, since, directory, log) in list(active.items()):
                timeout = time.monotonic() - since >= 900
                if stop_reason or timeout:
                    terminate(proc)
                if proc.poll() is not None:
                    record = {"case": name, "returncode": proc.returncode,
                              "wall_s": round(time.monotonic() - since, 3),
                              "completion": stop_reason or ("case_timeout" if timeout else "process_finished")}
                    write(directory / "execution.json", record)
                    executions.append(record)
                    log.close()
                    del active[name]
                    print(json.dumps(record), flush=True)
            if time.monotonic() - last_status > 30:
                print(json.dumps({"active": list(active), "model_calls": len(rows),
                                  "cost_usd": sum(r.get("cost_usd") or 0 for r in rows)}), flush=True)
                last_status = time.monotonic()
            time.sleep(0.5)
        write(output / "execution_summary.json", {"executions": executions,
                                                  "stop_reason": stop_reason,
                                                  "wall_s": time.monotonic() - started})
    finally:
        for proc, _, _, log in active.values():
            terminate(proc)
            log.close()
        terminate(proxy)
        proxy_log.close()


if __name__ == "__main__":
    main()
