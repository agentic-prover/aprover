#!/usr/bin/env python3
"""AutoSpec reproduction and native ACSL comparison runner.

This is an experiment adapter. It does not replace or modify BMC-Agent's
production CBMC/Kani pipeline.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = Path("/mnt/disk7/jw_bmc/spec_quality_data")
DEFAULT_ZIP = DEFAULT_DATA_DIR / "AutoSpec.zip"
DEFAULT_AUTOSPEC_ROOT = DEFAULT_DATA_DIR / "autospec_artifact" / "AutoSpec"
DEFAULT_AUTOSPEC_ENV = DEFAULT_DATA_DIR / "autospec_env"
DEFAULT_WRAPPER_DIR = DEFAULT_DATA_DIR / "autospec_wrappers"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "spec_quality_benchmark" / "autospec_full"
DEFAULT_SECRET_ENV = Path("/mnt/disk7/jw_bmc/secrets/openrouter.env")
DEFAULT_FRAMA_IMAGE = "framac/frama-c:26.0.debian"
OPENROUTER_MODEL_ALIASES = {
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-sonnet-4.6": "anthropic/claude-sonnet-4.6",
    "claude-sonnet-4-5": "anthropic/claude-sonnet-4.5",
    "claude-sonnet-4.5": "anthropic/claude-sonnet-4.5",
}
AUTOSPEC_WP_FLAGS = [
    "-wp",
    "-wp-precond-weakening",
    "-wp-no-callee-precond",
    "-wp-prover",
    "Alt-Ergo,Z3",
    "-wp-print",
]
MAIN_BENCHMARK_PREFIXES = {
    "fib_46": "AutoSpec/benchmark/fib_46_benchmark/",
    "code2inv_133": "AutoSpec/benchmark/code2inv_133_benchmark/",
    "svcomp": "AutoSpec/benchmark/SVCOMP/",
    "frama_c_problems": "AutoSpec/benchmark/frama-c-problems/",
}
X509_PREFIX = "AutoSpec/benchmark/X509-parser/"
MUTANT_PREFIX = "AutoSpec/benchmark/100mutants/"
VERIFIED_MARKERS = ("_verified.c", "_final_simplified.c", "_verifed.c")
GENERATED_MARKERS = ("_marked.c", "_infilled.c", "_marked", "_infilled")
KEY_LIKE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|sk-or-v1-[A-Za-z0-9_-]{16,}|"
    r"OPENAI_API_KEY\s*=\s*(?!<redacted>)[^\s]+|"
    r"OPENROUTER_API_KEY\s*=\s*[^\s]+)",
    re.IGNORECASE,
)
WP_GOALS_RE = re.compile(r"\[wp\]\s+Proved goals:\s+(\d+)\s*/\s*(\d+)")


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sanitize_id(text: str) -> str:
    text = re.sub(r"^AutoSpec/benchmark/", "", text)
    text = re.sub(r"\.c$", "", text)
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", text).strip("_")


def _case_id(family: str, source_in_zip: str) -> str:
    return f"{family}__{_sanitize_id(source_in_zip)}"


def _is_c_source(name: str) -> bool:
    return name.startswith("AutoSpec/benchmark/") and name.endswith(".c")


def _is_generated_or_verified(name: str) -> bool:
    base = Path(name).name
    return _is_generated(name) or _is_verified_annotation(name)


def _is_generated(name: str) -> bool:
    base = Path(name).name
    return any(marker in base for marker in GENERATED_MARKERS)


def _is_verified_annotation(name: str) -> bool:
    base = Path(name).name
    return base.endswith("_final_simplified.c") or bool(
        re.search(r"_(?:verified|verifed)\d*\.c$", base)
    )


def _is_mutant(name: str) -> bool:
    return name.startswith(MUTANT_PREFIX) and "mutation" in Path(name).name.lower() and name.endswith(".c")


def _is_mutant_seed(name: str) -> bool:
    return name.startswith(MUTANT_PREFIX) and name.endswith(".c") and not _is_mutant(name)


def _family_for_source(name: str) -> str:
    for family, prefix in MAIN_BENCHMARK_PREFIXES.items():
        if name.startswith(prefix):
            return family
    if name.startswith(X509_PREFIX):
        return "x509_extra"
    if name.startswith(MUTANT_PREFIX):
        return "mutants_100"
    return "unknown"


def source_to_local_path(source_in_zip: str, autospec_root: Path) -> Path:
    prefix = "AutoSpec/"
    if source_in_zip.startswith(prefix):
        return autospec_root / source_in_zip[len(prefix) :]
    return autospec_root / source_in_zip


def build_manifest_from_zip(zip_path: Path, *, autospec_root: Path = DEFAULT_AUTOSPEC_ROOT) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(info.filename for info in zf.infolist() if not info.is_dir())

    benchmark_sources = [name for name in names if _is_c_source(name)]
    official_candidates = []
    x509_extra = []
    mutants = []
    mutant_seeds = []
    verified = []

    for name in benchmark_sources:
        if _is_mutant(name):
            mutants.append(_manifest_case("mutants_100", name, autospec_root))
            continue
        if _is_mutant_seed(name):
            mutant_seeds.append(_manifest_case("mutant_seed", name, autospec_root))
            continue
        if name.startswith(X509_PREFIX):
            if not _is_generated_or_verified(name):
                x509_extra.append(_manifest_case("x509_extra", name, autospec_root))
            continue
        if any(name.startswith(prefix) for prefix in MAIN_BENCHMARK_PREFIXES.values()):
            if _is_generated(name):
                continue
            if _is_verified_annotation(name):
                verified.append(_manifest_case(_family_for_source(name), name, autospec_root, role="verified_annotation"))
            else:
                official_candidates.append(_manifest_case(_family_for_source(name), name, autospec_root))
            continue
        if _is_verified_annotation(name):
            verified.append(_manifest_case(_family_for_source(name), name, autospec_root, role="verified_annotation"))

    for name in benchmark_sources:
        if _is_verified_annotation(name) and not any(c["source_in_zip"] == name for c in verified):
            verified.append(_manifest_case(_family_for_source(name), name, autospec_root, role="verified_annotation"))

    counts_by_family = Counter(case["family"] for case in official_candidates)
    warnings = []
    if len(official_candidates) != 251:
        warnings.append(
            "official_251_candidates count is "
            f"{len(official_candidates)}, not 251; use this as artifact-derived candidates "
            "and reconcile against raw results before claiming exact 251-program reproduction."
        )

    pilot10 = select_pilot10(official_candidates, x509_extra)
    bmc_agent_stratified50 = select_bmc_agent_stratified50(official_candidates, pilot10)
    return {
        "schema_version": 1,
        "created_at_unix": int(time.time()),
        "artifact": {
            "zip_path": str(zip_path),
            "autospec_root": str(autospec_root),
        },
        "warnings": warnings,
        "case_sets": {
            "official_251_candidates": official_candidates,
            "pilot10": pilot10,
            "bmc_agent_stratified50": bmc_agent_stratified50,
            "mutants_100": mutants,
            "mutant_seeds": mutant_seeds,
            "x509_extra": x509_extra,
            "verified_annotations": sorted(verified, key=lambda c: c["source_in_zip"]),
        },
        "summary": {
            "benchmark_c_files": len(benchmark_sources),
            "official_251_candidates": len(official_candidates),
            "official_251_expected": 251,
            "official_by_family": dict(sorted(counts_by_family.items())),
            "pilot10": len(pilot10),
            "bmc_agent_stratified50": len(bmc_agent_stratified50),
            "mutants_100": len(mutants),
            "mutant_seeds": len(mutant_seeds),
            "x509_extra": len(x509_extra),
            "verified_annotations": len(verified),
        },
    }


def _manifest_case(family: str, source_in_zip: str, autospec_root: Path, *, role: str = "source") -> dict[str, Any]:
    local_source = source_to_local_path(source_in_zip, autospec_root)
    return {
        "case_id": _case_id(family, source_in_zip),
        "family": family,
        "role": role,
        "source_in_zip": source_in_zip,
        "local_source": str(local_source),
        "source_exists": local_source.is_file(),
    }


def select_pilot10(official_cases: Sequence[Mapping[str, Any]], x509_cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_path = {case["source_in_zip"]: dict(case) for case in official_cases}
    selected_paths = [
        "AutoSpec/benchmark/frama-c-problems/general_wp_problems/max_of_2.c",
        "AutoSpec/benchmark/frama-c-problems/pointers/add_pointers.c",
        "AutoSpec/benchmark/frama-c-problems/immutable_arrays/array_sum.c",
        "AutoSpec/benchmark/frama-c-problems/loops/fact.c",
        "AutoSpec/benchmark/fib_46_benchmark/01.c",
        "AutoSpec/benchmark/code2inv_133_benchmark/1.c",
        "AutoSpec/benchmark/SVCOMP/quantifier-free/afnp2014_true-unreach-call/afnp2014_true-unreach-call.c",
        "AutoSpec/benchmark/SVCOMP/quantifier/array_true-unreach-call1/array_true-unreach-call1.c",
        "AutoSpec/benchmark/frama-c-problems/mutable_arrays/bubble_sort.c",
    ]
    out = [by_path[path] for path in selected_paths if path in by_path]
    if x509_cases:
        out.append(dict(sorted(x509_cases, key=lambda c: c["source_in_zip"])[0]))

    seen = {case["source_in_zip"] for case in out}
    for case in sorted(official_cases, key=lambda c: (c["family"], c["source_in_zip"])):
        if len(out) >= 10:
            break
        if case["source_in_zip"] not in seen:
            out.append(dict(case))
            seen.add(case["source_in_zip"])
    return out[:10]


def _evenly_spaced_cases(cases: Sequence[Mapping[str, Any]], count: int) -> list[dict[str, Any]]:
    ordered = sorted(cases, key=lambda c: c["source_in_zip"])
    if count <= 0:
        return []
    if len(ordered) <= count:
        return [dict(case) for case in ordered]
    if count == 1:
        return [dict(ordered[0])]
    indexes = []
    last = len(ordered) - 1
    for i in range(count):
        idx = round(i * last / (count - 1))
        if idx not in indexes:
            indexes.append(idx)
    cursor = 0
    while len(indexes) < count and cursor < len(ordered):
        if cursor not in indexes:
            indexes.append(cursor)
        cursor += 1
    return [dict(ordered[idx]) for idx in sorted(indexes[:count])]


def select_bmc_agent_stratified50(
    official_cases: Sequence[Mapping[str, Any]],
    pilot10: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build a deterministic 50-case set for native ACSL pilot scaling.

    This is intentionally smaller than AutoSpec's full 251-program set. It keeps
    the first scale-up decision-oriented while covering scalar, loop, invariant,
    and SV-COMP-style sources.
    """
    targets = {
        "code2inv_133": 15,
        "fib_46": 10,
        "frama_c_problems": 15,
        "svcomp": 10,
    }
    by_family: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in official_cases:
        by_family[str(case["family"])].append(case)

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for case in pilot10:
        family = str(case.get("family", ""))
        if family not in targets:
            continue
        if case["source_in_zip"] in seen:
            continue
        if sum(1 for item in selected if item["family"] == family) >= targets[family]:
            continue
        selected.append(dict(case))
        seen.add(str(case["source_in_zip"]))

    for family, target in targets.items():
        current = sum(1 for item in selected if item["family"] == family)
        needed = target - current
        if needed <= 0:
            continue
        candidates = [case for case in by_family.get(family, []) if case["source_in_zip"] not in seen]
        for case in _evenly_spaced_cases(candidates, needed):
            selected.append(case)
            seen.add(str(case["source_in_zip"]))

    family_order = {family: index for index, family in enumerate(targets)}
    return sorted(selected, key=lambda c: (family_order.get(c["family"], 99), c["source_in_zip"]))


