#!/usr/bin/env python3
"""Native ACSL/Frama-C spec-quality benchmark adapter.

This is an evaluation adapter, not a replacement for BMC-Agent's normal
CBMC/Kani pipeline. It builds small ACSL benchmark manifests, runs native ACSL
quality checks when Frama-C/WP is available, and keeps strength, validity,
coverage, overconstraint, and downstream utility as separate result dimensions.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = Path("/mnt/disk7/jw_bmc/spec_quality_data")
DEFAULT_ARTIFACT_DIR = REPO_ROOT / "artifacts" / "spec_quality_benchmark"
AUTOSPEC_RECORD_URL = "https://zenodo.org/records/10912658"
AUTOSPEC_DOWNLOAD_URL = "https://zenodo.org/api/records/10912658/files/AutoSpec.zip/content"
DEFAULT_FRAMA_C_DOCKER_IMAGE = "framac/frama-c:26.0.debian"


def _repo_rel(path: str | Path) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.relative_to(REPO_ROOT))
        except ValueError:
            return str(p)
    return str(p)


def _resolve(path: str | Path | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    if p.is_absolute():
        return p
    return REPO_ROOT / p


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_probe(command: list[str], timeout: int = 10) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {"configured": False, "error": "not_found", "command": command}
    except subprocess.TimeoutExpired:
        return {"configured": True, "ok": False, "error": "timeout", "command": command}
    return {
        "configured": True,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip()[-500:],
        "stderr": proc.stderr.strip()[-500:],
        "command": command,
    }


def probe_toolchain(
    *,
    frama_c_cmd: str = "",
    docker_image: str = DEFAULT_FRAMA_C_DOCKER_IMAGE,
) -> dict[str, Any]:
    local_frama = shutil.which(frama_c_cmd.split()[0]) if frama_c_cmd else shutil.which("frama-c")
    docker_bin = shutil.which("docker")
    docker_probe = _run_probe([docker_bin, "info"], timeout=10) if docker_bin else {"configured": False}
    docker_available = bool(docker_bin and docker_probe.get("ok"))
    frama_c_available = bool(local_frama or docker_available)

    try:
        from bmc_agent.config import Config

        config = Config.from_env()
        model = config.llm_role_overrides.get("spec_gen", {}).get("model") or config.llm_model
        provider = config.llm_role_overrides.get("spec_gen", {}).get("provider") or config.llm_provider
    except Exception as exc:  # pragma: no cover - defensive only
        model = ""
        provider = ""
        config_error = str(exc)
    else:
        config_error = ""

    secret_env_names = (
        "BMC_AGENT_HYBRID_SPEC_GEN_KEY",
        "BMC_AGENT_LLM_SPEC_GEN_API_KEY",
        "BMC_AGENT_LLM_DEFAULT_API_KEY",
        "BMC_AGENT_LLM_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OPEN_ROUTER_KEY",
    )
    return {
        "frama_c": {
            "available": frama_c_available,
            "status": "available_local" if local_frama else ("available_docker" if docker_available else "frama_c_unavailable"),
            "local_path": local_frama or "",
            "docker_image": docker_image,
        },
        "docker": {
            "path": docker_bin or "",
            "daemon_available": docker_available,
            "probe": docker_probe,
        },
        "gcc": {"path": shutil.which("gcc") or ""},
        "uv": {"path": shutil.which("uv") or ""},
        "why3": {"path": shutil.which("why3") or ""},
        "z3": {"path": shutil.which("z3") or ""},
        "llm": {
            "configured": any(os.environ.get(name) for name in secret_env_names),
            "model": model,
            "provider": provider,
            "config_error": config_error,
            "secret_values_recorded": False,
        },
    }


def discover_artifacts(data_dir: Path) -> dict[str, Any]:
    autospec_zip = locate_autospec_zip(data_dir)
    return {
        "data_dir": str(data_dir),
        "autospec": {
            "status": "zip_present" if autospec_zip else "zip_missing",
            "zip_path": str(autospec_zip) if autospec_zip else "",
            "record_url": AUTOSPEC_RECORD_URL,
            "replication_claim": "AutoSpec replication only after importing this official artifact and using a compatible Frama-C toolchain.",
        },
        "specsyn": {
            "status": "official_artifact_not_found",
            "replication_claim": "Use SpecSyn-inspired metrics unless an official artifact is later found.",
            "paper": "https://arxiv.org/abs/2604.21570",
        },
        "specgen": {
            "status": "related_metric_only",
            "reason": "SpecGen targets Java/JML/OpenJML rather than C/ACSL/Frama-C.",
            "paper": "https://arxiv.org/abs/2401.08807",
        },
        "fallback_datasets": [
            {
                "name": "FM-Bench-Verified",
                "url": "https://huggingface.co/datasets/fm-universe/FM-bench-verified",
                "claim": "public C programs with ground-truth ACSL specs",
            },
            {
                "name": "ACSL-by-Example",
                "url": "https://fraunhoferfokus.github.io/acsl-by-example/",
                "claim": "public Frama-C/WP verified ACSL examples",
            },
        ],
    }


def locate_autospec_zip(data_dir: Path, explicit: str | None = None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            data_dir / "AutoSpec.zip",
            Path("/mnt/disk7/jw_bmc/papers/AutoSpec.zip"),
            Path("/mnt/disk7/jw_bmc/AutoSpec.zip"),
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def download_autospec_zip(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".zip.partial")
    with urllib.request.urlopen(AUTOSPEC_DOWNLOAD_URL, timeout=60) as resp:
        with tmp.open("wb") as out:
            shutil.copyfileobj(resp, out)
    tmp.replace(dest)
    return dest


def _zip_c_files(names: list[str], *, verified: bool) -> list[str]:
    out = []
    for name in names:
        lower = name.lower()
        if not lower.endswith(".c"):
            continue
        if "/benchmark/" not in lower and not lower.startswith("benchmark/"):
            continue
        is_verified = "_verified/" in lower or lower.endswith("_verified.c") or "/verified/" in lower
        if verified != is_verified:
            continue
        if "__macosx" in lower:
            continue
        out.append(name)
    return sorted(out)


def inspect_autospec_zip(zip_path: Path, *, sample_limit: int = 50) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as zf:
        names = [info.filename for info in zf.infolist() if not info.is_dir()]
    benchmark_sources = _zip_c_files(names, verified=False)
    verified_sources = _zip_c_files(names, verified=True)
    mutation_sources = sorted(
        name for name in benchmark_sources if "mutat" in name.lower() or "_mutation" in name.lower()
    )
    categories = Counter(_autospec_category(name) for name in benchmark_sources)
    candidates = [
        {
            "case_id": f"autospec_{i:04d}_{Path(name).stem}",
            "source_in_zip": name,
            "category": _autospec_category(name),
            "verified_counterpart_in_zip": _find_verified_counterpart(name, verified_sources),
        }
        for i, name in enumerate(benchmark_sources[:sample_limit], start=1)
    ]
    return {
        "source": "AutoSpec official artifact",
        "artifact_url": AUTOSPEC_RECORD_URL,
        "zip_path": str(zip_path),
        "counts": {
            "benchmark_c_files": len(benchmark_sources),
            "verified_c_files": len(verified_sources),
            "mutation_like_c_files": len(mutation_sources),
        },
        "categories": dict(sorted(categories.items())),
        "candidates": candidates,
        "replication_label": "AutoSpec replication candidate metadata",
    }


def _autospec_category(path: str) -> str:
    parts = Path(path).parts
    lowered = [part.lower() for part in parts]
    try:
        idx = lowered.index("benchmark")
    except ValueError:
        return "unknown"
    if idx + 1 >= len(parts):
        return "unknown"
    category = parts[idx + 1]
    return category.replace("_benchmark", "").replace("_verified", "")


def _find_verified_counterpart(source: str, verified_sources: list[str]) -> str:
    stem = Path(source).stem
    for candidate in verified_sources:
        if Path(candidate).stem == stem:
            return candidate
    return ""


def build_pilot_manifest(
    *,
    size: int,
    data_dir: Path,
    autospec_manifest: Path | None = None,
    allow_placeholders: bool = True,
) -> dict[str, Any]:
    if size not in (4, 10):
        raise ValueError("pilot size must be 4 or 10")

    cases = [_max2_case(), _read_at_case(), _public_reference_case(data_dir, autospec_manifest), _witness_case()]
    warnings = []
    if cases[2]["selection_status"] != "selected":
        warnings.append("No imported public ACSL dataset case is currently runnable; public reference case is a placeholder.")

    if size == 10:
        imported = _imported_autospec_cases(autospec_manifest)
        for imported_case in imported[:6]:
            cases.append(imported_case)
        while len(cases) < 10 and allow_placeholders:
            idx = len(cases) + 1
            cases.append(
                {
                    "case_id": f"public_reference_placeholder_{idx}",
                    "category": "public_reference",
                    "selection_status": "not_available",
                    "benchmark_origin": "requires_imported_acsl_dataset",
                    "methods": [
                        {
                            "method_id": "not_available",
                            "method_type": "not_available",
                            "reason": "Run import-autospec or import another public ACSL dataset before 10-case expansion.",
                        }
                    ],
                }
            )
        if len(cases) < 10:
            warnings.append("10-case manifest requested but not enough imported cases are available.")

    return {
        "schema_version": 1,
        "created_at_unix": int(time.time()),
        "protocol_label": "SpecSyn-inspired native ACSL spec-quality benchmark",
        "replication_labels": {
            "autospec": "AutoSpec replication only for imported official artifact rows with compatible toolchain",
            "specsyn": "SpecSyn-inspired unless official artifact is found",
            "specgen": "SpecGen-related metric only in v1",
        },
        "data_dir": str(data_dir),
        "size": size,
        "warnings": warnings,
        "cases": cases[:size],
    }


def _max2_case() -> dict[str, Any]:
    return {
        "case_id": "max2_scalar",
        "category": "scalar",
        "selection_status": "selected",
        "benchmark_origin": "local_smoke",
        "source": "experiments/acsl_backend_pilot/max2.c",
        "function": "max2",
        "recover_asserts": True,
        "notes": "Scalar functional spec with recovered assertion targets for downstream proof utility.",
        "methods": [
            {
                "method_id": "reference_strong",
                "method_type": "static_native_acsl",
                "spec_json": "experiments/spec_quality_compare/max2_native_strong_acsl.json",
                "ground_truth_json": "experiments/spec_quality_compare/max2_coverage_strong.json",
                "mutation_json": "experiments/spec_quality_compare/max2_mutations.json",
            },
            {
                "method_id": "reference_weak",
                "method_type": "static_native_acsl",
                "spec_json": "experiments/spec_quality_compare/max2_native_weak_acsl.json",
                "ground_truth_json": "experiments/spec_quality_compare/max2_coverage_weak.json",
                "mutation_json": "experiments/spec_quality_compare/max2_mutations.json",
            },
            {
                "method_id": "bmc_agent_acsl_configured_model",
                "method_type": "generate_native_acsl",
                "ground_truth_json": "experiments/spec_quality_compare/max2_coverage_strong.json",
                "mutation_json": "experiments/spec_quality_compare/max2_mutations.json",
                "model_policy": "use configured BMC-Agent/OpenRouter Claude model",
            },
        ],
    }


def _read_at_case() -> dict[str, Any]:
    return {
        "case_id": "read_at_bounds",
        "category": "pointer_bounds",
        "selection_status": "selected",
        "benchmark_origin": "local_smoke",
        "source": "experiments/spec_quality_compare/read_at.c",
        "function": "read_at",
        "recover_asserts": False,
        "notes": "Pointer/bounds case that separates valid strong and weak ACSL specs by VDR.",
        "methods": [
            {
                "method_id": "reference_strong",
                "method_type": "static_native_acsl",
                "spec_json": "experiments/spec_quality_compare/read_at_native_strong_acsl.json",
                "ground_truth_json": "experiments/spec_quality_compare/read_at_coverage_strong.json",
                "mutation_json": "experiments/spec_quality_compare/read_at_mutations.json",
            },
            {
                "method_id": "reference_weak",
                "method_type": "static_native_acsl",
                "spec_json": "experiments/spec_quality_compare/read_at_native_weak_acsl.json",
                "ground_truth_json": "experiments/spec_quality_compare/read_at_coverage_weak.json",
                "mutation_json": "experiments/spec_quality_compare/read_at_mutations.json",
            },
            {
                "method_id": "bmc_agent_acsl_configured_model",
                "method_type": "generate_native_acsl",
                "ground_truth_json": "experiments/spec_quality_compare/read_at_coverage_strong.json",
                "mutation_json": "experiments/spec_quality_compare/read_at_mutations.json",
                "model_policy": "use configured BMC-Agent/OpenRouter Claude model",
            },
        ],
    }


def _public_reference_case(data_dir: Path, autospec_manifest: Path | None) -> dict[str, Any]:
    imported = _imported_autospec_cases(autospec_manifest)
    if imported:
        case = imported[0]
        case["case_id"] = "autospec_public_reference_1"
        return case
    return {
        "case_id": "public_acsl_reference_pending",
        "category": "public_reference",
        "selection_status": "not_available",
        "benchmark_origin": "AutoSpec/FM-Bench/ACSL-by-Example",
        "data_dir": str(data_dir),
        "methods": [
            {
                "method_id": "not_available",
                "method_type": "not_available",
                "reason": "No imported public ACSL benchmark source is available yet.",
            }
        ],
    }


def _witness_case() -> dict[str, Any]:
    return {
        "case_id": "ncdev_bar_read_overconstraint",
        "category": "overconstraint",
        "selection_status": "selected",
        "benchmark_origin": "BMC-Agent known failure mode fixture",
        "notes": "Witness-preservation check for preconditions that assume away a known bug state.",
        "methods": [
            {
                "method_id": "precondition_witness_preservation",
                "method_type": "witness_preservation",
            }
        ],
    }


def _imported_autospec_cases(autospec_manifest: Path | None) -> list[dict[str, Any]]:
    if not autospec_manifest or not autospec_manifest.is_file():
        return []
    data = _load_json(autospec_manifest)
    cases = []
    for candidate in data.get("candidates", []):
        cases.append(
            {
                "case_id": candidate.get("case_id", "autospec_case"),
                "category": "public_reference",
                "selection_status": "metadata_only",
                "benchmark_origin": "AutoSpec official artifact",
                "source_in_zip": candidate.get("source_in_zip", ""),
                "verified_counterpart_in_zip": candidate.get("verified_counterpart_in_zip", ""),
                "methods": [
                    {
                        "method_id": "autospec_metadata_only",
                        "method_type": "not_available",
                        "reason": "AutoSpec source is identified in the official zip but not extracted into a runnable local ACSL case.",
                    }
                ],
            }
        )
    return cases


def run_manifest(
    manifest: Mapping[str, Any],
    *,
    run_dir: Path,
    allow_llm: bool = False,
    force_frama_c: bool = False,
    wp_timeout: int = 30,
    timeout: int = 120,
    cpus: float = 2.0,
    frama_c_cmd: str = "",
    frama_c_docker_image: str = DEFAULT_FRAMA_C_DOCKER_IMAGE,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    toolchain = probe_toolchain(frama_c_cmd=frama_c_cmd, docker_image=frama_c_docker_image)
    frama_available = bool(toolchain["frama_c"]["available"]) or force_frama_c
    rows = []

    for case in manifest.get("cases", []):
        for method in case.get("methods", []):
            method_type = method.get("method_type", "")
            if method_type == "static_native_acsl":
                if not frama_available:
                    rows.append(_static_preview_row(case, method, "frama_c_unavailable"))
                else:
                    rows.append(
                        _run_static_acsl_quality(
                            case,
                            method,
                            run_dir=run_dir,
                            wp_timeout=wp_timeout,
                            timeout=timeout,
                            cpus=cpus,
                            frama_c_cmd=frama_c_cmd,
                            frama_c_docker_image=frama_c_docker_image,
                        )
                    )
            elif method_type == "generate_native_acsl":
                if not allow_llm:
                    rows.append(_base_row(case, method, result_class="llm_disabled", status="llm_disabled"))
                else:
                    rows.append(
                        _run_generated_acsl_quality(
                            case,
                            method,
                            run_dir=run_dir,
                            frama_available=frama_available,
                            wp_timeout=wp_timeout,
                            timeout=timeout,
                            cpus=cpus,
                            frama_c_cmd=frama_c_cmd,
                            frama_c_docker_image=frama_c_docker_image,
                        )
                    )
            elif method_type == "witness_preservation":
                rows.append(_run_witness_preservation(case, method, run_dir=run_dir))
            else:
                rows.append(
                    _base_row(
                        case,
                        method,
                        result_class="not_available",
                        status=method.get("reason", case.get("selection_status", "not_available")),
                    )
                )

    report = {
        "schema_version": 1,
        "created_at_unix": int(time.time()),
        "manifest": manifest,
        "toolchain": toolchain,
        "rows": rows,
    }
    report["summary"] = summarize_rows(rows)
    _write_json(run_dir / "run_report.json", report)
    (run_dir / "summary.md").write_text(render_summary(report), encoding="utf-8")
    return report


def _base_row(
    case: Mapping[str, Any],
    method: Mapping[str, Any],
    *,
    result_class: str,
    status: str,
) -> dict[str, Any]:
    return {
        "case_id": case.get("case_id"),
        "category": case.get("category"),
        "method_id": method.get("method_id"),
        "method_type": method.get("method_type"),
        "status": status,
        "result_class": result_class,
        "frama_c_validity": {"status": "not_run"},
        "reference_coverage": {"status": "not_run"},
        "mutation_vdr": {"status": "not_run"},
        "vacuity_warnings": [],
        "overconstraint_warnings": [],
        "downstream_proof_utility": {"status": "not_run"},
        "artifact_paths": {},
    }


def _static_preview_row(case: Mapping[str, Any], method: Mapping[str, Any], reason: str) -> dict[str, Any]:
    row = _base_row(case, method, result_class=reason, status=reason)
    spec_path = _resolve(method.get("spec_json"))
    coverage_path = _resolve(method.get("ground_truth_json"))
    try:
        from bmc_agent.acsl_native import load_ground_truth_coverage, load_native_acsl_specs, vacuity_warnings

        if spec_path and spec_path.is_file():
            specs = load_native_acsl_specs(spec_path)
            row["vacuity_warnings"] = vacuity_warnings(specs)
            row["artifact_paths"]["spec_json"] = str(spec_path)
        else:
            row["status"] = "missing_spec_json"
            row["result_class"] = "missing_input"
        if coverage_path and coverage_path.is_file():
            row["reference_coverage"] = load_ground_truth_coverage(coverage_path)
            row["artifact_paths"]["ground_truth_json"] = str(coverage_path)
    except Exception as exc:
        row["status"] = f"preview_error: {exc}"
        row["result_class"] = "error"
    return row


def _run_static_acsl_quality(
    case: Mapping[str, Any],
    method: Mapping[str, Any],
    *,
    run_dir: Path,
    wp_timeout: int,
    timeout: int,
    cpus: float,
    frama_c_cmd: str,
    frama_c_docker_image: str,
) -> dict[str, Any]:
    source = _resolve(case.get("source"))
    spec_json = _resolve(method.get("spec_json"))
    if not source or not source.is_file() or not spec_json or not spec_json.is_file():
        return _base_row(case, method, result_class="missing_input", status="missing source/spec")

    driver = f"{case['case_id']}__{method['method_id']}"
    output_root = run_dir / "tool_runs"
    cmd = [
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
        str(output_root),
        "--function",
        str(case.get("function", "")),
        "--wp-timeout",
        str(wp_timeout),
        "--timeout",
        str(timeout),
        "--cpus",
        str(cpus),
        "--frama-c-docker-image",
        frama_c_docker_image,
    ]
    if case.get("recover_asserts"):
        cmd.append("--recover-asserts")
    if frama_c_cmd:
        cmd.extend(["--frama-c-cmd", frama_c_cmd])
    if method.get("ground_truth_json"):
        cmd.extend(["--ground-truth-json", str(_resolve(method.get("ground_truth_json")))])
    if method.get("mutation_json"):
        cmd.extend(["--mutation-json", str(_resolve(method.get("mutation_json")))])

    proc = _run_command(cmd, cwd=REPO_ROOT, timeout=timeout + 30)
    report_path = output_root / driver / "acsl_quality" / "quality_report.json"
    row = _row_from_quality_report(case, method, report_path)
    row["command"] = cmd
    row["process"] = proc
    if not report_path.is_file() and proc.get("returncode") != 0:
        row["status"] = "error"
        row["result_class"] = "error"
    return row


def _run_generated_acsl_quality(
    case: Mapping[str, Any],
    method: Mapping[str, Any],
    *,
    run_dir: Path,
    frama_available: bool,
    wp_timeout: int,
    timeout: int,
    cpus: float,
    frama_c_cmd: str,
    frama_c_docker_image: str,
) -> dict[str, Any]:
    source = _resolve(case.get("source"))
    if not source or not source.is_file():
        return _base_row(case, method, result_class="missing_input", status="missing source")
    driver = f"{case['case_id']}__{method['method_id']}"
    output_root = run_dir / "tool_runs"
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
        str(output_root),
        "--function",
        str(case.get("function", "")),
        "--no-run-frama-c",
    ]
    gen_proc = _run_command(gen_cmd, cwd=REPO_ROOT, timeout=timeout + 300)
    spec_json = output_root / driver / "acsl_native" / "acsl_specs.json"
    if not spec_json.is_file():
        row = _base_row(case, method, result_class="generation_error", status="generation_error")
        row["command"] = gen_cmd
        row["process"] = gen_proc
        return row
    static_method = dict(method)
    static_method["method_type"] = "static_native_acsl"
    static_method["spec_json"] = str(spec_json)
    if not frama_available:
        row = _static_preview_row(case, static_method, "frama_c_unavailable")
        row["method_type"] = "generate_native_acsl"
        row["generation_process"] = gen_proc
        return row
    row = _run_static_acsl_quality(
        case,
        static_method,
        run_dir=run_dir,
        wp_timeout=wp_timeout,
        timeout=timeout,
        cpus=cpus,
        frama_c_cmd=frama_c_cmd,
        frama_c_docker_image=frama_c_docker_image,
    )
    row["method_type"] = "generate_native_acsl"
    row["generation_process"] = gen_proc
    return row


def _run_witness_preservation(
    case: Mapping[str, Any],
    method: Mapping[str, Any],
    *,
    run_dir: Path,
) -> dict[str, Any]:
    out = run_dir / "witness_preservation" / str(case["case_id"])
    cmd = [
        "uv",
        "run",
        "python",
        "experiments/spec_quality_compare/witness_preservation_smoke.py",
        "--output",
        str(out),
    ]
    proc = _run_command(cmd, cwd=REPO_ROOT, timeout=60)
    report_path = out / "report.json"
    row = _base_row(case, method, result_class="overconstraint_checked", status="success")
    row["command"] = cmd
    row["process"] = proc
    row["artifact_paths"]["witness_report"] = str(report_path)
    if report_path.is_file():
        report = _load_json(report_path)
        warnings = [
            {
                "name": item.get("name"),
                "precondition": item.get("precondition"),
                "note": item.get("note"),
            }
            for item in report.get("results", [])
            if item.get("overconstraint_for_bug_discovery")
        ]
        row["overconstraint_warnings"] = warnings
        row["overconstraint_report"] = report
        row["result_class"] = "overconstraint_detected" if warnings else "witness_preserved"
    else:
        row["status"] = "error"
        row["result_class"] = "error"
    return row


def _run_command(command: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "timed_out": True,
            "runtime_s": time.time() - started,
            "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }
    return {
        "returncode": proc.returncode,
        "timed_out": False,
        "runtime_s": time.time() - started,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def _row_from_quality_report(case: Mapping[str, Any], method: Mapping[str, Any], report_path: Path) -> dict[str, Any]:
    row = _base_row(case, method, result_class="error", status="missing_quality_report")
    row["artifact_paths"]["quality_report"] = str(report_path)
    if not report_path.is_file():
        return row
    report = _load_json(report_path)
    frama = report.get("frama_c", {})
    row.update(
        {
            "status": frama.get("status", "unknown"),
            "frama_c_validity": frama,
            "reference_coverage": report.get("ground_truth_coverage", {"status": "not_run"}),
            "mutation_vdr": report.get("mutation_vdr", {"status": "not_run"}),
            "vacuity_warnings": report.get("build", {}).get("vacuity_warnings", []),
            "downstream_proof_utility": report.get("downstream_proof_utility", {"status": "not_run"}),
            "artifact_paths": {
                "quality_report": str(report_path),
                "annotated_source": report.get("annotated_source", ""),
                "spec_json": report.get("spec_json", ""),
            },
        }
    )
    row["result_class"] = classify_quality_row(row)
    return row


def classify_quality_row(row: Mapping[str, Any]) -> str:
    status = row.get("status")
    if status in {"frama_c_unavailable", "llm_disabled", "missing_spec_json"}:
        return str(status)
    if row.get("overconstraint_warnings"):
        return "overconstrained"
    frama_status = row.get("frama_c_validity", {}).get("status")
    if frama_status and frama_status not in {"success", "not_run"}:
        return "invalid"
    if row.get("result_class") in {"not_available", "missing_input", "generation_error"}:
        return str(row.get("result_class"))
    if frama_status == "not_run":
        return str(row.get("result_class", "not_run"))

    coverage = row.get("reference_coverage", {}).get("coverage")
    mutation_score = row.get("mutation_vdr", {}).get("mutation_score")
    downstream_ratio = row.get("downstream_proof_utility", {}).get("proof_ratio")
    vacuity = bool(row.get("vacuity_warnings"))
    if vacuity and (mutation_score in (None, 0) or coverage in (None, 0)):
        return "valid_but_weak"
    if mutation_score == 0 and (coverage is None or coverage <= 0.25):
        return "valid_but_weak"
    if any(
        value is not None and value > 0
        for value in (coverage, mutation_score, downstream_ratio)
    ):
        return "useful"
    return "valid_unknown_strength"


def summarize_rows(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    classes = Counter(str(row.get("result_class", "unknown")) for row in rows)
    statuses = Counter(str(row.get("status", "unknown")) for row in rows)
    return {
        "row_count": len(rows),
        "result_classes": dict(sorted(classes.items())),
        "statuses": dict(sorted(statuses.items())),
        "interpretable": any(cls in classes for cls in ("useful", "valid_but_weak", "overconstraint_detected", "overconstrained")),
    }


def render_summary(report: Mapping[str, Any]) -> str:
    lines = [
        "# Native ACSL Spec-Quality Benchmark",
        "",
        f"Protocol: {report.get('manifest', {}).get('protocol_label', '')}",
        f"Rows: {report.get('summary', {}).get('row_count', 0)}",
        "",
        "## Toolchain",
        "",
        f"- Frama-C: {report.get('toolchain', {}).get('frama_c', {}).get('status', 'unknown')}",
        f"- GCC: {report.get('toolchain', {}).get('gcc', {}).get('path', '') or 'missing'}",
        f"- LLM model: {report.get('toolchain', {}).get('llm', {}).get('model', '')}",
        "",
        "## Result Classes",
        "",
    ]
    for name, count in report.get("summary", {}).get("result_classes", {}).items():
        lines.append(f"- {name}: {count}")
    lines.extend(["", "## Rows", "", "| case | method | class | Frama-C | coverage | VDR | overconstraint | downstream |", "|---|---|---|---|---:|---:|---:|---:|"])
    for row in report.get("rows", []):
        coverage = row.get("reference_coverage", {}).get("coverage")
        vdr = row.get("mutation_vdr", {}).get("mutation_score")
        downstream = row.get("downstream_proof_utility", {}).get("proof_ratio")
        lines.append(
            "| {case} | {method} | {klass} | {frama} | {coverage} | {vdr} | {over} | {downstream} |".format(
                case=row.get("case_id", ""),
                method=row.get("method_id", ""),
                klass=row.get("result_class", ""),
                frama=row.get("frama_c_validity", {}).get("status", row.get("status", "")),
                coverage=_fmt_ratio(coverage),
                vdr=_fmt_ratio(vdr),
                over=len(row.get("overconstraint_warnings", [])),
                downstream=_fmt_ratio(downstream),
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation Guardrails",
            "",
            "- AutoSpec rows are replication rows only when they come from the official Zenodo artifact and a compatible Frama-C toolchain.",
            "- SpecSyn rows are SpecSyn-inspired unless an official SpecSyn artifact is imported.",
            "- SpecGen is treated as related metric context in v1 because it targets Java/JML/OpenJML.",
        ]
    )
    return "\n".join(lines) + "\n"


def _fmt_ratio(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:.2f}"
    return str(value)


def cmd_discover(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    report = {
        "artifacts": discover_artifacts(data_dir),
        "toolchain": probe_toolchain(
            frama_c_cmd=args.frama_c_cmd,
            docker_image=args.frama_c_docker_image,
        ),
    }
    output = Path(args.output)
    _write_json(output, report)
    print(f"Discovery report: {output}")
    print(f"Frama-C status: {report['toolchain']['frama_c']['status']}")
    print(f"AutoSpec status: {report['artifacts']['autospec']['status']}")
    return 0


def cmd_import_autospec(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    zip_path = locate_autospec_zip(data_dir, args.autospec_zip)
    if zip_path is None and args.download:
        zip_path = download_autospec_zip(data_dir / "AutoSpec.zip")
    if zip_path is None:
        print(
            "AutoSpec.zip not found. Put it under "
            f"{data_dir}/AutoSpec.zip or pass --autospec-zip.",
            file=sys.stderr,
        )
        return 2
    manifest = inspect_autospec_zip(zip_path, sample_limit=args.sample_limit)
    output = Path(args.output) if args.output else data_dir / "autospec_manifest.json"
    _write_json(output, manifest)
    print(f"AutoSpec manifest: {output}")
    print(json.dumps(manifest["counts"], indent=2, sort_keys=True))
    return 0


def cmd_select_pilot(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    autospec_manifest = Path(args.autospec_manifest) if args.autospec_manifest else data_dir / "autospec_manifest.json"
    manifest = build_pilot_manifest(
        size=args.size,
        data_dir=data_dir,
        autospec_manifest=autospec_manifest if autospec_manifest.is_file() else None,
        allow_placeholders=True,
    )
    output = Path(args.output)
    _write_json(output, manifest)
    write_manifest_summary(output.with_suffix(".md"), manifest)
    print(f"Pilot manifest: {output}")
    print(f"Cases: {len(manifest['cases'])}")
    if manifest["warnings"]:
        print("Warnings:")
        for warning in manifest["warnings"]:
            print(f"- {warning}")
    return 0


def write_manifest_summary(path: Path, manifest: Mapping[str, Any]) -> None:
    lines = [
        "# Spec-Quality Pilot Manifest",
        "",
        f"Protocol: {manifest.get('protocol_label')}",
        f"Size: {manifest.get('size')}",
        "",
        "## Cases",
        "",
        "| case | category | status | methods |",
        "|---|---|---|---|",
    ]
    for case in manifest.get("cases", []):
        methods = ", ".join(method.get("method_id", "") for method in case.get("methods", []))
        lines.append(f"| {case.get('case_id')} | {case.get('category')} | {case.get('selection_status')} | {methods} |")
    if manifest.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in manifest["warnings"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_run(args: argparse.Namespace) -> int:
    manifest = _load_json(args.manifest)
    run_dir = Path(args.output)
    report = run_manifest(
        manifest,
        run_dir=run_dir,
        allow_llm=args.allow_llm,
        force_frama_c=args.force_frama_c,
        wp_timeout=args.wp_timeout,
        timeout=args.timeout,
        cpus=args.cpus,
        frama_c_cmd=args.frama_c_cmd,
        frama_c_docker_image=args.frama_c_docker_image,
    )
    print(f"Run report: {run_dir / 'run_report.json'}")
    print(f"Summary:    {run_dir / 'summary.md'}")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs]
    rows = []
    manifests = []
    toolchain = {}
    for path in inputs:
        report_path = path / "run_report.json" if path.is_dir() else path
        data = _load_json(report_path)
        rows.extend(data.get("rows", []))
        manifests.append(data.get("manifest", {}))
        toolchain = data.get("toolchain", toolchain)
    report = {
        "schema_version": 1,
        "created_at_unix": int(time.time()),
        "manifest": {"protocol_label": "Aggregated native ACSL spec-quality benchmark", "inputs": [str(p) for p in inputs], "source_manifests": manifests},
        "toolchain": toolchain,
        "rows": rows,
        "summary": summarize_rows(rows),
    }
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "report.json", report)
    (output / "summary.md").write_text(render_summary(report), encoding="utf-8")
    print(f"Aggregate report: {output / 'report.json'}")
    print(f"Summary:          {output / 'summary.md'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    discover = sub.add_parser("discover", help="Record artifact and toolchain availability")
    discover.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    discover.add_argument("--output", default=str(DEFAULT_ARTIFACT_DIR / "discovery.json"))
    discover.add_argument("--frama-c-cmd", default="")
    discover.add_argument("--frama-c-docker-image", default=DEFAULT_FRAMA_C_DOCKER_IMAGE)
    discover.set_defaults(func=cmd_discover)

    autospec = sub.add_parser("import-autospec", help="Import AutoSpec metadata from the official zip")
    autospec.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    autospec.add_argument("--autospec-zip", default="")
    autospec.add_argument("--download", action="store_true", help="Download the 1.4GB AutoSpec zip into --data-dir")
    autospec.add_argument("--sample-limit", type=int, default=50)
    autospec.add_argument("--output", default="")
    autospec.set_defaults(func=cmd_import_autospec)

    select = sub.add_parser("select-pilot", help="Build a 4-case or 10-case pilot manifest")
    select.add_argument("--size", type=int, choices=(4, 10), default=4)
    select.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    select.add_argument("--autospec-manifest", default="")
    select.add_argument("--output", default=str(DEFAULT_ARTIFACT_DIR / "pilot4_manifest.json"))
    select.set_defaults(func=cmd_select_pilot)

    run = sub.add_parser("run", help="Run a selected spec-quality manifest")
    run.add_argument("--manifest", required=True)
    run.add_argument("--output", default=str(DEFAULT_ARTIFACT_DIR / "pilot_run"))
    run.add_argument("--allow-llm", action="store_true", help="Allow generated-spec rows to call the configured LLM")
    run.add_argument("--force-frama-c", action="store_true", help="Run Frama-C commands even if preflight says unavailable")
    run.add_argument("--wp-timeout", type=int, default=30)
    run.add_argument("--timeout", type=int, default=120)
    run.add_argument("--cpus", type=float, default=2.0)
    run.add_argument("--frama-c-cmd", default="")
    run.add_argument("--frama-c-docker-image", default=DEFAULT_FRAMA_C_DOCKER_IMAGE)
    run.set_defaults(func=cmd_run)

    aggregate = sub.add_parser("aggregate", help="Aggregate one or more run reports")
    aggregate.add_argument("inputs", nargs="+")
    aggregate.add_argument("--output", default=str(DEFAULT_ARTIFACT_DIR / "aggregate"))
    aggregate.set_defaults(func=cmd_aggregate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
