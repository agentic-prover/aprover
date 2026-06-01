"""CLI handlers for the optional ACSL/Frama-C evaluation backend."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable


def _resolve_domain_knowledge(raw: str) -> str:
    """Return domain knowledge text: read from file if raw is a valid path."""
    try:
        path = Path(raw)
        if path.exists():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return raw


def _apply_model_arg(config: object, args: argparse.Namespace) -> None:
    model = getattr(args, "model", None)
    if model:
        config.llm_model = model  # type: ignore[attr-defined]


def _cmd_acsl_pilot(args: argparse.Namespace) -> int:
    """Translate existing BMC-Agent DSL specs to ACSL and run Frama-C/WP."""
    from bmc_agent.acsl import (
        acsl_build_report,
        build_acsl_source,
        load_specs_json,
        recover_plain_asserts_to_acsl,
        run_frama_c_wp,
    )
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.source_parser import parse_source_file
    from bmc_agent.spec_generator import SpecGenerator

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output
    if getattr(args, "strict_dsl", False):
        config.strict_dsl = True
    _apply_model_arg(config, args)

    source = Path(args.source).resolve()
    raw_source = source.read_text(encoding="utf-8", errors="replace")
    working_source = raw_source
    recovered_asserts = 0
    if args.recover_asserts:
        working_source, recovered_asserts = recover_plain_asserts_to_acsl(raw_source)

    parsed = parse_source_file(source, source_text=working_source)
    selected_functions = [args.function] if args.function else list(parsed.functions.keys())

    store = ArtifactStore(config.artifact_dir)
    if args.spec_json:
        specs = load_specs_json(args.spec_json)
    elif args.use_existing_specs:
        specs = {}
        for fn_name in selected_functions:
            spec = store.load_spec(args.driver, fn_name)
            if spec is not None:
                specs[fn_name] = spec
    else:
        llm = LLMClient(config)
        generator = SpecGenerator(config, llm, store)
        domain_knowledge = _resolve_domain_knowledge(args.domain_knowledge) if args.domain_knowledge else ""
        specs = generator.generate_specs(
            source_file=str(source),
            driver_name=args.driver,
            domain_knowledge=domain_knowledge,
            source_text=raw_source,
        )

    if args.function:
        specs = {args.function: specs[args.function]} if args.function in specs else {}

    if not specs:
        print("No specs available for ACSL pilot.", file=sys.stderr)
        return 1

    build = build_acsl_source(
        working_source,
        parsed,
        specs,
        recover_asserts=False,
        add_assigns_nothing=args.add_assigns_nothing,
        functions=selected_functions,
    )
    build.recovered_asserts = recovered_asserts

    out_dir = Path(config.artifact_dir) / args.driver / "acsl_pilot"
    out_dir.mkdir(parents=True, exist_ok=True)
    annotated_path = out_dir / f"{source.stem}.acsl.c"
    annotated_path.write_text(build.source_text, encoding="utf-8")

    report = {
        "source": str(source),
        "driver": args.driver,
        "annotated_source": str(annotated_path),
        "scope": "function contracts translated from BMC-Agent DSL plus optional ACSL statement assertions",
        "build": acsl_build_report(build),
    }

    frama_result = None
    if not args.no_run_frama_c:
        frama_result = run_frama_c_wp(
            annotated_path,
            wp_timeout=args.wp_timeout,
            command=args.frama_c_cmd,
            docker_image=args.frama_c_docker_image,
            timeout=args.timeout,
            cpus=args.cpus,
            extra_args=args.frama_c_arg,
        )
        (out_dir / "frama_c_stdout.txt").write_text(frama_result.stdout, encoding="utf-8")
        (out_dir / "frama_c_stderr.txt").write_text(frama_result.stderr, encoding="utf-8")
        report["frama_c"] = frama_result.to_dict()

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Annotated source: {annotated_path}")
    print(f"Report:           {report_path}")
    print(f"Contracts inserted: {len(build.inserted_functions)}")
    if build.recovered_asserts:
        print(f"Recovered ACSL assertions: {build.recovered_asserts}")
    unsupported = sum(len(c.unsupported) for c in build.contracts.values())
    if unsupported:
        print(f"Unsupported clauses: {unsupported}")
    if frama_result is not None:
        goals = ""
        if frama_result.proved_goals is not None and frama_result.total_goals is not None:
            goals = f" ({frama_result.proved_goals}/{frama_result.total_goals} goals)"
        print(f"Frama-C/WP: {frama_result.status}{goals}, {frama_result.runtime_s:.1f}s")
        return 0 if frama_result.status == "success" else 1
    return 0


def _cmd_acsl_generate(args: argparse.Namespace) -> int:
    """Generate native ACSL specs and annotated C source."""
    from bmc_agent.acsl import run_frama_c_wp
    from bmc_agent.acsl_native import (
        build_native_acsl_source,
        generate_native_acsl_specs,
        load_native_acsl_specs,
        native_acsl_build_report,
        write_native_acsl_specs,
    )
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.source_parser import parse_source_file

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output
    _apply_model_arg(config, args)

    source = Path(args.source).resolve()
    raw_source = source.read_text(encoding="utf-8", errors="replace")
    parsed = parse_source_file(source, source_text=raw_source)
    selected_functions = [args.function] if args.function else list(parsed.functions.keys())

    if args.spec_json:
        specs = load_native_acsl_specs(args.spec_json)
    else:
        llm = LLMClient(config)
        domain_knowledge = _resolve_domain_knowledge(args.domain_knowledge) if args.domain_knowledge else ""
        specs = generate_native_acsl_specs(
            source_path=source,
            source_text=raw_source,
            parsed=parsed,
            function_names=selected_functions,
            llm=llm,
            model=config.llm_model,
            domain_knowledge=domain_knowledge,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
        )

    if args.function:
        specs = {args.function: specs[args.function]} if args.function in specs else {}
    if not specs:
        print("No native ACSL specs available.", file=sys.stderr)
        return 1

    build = build_native_acsl_source(
        raw_source,
        parsed,
        specs,
        functions=selected_functions,
        recover_asserts=args.recover_asserts,
    )

    out_dir = Path(config.artifact_dir) / args.driver / "acsl_native"
    out_dir.mkdir(parents=True, exist_ok=True)
    specs_path = out_dir / "acsl_specs.json"
    annotated_path = out_dir / f"{source.stem}.acsl.c"
    report_path = out_dir / "generation_report.json"
    write_native_acsl_specs(specs_path, specs)
    annotated_path.write_text(build.source_text, encoding="utf-8")

    report = {
        "source": str(source),
        "driver": args.driver,
        "specs": str(specs_path),
        "annotated_source": str(annotated_path),
        "scope": "native ACSL clauses generated directly, not translated from BMC-Agent DSL",
        "build": native_acsl_build_report(build),
    }

    frama_result = None
    if not args.no_run_frama_c:
        frama_result = run_frama_c_wp(
            annotated_path,
            wp_timeout=args.wp_timeout,
            command=args.frama_c_cmd,
            docker_image=args.frama_c_docker_image,
            timeout=args.timeout,
            cpus=args.cpus,
            extra_args=args.frama_c_arg,
        )
        (out_dir / "frama_c_stdout.txt").write_text(frama_result.stdout, encoding="utf-8")
        (out_dir / "frama_c_stderr.txt").write_text(frama_result.stderr, encoding="utf-8")
        report["frama_c"] = frama_result.to_dict()

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Native ACSL specs: {specs_path}")
    print(f"Annotated source:   {annotated_path}")
    print(f"Report:             {report_path}")
    print(f"Contracts inserted: {len(build.inserted_functions)}")
    if build.recovered_asserts:
        print(f"Recovered ACSL assertions: {build.recovered_asserts}")
    if frama_result is not None:
        goals = ""
        if frama_result.proved_goals is not None and frama_result.total_goals is not None:
            goals = f" ({frama_result.proved_goals}/{frama_result.total_goals} goals)"
        print(f"Frama-C/WP: {frama_result.status}{goals}, {frama_result.runtime_s:.1f}s")
        return 0 if frama_result.status == "success" else 1
    return 0


def _cmd_acsl_quality(args: argparse.Namespace) -> int:
    """Evaluate native ACSL specs with SpecSyn-style quality dimensions."""
    from bmc_agent.acsl import run_frama_c_wp
    from bmc_agent.acsl_native import (
        build_native_acsl_source,
        downstream_proof_utility,
        load_ground_truth_coverage,
        load_mutations_json,
        load_native_acsl_specs,
        native_acsl_build_report,
        run_mutation_vdr,
    )
    from bmc_agent.config import Config
    from bmc_agent.source_parser import parse_source_file

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output

    source = Path(args.source).resolve()
    raw_source = source.read_text(encoding="utf-8", errors="replace")
    parsed = parse_source_file(source, source_text=raw_source)
    specs = load_native_acsl_specs(args.spec_json)
    selected_functions = [args.function] if args.function else list(specs.keys())
    if args.function:
        specs = {args.function: specs[args.function]} if args.function in specs else {}
    if not specs:
        print("No native ACSL specs to evaluate.", file=sys.stderr)
        return 1

    build = build_native_acsl_source(
        raw_source,
        parsed,
        specs,
        functions=selected_functions,
        recover_asserts=args.recover_asserts,
    )

    out_dir = Path(config.artifact_dir) / args.driver / "acsl_quality"
    out_dir.mkdir(parents=True, exist_ok=True)
    annotated_path = out_dir / f"{source.stem}.acsl.c"
    report_path = out_dir / "quality_report.json"
    annotated_path.write_text(build.source_text, encoding="utf-8")

    report = {
        "source": str(source),
        "driver": args.driver,
        "spec_json": str(Path(args.spec_json).resolve()),
        "annotated_source": str(annotated_path),
        "build": native_acsl_build_report(build),
    }

    frama_result = run_frama_c_wp(
        annotated_path,
        wp_timeout=args.wp_timeout,
        command=args.frama_c_cmd,
        docker_image=args.frama_c_docker_image,
        timeout=args.timeout,
        cpus=args.cpus,
        extra_args=args.frama_c_arg,
    )
    (out_dir / "frama_c_stdout.txt").write_text(frama_result.stdout, encoding="utf-8")
    (out_dir / "frama_c_stderr.txt").write_text(frama_result.stderr, encoding="utf-8")
    report["frama_c"] = frama_result.to_dict()
    report["downstream_proof_utility"] = downstream_proof_utility(
        frama_result,
        recovered_asserts=build.recovered_asserts,
    )

    if args.ground_truth_json:
        report["ground_truth_coverage"] = load_ground_truth_coverage(args.ground_truth_json)

    if args.mutation_json:
        mutations = load_mutations_json(args.mutation_json)
        report["mutation_vdr"] = run_mutation_vdr(
            source_path=source,
            source_text=raw_source,
            specs=specs,
            mutations=mutations,
            output_dir=out_dir / "mutations",
            wp_timeout=args.wp_timeout,
            timeout=args.timeout,
            cpus=args.cpus,
            use_tce=not args.no_tce,
        )

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    goals = ""
    if frama_result.proved_goals is not None and frama_result.total_goals is not None:
        goals = f" ({frama_result.proved_goals}/{frama_result.total_goals} goals)"
    print(f"Annotated source: {annotated_path}")
    print(f"Report:           {report_path}")
    print(f"Frama-C/WP:       {frama_result.status}{goals}, {frama_result.runtime_s:.1f}s")
    if "mutation_vdr" in report:
        vdr = report["mutation_vdr"]
        print(f"Mutation/VDR:     {vdr['mutants_killed']}/{vdr['mutants_tried']} killed")
    return 0 if frama_result.status == "success" else 1


def add_acsl_subcommands(
    subparsers: argparse._SubParsersAction,
    add_model_arg: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register optional ACSL experiment commands."""

    acsl = subparsers.add_parser(
        "acsl-pilot",
        help=(
            "Pilot ACSL/Frama-C path: translate BMC-Agent DSL specs to ACSL "
            "contracts and run Frama-C/WP without changing the CBMC pipeline"
        ),
    )
    acsl.add_argument("--source", required=True, help="Path to a C source file")
    acsl.add_argument("--driver", required=True, help="Driver name (artifact namespace)")
    acsl.add_argument("--output", default="artifacts", help="Artifact output directory")
    acsl.add_argument("--function", default="", help="Specific function to translate/check")
    acsl.add_argument(
        "--domain-knowledge",
        default="",
        metavar="TEXT_OR_FILE",
        help="Domain knowledge string or path to a file containing domain knowledge",
    )
    acsl.add_argument("--spec-json", default="", help="Load specs from a JSON file instead of calling the LLM")
    acsl.add_argument(
        "--use-existing-specs",
        action="store_true",
        default=False,
        help="Load specs already stored under --output/--driver instead of calling the LLM",
    )
    acsl.add_argument(
        "--recover-asserts",
        action="store_true",
        default=False,
        help="Convert plain C assert(EXPR); statements into ACSL //@ assert EXPR; annotations",
    )
    acsl.add_argument(
        "--add-assigns-nothing",
        action="store_true",
        default=False,
        help="Add assigns \\nothing; to translated function contracts (only sound for pure functions)",
    )
    acsl.add_argument(
        "--strict-dsl",
        action="store_true",
        default=False,
        help="Ask spec generation for strict C-like DSL clauses before ACSL translation",
    )
    acsl.add_argument(
        "--no-run-frama-c",
        action="store_true",
        default=False,
        help="Only write the annotated ACSL source and report; do not invoke Frama-C/WP",
    )
    acsl.add_argument("--wp-timeout", type=int, default=30, help="Frama-C/WP per-goal timeout in seconds")
    acsl.add_argument("--timeout", type=int, default=120, help="Overall Frama-C command timeout in seconds")
    acsl.add_argument("--cpus", type=float, default=4.0, help="Docker CPU quota for Frama-C")
    acsl.add_argument("--frama-c-cmd", default="", help="Local Frama-C executable/command prefix")
    acsl.add_argument(
        "--frama-c-docker-image",
        default="framac/frama-c:26.0.debian",
        help="Docker image used when --frama-c-cmd is empty",
    )
    acsl.add_argument("--frama-c-arg", action="append", default=[], help="Extra Frama-C/WP argument")
    add_model_arg(acsl)
    acsl.set_defaults(func=_cmd_acsl_pilot)

    acsl_gen = subparsers.add_parser("acsl-generate", help="Generate native ACSL specs and annotated C source")
    acsl_gen.add_argument("--source", required=True, help="Path to a C source file")
    acsl_gen.add_argument("--driver", required=True, help="Driver name (artifact namespace)")
    acsl_gen.add_argument("--output", default="artifacts", help="Artifact output directory")
    acsl_gen.add_argument("--function", default="", help="Specific function to generate/check")
    acsl_gen.add_argument(
        "--domain-knowledge",
        default="",
        metavar="TEXT_OR_FILE",
        help="Domain knowledge string or path to a file containing domain knowledge",
    )
    acsl_gen.add_argument("--spec-json", default="", help="Load native ACSL specs from JSON instead of calling the LLM")
    acsl_gen.add_argument(
        "--recover-asserts",
        action="store_true",
        default=False,
        help="Convert plain C assert(EXPR); statements into ACSL //@ assert EXPR; annotations",
    )
    acsl_gen.add_argument("--no-run-frama-c", action="store_true", default=False)
    acsl_gen.add_argument("--wp-timeout", type=int, default=30, help="Frama-C/WP per-goal timeout in seconds")
    acsl_gen.add_argument("--timeout", type=int, default=120, help="Overall Frama-C command timeout in seconds")
    acsl_gen.add_argument("--cpus", type=float, default=4.0, help="Docker CPU quota for Frama-C")
    acsl_gen.add_argument("--max-tokens", type=int, default=4096, help="LLM max output tokens")
    acsl_gen.add_argument("--temperature", type=float, default=0.0, help="LLM temperature")
    acsl_gen.add_argument("--frama-c-cmd", default="", help="Local Frama-C executable/command prefix")
    acsl_gen.add_argument(
        "--frama-c-docker-image",
        default="framac/frama-c:26.0.debian",
        help="Docker image used when --frama-c-cmd is empty",
    )
    acsl_gen.add_argument("--frama-c-arg", action="append", default=[], help="Extra Frama-C/WP argument")
    add_model_arg(acsl_gen)
    acsl_gen.set_defaults(func=_cmd_acsl_generate)

    acsl_quality = subparsers.add_parser(
        "acsl-quality",
        help="Evaluate native ACSL specs with validity, coverage, and VDR metrics",
    )
    acsl_quality.add_argument("--source", required=True, help="Path to a C source file")
    acsl_quality.add_argument("--driver", required=True, help="Driver name (artifact namespace)")
    acsl_quality.add_argument("--spec-json", required=True, help="Native ACSL spec JSON")
    acsl_quality.add_argument("--output", default="artifacts", help="Artifact output directory")
    acsl_quality.add_argument("--function", default="", help="Specific function to evaluate")
    acsl_quality.add_argument(
        "--recover-asserts",
        action="store_true",
        default=False,
        help="Convert plain C assert(EXPR); statements into ACSL //@ assert EXPR; annotations",
    )
    acsl_quality.add_argument("--ground-truth-json", default="", help="Manual coverage labels")
    acsl_quality.add_argument("--mutation-json", default="", help="Mutation list JSON for VDR scoring")
    acsl_quality.add_argument("--no-tce", action="store_true", default=False, help="Disable GCC -O2 TCE filtering")
    acsl_quality.add_argument("--wp-timeout", type=int, default=30, help="Frama-C/WP per-goal timeout in seconds")
    acsl_quality.add_argument("--timeout", type=int, default=120, help="Overall Frama-C command timeout in seconds")
    acsl_quality.add_argument("--cpus", type=float, default=4.0, help="Docker CPU quota for Frama-C")
    acsl_quality.add_argument("--frama-c-cmd", default="", help="Local Frama-C executable/command prefix")
    acsl_quality.add_argument(
        "--frama-c-docker-image",
        default="framac/frama-c:26.0.debian",
        help="Docker image used when --frama-c-cmd is empty",
    )
    acsl_quality.add_argument("--frama-c-arg", action="append", default=[], help="Extra Frama-C/WP argument")
    acsl_quality.set_defaults(func=_cmd_acsl_quality)
