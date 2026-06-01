"""Native ACSL generation and quality helpers.

This module is separate from ``bmc_agent.acsl`` on purpose.  The older
``acsl-pilot`` path translates BMC-Agent DSL specs into ACSL.  The helpers here
represent ACSL as the primary generated artifact.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from bmc_agent.acsl import recover_plain_asserts_to_acsl, run_frama_c_wp
from bmc_agent.parser import ParsedCFile


@dataclass
class NativeAcslSpec:
    """Native ACSL clauses for one C function."""

    function_name: str
    requires: list[str] = field(default_factory=list)
    ensures: list[str] = field(default_factory=list)
    assigns: list[str] = field(default_factory=list)
    loop_invariants: list[str] = field(default_factory=list)
    raw_acsl: str = ""
    generation_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "function_name": self.function_name,
            "requires": list(self.requires),
            "ensures": list(self.ensures),
            "assigns": list(self.assigns),
            "loop_invariants": list(self.loop_invariants),
            "raw_acsl": self.raw_acsl,
            "generation_metadata": dict(self.generation_metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NativeAcslSpec":
        if not isinstance(data, Mapping):
            raise ValueError("ACSL spec must be a JSON object")
        function_name = str(data.get("function_name") or "").strip()
        if not function_name:
            raise ValueError("ACSL spec missing non-empty function_name")
        return cls(
            function_name=function_name,
            requires=_string_list(data.get("requires", []), "requires"),
            ensures=_string_list(data.get("ensures", []), "ensures"),
            assigns=_string_list(data.get("assigns", []), "assigns"),
            loop_invariants=_string_list(
                data.get("loop_invariants", []), "loop_invariants"
            ),
            raw_acsl=str(data.get("raw_acsl") or ""),
            generation_metadata=dict(data.get("generation_metadata") or {}),
        )

    def render_contract(self) -> str:
        lines: list[str] = []
        for clause in self.requires:
            rendered = _strip_clause_prefix(clause, "requires")
            if rendered:
                lines.append(f"  requires {rendered};")
        for target in self.assigns:
            rendered = _strip_clause_prefix(target, "assigns")
            if rendered:
                lines.append(f"  assigns {rendered};")
        for clause in self.ensures:
            rendered = _strip_clause_prefix(clause, "ensures")
            if rendered:
                lines.append(f"  ensures {rendered};")
        if not lines and self.raw_acsl.strip():
            return self.raw_acsl.strip()
        if not lines:
            return ""
        return "/*@\n" + "\n".join(lines) + "\n*/"

    def rendered_loop_invariants(self) -> list[str]:
        rendered = []
        for clause in self.loop_invariants:
            stripped = _strip_clause_prefix(clause, "loop invariant")
            if stripped:
                rendered.append(f"loop invariant {stripped};")
        return rendered


@dataclass
class NativeAcslBuild:
    source_text: str
    specs: dict[str, NativeAcslSpec]
    inserted_functions: list[str] = field(default_factory=list)
    skipped_functions: dict[str, str] = field(default_factory=dict)
    inserted_loop_invariants: dict[str, int] = field(default_factory=dict)
    skipped_loop_invariants: dict[str, str] = field(default_factory=dict)
    recovered_asserts: int = 0


@dataclass(frozen=True)
class MutationCase:
    name: str
    old: str
    new: str
    equivalent_hint: bool = False


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must contain only strings")
        item = item.strip()
        if item:
            out.append(item)
    return out


def _strip_clause_prefix(clause: str, keyword: str) -> str:
    clause = clause.strip()
    clause = clause.removeprefix("/*@").removesuffix("*/").strip()
    clause = re.sub(r";\s*$", "", clause)
    pattern = re.compile(rf"^(?:{re.escape(keyword)})\s+", re.IGNORECASE)
    return pattern.sub("", clause).strip()


def parse_native_acsl_specs(raw: str | Mapping[str, Any] | Sequence[Any]) -> dict[str, NativeAcslSpec]:
    """Parse model JSON or an already-loaded JSON value into ACSL specs."""

    data: Any
    if isinstance(raw, str):
        data = json.loads(_extract_json_payload(raw))
    else:
        data = raw

    if isinstance(data, Mapping) and "specs" in data:
        data = data["specs"]
    if isinstance(data, Mapping) and "function_name" in data:
        spec = NativeAcslSpec.from_dict(data)
        return {spec.function_name: spec}
    if isinstance(data, Mapping):
        specs = {}
        for name, item in data.items():
            if not isinstance(item, Mapping):
                raise ValueError(f"Spec entry for {name!r} must be an object")
            spec_data = dict(item)
            spec_data.setdefault("function_name", name)
            spec = NativeAcslSpec.from_dict(spec_data)
            specs[spec.function_name] = spec
        return specs
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        specs = {}
        for item in data:
            spec = NativeAcslSpec.from_dict(item)
            specs[spec.function_name] = spec
        return specs
    raise ValueError("Unsupported native ACSL JSON shape")


def load_native_acsl_specs(path: str | Path) -> dict[str, NativeAcslSpec]:
    return parse_native_acsl_specs(Path(path).read_text(encoding="utf-8"))


def write_native_acsl_specs(path: str | Path, specs: Mapping[str, NativeAcslSpec]) -> None:
    payload = {
        "specs": [spec.to_dict() for spec in specs.values()],
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _extract_json_payload(raw: str) -> str:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    if text.startswith("{") or text.startswith("["):
        return text
    start_candidates = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not start_candidates:
        raise ValueError("No JSON object or array found in LLM response")
    start = min(start_candidates)
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(text[start:])
    return json.dumps(value)


def build_native_acsl_source(
    source_text: str,
    parsed: ParsedCFile,
    specs: Mapping[str, NativeAcslSpec],
    *,
    functions: Iterable[str] | None = None,
    recover_asserts: bool = False,
) -> NativeAcslBuild:
    """Inject native ACSL contracts and loop invariants into C source."""

    working = source_text
    recovered = 0
    if recover_asserts:
        working, recovered = recover_plain_asserts_to_acsl(working)

    selected = list(functions or specs.keys())
    selected_specs = {name: specs[name] for name in selected if name in specs}
    inserted_loops: dict[str, int] = {}
    skipped_loops: dict[str, str] = {}

    loop_insertions: list[tuple[int, str, str]] = []
    for name, spec in selected_specs.items():
        loop_lines = spec.rendered_loop_invariants()
        if not loop_lines:
            continue
        definition = parsed.function_definitions.get(name, "")
        if not definition:
            skipped_loops[name] = "function definition unavailable"
            continue
        def_start = working.find(definition)
        if def_start < 0:
            skipped_loops[name] = "function definition text not found"
            continue
        loop_match = re.search(r"(?m)^(\s*)(for|while)\s*\(", definition)
        if loop_match is None:
            skipped_loops[name] = "no for/while loop found"
            continue
        indent = loop_match.group(1)
        block_lines = ["/*@", *(f"  {line.strip()}" for line in loop_lines), "*/"]
        text = "\n".join(indent + line for line in block_lines) + "\n"
        loop_insertions.append((def_start + loop_match.start(), name, text))

    for idx, name, text in sorted(loop_insertions, key=lambda x: x[0], reverse=True):
        working = working[:idx] + text + working[idx:]
        inserted_loops[name] = inserted_loops.get(name, 0) + text.count("loop invariant")

    contract_insertions: list[tuple[int, str, str]] = []
    skipped_functions: dict[str, str] = {}
    for name, spec in selected_specs.items():
        contract = spec.render_contract()
        if not contract:
            skipped_functions[name] = "empty ACSL contract"
            continue
        definition = parsed.function_definitions.get(name, "")
        idx = working.find(definition) if definition else -1
        if idx < 0:
            idx = _find_function_definition_start(working, name)
        if idx < 0:
            skipped_functions[name] = "function definition text not found"
            continue
        contract_insertions.append((idx, name, contract))

    for idx, _name, contract in sorted(contract_insertions, key=lambda x: x[0], reverse=True):
        working = working[:idx] + contract + "\n" + working[idx:]

    inserted = [name for _idx, name, _text in sorted(contract_insertions, key=lambda x: x[0])]
    return NativeAcslBuild(
        source_text=working,
        specs=dict(selected_specs),
        inserted_functions=inserted,
        skipped_functions=skipped_functions,
        inserted_loop_invariants=inserted_loops,
        skipped_loop_invariants=skipped_loops,
        recovered_asserts=recovered,
    )


def _find_function_definition_start(source_text: str, function_name: str) -> int:
    pattern = re.compile(
        rf"(?m)^[A-Za-z_][\w\s\*\(\),\[\]]*?\b{re.escape(function_name)}\s*"
        rf"\([^;{{}}]*\)\s*\{{"
    )
    match = pattern.search(source_text)
    return match.start() if match else -1


def native_acsl_build_report(build: NativeAcslBuild) -> dict[str, Any]:
    return {
        "recovered_asserts": build.recovered_asserts,
        "inserted_functions": build.inserted_functions,
        "skipped_functions": build.skipped_functions,
        "inserted_loop_invariants": build.inserted_loop_invariants,
        "skipped_loop_invariants": build.skipped_loop_invariants,
        "clause_counts": clause_counts(build.specs),
        "vacuity_warnings": vacuity_warnings(build.specs),
    }


def downstream_proof_utility(frama_result: Any, *, recovered_asserts: int = 0) -> dict[str, Any]:
    """Summarize target proof utility from a Frama-C/WP result.

    The current native ACSL runner can recover C ``assert`` statements into ACSL
    statement assertions. Frama-C/WP reports aggregate goal counts, so this is a
    conservative utility summary rather than a per-target proof split.
    """

    proved = getattr(frama_result, "proved_goals", None)
    total = getattr(frama_result, "total_goals", None)
    status = getattr(frama_result, "status", "unknown")
    if recovered_asserts <= 0:
        return {
            "status": "not_applicable",
            "target_assertions": 0,
            "target_goals_proved": None,
            "target_goals_total": None,
            "proof_ratio": None,
        }
    ratio = None
    if proved is not None and total:
        ratio = proved / total
    return {
        "status": "measured_from_recovered_assertions",
        "target_assertions": recovered_asserts,
        "target_goals_proved": proved,
        "target_goals_total": total,
        "proof_ratio": ratio,
        "frama_c_status": status,
        "caveat": (
            "Frama-C/WP aggregate goal counts may include contract obligations "
            "as well as recovered target assertions."
        ),
    }


def clause_counts(specs: Mapping[str, NativeAcslSpec]) -> dict[str, Any]:
    per_function = {}
    totals = {"requires": 0, "ensures": 0, "assigns": 0, "loop_invariants": 0}
    for name, spec in specs.items():
        counts = {
            "requires": len(spec.requires),
            "ensures": len(spec.ensures),
            "assigns": len(spec.assigns),
            "loop_invariants": len(spec.loop_invariants),
        }
        per_function[name] = counts
        for key, value in counts.items():
            totals[key] += value
    return {"total": totals, "per_function": per_function}


def vacuity_warnings(specs: Mapping[str, NativeAcslSpec]) -> list[dict[str, str]]:
    warnings = []
    for name, spec in specs.items():
        if not spec.ensures:
            warnings.append({"function": name, "kind": "missing_ensures", "detail": "no postcondition"})
        elif all(_is_true_clause(c) for c in spec.ensures):
            warnings.append({"function": name, "kind": "vacuous_ensures", "detail": "all ensures clauses are true"})
        if any(_is_false_clause(c) for c in spec.requires):
            warnings.append({"function": name, "kind": "unsatisfiable_requires", "detail": "requires contains false"})
    return warnings


def _is_true_clause(clause: str) -> bool:
    stripped = _strip_clause_prefix(clause, "ensures").strip().lower()
    return stripped in {"true", "\\true", "1"}


def _is_false_clause(clause: str) -> bool:
    stripped = _strip_clause_prefix(clause, "requires").strip().lower()
    return stripped in {"false", "\\false", "0"}


def render_native_acsl_prompt(
    *,
    source_path: str,
    function_name: str,
    function_definition: str,
    domain_knowledge: str = "",
) -> tuple[str, str]:
    system = (
        "You generate native ACSL contracts for C functions. "
        "Return JSON only. Do not emit markdown or prose. "
        "Prefer concise, verifiable ACSL clauses over natural language. "
        "Use arrays of clause bodies without the leading ACSL keyword."
    )
    user = {
        "task": "Generate ACSL specifications for one C function.",
        "source_path": source_path,
        "function_name": function_name,
        "domain_knowledge": domain_knowledge,
        "output_schema": {
            "function_name": function_name,
            "requires": ["ACSL precondition clause body"],
            "assigns": ["ACSL assigns target such as \\nothing or *p"],
            "ensures": ["ACSL postcondition clause body"],
            "loop_invariants": ["ACSL loop invariant body, if needed"],
            "raw_acsl": "optional complete ACSL comment",
        },
        "function_definition": function_definition,
    }
    return system, json.dumps(user, indent=2)


def generate_native_acsl_specs(
    *,
    source_path: str | Path,
    source_text: str,
    parsed: ParsedCFile,
    function_names: Sequence[str],
    llm: Any,
    model: str,
    domain_knowledge: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> dict[str, NativeAcslSpec]:
    specs = {}
    for name in function_names:
        definition = parsed.function_definitions.get(name, "")
        if not definition:
            continue
        system, user = render_native_acsl_prompt(
            source_path=str(source_path),
            function_name=name,
            function_definition=definition,
            domain_knowledge=domain_knowledge,
        )
        response = llm.complete(
            system,
            user,
            max_tokens=max_tokens,
            temperature=temperature,
            role="spec_gen",
        )
        parsed_specs = parse_native_acsl_specs(response)
        if name not in parsed_specs and len(parsed_specs) == 1:
            spec = next(iter(parsed_specs.values()))
            spec.function_name = name
        else:
            spec = parsed_specs[name]
        start = source_text.find(definition)
        metadata = dict(spec.generation_metadata)
        metadata.update(
            {
                "model": model,
                "prompt_sha256": hashlib.sha256((system + "\n" + user).encode("utf-8")).hexdigest(),
                "source_span": [start, start + len(definition)] if start >= 0 else None,
                "generated_at_unix": int(time.time()),
            }
        )
        spec.generation_metadata = metadata
        specs[name] = spec
    return specs


def load_mutations_json(path: str | Path) -> list[MutationCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, Mapping) and "mutations" in data:
        data = data["mutations"]
    if not isinstance(data, list):
        raise ValueError("mutation JSON must be a list or object with mutations")
    out = []
    for item in data:
        if not isinstance(item, Mapping):
            raise ValueError("each mutation must be an object")
        out.append(
            MutationCase(
                name=str(item["name"]),
                old=str(item["old"]),
                new=str(item["new"]),
                equivalent_hint=bool(item.get("equivalent_hint", False)),
            )
        )
    return out


def run_mutation_vdr(
    *,
    source_path: str | Path,
    source_text: str,
    specs: Mapping[str, NativeAcslSpec],
    mutations: Sequence[MutationCase],
    output_dir: str | Path,
    wp_timeout: int = 30,
    timeout: int = 120,
    cpus: float = 2.0,
    use_tce: bool = True,
) -> dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    original_annotated = out / "original.acsl.c"
    parsed = _parse_source_for_annotation(source_path, source_text)
    original_build = build_native_acsl_source(source_text, parsed, specs)
    original_annotated.write_text(original_build.source_text, encoding="utf-8")
    original_wp = run_frama_c_wp(original_annotated, wp_timeout=wp_timeout, timeout=timeout, cpus=cpus)
    original_success = original_wp.status == "success"

    results = []
    for mutation in mutations:
        if mutation.old not in source_text:
            results.append(
                {
                    "name": mutation.name,
                    "status": "skipped",
                    "reason": "old text not found",
                    "counted": False,
                    "killed": False,
                }
            )
            continue
        mutated_text = source_text.replace(mutation.old, mutation.new, 1)
        tce = _tce_equivalent(source_text, mutated_text, out / f"tce_{mutation.name}") if use_tce else None
        equivalent = mutation.equivalent_hint or bool(tce and tce.get("equivalent"))
        if equivalent:
            results.append(
                {
                    "name": mutation.name,
                    "status": "equivalent",
                    "equivalent_hint": mutation.equivalent_hint,
                    "tce": tce,
                    "counted": False,
                    "killed": False,
                }
            )
            continue
        case_source = out / f"{mutation.name}.acsl.c"
        parsed_mutant = _parse_source_for_annotation(source_path, mutated_text)
        build = build_native_acsl_source(mutated_text, parsed_mutant, specs)
        case_source.write_text(build.source_text, encoding="utf-8")
        wp = run_frama_c_wp(case_source, wp_timeout=wp_timeout, timeout=timeout, cpus=cpus)
        killed = bool(original_success and wp.status != "success")
        results.append(
            {
                "name": mutation.name,
                "status": wp.status,
                "counted": True,
                "killed": killed,
                "proved_goals": wp.proved_goals,
                "total_goals": wp.total_goals,
                "runtime_s": wp.runtime_s,
                "annotated_source": str(case_source),
                "tce": tce,
            }
        )

    counted = sum(1 for item in results if item.get("counted"))
    killed = sum(1 for item in results if item.get("killed"))
    return {
        "original": {
            "status": original_wp.status,
            "proved_goals": original_wp.proved_goals,
            "total_goals": original_wp.total_goals,
            "runtime_s": original_wp.runtime_s,
            "annotated_source": str(original_annotated),
        },
        "original_success": original_success,
        "mutants_tried": counted,
        "mutants_killed": killed,
        "mutation_score": (killed / counted) if counted else None,
        "results": results,
    }


def _parse_source_for_annotation(source_path: str | Path, source_text: str) -> ParsedCFile:
    from bmc_agent.source_parser import parse_source_file

    return parse_source_file(source_path, source_text=source_text)  # type: ignore[return-value]


def _tce_equivalent(original: str, mutated: str, work_prefix: Path) -> dict[str, Any]:
    work_prefix.parent.mkdir(parents=True, exist_ok=True)
    orig_c = work_prefix.with_suffix(".orig.c")
    mut_c = work_prefix.with_suffix(".mut.c")
    orig_o = work_prefix.with_suffix(".orig.s")
    mut_o = work_prefix.with_suffix(".mut.s")
    orig_c.write_text(original, encoding="utf-8")
    mut_c.write_text(mutated, encoding="utf-8")
    cmds = [
        ["gcc", "-O2", "-S", "-fno-ident", str(orig_c), "-o", str(orig_o)],
        ["gcc", "-O2", "-S", "-fno-ident", str(mut_c), "-o", str(mut_o)],
    ]
    for cmd in cmds:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
        if proc.returncode != 0:
            return {
                "equivalent": False,
                "error": proc.stderr[-2000:],
                "command": cmd,
            }
    equivalent = _normalized_assembly(orig_o) == _normalized_assembly(mut_o)
    return {"equivalent": equivalent, "method": "gcc -O2 normalized assembly equality"}


def _normalized_assembly(path: Path) -> list[str]:
    lines = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith((".file", ".ident", ".section .note.GNU-stack")):
            continue
        lines.append(stripped)
    return lines


def load_ground_truth_coverage(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    labels = data.get("coverage_labels", data) if isinstance(data, Mapping) else data
    if not isinstance(labels, list):
        raise ValueError("ground truth coverage file must be a list or contain coverage_labels")
    total = 0
    covered = 0
    for item in labels:
        if isinstance(item, Mapping) and "covered" in item:
            total += 1
            covered += 1 if bool(item["covered"]) else 0
    return {
        "labels": labels,
        "covered": covered,
        "total": total,
        "coverage": (covered / total) if total else None,
    }


def evaluate_witness_formula(expr: str, env: Mapping[str, Any]) -> bool:
    """Small deterministic evaluator for witness-preservation predicates."""

    expr = expr.strip()
    if expr.lower() in {"true", "\\true", "1"}:
        return True
    if expr.lower() in {"false", "\\false", "0"}:
        return False
    expr = expr.replace("&&", " and ").replace("||", " or ")
    tree = ast.parse(expr, mode="eval")
    return bool(_eval_witness_ast(tree.body, env))


def _eval_witness_ast(node: ast.AST, env: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.BoolOp):
        values = [bool(_eval_witness_ast(v, env)) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not bool(_eval_witness_ast(node.operand, env))
    if isinstance(node, ast.Compare):
        left = _eval_witness_ast(node.left, env)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_witness_ast(comparator, env)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            else:
                raise ValueError(f"unsupported comparator: {ast.dump(op)}")
            if not ok:
                return False
            left = right
        return True
    raise ValueError(f"unsupported witness expression: {ast.dump(node)}")