def parse_final_result(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    result = "pass" if "Pass" in first else "fail" if "Fail" in first else "unknown"
    payload = "\n".join(lines[1:]).strip()
    iteration = None
    status = None
    tokens_usage = None
    match = re.search(r"'Iteration':\s*(\d+)", payload)
    if match:
        iteration = int(match.group(1))
    match = re.search(r"'Status':\s*(\d+)", payload)
    if match:
        status = int(match.group(1))
    match = re.search(r"'tokens_usage':\s*(\d+)", payload)
    if match:
        tokens_usage = int(match.group(1))
    return {
        "result": result,
        "headline": first,
        "iteration": iteration,
        "status": status,
        "tokens_usage": tokens_usage,
    }


def reconcile_raw(zip_path: Path) -> dict[str, Any]:
    rows = []
    by_raw_folder: dict[str, Counter[str]] = defaultdict(Counter)
    with zipfile.ZipFile(zip_path) as zf:
        finals = sorted(name for name in zf.namelist() if name.startswith("AutoSpec/raw/") and name.endswith("/final_result"))
        for name in finals:
            text = zf.read(name).decode("utf-8", errors="replace")
            parsed = parse_final_result(text)
            parts = name.split("/")
            raw_folder = parts[2] if len(parts) > 3 else "unknown"
            case_run = parts[-2]
            case_key = case_run.rsplit("_000", 1)[0] if "_000" in case_run else case_run
            by_raw_folder[raw_folder][parsed["result"]] += 1
            rows.append(
                {
                    "raw_folder": raw_folder,
                    "case_run": case_run,
                    "case_key": case_key,
                    "path_in_zip": name,
                    **parsed,
                }
            )
    return {
        "schema_version": 1,
        "created_at_unix": int(time.time()),
        "zip_path": str(zip_path),
        "total_final_results": len(rows),
        "by_raw_folder": {folder: dict(counter) for folder, counter in sorted(by_raw_folder.items())},
        "rows": rows,
    }


def parse_wp_output(stdout: str, stderr: str, returncode: int | None, timed_out: bool = False) -> dict[str, Any]:
    text = stdout + "\n" + stderr
    match = WP_GOALS_RE.search(text)
    proved = int(match.group(1)) if match else None
    total = int(match.group(2)) if match else None
    if timed_out:
        status = "timeout"
    elif "annot-error" in text or "invalid user input" in text or "wrong order of clause" in text:
        status = "annotation_error"
    elif proved is not None and total is not None and proved == total:
        status = "proved"
    elif proved is not None and total is not None:
        status = "unproved"
    elif returncode not in (0, None):
        status = "tool_error"
    else:
        status = "unknown"
    return {
        "status": status,
        "proved_goals": proved,
        "total_goals": total,
        "returncode": returncode,
    }


def scan_for_secret_text(text: str) -> list[str]:
    findings = []
    for match in KEY_LIKE_RE.finditer(text):
        token = match.group(0)
        prefix = token[:24]
        findings.append(f"key_like:{prefix}<redacted>")
    return findings


def scan_file_for_secrets(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return scan_for_secret_text(path.read_text(encoding="utf-8", errors="replace"))


def resolve_openrouter_model(model: str) -> str:
    return OPENROUTER_MODEL_ALIASES.get(model.strip(), model.strip())


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        return {
            "returncode": None,
            "timed_out": True,
            "runtime_s": time.time() - started,
            "stdout": stdout[-8000:],
            "stderr": stderr[-8000:],
        }
    return {
        "returncode": proc.returncode,
        "timed_out": False,
        "runtime_s": time.time() - started,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-8000:],
    }


def _run_probe(command: list[str], *, cwd: Path, timeout: int = 30, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    result = _run_command(command, cwd=cwd, timeout=timeout, env=env)
    return {
        "ok": result["returncode"] == 0 and not result["timed_out"],
        "returncode": result["returncode"],
        "timed_out": result["timed_out"],
        "stdout": result["stdout"][-1000:],
        "stderr": result["stderr"][-1000:],
        "command": command,
    }


def _read_env_file(path: Path) -> dict[str, str]:
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'").strip('"')
        out[key.strip()] = value
    return out


def autospec_env(
    *,
    autospec_root: Path,
    autospec_python_env: Path,
    wrapper_dir: Path,
    secret_env: Path = DEFAULT_SECRET_ENV,
    model_key_name: str = "OPENROUTER_API_KEY",
) -> dict[str, str]:
    env = os.environ.copy()
    secrets = _read_env_file(secret_env)
    api_key = env.get(model_key_name) or secrets.get(model_key_name) or secrets.get("OPEN_ROUTER_KEY")
    if api_key:
        env["OPENAI_API_KEY"] = api_key
    env["API_URL_BASE"] = "https://openrouter.ai/api/v1"
    env["ROOT_DIR"] = str(autospec_root)
    env["PATH"] = os.pathsep.join(
        [
            str(wrapper_dir),
            str(autospec_root / "clang+llvm" / "bin"),
            str(autospec_root / "llvm" / "bin"),
            str(autospec_python_env / "bin"),
            env.get("PATH", ""),
        ]
    )
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        [str(autospec_root / "clang+llvm" / "lib"), env.get("LD_LIBRARY_PATH", "")]
    )
    env["LLVM_COMPILER"] = "clang"
    env["ASAN_OPTIONS"] = "detect_leaks=0"
    env["VERI_LIB_PATH"] = str(autospec_root / "llvm")
    env["AUTOSPEC_DOCKER_CPUS"] = env.get("AUTOSPEC_DOCKER_CPUS", "2")
    return env


def bmc_agent_openrouter_env(
    *,
    secret_env: Path = DEFAULT_SECRET_ENV,
    model: str = "claude-sonnet-4-6",
) -> dict[str, str]:
    """Environment for BMC-Agent comparison runs via OpenRouter.

    The production config already supports these variables. The experiment
    runner maps the local secret file into the environment so subprocesses do
    not fall back to the Claude Code CLI and no key is written to artifacts.
    """
    env = os.environ.copy()
    secrets = _read_env_file(secret_env)
    api_key = (
        env.get("BMC_AGENT_LLM_API_KEY")
        or env.get("OPENROUTER_API_KEY")
        or env.get("OPEN_ROUTER_KEY")
        or secrets.get("BMC_AGENT_LLM_API_KEY")
        or secrets.get("OPENROUTER_API_KEY")
        or secrets.get("OPEN_ROUTER_KEY")
    )
    if api_key:
        env["BMC_AGENT_LLM_API_KEY"] = api_key
    env["BMC_AGENT_LLM_BASE_URL"] = env.get("BMC_AGENT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    env["BMC_AGENT_LLM_PROVIDER"] = env.get("BMC_AGENT_LLM_PROVIDER", "openai")
    env["BMC_AGENT_LLM_MODEL"] = resolve_openrouter_model(model)
    return env


def preflight(
    *,
    autospec_root: Path,
    autospec_python_env: Path,
    wrapper_dir: Path,
    zip_path: Path,
    secret_env: Path,
    smoke_llm: bool = False,
) -> dict[str, Any]:
    env = autospec_env(
        autospec_root=autospec_root,
        autospec_python_env=autospec_python_env,
        wrapper_dir=wrapper_dir,
        secret_env=secret_env,
    )
    probes = {
        "zip_present": {"ok": zip_path.is_file(), "path": str(zip_path), "size": zip_path.stat().st_size if zip_path.is_file() else 0},
        "autospec_root": {"ok": autospec_root.is_dir(), "path": str(autospec_root)},
        "safe_config": check_safe_autospec_config(autospec_root),
        "docker": _run_probe(["docker", "info"], cwd=REPO_ROOT, timeout=20),
        "frama_c": _run_probe(["frama-c", "-version"], cwd=autospec_root, timeout=60, env=env),
        "why3": _run_probe(["docker", "run", "--rm", DEFAULT_FRAMA_IMAGE, "why3", "--version"], cwd=REPO_ROOT, timeout=60),
        "z3": _run_probe(["docker", "run", "--rm", DEFAULT_FRAMA_IMAGE, "z3", "-version"], cwd=REPO_ROOT, timeout=60),
        "veri_clang": _run_probe(["veri-clang", "--version"], cwd=autospec_root, timeout=30, env=env),
        "python_openai": _run_probe(
            [str(autospec_python_env / "bin" / "python"), "-c", "import openai, tiktoken; print(openai.__version__)"],
            cwd=autospec_root,
            timeout=30,
            env=env,
        ),
        "openrouter_key_configured": {"ok": bool(env.get("OPENAI_API_KEY")), "secret_value_recorded": False},
    }
    if smoke_llm:
        probes["openrouter_llm_smoke"] = _run_probe(
            [
                str(autospec_python_env / "bin" / "python"),
                "-c",
                (
                    "import openai, os; "
                    "openai.api_key=os.environ['OPENAI_API_KEY']; "
                    "openai.api_base=os.environ.get('API_URL_BASE'); "
                    "r=openai.ChatCompletion.create(model='gpt-3.5-turbo',"
                    "messages=[{'role':'user','content':'Reply OK only.'}],"
                    "max_tokens=4,temperature=0,stream=False); "
                    "print(r.choices[0].message['content'].strip())"
                ),
            ],
            cwd=autospec_root,
            timeout=60,
            env=env,
        )
    return {
        "schema_version": 1,
        "created_at_unix": int(time.time()),
        "probes": probes,
        "overall_ok": all(probe.get("ok", False) for probe in probes.values() if isinstance(probe, Mapping)),
    }


def check_safe_autospec_config(autospec_root: Path) -> dict[str, Any]:
    parse_args = autospec_root / "src" / "parse_args.py"
    config = autospec_root / "conf" / "config.json"
    parse_text = parse_args.read_text(encoding="utf-8", errors="replace") if parse_args.is_file() else ""
    config_data: Mapping[str, Any] = {}
    if config.is_file():
        try:
            config_data = json.loads(config.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"ok": False, "reason": "conf/config.json is invalid JSON"}
    key_fields = [
        key
        for key in config_data
        if re.search(r"(^|_)(api_?key|open_ai_api_key|secret|password)($|_)", key, re.IGNORECASE)
    ]
    return {
        "ok": "OPENAI_API_KEY = <redacted>" in parse_text and not key_fields,
        "parse_args_redacted": "OPENAI_API_KEY = <redacted>" in parse_text,
        "config_key_fields": key_fields,
        "secret_value_recorded": False,
    }


def validate_verified(
    cases: Sequence[Mapping[str, Any]],
    *,
    output: Path,
    autospec_root: Path,
    timeout: int,
    cpus: float,
    workers: int,
    limit: int | None = None,
) -> dict[str, Any]:
    selected = list(cases[:limit] if limit else cases)
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    report = {
        "schema_version": 1,
        "created_at_unix": int(time.time()),
        "method": "autospec_verified_annotation_wp",
        "rows": rows,
        "summary": {},
    }

    def one(case: Mapping[str, Any]) -> dict[str, Any]:
        source = Path(case["local_source"])
        rel = str(source.relative_to(autospec_root))
        cmd = [
            "docker",
            "run",
            "--rm",
            "--cpus",
            str(cpus),
            "-v",
            f"{autospec_root}:/work",
            "-w",
            "/work",
            DEFAULT_FRAMA_IMAGE,
            "frama-c",
            *AUTOSPEC_WP_FLAGS,
            "-wp-timeout",
            "8",
            rel,
        ]
        proc = _run_command(cmd, cwd=REPO_ROOT, timeout=timeout)
        parsed = parse_wp_output(proc["stdout"], proc["stderr"], proc["returncode"], proc["timed_out"])
        return {
            "case_id": case["case_id"],
            "family": case["family"],
            "source": str(source),
            "source_in_zip": case["source_in_zip"],
            "method": "autospec_verified_annotation_wp",
            "runtime_s": proc["runtime_s"],
            "command": cmd,
            "short_failure_mode": _short_failure(proc["stdout"], proc["stderr"]),
            **parsed,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, case) for case in selected]
        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())
            report["summary"] = summarize_statuses(rows)
            _write_json(output / "report.json", report)
            (output / "summary.md").write_text(render_rows_summary(report), encoding="utf-8")
    return report


