#!/usr/bin/env python3
"""ACSL-vs-DSL final-outcome equivalence adapter.

This experiment keeps the production ``bmc-agent verify`` pipeline untouched.
It replays an existing BMC-Agent DSL ``Spec`` twice:

* DSL branch: the original ``Spec`` is consumed by the normal CBMC harness
  generator.
* ACSL branch: the same ``Spec`` is translated to ACSL, projected back into
  the supported BMC-Agent ``Spec`` subset, then consumed by the same harness
  generator.

The goal is to isolate whether changing the spec representation alters final
outcomes.  It is not a native ACSL quality benchmark and it does not call an
LLM.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import difflib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = Path("/mnt/disk7/jw_bmc")
DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "acsl_dsl_outcome_compare"
EXTERNAL_FINDINGS = Path("/mnt/disk7/jw_bmc/aprover-findings-embargoed")
DEFAULT_CBMC = "/mnt/disk7/jw_bmc/tools/cbmc/usr/bin/cbmc"


@dataclass
class Case:
    case_id: str
    source: str
    driver: str
    function: str
    case_kind: str
    expected_behavior: str
    dsl_spec_path: str = ""
    property_type: str = ""
    run_dynamic_validation: bool = False
    notes: str = ""
    mode: str = "spec_replay"
    family: str = ""
    native_acsl_spec_path: str = ""
    source_resolver_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Case":
        return cls(
            case_id=str(data["case_id"]),
            source=str(data["source"]),
            driver=str(data.get("driver") or data["case_id"]),
            function=str(data.get("function") or ""),
            case_kind=str(data.get("case_kind") or "unknown"),
            expected_behavior=str(data.get("expected_behavior") or "unknown"),
            dsl_spec_path=str(data.get("dsl_spec_path") or ""),
            property_type=str(data.get("property_type") or ""),
            run_dynamic_validation=bool(data.get("run_dynamic_validation", False)),
            notes=str(data.get("notes") or ""),
            mode=str(data.get("mode") or "spec_replay"),
            family=str(data.get("family") or ""),
            native_acsl_spec_path=str(data.get("native_acsl_spec_path") or ""),
            source_resolver_status=str(data.get("source_resolver_status") or ""),
        )


@dataclass
class BranchResult:
    branch: str
    status: str
    final_label: str
    runtime_s: float
    harness_path: str = ""
    result_path: str = ""
    error: str = ""
    failing_property: str = ""
    failure_description: str = ""
    failure_location: dict[str, str] = field(default_factory=dict)
    counterexample_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _repo_rel(path: str | Path) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.relative_to(REPO_ROOT))
        except ValueError:
            return str(p)
    return str(p)


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def _stable_existing_dirs(paths: Sequence[str | Path]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.is_dir():
            continue
        text = str(path)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _include_dirs_for_source(source: Path) -> list[str]:
    """Infer experiment-local include dirs needed by generated harnesses."""

    dirs: list[str | Path] = []
    if source:
        dirs.append(source.parent)
    env_dirs = os.environ.get("BMC_AGENT_INCLUDE_DIRS", "")
    if env_dirs:
        dirs.extend(d for d in env_dirs.split(":") if d)

    try:
        rel = source.relative_to(WORKSPACE_ROOT / "vibeos")
    except ValueError:
        rel = None
    if rel is not None:
        kernel = WORKSPACE_ROOT / "vibeos" / "kernel"
        dirs.extend(
            [
                kernel,
                kernel / "libc",
                kernel / "hal",
            ]
        )

    return _stable_existing_dirs(dirs)


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_one_spec(path: str | Path):
    """Load a single BMC-Agent Spec from direct or ArtifactStore JSON."""

    from bmc_agent.spec import Spec

    data = _load_json(path)
    if isinstance(data, dict) and isinstance(data.get("spec"), dict):
        return Spec.from_dict(data["spec"])
    if isinstance(data, dict) and "function_name" in data:
        return Spec.from_dict(data)
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return Spec.from_dict(data[0])
    raise ValueError(f"unsupported spec JSON shape: {path}")


def _case_id_safe(raw: str, *, prefix: str = "") -> str:
    text = "".join(ch if ch.isalnum() else "_" for ch in raw)
    text = "_".join(part for part in text.split("_") if part)
    text = text[:140] or "case"
    return f"{prefix}{text}" if prefix else text


def _dedupe_cases(cases: Sequence[Case]) -> list[Case]:
    seen: set[str] = set()
    out: list[Case] = []
    for case in cases:
        base = case.case_id
        cid = base
        idx = 2
        while cid in seen:
            cid = f"{base}_{idx}"
            idx += 1
        if cid != case.case_id:
            case = dataclasses.replace(case, case_id=cid)
        seen.add(cid)
        out.append(case)
    return out


def _spec_function_name(path: Path) -> str:
    try:
        return _load_one_spec(path).function_name
    except Exception:
        return path.parent.name


def _expected_from_bug_report(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "unknown", ""
    try:
        report = _load_json(path).get("report", {})
    except Exception as exc:
        return "unknown", f"bug_report unreadable: {exc}"
    if report.get("error"):
        err = str(report.get("error") or "")
        return ("timeout_error" if "timed out" in err.lower() else "error"), err[:240]
    if report.get("verified") is True:
        return "clean", ""
    if report.get("verified") is False:
        cex = report.get("counterexamples") or []
        if cex:
            prop = str((cex[0] or {}).get("failing_property") or "")
            if ".unwind." in prop or prop.endswith(".unwind"):
                return "timeout_error", prop
            return "bug", prop
        return "unknown", "verified=false without counterexample"
    return "unknown", ""


def _looks_like_natural_language(expr: str) -> bool:
    return bool(
        re.search(
            r"\b(the|when|where|considering|smallest|multiple|valid|packet|"
            r"count of|does not|is a|all|each|every|bytes|buffer)\b",
            expr,
            re.IGNORECASE,
        )
    )


def _default_cases() -> list[Case]:
    """Return a small decision-oriented manifest seed.

    Only cases with available local artifacts are emitted.  The direct harness
    controls do not exercise ACSL projection; they preserve known-finding
    smoke coverage until more confirmed finding-level DSL specs are available.
    """

    cases: list[Case] = []
    seeds = [
        Case(
            case_id="synthetic_max2_clean",
            source="experiments/acsl_backend_pilot/max2.c",
            driver="acsl_dsl_synthetic",
            function="max2",
            case_kind="synthetic_clean",
            expected_behavior="clean",
            dsl_spec_path="experiments/acsl_backend_pilot/max2_spec.json",
            property_type="functional_postcondition",
            family="synthetic",
            notes="Supported scalar postcondition roundtrip.",
        ),
        Case(
            case_id="synthetic_read_at_overconstraint",
            source="experiments/spec_quality_compare/read_at.c",
            driver="acsl_dsl_synthetic",
            function="read_at",
            case_kind="synthetic_overconstraint",
            expected_behavior="overconstraint_control",
            dsl_spec_path="experiments/spec_quality_compare/read_at_strong_spec.json",
            property_type="bounds",
            family="synthetic",
            notes="Known unsafe function under invalid idx; strong requires should warn.",
        ),
        Case(
            case_id="synthetic_read_at_weak",
            source="experiments/spec_quality_compare/read_at.c",
            driver="acsl_dsl_synthetic",
            function="read_at",
            case_kind="synthetic_weak_spec",
            expected_behavior="overconstraint_control",
            dsl_spec_path="experiments/spec_quality_compare/read_at_weak_spec.json",
            property_type="bounds",
            family="synthetic",
            notes="Weak postcondition but same bounds precondition; should still warn.",
        ),
    ]
    cases.extend(c for c in seeds if _resolve(c.source).is_file() and _resolve(c.dsl_spec_path).is_file())

    for name in (
        "archive_acl_text_len",
        "next_field",
        "archive_acl_clear",
        "archive_acl_to_text_l",
        "archive_acl_to_text_w",
        "append_entry",
    ):
        path = REPO_ROOT / "findings" / "v5" / f"{name}.harness.c"
        if path.is_file():
            cases.append(
                Case(
                    case_id=f"finding_{name}",
                    source=_repo_rel(path),
                    driver="findings_v5",
                    function="main",
                    case_kind="confirmed_bug_direct_harness",
                    expected_behavior="bug",
                    property_type="memory_safety",
                    mode="direct_harness",
                    family="findings_v5",
                    notes=(
                        "Direct CBMC finding harness control; not counted as "
                        "ACSL projection evidence."
                    ),
                )
            )

    if EXTERNAL_FINDINGS.is_dir():
        fp_docs = [
            EXTERNAL_FINDINGS / "findings/libarchive/false-positives/append_id_pointer_dereference.md",
            EXTERNAL_FINDINGS / "findings/libarchive/false-positives/next_field_w_trim_underflow.md",
            EXTERNAL_FINDINGS / "findings/libarchive/false-positives/append_id_w_pointer_arithmetic.md",
        ]
        for doc in fp_docs:
            if doc.is_file():
                cases.append(
                    Case(
                        case_id=f"metadata_{doc.stem}",
                        source=str(doc),
                        driver="embargoed_false_positive_docs",
                        function="",
                        case_kind="false_positive_metadata",
                        expected_behavior="false_positive_control",
                        property_type="triage_metadata",
                        mode="metadata_only",
                        family="embargoed_false_positive_docs",
                        notes="Documentation-only control; runner records it as non-runnable.",
                    )
                )

    return cases


def _autorocq_source_for_driver(driver: str) -> Path | None:
    if "__" not in driver:
        return None
    suite, benchmark = driver.split("__", 1)
    prepared = WORKSPACE_ROOT / "aprover" / "experiments" / "autorocq_compare" / "prepared" / suite / benchmark
    candidates = [
        prepared / "main.c",
        prepared / f"{benchmark}.c",
        prepared / "ctype.c",
        prepared / "strcmp.c",
        prepared / "strlen.c",
        prepared / "strchr.c",
        prepared / "memcmp.c",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    c_files = sorted(prepared.glob("*.c"))
    return c_files[0] if c_files else None


def _discover_autorocq_cases(limit: int = 40) -> list[Case]:
    roots = [
        WORKSPACE_ROOT / "aprover" / "experiments" / "autorocq_compare" / "runs_10case",
        WORKSPACE_ROOT / "aprover" / "experiments" / "autorocq_compare" / "runs",
    ]
    cases: list[Case] = []
    for root in roots:
        if not root.is_dir():
            continue
        for spec_path in sorted(root.glob("*/bmc_agent/artifacts/*/*/spec.json")):
            driver = spec_path.parents[1].name
            fn = _spec_function_name(spec_path)
            source = _autorocq_source_for_driver(driver)
            mode = "spec_replay" if source and source.is_file() else "missing_source"
            expected, detail = _expected_from_bug_report(spec_path.with_name("bug_report.json"))
            cases.append(
                Case(
                    case_id=_case_id_safe(f"autorocq_{driver}_{fn}"),
                    source=str(source or ""),
                    driver=driver,
                    function=fn,
                    case_kind="autorocq_artifact",
                    expected_behavior=expected,
                    dsl_spec_path=str(spec_path),
                    property_type=detail,
                    mode=mode,
                    family="autorocq",
                    source_resolver_status="resolved" if mode == "spec_replay" else "missing_source",
                    notes="AutoRocq comparison artifact paired with prepared C source.",
                )
            )
            if len(cases) >= limit:
                return cases
    return cases


def _vibeos_source_for_artifact(spec_path: Path) -> Path | None:
    parts = spec_path.parts
    try:
        idx = parts.index("vibeos_kernel")
    except ValueError:
        return None
    if idx + 1 >= len(parts):
        return None
    module = parts[idx + 1]
    candidates = [
        WORKSPACE_ROOT / "vibeos" / "kernel" / f"{module}.c",
        REPO_ROOT / "examples" / "vibeos" / f"vibeos_{module}.c",
        REPO_ROOT / "examples" / "vibeos" / f"{module}.c",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _discover_vibeos_cases(limit: int = 40) -> list[Case]:
    roots = [
        WORKSPACE_ROOT / "aprover" / "artifacts" / "vibeos_full_qemu_prioritized_dedup2_unique_sonnet46_20260531_001754",
        WORKSPACE_ROOT / "aprover" / "artifacts" / "vibeos_console_guard_smoke2_sonnet46_20260530_032220",
    ]
    cases: list[Case] = []
    for root in roots:
        if not root.is_dir():
            continue
        for spec_path in sorted(root.glob("vibeos_kernel/*/*/spec.json")):
            fn = _spec_function_name(spec_path)
            source = _vibeos_source_for_artifact(spec_path)
            mode = "spec_replay" if source and source.is_file() else "missing_source"
            expected, detail = _expected_from_bug_report(spec_path.with_name("bug_report.json"))
            cases.append(
                Case(
                    case_id=_case_id_safe(f"vibeos_{spec_path.parents[1].name}_{fn}"),
                    source=str(source or ""),
                    driver="vibeos_kernel",
                    function=fn,
                    case_kind="vibeos_artifact",
                    expected_behavior=expected,
                    dsl_spec_path=str(spec_path),
                    property_type=detail,
                    mode=mode,
                    family="vibeos",
                    source_resolver_status="resolved" if mode == "spec_replay" else "missing_source",
                    notes="VibeOS artifact paired with repository source when resolvable.",
                )
            )
            if len(cases) >= limit:
                return cases
    return cases


def _discover_projection_stress_cases(limit: int = 40) -> list[Case]:
    roots = [
        REPO_ROOT / "findings" / "aws_neuron_driver" / "llm_pipeline_demo",
        REPO_ROOT / "findings" / "llama_cpp_ggml" / "hybrid_demo",
    ]
    cases: list[Case] = []
    for root in roots:
        if not root.is_dir():
            continue
        for spec_path in sorted(root.glob("**/spec.json")):
            fn = _spec_function_name(spec_path)
            family = "aws_neuron" if "aws_neuron_driver" in str(spec_path) else "ggml"
            cases.append(
                Case(
                    case_id=_case_id_safe(f"projection_{family}_{fn}"),
                    source="",
                    driver=family,
                    function=fn,
                    case_kind="projection_stress",
                    expected_behavior="projection_only",
                    dsl_spec_path=str(spec_path),
                    mode="projection_only",
                    family=family,
                    source_resolver_status="projection_only_no_source",
                    notes="Existing DSL spec without source mapping; used for projection support/loss only.",
                )
            )
            if len(cases) >= limit:
                return cases
    return cases


def _discover_dsl_acsl_probe_cases(limit: int = 20) -> list[Case]:
    root = WORKSPACE_ROOT / "aprover" / "artifacts" / "dsl_acsl_probe" / "vibeos_current"
    if not root.is_dir():
        return []
    cases: list[Case] = []
    for source in sorted(root.glob("vibeos_kernel/*/*/*.c")):
        fn = source.stem
        report = source.with_name(f"{fn}.report.json")
        mode = "native_acsl_e2e" if report.is_file() else "missing_source"
        cases.append(
            Case(
                case_id=_case_id_safe(f"stageb_vibeos_probe_{fn}"),
                source=str(source),
                driver="vibeos_probe",
                function=fn,
                case_kind="stage_b_candidate",
                expected_behavior="unknown",
                mode=mode,
                family="stage_b_vibeos_probe",
                native_acsl_spec_path=str(report) if report.is_file() else "",
                source_resolver_status="resolved",
                notes="Stage-B candidate source from prior DSL/ACSL probe.",
            )
        )
        if len(cases) >= limit:
            return cases
    return cases


def discover_comprehensive_cases(*, limit: int | None = None) -> list[Case]:
    groups = [
        _default_cases(),
        _discover_autorocq_cases(),
        _discover_vibeos_cases(),
        _discover_projection_stress_cases(),
        _discover_dsl_acsl_probe_cases(),
    ]
    if limit is None:
        return _dedupe_cases([case for group in groups for case in group])
    out: list[Case] = []
    idx = 0
    while len(out) < limit and any(groups):
        group = groups[idx % len(groups)]
        if group:
            out.append(group.pop(0))
        groups = [g for g in groups if g]
        idx += 1
    return _dedupe_cases(out)


def _manifest_summary(cases: Sequence[Case]) -> str:
    by_mode = Counter(c.mode for c in cases)
    by_kind = Counter(c.case_kind for c in cases)
    lines = [
        "# ACSL vs DSL Outcome Manifest",
        "",
        f"Total cases: {len(cases)}",
        "",
        "## By Mode",
        "",
    ]
    lines.extend(f"- `{k}`: {v}" for k, v in sorted(by_mode.items()))
    lines.extend(["", "## By Case Kind", ""])
    lines.extend(f"- `{k}`: {v}" for k, v in sorted(by_kind.items()))
    lines.extend(["", "## Cases", ""])
    lines.extend(
        f"- `{c.case_id}`: `{c.mode}`, `{c.case_kind}`, expected `{c.expected_behavior}`"
        for c in cases
    )
    lines.append("")
    return "\n".join(lines)


def discover_cases(*, output: Path, limit: int | None = None, comprehensive: bool = False) -> dict[str, Any]:
    cases = discover_comprehensive_cases(limit=limit) if comprehensive else _default_cases()
    if limit is not None:
        cases = cases[:limit]

    payload = {
        "schema_version": 1,
        "manifest_kind": "comprehensive" if comprehensive else "pilot",
        "purpose": (
            "Compare final CBMC outcomes for original DSL specs versus "
            "DSL-derived ACSL projected back into the same harness semantics."
        ),
        "no_llm": True,
        "cases": [c.to_dict() for c in cases],
    }
    _write_json(output, payload)
    summary_path = output.with_name("manifest_summary.md")
    summary_path.write_text(_manifest_summary(cases), encoding="utf-8")
    return {
        "manifest": str(output),
        "summary": str(summary_path),
        "case_count": len(cases),
    }


def _contract_to_artifact(contract) -> dict[str, Any]:
    return {
        "function_name": contract.function_name,
        "raw_acsl": contract.text,
        "translated_clause_count": contract.translated_clause_count,
        "clauses": [
            {
                "kind": c.kind,
                "source": c.source,
                "expr": c.expr,
                "reason": c.reason,
                "translated": c.translated,
            }
            for c in contract.clauses
        ],
        "unsupported": [
            {
                "kind": c.kind,
                "source": c.source,
                "expr": c.expr,
                "reason": c.reason,
                "translated": c.translated,
            }
            for c in contract.unsupported
        ],
        "loop_invariants_unsupported": list(contract.loop_invariants_unsupported),
    }


def project_contract_to_spec(original_spec, contract_artifact: Mapping[str, Any]):
    """Project supported ACSL clauses back into the BMC-Agent Spec subset."""

    from bmc_agent.spec import Spec

    translated = [
        c
        for c in contract_artifact.get("clauses", [])
        if isinstance(c, Mapping) and c.get("translated") and c.get("source")
    ]
    pre = [str(c["source"]).strip() for c in translated if c.get("kind") == "requires"]
    post = [str(c["source"]).strip() for c in translated if c.get("kind") == "ensures"]

    return Spec(
        function_name=original_spec.function_name,
        precondition=" && ".join(pre) if pre else "true",
        postcondition=" && ".join(post) if post else "true",
        callee_specs=original_spec.callee_specs,
        loop_invariants=[],
        status=original_spec.status,
        spec_disagreement=original_spec.spec_disagreement,
        pre_validity="",
        pre_protocol="",
        evidence=original_spec.evidence,
    )


def _normalize_native_clause(clause: str, *, kind: str) -> tuple[str | None, str]:
    from bmc_agent.acsl_native import _strip_clause_prefix

    expr = _strip_clause_prefix(clause, kind).strip()
    if not expr:
        return None, "empty clause"
    expr = expr.replace("\\result", "result")
    expr = expr.replace("\\null", "NULL")
    expr = expr.replace("\\true", "true").replace("\\false", "false")
    if re.search(r"\b(forall|exists)\b|\\forall|\\exists|==>|<==>|=>", expr, re.IGNORECASE):
        return None, "unsupported quantifier/implication"
    if re.search(r"\\[A-Za-z_]\w*", expr):
        return None, "unsupported ACSL builtin"
    if _looks_like_natural_language(expr):
        return None, "not a pure C/ACSL formula"
    if not re.search(r"==|!=|<=|>=|<|>|\|\||&&|!|\btrue\b|\bfalse\b|\bNULL\b", expr):
        return None, "not a boolean formula"
    return expr, ""


def project_native_acsl_to_spec(native_spec) -> tuple[Any, dict[str, Any]]:
    """Project native ACSL clauses into the harness-oriented Spec subset."""

    from bmc_agent.spec import Spec, SpecStatus

    clauses: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    pre: list[str] = []
    post: list[str] = []

    for kind, values, sink in (
        ("requires", list(native_spec.requires), pre),
        ("ensures", list(native_spec.ensures), post),
    ):
        for source in values:
            expr, reason = _normalize_native_clause(source, kind=kind)
            item = {
                "kind": kind,
                "source": source,
                "expr": expr,
                "reason": reason,
                "translated": expr is not None,
            }
            clauses.append(item)
            if expr is None:
                unsupported.append(item)
            else:
                sink.append(expr)

    for target in native_spec.assigns:
        unsupported.append(
            {
                "kind": "assigns",
                "source": target,
                "expr": None,
                "reason": "assigns has no harness-oriented projection",
                "translated": False,
            }
        )
    for invariant in native_spec.loop_invariants:
        unsupported.append(
            {
                "kind": "loop_invariant",
                "source": invariant,
                "expr": None,
                "reason": "loop invariant has no current CBMC harness projection",
                "translated": False,
            }
        )
    if native_spec.raw_acsl.strip() and not clauses:
        unsupported.append(
            {
                "kind": "raw_acsl",
                "source": native_spec.raw_acsl.strip()[:500],
                "expr": None,
                "reason": "raw ACSL-only contract cannot be projected safely",
                "translated": False,
            }
        )

    projected = Spec(
        function_name=native_spec.function_name,
        precondition=" && ".join(pre) if pre else "true",
        postcondition=" && ".join(post) if post else "true",
        status=SpecStatus.GENERATED,
        evidence={},
    )
    artifact = {
        "function_name": native_spec.function_name,
        "native_acsl": native_spec.to_dict(),
        "clauses": clauses,
        "unsupported": unsupported,
        "unsupported_clause_count": len(unsupported),
        "projected": projected.to_dict(),
    }
    return projected, artifact


def analyze_dsl_projection_without_source(spec_path: Path) -> dict[str, Any]:
    """Classify support/loss for a DSL spec when no source signature exists."""

    spec = _load_one_spec(spec_path)
    unsupported: list[dict[str, Any]] = []
    supported: list[dict[str, Any]] = []
    parts = [
        ("requires", spec.precondition),
        ("ensures", spec.postcondition),
        *[("loop_invariant", item) for item in spec.loop_invariants],
    ]
    for kind, text in parts:
        if not text or text.strip().lower() in {"true", "1"}:
            continue
        reason = ""
        if kind == "loop_invariant":
            reason = "loop invariant has no current projection-only support"
        elif re.search(r"\b(forall|exists)\b|==>|<==>|=>|\bimplies\b", text, re.IGNORECASE):
            reason = "unsupported quantifier/implication"
        elif re.search(r"\b(no_overflow|locked|valid_user_pointer)\s*\(", text):
            reason = "unsupported DSL primitive"
        elif _looks_like_natural_language(text):
            reason = "not a pure C/ACSL formula"
        item = {
            "kind": kind,
            "source": text,
            "translated": not bool(reason),
            "reason": reason,
        }
        (unsupported if reason else supported).append(item)
    return {
        "function_name": spec.function_name,
        "original": spec.to_dict(),
        "supported": supported,
        "unsupported": unsupported,
        "unsupported_clause_count": len(unsupported),
        "status": "supported" if not unsupported else "unsupported_clause",
    }


def _load_native_specs_flexible(path: Path) -> dict[str, Any]:
    """Load native ACSL specs or prior probe report JSON as NativeAcslSpec."""

    from bmc_agent.acsl_native import NativeAcslSpec, load_native_acsl_specs

    load_error: Exception | None = None
    try:
        return load_native_acsl_specs(path)
    except Exception as exc:
        load_error = exc
        data = _load_json(path)
    if not isinstance(data, Mapping) or "function_name" not in data or "clauses" not in data:
        raise ValueError(f"unsupported native ACSL cache shape: {path}") from load_error
    requires = []
    ensures = []
    for clause in data.get("clauses", []):
        if not isinstance(clause, Mapping):
            continue
        kind = str(clause.get("kind") or "")
        expr = str(clause.get("acsl") or clause.get("source") or "").strip()
        if not expr:
            continue
        if kind == "requires":
            requires.append(expr)
        elif kind == "ensures":
            ensures.append(expr)
    spec = NativeAcslSpec(
        function_name=str(data["function_name"]),
        requires=requires,
        ensures=ensures,
        raw_acsl=str(data.get("acsl") or ""),
        generation_metadata={"source": "cached dsl_acsl_probe report", "path": str(path)},
    )
    return {spec.function_name: spec}


def _spec_projection_report(original_spec, projected_spec, contract_artifact: Mapping[str, Any]) -> dict[str, Any]:
    unsupported = list(contract_artifact.get("unsupported", []))
    loop_unsupported = list(contract_artifact.get("loop_invariants_unsupported", []))
    return {
        "function_name": original_spec.function_name,
        "original": original_spec.to_dict(),
        "projected": projected_spec.to_dict(),
        "precondition_changed": original_spec.precondition.strip() != projected_spec.precondition.strip(),
        "postcondition_changed": original_spec.postcondition.strip() != projected_spec.postcondition.strip(),
        "unsupported_clause_count": len(unsupported) + len(loop_unsupported),
        "unsupported": unsupported,
        "loop_invariants_unsupported": loop_unsupported,
    }


def _branch_from_verdict(branch: str, verdict, runtime_s: float, result_path: Path) -> BranchResult:
    label = "verified_clean"
    status = "success"
    error = verdict.error or ""
    if error:
        status = "timeout" if "timed out" in error.lower() else "error"
        label = "timeout" if status == "timeout" else "error"
    elif not verdict.verified:
        label = "bug_found" if verdict.counterexamples else "unknown"
    cex = verdict.counterexamples[0] if verdict.counterexamples else None
    return BranchResult(
        branch=branch,
        status=status,
        final_label=label,
        runtime_s=runtime_s,
        harness_path=str(verdict.harness_path or ""),
        result_path=str(result_path),
        error=error,
        failing_property=getattr(cex, "failing_property", "") if cex else "",
        failure_description=getattr(cex, "description", "") if cex else "",
        failure_location=dict(getattr(cex, "failure_location", {}) or {}) if cex else {},
        counterexample_count=len(verdict.counterexamples or []),
    )


def _direct_cbmc_branch(
    *,
    branch: str,
    source: Path,
    output_dir: Path,
    timeout: int,
    unwind: int,
    cbmc_path: str,
) -> BranchResult:
    from bmc_agent.cbmc import run_cbmc

    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = run_cbmc(
        source,
        unwind=unwind,
        timeout=timeout,
        cbmc_path=cbmc_path,
        pointer_check=True,
        bounds_check=True,
        div_by_zero_check=True,
        signed_overflow_check=True,
        unsigned_overflow_check=True,
        include_dirs=_include_dirs_for_source(source),
    )
    runtime_s = time.monotonic() - started
    result_path = output_dir / f"{branch}_result.json"
    payload = {
        "branch": branch,
        "source": str(source),
        "result": dataclasses.asdict(result),
        "runtime_s": runtime_s,
    }
    _write_json(result_path, payload)

    label = "verified_clean"
    status = "success"
    if result.error:
        status = "timeout" if "timed out" in result.error.lower() else "error"
        label = "timeout" if status == "timeout" else "error"
    elif not result.verified:
        label = "bug_found" if result.counterexamples else "unknown"
    cex = result.counterexamples[0] if result.counterexamples else None
    return BranchResult(
        branch=branch,
        status=status,
        final_label=label,
        runtime_s=runtime_s,
        harness_path=str(source),
        result_path=str(result_path),
        error=result.error or "",
        failing_property=getattr(cex, "failing_property", "") if cex else "",
        failure_description=getattr(cex, "description", "") if cex else "",
        failure_location=dict(getattr(cex, "failure_location", {}) or {}) if cex else {},
        counterexample_count=len(result.counterexamples or []),
    )


def _run_spec_branch(
    *,
    branch: str,
    case: Case,
    spec,
    output_dir: Path,
    timeout: int,
    unwind: int,
    cbmc_path: str,
) -> BranchResult:
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.config import Config
    from bmc_agent.source_parser import parse_source_file

    source = _resolve(case.source)
    parsed = parse_source_file(source)
    func = parsed.get_function_info(case.function)
    if func is None:
        result_path = output_dir / "branch_error.json"
        payload = {
            "branch": branch,
            "error": f"function not found: {case.function}",
            "available_functions": sorted(parsed.functions.keys()),
        }
        _write_json(result_path, payload)
        return BranchResult(
            branch=branch,
            status="error",
            final_label="error",
            runtime_s=0.0,
            result_path=str(result_path),
            error=payload["error"],
        )

    config = Config.from_env()
    config.artifact_dir = str(output_dir)
    config.cbmc_timeout = timeout
    config.cbmc_unwind = unwind
    config.cbmc_path = cbmc_path
    config.include_dirs = _include_dirs_for_source(source)
    config.enable_inlining_advisor = False
    config.enable_flag_selection = False
    config.enable_realism_check = False
    config.enable_dynamic_validation = False
    store = ArtifactStore(config.artifact_dir)
    engine = BMCEngine(config, store)
    all_funcs = {name: parsed.get_function_info(name) for name in parsed.functions}

    started = time.monotonic()
    verdict = engine.check_function(
        func,
        spec,
        parsed,
        driver_name=case.case_id,
        all_funcs=all_funcs,
    )
    runtime_s = time.monotonic() - started
    result_path = output_dir / case.case_id / case.function / "bug_report.json"
    return _branch_from_verdict(branch, verdict, runtime_s, result_path)


def _read_text_if_file(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    if p.is_file():
        return p.read_text(encoding="utf-8", errors="replace")
    return ""


def _write_harness_diff(dsl: BranchResult, acsl: BranchResult, output_path: Path) -> dict[str, Any]:
    dsl_text = _read_text_if_file(dsl.harness_path)
    acsl_text = _read_text_if_file(acsl.harness_path)
    if not dsl_text or not acsl_text:
        output_path.write_text("", encoding="utf-8")
        return {"status": "not_available", "path": str(output_path), "line_count": 0}
    diff = list(
        difflib.unified_diff(
            dsl_text.splitlines(),
            acsl_text.splitlines(),
            fromfile="dsl/harness.c",
            tofile="acsl/harness.c",
            lineterm="",
        )
    )
    output_path.write_text("\n".join(diff) + ("\n" if diff else ""), encoding="utf-8")
    return {
        "status": "identical" if not diff else "different",
        "path": str(output_path),
        "line_count": len(diff),
    }


def _counterexample_match(dsl: BranchResult, acsl: BranchResult) -> dict[str, Any]:
    if not dsl.failing_property and not acsl.failing_property:
        return {"status": "not_applicable"}
    same_property = dsl.failing_property == acsl.failing_property
    same_function = (
        dsl.failure_location.get("function")
        and dsl.failure_location.get("function") == acsl.failure_location.get("function")
    )
    line_delta = None
    try:
        line_delta = abs(
            int(dsl.failure_location.get("line", "0"))
            - int(acsl.failure_location.get("line", "0"))
        )
    except ValueError:
        line_delta = None
    return {
        "status": "match" if same_property or same_function else "different",
        "same_property": same_property,
        "same_function": bool(same_function),
        "line_delta": line_delta,
        "dsl_property": dsl.failing_property,
        "acsl_property": acsl.failing_property,
        "dsl_location": dsl.failure_location,
        "acsl_location": acsl.failure_location,
    }


def _overconstraint_report(case: Case, spec, projection_report: Mapping[str, Any] | None = None) -> dict[str, Any]:
    pre = (spec.precondition or "").strip()
    warnings: list[str] = []
    if pre.lower() in {"false", "0", "\\false"}:
        warnings.append("requires is unsatisfiable")
    nontrivial_pre = pre and pre.lower() not in {"true", "1", "\\true"}
    if case.expected_behavior in {"bug", "confirmed_bug", "overconstraint_control"} and nontrivial_pre:
        warnings.append("bug/control case has nontrivial requires; check witness preservation")
    if case.case_kind == "synthetic_overconstraint" and "idx < len" in pre:
        warnings.append("synthetic read_at witness family idx >= len is excluded by idx < len")
    if projection_report and projection_report.get("unsupported_clause_count"):
        warnings.append("projection has unsupported clauses; final outcome may not isolate representation only")
    return {
        "status": "warning" if warnings else "ok",
        "warnings": warnings,
        "precondition": pre,
    }


def _compare_case(
    case: Case,
    dsl: BranchResult,
    acsl: BranchResult,
    *,
    projection_report: Mapping[str, Any] | None,
    harness_diff: Mapping[str, Any],
    overconstraint: Mapping[str, Any],
) -> dict[str, Any]:
    unsupported = bool(projection_report and projection_report.get("unsupported_clause_count"))
    if case.mode == "missing_source":
        outcome = "missing_source"
    elif case.mode == "metadata_only":
        outcome = "inconclusive"
    elif case.mode == "direct_harness":
        outcome = "direct_harness_control" if dsl.final_label == acsl.final_label else "inconclusive"
    elif dsl.status == "error" or acsl.status == "error":
        outcome = "unsupported_clause" if unsupported else "inconclusive"
    elif unsupported:
        outcome = "unsupported_clause"
    elif (
        case.expected_behavior in {"bug", "confirmed_bug"}
        and dsl.final_label == "bug_found"
        and acsl.final_label != "bug_found"
    ):
        outcome = "acsl_missed_confirmed_bug"
    elif dsl.final_label == "timeout" and acsl.final_label != "timeout":
        outcome = "dsl_only_timeout"
    elif acsl.final_label == "timeout" and dsl.final_label != "timeout":
        outcome = "acsl_only_timeout"
    elif overconstraint.get("status") == "warning" and acsl.final_label == "verified_clean":
        outcome = "acsl_overconstrained"
    elif dsl.final_label == acsl.final_label:
        outcome = "same_decision"
    elif harness_diff.get("status") == "different":
        outcome = "harness_diff"
    else:
        outcome = "inconclusive"

    return {
        "outcome_class": outcome,
        "final_label_agreement": dsl.final_label == acsl.final_label,
        "dsl_final_label": dsl.final_label,
        "acsl_final_label": acsl.final_label,
        "runtime_ratio": (
            acsl.runtime_s / dsl.runtime_s
            if dsl.runtime_s and acsl.runtime_s
            else None
        ),
    }


def run_case(case: Case, *, output_root: Path, timeout: int, unwind: int, cbmc_path: str) -> dict[str, Any]:
    case_dir = output_root / "cases" / case.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    _write_json(case_dir / "case.json", case.to_dict())

    if case.mode in {"metadata_only", "missing_source"}:
        label = "missing_source" if case.mode == "missing_source" else "metadata_only"
        branch = BranchResult(
            branch=case.mode,
            status="skipped",
            final_label=label,
            runtime_s=0.0,
            result_path=str(case_dir / f"{case.mode}.json"),
            error=f"{case.mode} case is not runnable",
        )
        _write_json(case_dir / f"{case.mode}.json", {"case": case.to_dict(), "status": case.mode})
        comparison = _compare_case(
            case,
            branch,
            branch,
            projection_report=None,
            harness_diff={"status": "not_applicable"},
            overconstraint={"status": "not_applicable", "warnings": []},
        )
        return {
            "case": case.to_dict(),
            "dsl": branch.to_dict(),
            "acsl": branch.to_dict(),
            "projection": {},
            "harness_diff": {"status": "not_applicable"},
            "counterexample_match": {"status": "not_applicable"},
            "overconstraint_report": {"status": "not_applicable", "warnings": []},
            "comparison": comparison,
        }

    if case.mode == "projection_only":
        projection = analyze_dsl_projection_without_source(_resolve(case.dsl_spec_path))
        _write_json(case_dir / "spec_projection_diff.json", projection)
        branch = BranchResult(
            branch="projection",
            status=projection["status"],
            final_label="projection_only",
            runtime_s=0.0,
            result_path=str(case_dir / "spec_projection_diff.json"),
            error="projection-only case has no source/harness replay",
        )
        comparison = {
            "outcome_class": "projection_only" if projection["unsupported_clause_count"] == 0 else "unsupported_clause",
            "final_label_agreement": None,
            "dsl_final_label": "projection_only",
            "acsl_final_label": "projection_only",
            "runtime_ratio": None,
        }
        result = {
            "case": case.to_dict(),
            "dsl": branch.to_dict(),
            "acsl": branch.to_dict(),
            "projection": projection,
            "harness_diff": {"status": "not_applicable"},
            "counterexample_match": {"status": "not_applicable"},
            "overconstraint_report": {"status": "not_applicable", "warnings": []},
            "comparison": comparison,
        }
        _write_json(case_dir / "case_result.json", result)
        return result

    if case.mode == "direct_harness":
        source = _resolve(case.source)
        dsl = _direct_cbmc_branch(
            branch="dsl",
            source=source,
            output_dir=case_dir / "dsl",
            timeout=timeout,
            unwind=unwind,
            cbmc_path=cbmc_path,
        )
        acsl = _direct_cbmc_branch(
            branch="acsl",
            source=source,
            output_dir=case_dir / "acsl",
            timeout=timeout,
            unwind=unwind,
            cbmc_path=cbmc_path,
        )
        overconstraint = {"status": "not_applicable", "warnings": []}
        comparison = _compare_case(
            case,
            dsl,
            acsl,
            projection_report=None,
            harness_diff={"status": "not_applicable"},
            overconstraint=overconstraint,
        )
        result = {
            "case": case.to_dict(),
            "dsl": dsl.to_dict(),
            "acsl": acsl.to_dict(),
            "projection": {"status": "not_applicable", "reason": "direct_harness_control"},
            "harness_diff": {"status": "not_applicable"},
            "counterexample_match": _counterexample_match(dsl, acsl),
            "overconstraint_report": overconstraint,
            "comparison": comparison,
        }
        _write_json(case_dir / "case_result.json", result)
        return result

    from bmc_agent.acsl import build_acsl_source, translate_spec_to_acsl
    from bmc_agent.source_parser import parse_source_file

    source = _resolve(case.source)
    original_spec = _load_one_spec(_resolve(case.dsl_spec_path))
    if case.function and original_spec.function_name != case.function:
        raise ValueError(
            f"case {case.case_id}: spec function {original_spec.function_name!r} "
            f"does not match case function {case.function!r}"
        )

    parsed = parse_source_file(source)
    sig = parsed.functions.get(original_spec.function_name)
    if sig is None:
        raise ValueError(f"case {case.case_id}: function not found: {original_spec.function_name}")

    contract = translate_spec_to_acsl(original_spec, sig)
    contract_artifact = _contract_to_artifact(contract)
    _write_json(case_dir / "acsl_contract.json", contract_artifact)
    projected_spec = project_contract_to_spec(original_spec, contract_artifact)
    _write_json(case_dir / "dsl_spec.json", original_spec.to_dict())
    _write_json(case_dir / "projected_spec.json", projected_spec.to_dict())

    projection_report = _spec_projection_report(original_spec, projected_spec, contract_artifact)
    _write_json(case_dir / "spec_projection_diff.json", projection_report)

    source_text = source.read_text(encoding="utf-8", errors="replace")
    annotated = build_acsl_source(source_text, parsed, {original_spec.function_name: original_spec})
    (case_dir / f"{source.stem}.acsl.c").write_text(annotated.source_text, encoding="utf-8")

    dsl = _run_spec_branch(
        branch="dsl",
        case=case,
        spec=original_spec,
        output_dir=case_dir / "dsl",
        timeout=timeout,
        unwind=unwind,
        cbmc_path=cbmc_path,
    )
    acsl = _run_spec_branch(
        branch="acsl",
        case=case,
        spec=projected_spec,
        output_dir=case_dir / "acsl",
        timeout=timeout,
        unwind=unwind,
        cbmc_path=cbmc_path,
    )
    harness_diff = _write_harness_diff(dsl, acsl, case_dir / "harness_diff.patch")
    cex_match = _counterexample_match(dsl, acsl)
    overconstraint = _overconstraint_report(case, projected_spec, projection_report)
    _write_json(case_dir / "counterexample_match.json", cex_match)
    _write_json(case_dir / "overconstraint_report.json", overconstraint)

    comparison = _compare_case(
        case,
        dsl,
        acsl,
        projection_report=projection_report,
        harness_diff=harness_diff,
        overconstraint=overconstraint,
    )
    result = {
        "case": case.to_dict(),
        "dsl": dsl.to_dict(),
        "acsl": acsl.to_dict(),
        "projection": projection_report,
        "harness_diff": harness_diff,
        "counterexample_match": cex_match,
        "overconstraint_report": overconstraint,
        "comparison": comparison,
    }
    _write_json(case_dir / "case_result.json", result)
    return result


def run_native_e2e_case(
    case: Case,
    *,
    output_root: Path,
    timeout: int,
    unwind: int,
    cbmc_path: str,
    allow_llm: bool,
) -> dict[str, Any]:
    if not allow_llm:
        raise RuntimeError("run-stage-b requires --allow-llm")

    from bmc_agent.acsl_native import generate_native_acsl_specs, write_native_acsl_specs
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.source_parser import parse_source_file
    from bmc_agent.spec_generator_v2 import SpecGeneratorV2

    case = dataclasses.replace(case, mode="native_acsl_e2e")
    case_dir = output_root / "cases" / case.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    _write_json(case_dir / "case.json", case.to_dict())

    source = _resolve(case.source)
    if not source.is_file():
        raise FileNotFoundError(f"source not found for stage-b case: {source}")
    raw_source = source.read_text(encoding="utf-8", errors="replace")
    parsed = parse_source_file(source, source_text=raw_source)
    if case.function not in parsed.functions:
        raise ValueError(f"function not found for stage-b case: {case.function}")

    config = Config.from_env()
    config.artifact_dir = str(case_dir / "generation")
    config.cbmc_timeout = timeout
    config.cbmc_unwind = unwind
    config.cbmc_path = cbmc_path
    store = ArtifactStore(config.artifact_dir)
    llm = LLMClient(config)

    dsl_gen = SpecGeneratorV2(config, llm, store)
    dsl_specs = dsl_gen.generate_specs(str(source), case.case_id, source_text=raw_source)
    if case.function not in dsl_specs:
        raise RuntimeError(f"DSL generator did not produce spec for {case.function}")
    dsl_spec = dsl_specs[case.function]
    _write_json(case_dir / "dsl_generated_spec.json", dsl_spec.to_dict())

    if case.native_acsl_spec_path and Path(case.native_acsl_spec_path).is_file():
        native_specs = _load_native_specs_flexible(Path(case.native_acsl_spec_path))
    else:
        native_specs = generate_native_acsl_specs(
            source_path=source,
            source_text=raw_source,
            parsed=parsed,
            function_names=[case.function],
            llm=llm,
            model=config.llm_model,
            max_tokens=4096,
            temperature=0.0,
        )
    if case.function not in native_specs:
        raise RuntimeError(f"native ACSL generator did not produce spec for {case.function}")
    write_native_acsl_specs(case_dir / "native_acsl_specs.json", native_specs)
    projected_spec, native_projection = project_native_acsl_to_spec(native_specs[case.function])
    _write_json(case_dir / "native_projection.json", native_projection)

    dsl = _run_spec_branch(
        branch="dsl",
        case=case,
        spec=dsl_spec,
        output_dir=case_dir / "dsl",
        timeout=timeout,
        unwind=unwind,
        cbmc_path=cbmc_path,
    )
    acsl = _run_spec_branch(
        branch="acsl",
        case=case,
        spec=projected_spec,
        output_dir=case_dir / "acsl",
        timeout=timeout,
        unwind=unwind,
        cbmc_path=cbmc_path,
    )
    harness_diff = _write_harness_diff(dsl, acsl, case_dir / "harness_diff.patch")
    cex_match = _counterexample_match(dsl, acsl)
    overconstraint = _overconstraint_report(case, projected_spec, native_projection)
    _write_json(case_dir / "counterexample_match.json", cex_match)
    _write_json(case_dir / "overconstraint_report.json", overconstraint)
    comparison = _compare_case(
        case,
        dsl,
        acsl,
        projection_report=native_projection,
        harness_diff=harness_diff,
        overconstraint=overconstraint,
    )
    result = {
        "case": case.to_dict(),
        "dsl": dsl.to_dict(),
        "acsl": acsl.to_dict(),
        "projection": native_projection,
        "harness_diff": harness_diff,
        "counterexample_match": cex_match,
        "overconstraint_report": overconstraint,
        "comparison": comparison,
        "llm": {
            "used": True,
            "provider": config.llm_provider,
            "model": config.llm_model,
            "secret_values_recorded": False,
        },
    }
    _write_json(case_dir / "case_result.json", result)
    return result


def _load_manifest(path: Path) -> list[Case]:
    data = _load_json(path)
    raw_cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(raw_cases, list):
        raise ValueError(f"manifest must contain a cases list: {path}")
    return [Case.from_dict(item) for item in raw_cases]


def _summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(str(r.get("comparison", {}).get("outcome_class", "unknown")) for r in results)
    modes = Counter(str(r.get("case", {}).get("mode", "unknown")) for r in results)
    families = Counter(str(r.get("case", {}).get("family", "") or "unknown") for r in results)
    branch_statuses = Counter(
        f"{r.get('dsl', {}).get('status', 'unknown')}/{r.get('acsl', {}).get('status', 'unknown')}"
        for r in results
    )
    spec_replay_bug_cases = [
        r for r in results
        if r.get("case", {}).get("expected_behavior") in {"bug", "confirmed_bug"}
        and r.get("case", {}).get("mode") == "spec_replay"
    ]
    spec_replay_dsl_bug_cases = [
        r for r in spec_replay_bug_cases
        if r.get("dsl", {}).get("final_label") == "bug_found"
    ]
    spec_replay_bug_preserved = [
        r for r in spec_replay_dsl_bug_cases
        if r.get("acsl", {}).get("final_label") == "bug_found"
    ]
    direct_bug_cases = [
        r for r in results
        if r.get("case", {}).get("expected_behavior") in {"bug", "confirmed_bug"}
        and r.get("case", {}).get("mode") == "direct_harness"
    ]
    direct_bug_replayed = [
        r for r in direct_bug_cases
        if r.get("dsl", {}).get("final_label") == "bug_found"
        and r.get("acsl", {}).get("final_label") == "bug_found"
    ]
    spec_replay = [r for r in results if r.get("case", {}).get("mode") == "spec_replay"]
    spec_replay_runnable = [
        r for r in spec_replay
        if r.get("dsl", {}).get("status") not in {"skipped", "error"}
        and r.get("acsl", {}).get("status") not in {"skipped", "error"}
    ]
    native_e2e = [r for r in results if r.get("case", {}).get("mode") == "native_acsl_e2e"]
    native_e2e_runnable = [
        r for r in native_e2e
        if r.get("dsl", {}).get("status") not in {"skipped", "error"}
        and r.get("acsl", {}).get("status") not in {"skipped", "error"}
    ]
    projection_only = [r for r in results if r.get("case", {}).get("mode") == "projection_only"]
    projection_supported = [
        r for r in projection_only
        if int(r.get("projection", {}).get("unsupported_clause_count") or 0) == 0
    ]
    missing_source = [r for r in results if r.get("case", {}).get("mode") == "missing_source"]
    unsupported = [
        r for r in results
        if int(r.get("projection", {}).get("unsupported_clause_count") or 0) > 0
    ]
    runnable = [
        r for r in results
        if r.get("case", {}).get("mode") not in {"metadata_only", "missing_source", "projection_only"}
    ]
    interpretable = [
        r for r in runnable
        if r.get("dsl", {}).get("status") not in {"skipped", "error"}
        and r.get("acsl", {}).get("status") not in {"skipped", "error"}
    ]
    agreements = [
        r for r in interpretable
        if r.get("comparison", {}).get("final_label_agreement")
    ]
    raw_agreements = [
        r for r in runnable
        if r.get("comparison", {}).get("final_label_agreement")
    ]
    return {
        "case_count": len(results),
        "runnable_case_count": len(runnable),
        "interpretable_case_count": len(interpretable),
        "outcomes": dict(sorted(outcomes.items())),
        "modes": dict(sorted(modes.items())),
        "families": dict(sorted(families.items())),
        "branch_statuses": dict(sorted(branch_statuses.items())),
        "spec_replay_runnable": {
            "count": len(spec_replay_runnable),
            "denominator": len(spec_replay),
        },
        "final_label_agreement": {
            "count": len(agreements),
            "denominator": len(interpretable),
            "rate": (len(agreements) / len(interpretable)) if interpretable else None,
        },
        "raw_final_label_agreement_including_errors": {
            "count": len(raw_agreements),
            "denominator": len(runnable),
            "rate": (len(raw_agreements) / len(runnable)) if runnable else None,
        },
        "spec_replay_confirmed_bug_preservation": {
            "count": len(spec_replay_bug_preserved),
            "denominator": len(spec_replay_dsl_bug_cases),
            "rate": (
                len(spec_replay_bug_preserved) / len(spec_replay_dsl_bug_cases)
                if spec_replay_dsl_bug_cases else None
            ),
            "expected_bug_case_count": len(spec_replay_bug_cases),
        },
        "direct_harness_bug_replay": {
            "count": len(direct_bug_replayed),
            "denominator": len(direct_bug_cases),
            "rate": (len(direct_bug_replayed) / len(direct_bug_cases)) if direct_bug_cases else None,
        },
        "native_acsl_e2e_runnable": {
            "count": len(native_e2e_runnable),
            "denominator": len(native_e2e),
        },
        "projection_only_support_rate": {
            "count": len(projection_supported),
            "denominator": len(projection_only),
            "rate": (len(projection_supported) / len(projection_only)) if projection_only else None,
        },
        "missing_source": len(missing_source),
        "unsupported_clause": len(unsupported),
    }


def _write_decision_table(results: Sequence[Mapping[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "case_id",
        "family",
        "mode",
        "case_kind",
        "expected_behavior",
        "source_resolver_status",
        "dsl_final_label",
        "acsl_final_label",
        "outcome_class",
        "runtime_ratio",
        "unsupported_clause_count",
        "overconstraint_status",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            case = r.get("case", {})
            projection = r.get("projection", {})
            over = r.get("overconstraint_report", {})
            comp = r.get("comparison", {})
            writer.writerow(
                {
                    "case_id": case.get("case_id", ""),
                    "family": case.get("family", ""),
                    "mode": case.get("mode", ""),
                    "case_kind": case.get("case_kind", ""),
                    "expected_behavior": case.get("expected_behavior", ""),
                    "source_resolver_status": case.get("source_resolver_status", ""),
                    "dsl_final_label": r.get("dsl", {}).get("final_label", ""),
                    "acsl_final_label": r.get("acsl", {}).get("final_label", ""),
                    "outcome_class": comp.get("outcome_class", ""),
                    "runtime_ratio": comp.get("runtime_ratio", ""),
                    "unsupported_clause_count": projection.get("unsupported_clause_count", ""),
                    "overconstraint_status": over.get("status", ""),
                }
            )


def _write_markdown_report(summary: Mapping[str, Any], results: Sequence[Mapping[str, Any]], path: Path) -> None:
    lines = [
        "# ACSL vs DSL Final-Outcome Equivalence Report",
        "",
        "This report compares the original DSL branch with the DSL-derived ACSL replay branch.",
        "The production `bmc-agent verify` pipeline is not modified by this experiment.",
        "",
        "## Summary",
        "",
        f"- Cases: {summary.get('case_count')} total, {summary.get('runnable_case_count')} runnable, "
        f"{summary.get('interpretable_case_count')} interpretable",
        f"- Outcome classes: `{summary.get('outcomes')}`",
        f"- Modes: `{summary.get('modes')}`",
        f"- Families: `{summary.get('families')}`",
        f"- Branch statuses: `{summary.get('branch_statuses')}`",
        f"- Spec replay runnable: `{summary.get('spec_replay_runnable')}`",
        f"- Final-label agreement: `{summary.get('final_label_agreement')}`",
        f"- Raw agreement including errors: `{summary.get('raw_final_label_agreement_including_errors')}`",
        f"- Spec-replay confirmed-bug preservation: `{summary.get('spec_replay_confirmed_bug_preservation')}`",
        f"- Native ACSL E2E runnable: `{summary.get('native_acsl_e2e_runnable')}`",
        f"- Projection-only support rate: `{summary.get('projection_only_support_rate')}`",
        f"- Missing source: `{summary.get('missing_source')}`",
        f"- Unsupported clause cases: `{summary.get('unsupported_clause')}`",
        f"- Direct-harness bug replay controls: `{summary.get('direct_harness_bug_replay')}`",
        "",
        "Direct harness controls are known-finding smoke checks only; they do not",
        "prove ACSL projection equivalence because no DSL `spec.json` is replayed.",
        "",
        "## Decision Table",
        "",
        "| Case | Mode | Expected | DSL | ACSL | Outcome |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        case = r.get("case", {})
        comp = r.get("comparison", {})
        lines.append(
            "| `{case}` | `{mode}` | `{expected}` | `{dsl}` | `{acsl}` | `{outcome}` |".format(
                case=case.get("case_id", ""),
                mode=case.get("mode", ""),
                expected=case.get("expected_behavior", ""),
                dsl=r.get("dsl", {}).get("final_label", ""),
                acsl=r.get("acsl", {}).get("final_label", ""),
                outcome=comp.get("outcome_class", ""),
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run_manifest(
    *,
    manifest: Path,
    output: Path,
    timeout: int,
    unwind: int,
    cbmc_path: str,
    limit: int | None = None,
    case_ids: set[str] | None = None,
    stage: str = "stage_a",
    allow_llm: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    if stage == "stage_b" and not allow_llm:
        raise RuntimeError("run-stage-b requires --allow-llm")
    cases = _load_manifest(manifest)
    if case_ids:
        cases = [c for c in cases if c.case_id in case_ids]
    if stage == "stage_a":
        cases = [c for c in cases if c.mode != "native_acsl_e2e"]
    elif stage == "stage_b":
        cases = [
            c for c in cases
            if c.mode in {"native_acsl_e2e", "spec_replay"}
            and c.source
            and c.function
        ]
        cases = [dataclasses.replace(c, mode="native_acsl_e2e") for c in cases]
    if limit is not None:
        cases = cases[:limit]
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest, output / "manifest.json")

    def execute_case(case: Case) -> dict[str, Any]:
        try:
            return (
                run_native_e2e_case(
                    case,
                    output_root=output,
                    timeout=timeout,
                    unwind=unwind,
                    cbmc_path=cbmc_path,
                    allow_llm=allow_llm,
                )
                if stage == "stage_b"
                else run_case(
                    case,
                    output_root=output,
                    timeout=timeout,
                    unwind=unwind,
                    cbmc_path=cbmc_path,
                )
            )
        except Exception as exc:
            case_dir = output / "cases" / case.case_id
            case_dir.mkdir(parents=True, exist_ok=True)
            result = _error_result(case, exc)
            _write_json(case_dir / "case_result.json", result)
            return result

    results_by_index: dict[int, dict[str, Any]] = {}
    family_repeats: dict[str, tuple[str, int]] = {}
    stopped_families: set[str] = set()

    def current_results() -> list[dict[str, Any]]:
        return [results_by_index[i] for i in sorted(results_by_index)]

    def record_result(index: int, result: dict[str, Any]) -> None:
        results_by_index[index] = result
        _write_run_outputs(
            output=output,
            stage=stage,
            manifest=manifest,
            timeout=timeout,
            unwind=unwind,
            cbmc_path=cbmc_path,
            workers=workers,
            allow_llm=allow_llm,
            stopped_families=stopped_families,
            results=current_results(),
        )

    def update_early_stop(case: Case, result: Mapping[str, Any]) -> None:
        if stage != "stage_a":
            return
        family = case.family or case.case_kind or "unknown"
        outcome = str(result.get("comparison", {}).get("outcome_class", ""))
        if outcome in {"missing_source", "unsupported_clause"}:
            prev_outcome, count = family_repeats.get(family, ("", 0))
            count = count + 1 if prev_outcome == outcome else 1
            family_repeats[family] = (outcome, count)
            if count >= 10:
                stopped_families.add(family)
        else:
            family_repeats[family] = (outcome, 0)

    worker_count = max(1, int(workers or 1))
    if worker_count == 1:
        for idx, case in enumerate(cases):
            family = case.family or case.case_kind or "unknown"
            if stage == "stage_a" and family in stopped_families:
                result = _early_stopped_result(case)
            else:
                result = execute_case(case)
            record_result(idx, result)
            update_early_stop(case, result)
    else:
        pending: list[tuple[int, Case]] = list(enumerate(cases))
        active = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            while pending or active:
                while pending and len(active) < worker_count:
                    idx, case = pending.pop(0)
                    family = case.family or case.case_kind or "unknown"
                    if stage == "stage_a" and family in stopped_families:
                        result = _early_stopped_result(case)
                        record_result(idx, result)
                        continue
                    fut = executor.submit(execute_case, case)
                    active[fut] = (idx, case)
                if not active:
                    continue
                done, _ = wait(active.keys(), return_when=FIRST_COMPLETED)
                for fut in done:
                    idx, case = active.pop(fut)
                    result = fut.result()
                    record_result(idx, result)
                    update_early_stop(case, result)

    return _write_run_outputs(
        output=output,
        stage=stage,
        manifest=manifest,
        timeout=timeout,
        unwind=unwind,
        cbmc_path=cbmc_path,
        workers=workers,
        allow_llm=allow_llm,
        stopped_families=stopped_families,
        results=current_results(),
    )


def render_report(*, input_report: Path, output: Path | None = None) -> dict[str, Any]:
    report = _load_json(input_report)
    results = report.get("results", [])
    summary = _summary(results)
    report["summary"] = summary
    out_dir = output or input_report.parent
    _write_json(out_dir / "report.json", report)
    _write_decision_table(results, out_dir / "decision_table.csv")
    _write_markdown_report(summary, results, out_dir / "summary.md")
    return report


def _early_stopped_result(case: Case) -> dict[str, Any]:
    return {
        "case": case.to_dict(),
        "dsl": BranchResult("dsl", "skipped", "early_stopped", 0.0).to_dict(),
        "acsl": BranchResult("acsl", "skipped", "early_stopped", 0.0).to_dict(),
        "projection": {},
        "harness_diff": {"status": "not_available"},
        "counterexample_match": {"status": "not_available"},
        "overconstraint_report": {"status": "not_available", "warnings": []},
        "comparison": {
            "outcome_class": "inconclusive",
            "final_label_agreement": None,
            "dsl_final_label": "early_stopped",
            "acsl_final_label": "early_stopped",
            "runtime_ratio": None,
        },
    }


def _error_result(case: Case, exc: Exception) -> dict[str, Any]:
    case_payload = case.to_dict()
    return {
        "case": case_payload,
        "dsl": BranchResult("dsl", "error", "error", 0.0, error=str(exc)).to_dict(),
        "acsl": BranchResult("acsl", "error", "error", 0.0, error=str(exc)).to_dict(),
        "projection": {},
        "harness_diff": {"status": "not_available"},
        "counterexample_match": {"status": "not_available"},
        "overconstraint_report": {"status": "not_available", "warnings": []},
        "comparison": {
            "outcome_class": "inconclusive",
            "final_label_agreement": True,
            "dsl_final_label": "error",
            "acsl_final_label": "error",
            "runtime_ratio": None,
        },
    }


def _write_run_outputs(
    *,
    output: Path,
    stage: str,
    manifest: Path,
    timeout: int,
    unwind: int,
    cbmc_path: str,
    workers: int,
    allow_llm: bool,
    stopped_families: set[str],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    summary = _summary(results)
    report = {
        "schema_version": 1,
        "stage": stage,
        "manifest": str(manifest),
        "output": str(output),
        "timeout": timeout,
        "unwind": unwind,
        "cbmc_path": cbmc_path,
        "workers_requested": workers,
        "allow_llm": bool(allow_llm),
        "early_stopped_families": sorted(stopped_families),
        "summary": summary,
        "results": list(results),
    }
    _write_json(output / "report.json", report)
    _write_decision_table(results, output / "decision_table.csv")
    _write_markdown_report(summary, results, output / "summary.md")
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_discover = sub.add_parser("discover", help="Write a local pilot manifest")
    p_discover.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT / "pilot_manifest.json",
        help="Manifest JSON path",
    )
    p_discover.add_argument("--limit", type=int, default=None)
    p_discover.add_argument(
        "--comprehensive",
        action="store_true",
        help="Scan AutoRocq, VibeOS, projection-stress, and Stage-B candidate artifacts",
    )

    p_run = sub.add_parser("run", help="Run paired DSL/ACSL replay")
    p_run.add_argument("--manifest", type=Path, required=True)
    p_run.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "pilot_run")
    p_run.add_argument("--timeout", type=int, default=30)
    p_run.add_argument("--unwind", type=int, default=4)
    p_run.add_argument("--cbmc", default=os.environ.get("BMC_AGENT_CBMC_PATH", DEFAULT_CBMC))
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--case-id", action="append", default=[])
    p_run.add_argument("--workers", type=int, default=1)

    p_stage_a = sub.add_parser("run-stage-a", help="Run Stage A no-LLM representation replay")
    p_stage_a.add_argument("--manifest", type=Path, required=True)
    p_stage_a.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "stage_a")
    p_stage_a.add_argument("--timeout", type=int, default=60)
    p_stage_a.add_argument("--unwind", type=int, default=4)
    p_stage_a.add_argument("--cbmc", default=os.environ.get("BMC_AGENT_CBMC_PATH", DEFAULT_CBMC))
    p_stage_a.add_argument("--limit", type=int, default=None)
    p_stage_a.add_argument("--case-id", action="append", default=[])
    p_stage_a.add_argument("--workers", type=int, default=1)

    p_stage_b = sub.add_parser("run-stage-b", help="Run Stage B native ACSL end-to-end comparison")
    p_stage_b.add_argument("--manifest", type=Path, required=True)
    p_stage_b.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "stage_b_e2e")
    p_stage_b.add_argument("--timeout", type=int, default=120)
    p_stage_b.add_argument("--unwind", type=int, default=4)
    p_stage_b.add_argument("--cbmc", default=os.environ.get("BMC_AGENT_CBMC_PATH", DEFAULT_CBMC))
    p_stage_b.add_argument("--limit", type=int, default=None)
    p_stage_b.add_argument("--case-id", action="append", default=[])
    p_stage_b.add_argument("--workers", type=int, default=1)
    p_stage_b.add_argument("--allow-llm", action="store_true", help="Required to spend LLM/API calls")

    p_report = sub.add_parser("report", help="Regenerate summary artifacts from report.json")
    p_report.add_argument("--input", type=Path, required=True)
    p_report.add_argument("--output", type=Path, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "discover":
        out = discover_cases(output=args.output, limit=args.limit, comprehensive=args.comprehensive)
        print(f"Manifest: {out['manifest']}")
        print(f"Summary:  {out['summary']}")
        print(f"Cases:    {out['case_count']}")
        return 0
    if args.cmd in {"run", "run-stage-a", "run-stage-b"}:
        stage = "stage_b" if args.cmd == "run-stage-b" else "stage_a"
        report = run_manifest(
            manifest=args.manifest,
            output=args.output,
            timeout=args.timeout,
            unwind=args.unwind,
            cbmc_path=args.cbmc,
            limit=args.limit,
            case_ids=set(args.case_id) if args.case_id else None,
            stage=stage,
            allow_llm=bool(getattr(args, "allow_llm", False)),
            workers=args.workers,
        )
        print(f"Report:   {args.output / 'report.json'}")
        print(f"Summary:  {args.output / 'summary.md'}")
        print(f"CSV:      {args.output / 'decision_table.csv'}")
        print(f"Outcomes: {report['summary']['outcomes']}")
        return 0
    if args.cmd == "report":
        report = render_report(input_report=args.input, output=args.output)
        out_dir = args.output or args.input.parent
        print(f"Report:   {out_dir / 'report.json'}")
        print(f"Summary:  {out_dir / 'summary.md'}")
        print(f"CSV:      {out_dir / 'decision_table.csv'}")
        print(f"Outcomes: {report['summary']['outcomes']}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
