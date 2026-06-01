"""Small SpecSyn-style mutation smoke for the ACSL backend pilot.

This is intentionally narrow: it checks whether a translated BMC-Agent DSL
contract can distinguish a few behavior-changing variants of the same C
function. It is not a benchmark runner.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from bmc_agent.acsl import (
    acsl_build_report,
    build_acsl_source,
    load_specs_json,
    run_frama_c_wp,
)
from bmc_agent.source_parser import parse_source_file


@dataclass(frozen=True)
class Mutation:
    name: str
    old: str
    new: str
    equivalent_hint: bool = False


MAX2_MUTATIONS = [
    Mutation(
        name="return_left_argument",
        old="return x >= y ? x : y;",
        new="return x;",
    ),
    Mutation(
        name="return_zero",
        old="return x >= y ? x : y;",
        new="return 0;",
    ),
    Mutation(
        name="return_minimum",
        old="return x >= y ? x : y;",
        new="return x < y ? x : y;",
    ),
    Mutation(
        name="tie_break_equal_case",
        old="return x >= y ? x : y;",
        new="return x > y ? x : y;",
        equivalent_hint=True,
    ),
]


READ_AT_MUTATIONS = [
    Mutation(
        name="return_first_element",
        old="return arr[idx];",
        new="return arr[0];",
    ),
    Mutation(
        name="return_zero",
        old="return arr[idx];",
        new="return 0;",
    ),
    Mutation(
        name="return_last_element",
        old="return arr[idx];",
        new="return arr[len - 1];",
    ),
]

MUTATION_SETS = {
    "max2": MAX2_MUTATIONS,
    "read_at": READ_AT_MUTATIONS,
}


def _write_annotated_case(
    *,
    source_text: str,
    parsed_source: Path,
    spec_json: Path,
    function: str,
    output_dir: Path,
    case_name: str,
    add_assigns_nothing: bool,
) -> tuple[Path, dict]:
    tmp_source = output_dir / f"{case_name}.raw.c"
    tmp_source.write_text(source_text, encoding="utf-8")
    parsed = parse_source_file(str(tmp_source))
    specs = load_specs_json(spec_json)
    build = build_acsl_source(
        source_text,
        parsed,
        specs,
        recover_asserts=True,
        add_assigns_nothing=add_assigns_nothing,
        functions=[function],
    )
    annotated = output_dir / f"{case_name}.acsl.c"
    annotated.write_text(build.source_text, encoding="utf-8")
    tmp_source.unlink(missing_ok=True)
    report = acsl_build_report(build)
    report["source_template"] = str(parsed_source)
    return annotated, report


def _case_status(result) -> str:
    if result.timed_out:
        return "timeout"
    if result.returncode not in (0, None):
        return "error"
    if result.proved_goals is not None and result.total_goals is not None:
        return "success" if result.proved_goals == result.total_goals else "unproved"
    return result.status


def run(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    spec_json = Path(args.spec_json).resolve()
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    original_text = source.read_text(encoding="utf-8")
    cases: list[tuple[str, str, bool, bool]] = [("original", original_text, False, False)]
    mutations = MUTATION_SETS[args.mutation_set]
    for mutation in mutations:
        if mutation.old not in original_text:
            continue
        cases.append(
            (
                mutation.name,
                original_text.replace(mutation.old, mutation.new),
                True,
                mutation.equivalent_hint,
            )
        )

    results = []
    original_success = False
    for case_name, text, is_mutant, equivalent_hint in cases:
        annotated, build_report = _write_annotated_case(
            source_text=text,
            parsed_source=source,
            spec_json=spec_json,
            function=args.function,
            output_dir=out_dir,
            case_name=case_name,
            add_assigns_nothing=args.add_assigns_nothing,
        )
        wp = run_frama_c_wp(
            annotated,
            wp_timeout=args.wp_timeout,
            timeout=args.timeout,
            cpus=args.cpus,
        )
        status = _case_status(wp)
        if case_name == "original":
            original_success = status == "success"
        killed = bool(
            is_mutant
            and not equivalent_hint
            and original_success
            and status != "success"
        )
        results.append(
            {
                "case": case_name,
                "is_mutant": is_mutant,
                "equivalent_hint": equivalent_hint,
                "status": status,
                "killed": killed,
                "runtime_s": wp.runtime_s,
                "proved_goals": wp.proved_goals,
                "total_goals": wp.total_goals,
                "annotated_source": str(annotated),
                "build": build_report,
            }
        )

    tried = sum(
        1 for item in results if item["is_mutant"] and not item["equivalent_hint"]
    )
    killed = sum(1 for item in results if item["killed"])
    report = {
        "source": str(source),
        "spec_json": str(spec_json),
        "function": args.function,
        "mutation_set": args.mutation_set,
        "original_success": original_success,
        "mutants_tried": tried,
        "mutants_killed": killed,
        "mutation_score": (killed / tried) if tried else None,
        "results": results,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0 if original_success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default="experiments/acsl_backend_pilot/max2.c",
        help="C source file to mutate",
    )
    parser.add_argument(
        "--spec-json",
        default="experiments/acsl_backend_pilot/max2_spec.json",
        help="BMC-Agent spec JSON to translate to ACSL",
    )
    parser.add_argument("--function", default="max2")
    parser.add_argument(
        "--mutation-set",
        choices=sorted(MUTATION_SETS),
        default="max2",
    )
    parser.add_argument(
        "--output",
        default="artifacts/spec_quality_compare/max2_mutation_smoke",
    )
    parser.add_argument("--wp-timeout", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--cpus", type=float, default=2.0)
    parser.add_argument("--add-assigns-nothing", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