def _short_failure(stdout: str, stderr: str) -> str:
    text = stdout + "\n" + stderr
    for pattern in ("annot-error", "User Error", "Proved goals", "Timeout", "Error"):
        idx = text.find(pattern)
        if idx >= 0:
            return text[idx : idx + 500].strip()
    return text[-500:].strip()


def summarize_statuses(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row.get("status", "unknown")) for row in rows)
    methods = Counter(str(row.get("method", "unknown")) for row in rows)
    return {
        "row_count": len(rows),
        "statuses": dict(sorted(statuses.items())),
        "methods": dict(sorted(methods.items())),
    }


def render_rows_summary(report: Mapping[str, Any]) -> str:
    lines = [
        "# AutoSpec Reproduction Report",
        "",
        f"Rows: {report.get('summary', {}).get('row_count', 0)}",
        "",
        "## Statuses",
        "",
    ]
    for status, count in report.get("summary", {}).get("statuses", {}).items():
        lines.append(f"- {status}: {count}")
    lines.extend(["", "## Rows", "", "| case | method | status | goals | runtime | failure |", "|---|---|---|---:|---:|---|"])
    for row in sorted(report.get("rows", []), key=lambda r: (str(r.get("method")), str(r.get("case_id")))):
        goals = ""
        if row.get("proved_goals") is not None and row.get("total_goals") is not None:
            goals = f"{row.get('proved_goals')}/{row.get('total_goals')}"
        failure = str(row.get("short_failure_mode", "")).replace("\n", " ")[:120]
        lines.append(
            f"| {row.get('case_id')} | {row.get('method')} | {row.get('status')} | {goals} | "
            f"{float(row.get('runtime_s') or 0):.1f} | {failure} |"
        )
    return "\n".join(lines) + "\n"


