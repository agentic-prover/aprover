"""Witness-preservation smoke for BMC-Agent spec quality.

SpecSyn-style mutation killing measures whether a spec is too weak. BMC-style
preconditions also need the opposite check: did a generated spec assume away a
known violating caller state?

This script encodes the `ncdev_bar_read` caller-state mismatch from the private
findings repo as a small deterministic witness and evaluates a few representative
preconditions against it.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bmc_agent.dsl_to_cbmc import _match_call, _top_level_split


DEFAULT_WITNESS = {
    "bar": 2,
    "data_count": 8,
    "address_count": 1,
    "reg_addresses_capacity": 1,
    "reg_addresses_nonnull": True,
}


@dataclass(frozen=True)
class PreconditionCase:
    name: str
    precondition: str
    note: str


DEFAULT_CASES = [
    PreconditionCase(
        name="trivial_no_spec",
        precondition="true",
        note="No inferred precondition; preserves the violating caller state.",
    ),
    PreconditionCase(
        name="allocated_range_only",
        precondition="valid_range(reg_addresses, 0, address_count)",
        note="Requires only the caller-allocated range; preserves the mismatch.",
    ),
    PreconditionCase(
        name="llm_callee_min_contract",
        precondition="valid_range(reg_addresses, 0, data_count)",
        note="The failure mode: requires the buffer to be data_count-sized.",
    ),
    PreconditionCase(
        name="bug_fix_contract",
        precondition="bar == 0 || data_count <= 1",
        note=(
            "Rejects the witness by explicitly forbidding the buggy call shape; "
            "good as a fix, but not witness-preserving for bug discovery."
        ),
    ),
]


class FormulaError(ValueError):
    pass


def _eval_formula(expr: str, env: dict[str, Any]) -> bool:
    expr = expr.strip()
    if not expr:
        raise FormulaError("empty formula")
    if expr.lower() in {"true", "1", "\\true"}:
        return True
    if expr.lower() in {"false", "0", "\\false"}:
        return False

    or_parts = _top_level_split(expr, "||")
    if len(or_parts) > 1:
        return any(_eval_formula(part, env) for part in or_parts)

    and_parts = _top_level_split(expr, "&&")
    if len(and_parts) > 1:
        return all(_eval_formula(part, env) for part in and_parts)

    if expr.startswith("(") and expr.endswith(")"):
        try:
            return _eval_formula(expr[1:-1], env)
        except FormulaError:
            pass

    call = _whole_call(expr, "valid_range")
    if call is not None:
        if len(call) != 3:
            raise FormulaError("valid_range expects 3 arguments")
        ptr, lo_raw, hi_raw = call
        lo = _eval_int(lo_raw, env)
        hi = _eval_int(hi_raw, env)
        capacity_key = f"{ptr}_capacity"
        nonnull_key = f"{ptr}_nonnull"
        capacity = int(env.get(capacity_key, 0))
        nonnull = bool(env.get(nonnull_key, False))
        return nonnull and lo >= 0 and hi >= lo and capacity >= hi - lo

    if expr.startswith("!"):
        return not _eval_formula(expr[1:].strip(), env)

    return _eval_bool_ast(_c_bool_to_python(expr), env)


def _whole_call(expr: str, name: str) -> list[str] | None:
    match = _match_call(expr, name)
    if match is None:
        return None
    start, end, args = match
    if expr[:start].strip() or expr[end:].strip():
        return None
    return args


def _c_bool_to_python(expr: str) -> str:
    return expr.replace("&&", " and ").replace("||", " or ")


def _eval_int(expr: str, env: dict[str, Any]) -> int:
    value = _eval_ast(expr, env)
    if isinstance(value, bool):
        raise FormulaError(f"expected integer, got boolean: {expr}")
    return int(value)


def _eval_bool_ast(expr: str, env: dict[str, Any]) -> bool:
    value = _eval_ast(expr, env)
    if not isinstance(value, bool):
        raise FormulaError(f"expected boolean, got integer: {expr}")
    return value


def _eval_ast(expr: str, env: dict[str, Any]) -> int | bool:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(str(exc)) from exc
    return _eval_node(tree.body, env)


def _eval_node(node: ast.AST, env: dict[str, Any]) -> int | bool:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (bool, int)):
            return node.value
        raise FormulaError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise FormulaError(f"unknown name: {node.id}")
        value = env[node.id]
        if isinstance(value, (bool, int)):
            return value
        raise FormulaError(f"unsupported value for {node.id}: {value!r}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -int(_eval_node(node.operand, env))
    if isinstance(node, ast.BoolOp):
        values = [bool(_eval_node(value, env)) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if isinstance(node, ast.BinOp):
        left = int(_eval_node(node.left, env))
        right = int(_eval_node(node.right, env))
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, env)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, env)
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
                raise FormulaError(f"unsupported comparator: {ast.dump(op)}")
            if not ok:
                return False
            left = right
        return True
    raise FormulaError(f"unsupported expression: {ast.dump(node)}")


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    witness = DEFAULT_WITNESS.copy()
    results = []
    for case in DEFAULT_CASES:
        try:
            accepts = _eval_formula(case.precondition, witness)
            error = ""
        except FormulaError as exc:
            accepts = False
            error = str(exc)
        results.append(
            {
                "name": case.name,
                "precondition": case.precondition,
                "accepts_witness": accepts,
                "overconstraint_for_bug_discovery": not accepts,
                "note": case.note,
                "error": error,
            }
        )

    report = {
        "source_case": "ncdev_bar_read",
        "witness": witness,
        "witness_meaning": (
            "bar != 0 makes the caller allocate one address, while "
            "data_count > 1 is still passed to the callee loop."
        ),
        "results": results,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="artifacts/spec_quality_compare/ncdev_bar_read_witness_smoke",
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
