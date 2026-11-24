#!/usr/bin/env python3
"""Pinned BMC-Agent RQ1/RQ3 runs. Without --execute, only write a run plan.

All profiles use this checkout; no historical checkout switching or external
experiment helper is performed. Credentials are read from the environment and
excluded from the recorded manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ARTIFACT_ROOT = ROOT.parent
PROFILES = json.loads((HERE / "profiles.json").read_text())
SECRET = re.compile(r"sk-(?:or-v1-)?[A-Za-z0-9_-]{20,}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(SECRET.sub("[REDACTED]", text) + "\n")


def source_snapshot() -> dict:
    # Include untracked experiment helpers as well as tracked production files.
    files = sorted(list((ROOT / "bmc_agent").rglob("*.py")) +
                   list((ROOT / "experiments/paper_reproduction").rglob("*.py")) +
                   list((ROOT / "experiments/paper_reproduction").glob("*.json")) +
                   list((ROOT / "experiments/spec_quality_compare").glob("*.py")) +
                   list((ROOT / "experiments/specgen_compare").glob("*.py")) +
                   [p for p in (HERE / "tools").rglob("*") if p.is_file()] +
                   [ROOT / "pyproject.toml", ROOT / "uv.lock"])
    hashes = {str(p.relative_to(ROOT)): sha256(p) for p in files}
    return {"commit": git("rev-parse", "HEAD"),
            "tracked_changes": git("diff", "--name-only", "HEAD"),
            "files_sha256": hashes}


def installed_packages() -> dict:
    versions = {}
    for name in ["anthropic", "httpx", "tree-sitter", "tree-sitter-c", "tree-sitter-rust", "pydantic", "rich"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def child_environment(profile: dict, args) -> tuple[dict, dict]:
    env = os.environ.copy()
    base_url = args.base_url or profile.get("base_url", "https://openrouter.ai/api/v1")
    model = args.model or profile["model"]
    if getattr(args, "api_key_env", ""):
        api_key = env.get(args.api_key_env, "")
    else:
        endpoint_key = ("DEEPSEEK_API_KEY" if urlsplit(base_url).hostname == "api.deepseek.com"
                        else "OPENAI_API_KEY" if "api.openai.com" in base_url else "OPENROUTER_API_KEY")
        api_key = (env.get(endpoint_key) or env.get("BMC_AGENT_LLM_DEFAULT_API_KEY")
                   or env.get("BMC_AGENT_LLM_API_KEY") or "")
    # Do not inherit strategy, role routing, or experimental knobs from other work.
    for key in list(env):
        if key.startswith(("BMC_", "AMC_", "SVCOMP_", "K2THINK_", "PAPER_REPRO_",
                           "CONFIG_AUDIT_")):
            del env[key]
    settings = {
        "BMC_AGENT_LLM_DEFAULT_PROVIDER": "openai",
        "BMC_AGENT_LLM_DEFAULT_BASE_URL": base_url,
        "BMC_AGENT_LLM_DEFAULT_MODEL": model,
        "BMC_AGENT_ENABLE_REALISM_CHECK": "true",
        "BMC_AGENT_ENABLE_PHASE_3E_TRIAGE": "false",
        "BMC_AGENT_C_PROMPT_EXAMPLES": "4",
        "PYTHONPATH": str(ROOT),
        "PYTHONHASHSEED": "0",
    }
    if profile.get("deepseek_thinking") == "disabled" and model.lower().startswith("deepseek-"):
        settings["BMC_AGENT_DEEPSEEK_THINKING"] = "disabled"
    esbmc = profile["kind"] == "rq1" and getattr(args, "c_backend", "cbmc") == "esbmc"
    if esbmc:
        requested_esbmc = str(args.esbmc_path or "esbmc")
        settings["PAPER_REPRO_ESBMC_REAL"] = str(Path(shutil.which(requested_esbmc) or requested_esbmc).resolve())
        settings["PAPER_REPRO_ESBMC_LOG"] = str(args.output.resolve() / "esbmc_invocations.jsonl")
        settings["BMC_AGENT_C_BACKEND"] = "esbmc"
        settings["BMC_AGENT_ESBMC_PATH"] = str(HERE / "tools/esbmc-record")
    elif profile["kind"] in ("rq1", "acsl"):
        requested_cbmc = str(args.cbmc_path or "cbmc")
        settings["PAPER_REPRO_CBMC_REAL"] = str(Path(shutil.which(requested_cbmc) or requested_cbmc).resolve())
        settings["PAPER_REPRO_CBMC_LOG"] = str(args.output.resolve() / "cbmc_invocations.jsonl")
        settings["BMC_AGENT_CBMC_PATH"] = str(HERE / "tools/cbmc-record")
    if profile["kind"] == "rq1":
        settings.update(SVCOMP_PROP="unreach", SVCOMP_ARCH=profile["arch"])
        if profile["strategy"] == "ldv_property_guard":
            # Validated LDV mode: the production pipeline runs with the
            # configuration agent enabled, its inline/stub decisions respected,
            # the property guard on and strict evidence grading on. The default
            # unwind is 1 and the agent may raise it; there is deliberately NO
            # SV-COMP floor, NO unwind cap and NO transitive-inline override, and
            # the feasibility recheck inherits the value the function actually
            # ran with (see the effective_unwind_by_function record).
            settings.update(
                SVCOMP_UNWIND="0", SVCOMP_TIMEOUT="0",
                BMC_AGENT_CBMC_UNWIND=str(profile["config_unwind"]),
                BMC_AGENT_CBMC_TIMEOUT=str(profile["solver_timeout"]),
                CONFIG_AUDIT_CONFIG_MAX_TOKENS=str(profile.get("config_output_tokens", 4096)),
                BMC_FRAME_HAVOC="1", BMC_BUGHUNT="1", BMC_FAITHFUL_MAIN="0",
                BMC_AGENT_ENABLE_BMC_CONFIG_AGENT="true",
                BMC_AGENT_ENABLE_FLAG_SELECTION="true",
                BMC_AGENT_RESPECT_INLINE_DECISIONS="true",
                BMC_AGENT_PRESERVE_LDV_PROPERTY="true",
                BMC_AGENT_STRICT_EVIDENCE_GRADING="true")
            settings["unwind_policy"] = profile.get("unwind_policy", "agent_may_override")
        else:
            settings.update(SVCOMP_UNWIND=str(profile["unwind"]),
                            SVCOMP_TIMEOUT=str(profile["solver_timeout"]),
                            BMC_AGENT_CBMC_UNWIND=str(profile["config_unwind"]),
                            BMC_UNWIND_CAP=str(profile["unwind"]))
        if profile["strategy"] == "public_ldv_fallback":
            settings.update(BMC_FRAME_HAVOC="1", BMC_BUGHUNT="1",
                            BMC_FAITHFUL_MAIN="0", BMC_TRANSITIVE_INLINE="0",
                            BMC_AGENT_ENABLE_BMC_CONFIG_AGENT="false",
                            BMC_AGENT_ENABLE_FLAG_SELECTION="false")
    if args.autospec_root:
        settings["AUTOSPEC_BENCHMARK_ROOT"] = str(Path(args.autospec_root).resolve())
    if profile["kind"] == "acsl":
        settings["BMC_AGENT_CANONICALIZE_SPEC_BUFFER_EXTENTS"] = str(profile["canonicalize_spec_buffer_extents"]).lower()
        settings["BMC_AGENT_DSL_FILTER_POLICY"] = profile["dsl_filter_policy"]
        settings["BMC_AGENT_C_HARNESS_POLICY"] = profile["c_harness_policy"]
    bin_dirs = [str(Path(p).resolve().parent) for p in (args.cbmc_path, args.frama_c_path) if p]
    if esbmc and args.esbmc_path:
        bin_dirs.insert(0, str(Path(args.esbmc_path).resolve().parent))
    settings["PATH"] = os.pathsep.join(bin_dirs + [env.get("PATH", "")])
    env.update(settings)
    env["ANTHROPIC_API_KEY"] = ""
    env["BMC_AGENT_LLM_DEFAULT_API_KEY"] = api_key
    env["BMC_AGENT_LLM_API_KEY"] = api_key
    return env, settings


def tool_versions(profile: dict, args, env: dict) -> dict:
    commands = []
    if profile["kind"] == "rq1" and getattr(args, "c_backend", "cbmc") == "esbmc":
        commands.append(("esbmc", env["PAPER_REPRO_ESBMC_REAL"], "--version", "8.3.0"))
    elif profile["kind"] in ("rq1", "acsl"):
        commands.append(("cbmc", str(args.cbmc_path or "cbmc"), "--version",
                         "5.95.1" if profile["kind"] == "rq1" else "6.9.0"))
    if profile["kind"] == "acsl":
        commands.append(("frama-c", str(args.frama_c_path or "frama-c"), "--version", "26.0"))
    if profile["kind"] == "jml":
        commands.append(("openjml", str(args.openjml_path or "openjml"), "-version", "0.17.0-alpha-15"))
    versions = {}
    for name, executable, flag, expected in commands:
        proc = subprocess.run([executable, flag], capture_output=True, text=True,
                              env=env, cwd=ROOT, timeout=40)
        text = (proc.stdout + proc.stderr).strip()
        if proc.returncode or expected not in text:
            raise ValueError(f"{name}: expected pinned version {expected}; got {text[:300]}")
        versions[name] = {"executable": executable, "version_output": text}
    return versions


def extract_rq1(artifact_root: Path, data_dir: Path, suite: str) -> Path:
    filename = ("aws-c-common-tasks.tar.gz" if suite == "aws-c-common"
                else "ldv-linux-3.4-simple-unsafe-tasks.tar.gz")
    archive = artifact_root / "rq1/sources" / filename
    destination = data_dir / suite
    marker = destination / ".archive.sha256"
    digest = sha256(archive)
    if marker.exists():
        if marker.read_text().strip() != digest:
            raise ValueError("benchmark archive changed; use a fresh --data-dir")
    else:
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                target = (destination / member.name).resolve()
                if not target.is_relative_to(destination.resolve()) or member.issym() or member.islnk():
                    raise ValueError(f"unsafe archive member: {member.name}")
            tar.extractall(destination, filter="data")
        marker.write_text(digest + "\n")
    return destination


def choose(items: list[dict], args) -> list[dict]:
    if args.cases:
        wanted = set(args.cases)
        missing = wanted - {c["name"] for c in items}
        if missing:
            raise ValueError("unknown cases: " + ", ".join(sorted(missing)))
        items = [c for c in items if c["name"] in wanted]
    return items[:args.limit] if args.limit is not None else items


def build_jobs(profile: dict, args, env: dict) -> tuple[list[dict], list[dict]]:
    output = args.output.resolve()
    trials = args.trials or profile["trials"]
    model = args.model or profile["model"]
    if profile["kind"] == "rq1":
        if not args.artifact_root:
            raise ValueError("--artifact-root must point to the paper-artifacts checkout")
        source_dir = extract_rq1(args.artifact_root, args.data_dir, profile["suite"])
        items = [{"name": p.stem, "source": str(p.resolve()), "sha256": sha256(p)}
                 for p in sorted(source_dir.rglob("*.i"))]
        if profile["suite"] == "aws-c-common":
            task_list = args.artifact_root / "rq1/tier-a-aws/task_list.tsv"
            expected = {}
            for line in task_list.read_text().splitlines():
                fields = line.split("\t")
                if len(fields) > 1 and fields[1] in ("true", "false"):
                    expected[Path(fields[0]).stem] = fields[1]
            for case in items:
                case["expected_verdict"] = expected[case["name"]]
        else:
            for case in items:
                case["expected_verdict"] = "false"
        if len(items) != profile["tasks"]:
            raise ValueError(f"expected {profile['tasks']} tasks, found {len(items)}")
        selected = choose(items, args)
        jobs = []
        for case in selected:
            out = output / "cases" / case["name"]
            command = [sys.executable, "-m", "bmc_agent.cli", "verify", "--source", case["source"],
                       "--driver", "agdrv", "--entry", "main", "--scope-from-entry",
                       "--output", str(out / "artifacts")]
            if getattr(args, "c_backend", "cbmc") == "esbmc":
                command += ["--c-backend", "esbmc", "--esbmc-path", env["BMC_AGENT_ESBMC_PATH"]]
            if profile["strategy"] == "faithful_main":
                command += ["--agentic"]
            else:
                command += ["--no-agentic", "--legacy-spec-gen"]
            jobs.append({"name": case["name"], "command": command,
                         "timeout_s": profile["case_timeout"], "memory_kib": profile["memory_kib"],
                         "output": str(out)})
        return selected, jobs
    if profile["kind"] == "acsl":
        if not args.autospec_root:
            raise ValueError("--autospec-root must point to AutoSpec/benchmark")
        # The preserved historical discovery logic determines the 251 programs.
        path = ROOT / "experiments/spec_quality_compare/_autospec_251_base.py"
        spec = importlib.util.spec_from_file_location("paper_autospec_discovery", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.AUTOSPEC_BENCH = args.autospec_root.resolve()
        module.ROOTS = {"frama-c-problems": module.AUTOSPEC_BENCH / "frama-c-problems",
                        "fib_46": module.AUTOSPEC_BENCH / "fib_46_benchmark",
                        "svcomp": module.AUTOSPEC_BENCH / "SVCOMP",
                        "code2inv_133": module.AUTOSPEC_BENCH / "code2inv_133_benchmark"}
        items = [{"name": c["case_id"], "source": c["source"], "sha256": sha256(Path(c["source"]))}
                 for c in module.discover_cases()]
        if len(items) != profile["tasks"]:
            raise ValueError(f"expected 251 tasks, found {len(items)}")
        selected = choose(items, args)
        command = [sys.executable, str(ROOT / "experiments/spec_quality_compare/run_bmc_autospec_251_trials.py"),
                   "--benchmark-root", str(args.autospec_root.resolve()),
                   "--out-dir", str(output / "results"), "--trials", str(trials),
                   "--workers", str(args.workers), "--case-timeout", str(profile["case_timeout"]),
                   "--postcheck-timeout", str(profile["postcheck_timeout"]),
                   "--cases", *[c["name"] for c in selected]]
    else:
        if not args.specgen_root or not args.openjml_path:
            raise ValueError("JML requires --specgen-root and --openjml-path")
        bench = args.specgen_root.resolve() / "benchmark" / profile["suite"]
        common = bench / "common" if profile["suite"] == "SpecGenBench" else bench
        items = []
        for directory in sorted(p for p in common.iterdir() if p.is_dir()):
            source = directory / (directory.name + ".java")
            if not source.exists():
                candidates = sorted(p for p in directory.glob("*.java") if not p.name.endswith("Driver.java"))
                if not candidates:
                    continue
                source = candidates[0]
            items.append({"name": directory.name, "source": str(source), "sha256": sha256(source)})
        if len(items) != profile["tasks"]:
            raise ValueError(f"expected {profile['tasks']} tasks, found {len(items)}")
        selected = choose(items, args)
        command = [sys.executable, str(ROOT / "experiments/specgen_compare/run_bmc_jml_specgen.py"), "run",
                   "--bench-root", str(common), "--oracle-root", str(bench / "oracle"),
                   "--openjml-path", str(args.openjml_path.resolve()), "--output", str(output / "results"),
                   "--model", model, "--provider", "openai", "--base-url", args.base_url,
                   "--temperature", str(profile["temperature"]), "--trials", str(trials),
                   "--workers", str(args.workers), "--prompt-examples", "specgen-4shot",
                   "--max-iterations", str(profile["max_iterations"]),
                   "--openjml-timeout", str(profile["openjml_timeout"]),
                   "--cases", *[c["name"] for c in selected]]
    return selected, [{"name": args.profile, "command": command, "output": str(output / "batch"),
                       "timeout_s": args.batch_timeout, "memory_kib": None}]


def run_job(job: dict, env: dict) -> dict:
    out = Path(job["output"])
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "run.log"
    start = time.monotonic()
    # GNU timeout kills the complete subprocess group, including solver children.
    command = ["timeout", "--signal=TERM", "--kill-after=10", str(job["timeout_s"]), *job["command"]]
    if job.get("memory_kib"):
        command = ["prlimit", f"--as={job['memory_kib'] * 1024}", "--", *command]
    with log_path.open("w") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            returncode = process.wait()
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            raise
    result = {"name": job["name"], "returncode": returncode,
              "wall_s": round(time.monotonic() - start, 3), "log": str(log_path)}
    write_json(out / "execution.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=sorted(PROFILES))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--autospec-root", type=Path)
    parser.add_argument("--specgen-root", type=Path)
    parser.add_argument("--data-dir", type=Path,
                        help="extracted benchmark directory (default: <output>/inputs)")
    parser.add_argument("--cbmc-path", type=Path)
    parser.add_argument("--c-backend", choices=("cbmc", "esbmc"), default="cbmc",
                        help="RQ1 C backend; CBMC remains the default. ESBMC is pinned to 8.3.0.")
    parser.add_argument("--esbmc-path", type=Path)
    parser.add_argument("--frama-c-path", type=Path)
    parser.add_argument("--openjml-path", type=Path)
    parser.add_argument("--base-url", help="Override the profile endpoint (OpenRouter for historical profiles).")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key-env", default="", help="Name of a credential environment variable; its value is never recorded.")
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--trials", type=int)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--batch-timeout", type=int, default=604800)
    parser.add_argument("--call-log", type=Path, help="Recording proxy llm_calls.jsonl, for billed token/cost totals.")
    parser.add_argument("--replay", action="store_true", help="Label a loopback-only saved-response replay.")
    parser.add_argument("--replay-log", type=Path, help="Replay proxy JSONL log; unmatched requests invalidate a controlled replay.")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    profile = PROFILES[args.profile]
    if args.data_dir is None:
        args.data_dir = args.output / "inputs"
    args.base_url = args.base_url or profile.get("base_url", "https://openrouter.ai/api/v1")
    if args.replay and not re.match(r"^http://(?:127\.0\.0\.1|localhost):\d+/", args.base_url):
        parser.error("--replay requires a loopback replay endpoint")
    if args.replay and not args.replay_log:
        parser.error("--replay requires --replay-log to audit exact request matching")
    if any(x is not None and x < 1 for x in (args.limit, args.trials, args.workers, args.batch_timeout)):
        parser.error("limits, trials, workers and timeouts must be positive")
    if (args.c_backend == "esbmc" or args.esbmc_path) and profile["kind"] != "rq1":
        parser.error("ESBMC selection is supported only for RQ1 profiles")
    if args.esbmc_path and args.c_backend != "esbmc":
        parser.error("--esbmc-path requires --c-backend esbmc")
    env, settings = child_environment(profile, args)
    try:
        cases, jobs = build_jobs(profile, args, env)
        lock_key = profile.get("input_lock", args.profile)
        lock = json.loads((HERE / "benchmark_inputs.json").read_text())[lock_key]
        for case in cases:
            if lock.get(case["name"]) != case["sha256"]:
                raise ValueError(f"benchmark input differs from the frozen paper input: {case['name']}")
        versions = tool_versions(profile, args, env) if args.execute else {"status": "checked at execution"}
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {"profile": args.profile, "settings": profile, "environment": settings,
                "python": sys.version, "python_packages": installed_packages(),
                "source": source_snapshot(), "cases": cases,
                "jobs": jobs, "trials": args.trials or profile["trials"],
                "tool_versions": versions,
                "execution_kind": "saved_response_replay" if args.replay else "fresh_generation",
                "call_log": str(args.call_log.resolve()) if args.call_log else "",
                "replay_log": str(args.replay_log.resolve()) if args.replay_log else "",
                "pilot": len(cases) != profile["tasks"] or (args.trials or profile["trials"]) != profile["trials"]}
    if profile["kind"] == "rq1":
        manifest["c_backend"] = args.c_backend
    fingerprint_data = {k: v for k, v in manifest.items() if k != "tool_versions"}
    fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True).encode()).hexdigest()
    manifest["fingerprint"] = fingerprint
    manifest["created_at"] = datetime.now(timezone.utc).isoformat()
    plan_path = args.output / "run_manifest.json"
    if plan_path.exists() and json.loads(plan_path.read_text())["fingerprint"] != fingerprint:
        parser.error("output contains a different code/configuration snapshot; use a fresh output directory")
    write_json(plan_path, manifest)
    print(f"{args.profile}: {len(cases)} cases x {manifest['trials']} trials; pilot={manifest['pilot']}", flush=True)
    print(f"plan: {plan_path}", flush=True)
    if not args.execute:
        return 0
    if not env.get("BMC_AGENT_LLM_DEFAULT_API_KEY"):
        parser.error("set the endpoint's DEEPSEEK_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY, "
                     "BMC_AGENT_LLM_DEFAULT_API_KEY, or --api-key-env before --execute")
    if (args.output / "execution.json").exists():
        parser.error("this output has already executed; use a fresh output directory")
    if any((Path(job["output"]) / "run.log").exists() for job in jobs):
        parser.error("partial execution exists; use a fresh output directory")
    results = []
    for job in jobs:
        print(f"running {job['name']}", flush=True)
        results.append(run_job(job, env))
        write_json(args.output / "execution.json", results)
    collector_spec = importlib.util.spec_from_file_location("paper_result_collection", HERE / "collect.py")
    collector = importlib.util.module_from_spec(collector_spec)
    collector_spec.loader.exec_module(collector)
    metrics = collector.collect(args.output)
    write_json(args.output / "metrics.json", metrics)
    if args.replay and not metrics["replay_fidelity"]["verified"]:
        print("saved-response replay was not verified: inspect cache misses in the replay log", file=sys.stderr)
        return 2
    if profile["kind"] == "rq1" and not metrics["solver_configuration"]["matches_profile"]:
        print(f"effective {args.c_backend.upper()} configuration was not verified against this profile", file=sys.stderr)
        return 2
    return 0 if all(r["returncode"] == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