def _copy_autospec_workspace(autospec_root: Path, workspace: Path, *, model: str) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    for name in ("fuzz.py", "mark.py", "step.py", "requirements.txt", "README.md", "LICENSE"):
        src = autospec_root / name
        if src.exists():
            shutil.copy2(src, workspace / name)
    for name in ("src", "utils", "conf", "misc", "scripts", "benchmark"):
        src = autospec_root / name
        if src.exists():
            shutil.copytree(src, workspace / name, symlinks=True)
    for name in ("llvm", "clang+llvm"):
        src = autospec_root / name
        os.symlink(src, workspace / name)
    _adapt_parse_args(workspace / "src" / "parse_args.py", model)
    _adapt_gptcore(workspace / "utils" / "gptcore.py")


def _adapt_parse_args(path: Path, model: str) -> None:
    text = path.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        if "OPENAI_API_KEY = " in line and "print(" in line:
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f'{indent}print("[DEBUG] OPENAI_API_KEY = <redacted>")')
        else:
            lines.append(line)
    text = "\n".join(lines) + "\n"
    if model not in text:
        text = text.replace('"llama2-7b-chat-vllm"\n                    ]', f'"llama2-7b-chat-vllm",\n                    "{model}"\n                    ]')
    path.write_text(text, encoding="utf-8")


def _adapt_gptcore(path: Path) -> None:
    """Make AutoSpec's token estimator tolerate OpenRouter non-OpenAI IDs.

    AutoSpec only uses this value for accounting after a streamed response.
    Falling back to the GPT-3.5 chat-token heuristic is sufficient for the
    controlled same-Claude pilot and avoids changing prompt/verification logic.
    """
    text = path.read_text(encoding="utf-8")
    old = (
        "    else:\n"
        "        raise NotImplementedError(\n"
        "            f\"\"\"num_tokens_from_messages() is not implemented for model {model}. See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens.\"\"\"\n"
        "        )\n"
    )
    new = (
        "    else:\n"
        "        return num_tokens_from_messages(messages, model=\"gpt-3.5-turbo-0613\")\n"
    )
    if old in text:
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def _cleanup_case_side_effects(workspace: Path, source_in_zip: str) -> None:
    source = source_to_local_path(source_in_zip, workspace)
    if source.suffix != ".c":
        return
    stem = source.with_suffix("")
    for suffix in (".c.pickle", "_marked.c", "_infilled.c", "_m.c"):
        path = Path(str(stem) + suffix) if suffix.startswith("_") else Path(str(source) + suffix.removeprefix(".c"))
        if path.exists():
            path.unlink()


def _latest_final_result(out_dir: Path, source_in_zip: str) -> Path | None:
    stem = Path(source_in_zip).stem
    candidates = sorted(out_dir.glob(f"{stem}_*/final_result"))
    return candidates[-1] if candidates else None


def run_autospec(
    cases: Sequence[Mapping[str, Any]],
    *,
    output: Path,
    autospec_root: Path,
    autospec_python_env: Path,
    wrapper_dir: Path,
    secret_env: Path,
    model: str,
    method: str,
    timeout: int,
    workers: int = 1,
    limit: int | None = None,
) -> dict[str, Any]:
    output = output.resolve()
    autospec_root = autospec_root.resolve()
    autospec_python_env = autospec_python_env.resolve()
    wrapper_dir = wrapper_dir.resolve()
    secret_env = secret_env.resolve()
    selected = list(cases[:limit] if limit else cases)
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    report = {"schema_version": 1, "created_at_unix": int(time.time()), "method": method, "rows": rows, "summary": {}}
    resolved_model = resolve_openrouter_model(model)

    def one(case: Mapping[str, Any]) -> dict[str, Any]:
        case_dir = output / "cases" / case["case_id"]
        workspace = case_dir / "workspace"
        _copy_autospec_workspace(autospec_root, workspace, model=resolved_model)
        _cleanup_case_side_effects(workspace, case["source_in_zip"])
        rel_source = str(Path(case["source_in_zip"]).relative_to("AutoSpec"))
        env = autospec_env(
            autospec_root=workspace,
            autospec_python_env=autospec_python_env,
            wrapper_dir=wrapper_dir,
            secret_env=secret_env,
        )
        env["AUTOSPEC_DOCKER_CPUS"] = "2"
        cmd = [str(autospec_python_env / "bin" / "python"), "fuzz.py", "-f", rel_source, "-m", resolved_model]
        proc = _run_command(cmd, cwd=workspace, timeout=timeout, env=env)
        log_path = case_dir / "autospec.log"
        log_text = (proc["stdout"] + "\n" + proc["stderr"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(log_text, encoding="utf-8")
        secret_findings = scan_for_secret_text(log_text)
        final_result = _latest_final_result(workspace / "out", case["source_in_zip"])
        parsed = parse_final_result(final_result.read_text(encoding="utf-8", errors="replace")) if final_result else {"result": "missing"}
        row = {
            "case_id": case["case_id"],
            "family": case["family"],
            "source_in_zip": case["source_in_zip"],
            "method": method,
            "model": model,
            "resolved_model": resolved_model,
            "status": parsed["result"] if not proc["timed_out"] else "timeout",
            "runtime_s": proc["runtime_s"],
            "returncode": proc["returncode"],
            "timed_out": proc["timed_out"],
            "iteration": parsed.get("iteration"),
            "tokens_usage": parsed.get("tokens_usage"),
            "final_result": str(final_result) if final_result else "",
            "generated_merged": str(final_result.parent / f"{Path(case['source_in_zip']).stem}_merged.c") if final_result else "",
            "log": str(log_path),
            "secret_scan": secret_findings,
            "short_failure_mode": "" if parsed.get("result") == "pass" else _short_failure(proc["stdout"], proc["stderr"]),
        }
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, case) for case in selected]
        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())
            report["summary"] = summarize_statuses(rows)
            _write_json(output / "report.json", report)
            (output / "summary.md").write_text(render_rows_summary(report), encoding="utf-8")
    return report


def run_ours(
    cases: Sequence[Mapping[str, Any]],
    *,
    output: Path,
    model: str,
    secret_env: Path,
    timeout: int,
    limit: int | None = None,
) -> dict[str, Any]:
    output = output.resolve()
    secret_env = secret_env.resolve()
    selected = list(cases[:limit] if limit else cases)
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    report = {"schema_version": 1, "created_at_unix": int(time.time()), "method": "bmc_agent_native_acsl", "rows": rows, "summary": {}}
    resolved_model = resolve_openrouter_model(model)
    for case in selected:
        case_dir = output / "cases" / case["case_id"]
        artifact_root = case_dir / "artifacts"
        driver = f"{case['case_id']}__bmc_agent_acsl"
        source = Path(case["local_source"])
        env = bmc_agent_openrouter_env(secret_env=secret_env, model=resolved_model)
        gen_cmd = [
            "uv",
            "run",
            "bmc-agent",
            "acsl-generate",
            "--source",
            str(source),
            "--driver",
            driver,
            "--output",
            str(artifact_root),
            "--no-run-frama-c",
            "--model",
            resolved_model,
        ]
        gen = _run_command(gen_cmd, cwd=REPO_ROOT, timeout=timeout, env=env)
        spec_json = artifact_root / driver / "acsl_native" / "acsl_specs.json"
        qual = {"returncode": None, "timed_out": False, "runtime_s": 0.0, "stdout": "", "stderr": ""}
        quality_report = artifact_root / driver / "acsl_quality" / "quality_report.json"
        if spec_json.is_file():
            qual_cmd = [
                "uv",
                "run",
                "bmc-agent",
                "acsl-quality",
                "--source",
                str(source),
                "--driver",
                driver,
                "--spec-json",
                str(spec_json),
                "--output",
                str(artifact_root),
                "--recover-asserts",
                "--timeout",
                str(timeout),
                "--wp-timeout",
                "30",
                "--cpus",
                "2",
            ]
            qual = _run_command(qual_cmd, cwd=REPO_ROOT, timeout=timeout + 60, env=env)
        quality = _load_json(quality_report) if quality_report.is_file() else {}
        frama = quality.get("frama_c", {})
        status = frama.get("status") or ("generation_error" if not spec_json.is_file() else "quality_error")
        row = {
            "case_id": case["case_id"],
            "family": case["family"],
            "source_in_zip": case["source_in_zip"],
            "method": "bmc_agent_native_acsl",
            "model": model,
            "resolved_model": resolved_model,
            "status": status,
            "runtime_s": gen["runtime_s"] + qual["runtime_s"],
            "returncode": qual["returncode"] if spec_json.is_file() else gen["returncode"],
            "timed_out": bool(gen["timed_out"] or qual["timed_out"]),
            "proved_goals": frama.get("proved_goals"),
            "total_goals": frama.get("total_goals"),
            "spec_json": str(spec_json) if spec_json.is_file() else "",
            "quality_report": str(quality_report) if quality_report.is_file() else "",
            "vacuity_warnings": quality.get("build", {}).get("vacuity_warnings", []),
            "downstream_proof_utility": quality.get("downstream_proof_utility", {}),
            "secret_scan": scan_for_secret_text(gen["stdout"] + gen["stderr"] + qual["stdout"] + qual["stderr"]),
            "short_failure_mode": _short_failure(gen["stdout"] + qual["stdout"], gen["stderr"] + qual["stderr"]) if status != "success" else "",
        }
        rows.append(row)
        report["summary"] = summarize_statuses(rows)
        _write_json(output / "report.json", report)
        (output / "summary.md").write_text(render_rows_summary(report), encoding="utf-8")
    return report


def aggregate_reports(inputs: Sequence[Path], output: Path) -> dict[str, Any]:
    rows = []
    source_reports = []
    for path in inputs:
        report_path = path / "report.json" if path.is_dir() else path
        data = _load_json(report_path)
        source_reports.append(str(report_path))
        rows.extend(data.get("rows", []))
    report = {
        "schema_version": 1,
        "created_at_unix": int(time.time()),
        "source_reports": source_reports,
        "rows": rows,
        "summary": summarize_statuses(rows),
    }
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "report.json", report)
    (output / "summary.md").write_text(render_rows_summary(report), encoding="utf-8")
    return report


def _case_set(manifest: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    try:
        return [dict(item) for item in manifest["case_sets"][name]]
    except KeyError as exc:
        raise SystemExit(f"case set not found: {name}") from exc


def cmd_preflight(args: argparse.Namespace) -> int:
    report = preflight(
        autospec_root=Path(args.autospec_root),
        autospec_python_env=Path(args.autospec_python_env),
        wrapper_dir=Path(args.wrapper_dir),
        zip_path=Path(args.autospec_zip),
        secret_env=Path(args.secret_env),
        smoke_llm=args.smoke_llm,
    )
    _write_json(Path(args.output), report)
    print(f"Preflight report: {args.output}")
    print(f"Overall OK: {report['overall_ok']}")
    return 0 if report["overall_ok"] else 2


def cmd_manifest(args: argparse.Namespace) -> int:
    manifest = build_manifest_from_zip(Path(args.autospec_zip), autospec_root=Path(args.autospec_root))
    output = Path(args.output)
    _write_json(output, manifest)
    output.with_suffix(".md").write_text(render_manifest_summary(manifest), encoding="utf-8")
    print(f"Manifest: {output}")
    print(json.dumps(manifest["summary"], indent=2, sort_keys=True))
    return 0


def render_manifest_summary(manifest: Mapping[str, Any]) -> str:
    lines = [
        "# AutoSpec Manifest",
        "",
        "## Counts",
        "",
    ]
    for key, value in manifest.get("summary", {}).items():
        lines.append(f"- {key}: {value}")
    if manifest.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in manifest["warnings"])
    lines.extend(["", "## Pilot10", ""])
    for case in manifest.get("case_sets", {}).get("pilot10", []):
        lines.append(f"- {case['case_id']}: `{case['source_in_zip']}`")
    return "\n".join(lines) + "\n"


def cmd_reconcile_raw(args: argparse.Namespace) -> int:
    report = reconcile_raw(Path(args.autospec_zip))
    output = Path(args.output)
    _write_json(output, report)
    output.with_suffix(".md").write_text(render_raw_summary(report), encoding="utf-8")
    print(f"Raw reconciliation: {output}")
    print(json.dumps({"total_final_results": report["total_final_results"], "by_raw_folder": report["by_raw_folder"]}, indent=2, sort_keys=True))
    return 0


def render_raw_summary(report: Mapping[str, Any]) -> str:
    lines = ["# AutoSpec Raw Reconciliation", "", f"Final result files: {report.get('total_final_results', 0)}", "", "| raw folder | pass | fail | unknown |", "|---|---:|---:|---:|"]
    for folder, counts in report.get("by_raw_folder", {}).items():
        lines.append(f"| {folder} | {counts.get('pass', 0)} | {counts.get('fail', 0)} | {counts.get('unknown', 0)} |")
    return "\n".join(lines) + "\n"


def cmd_validate_verified(args: argparse.Namespace) -> int:
    manifest = _load_json(args.manifest)
    report = validate_verified(
        _case_set(manifest, args.case_set),
        output=Path(args.output),
        autospec_root=Path(args.autospec_root),
        timeout=args.timeout,
        cpus=args.cpus,
        workers=args.workers,
        limit=args.limit or None,
    )
    print(f"Validation report: {Path(args.output) / 'report.json'}")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


def cmd_run_autospec(args: argparse.Namespace) -> int:
    manifest = _load_json(args.manifest)
    report = run_autospec(
        _case_set(manifest, args.case_set),
        output=Path(args.output),
        autospec_root=Path(args.autospec_root),
        autospec_python_env=Path(args.autospec_python_env),
        wrapper_dir=Path(args.wrapper_dir),
        secret_env=Path(args.secret_env),
        model=args.model,
        method=args.method,
        timeout=args.timeout,
        workers=args.workers,
        limit=args.limit or None,
    )
    print(f"AutoSpec run report: {Path(args.output) / 'report.json'}")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


def cmd_run_ours(args: argparse.Namespace) -> int:
    manifest = _load_json(args.manifest)
    report = run_ours(
        _case_set(manifest, args.case_set),
        output=Path(args.output),
        model=args.model,
        secret_env=Path(args.secret_env),
        timeout=args.timeout,
        limit=args.limit or None,
    )
    print(f"BMC-Agent ACSL report: {Path(args.output) / 'report.json'}")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    report = aggregate_reports([Path(p) for p in args.inputs], Path(args.output))
    print(f"Aggregate report: {Path(args.output) / 'report.json'}")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autospec-zip", default=str(DEFAULT_ZIP))
    parser.add_argument("--autospec-root", default=str(DEFAULT_AUTOSPEC_ROOT))
    parser.add_argument("--autospec-python-env", default=str(DEFAULT_AUTOSPEC_ENV))
    parser.add_argument("--wrapper-dir", default=str(DEFAULT_WRAPPER_DIR))
    parser.add_argument("--secret-env", default=str(DEFAULT_SECRET_ENV))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preflight")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT / "preflight.json"))
    p.add_argument("--smoke-llm", action="store_true")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("manifest")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT / "manifest.json"))
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("reconcile-raw")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT / "raw_reconciliation.json"))
    p.set_defaults(func=cmd_reconcile_raw)

    p = sub.add_parser("validate-verified")
    p.add_argument("--manifest", default=str(DEFAULT_OUTPUT / "manifest.json"))
    p.add_argument("--case-set", default="verified_annotations")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT / "verified_validation"))
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--cpus", type=float, default=2.0)
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_validate_verified)

    p = sub.add_parser("run-autospec")
    p.add_argument("--manifest", default=str(DEFAULT_OUTPUT / "manifest.json"))
    p.add_argument("--case-set", default="pilot10")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT / "autospec_gpt35_pilot10"))
    p.add_argument("--model", default="gpt-3.5-turbo")
    p.add_argument("--method", default="autospec_gpt35_openrouter")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_run_autospec)

    p = sub.add_parser("run-ours")
    p.add_argument("--manifest", default=str(DEFAULT_OUTPUT / "manifest.json"))
    p.add_argument("--case-set", default="pilot10")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT / "bmc_agent_acsl_pilot10"))
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_run_ours)

    p = sub.add_parser("aggregate")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT / "aggregate"))
    p.set_defaults(func=cmd_aggregate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
