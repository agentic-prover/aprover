"""
AMC command-line interface.

Usage:
    uv run amc generate --source examples/simple_driver.c --driver mydriver --output artifacts/
    uv run amc check   --driver mydriver --function rb_write
    uv run amc verify  --source examples/simple_driver.c --driver mydriver
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _resolve_domain_knowledge(raw: str) -> str:
    """Return domain knowledge text: read from file if raw is a valid existing path, else use as-is."""
    try:
        p = Path(raw)
        if p.exists():
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return raw


def _apply_model_arg(config: "object", args: argparse.Namespace) -> None:
    """Override config.llm_model if --model was supplied on the command line."""
    model = getattr(args, "model", None)
    if model:
        config.llm_model = model  # type: ignore[attr-defined]


def _cmd_generate(args: argparse.Namespace) -> int:
    """Phase 1: Generate specs for all functions in a C source file."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.spec_generator import SpecGenerator

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output
    _apply_model_arg(config, args)

    store = ArtifactStore(config.artifact_dir)
    llm = LLMClient(config)
    generator = SpecGenerator(config, llm, store)

    domain_knowledge = _resolve_domain_knowledge(args.domain_knowledge) if args.domain_knowledge else ""

    print(f"Generating specs for: {args.source}")
    print(f"Driver name: {args.driver}")
    print(f"Output directory: {config.artifact_dir}")

    specs = generator.generate_specs(
        source_file=args.source,
        driver_name=args.driver,
        domain_knowledge=domain_knowledge,
    )

    print(f"\nGenerated {len(specs)} specs:")
    for fn_name, spec in sorted(specs.items()):
        fallback = spec.__dict__.get("fallback", False)
        tag = " [FALLBACK]" if fallback else ""
        print(f"  {fn_name}{tag}")
        print(f"    pre:  {spec.precondition[:80]}")
        print(f"    post: {spec.postcondition[:80]}")

    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Phase 2: Run BMC on previously generated specs."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.config import Config
    from bmc_agent.backends import backend_for
    from bmc_agent.source_parser import detect_language, parse_source_file

    config = Config.from_env()
    if hasattr(args, "output") and args.output:
        config.artifact_dir = args.output

    store = ArtifactStore(config.artifact_dir)
    # Pick CBMC for .c/.h and Kani for .rs; both implement BMCEngine's
    # backend interface.
    engine = BMCEngine(
        config,
        store,
        backend=backend_for(detect_language(args.source), config),
    )

    # Load the source file (required for harness generation)
    source_file = args.source
    print(f"Checking driver:  {args.driver}")
    print(f"Source file:      {source_file}")
    print(f"Artifact dir:     {config.artifact_dir}")

    parsed = parse_source_file(source_file)

    # Load specs from disk
    driver_name = args.driver
    specs: dict = {}
    target_func = getattr(args, "function", "") or ""
    functions_to_check = list(parsed.functions.keys())
    if target_func:
        if target_func not in parsed.functions:
            print(f"Error: function '{target_func}' not found in {source_file}", file=sys.stderr)
            return 1
        functions_to_check = [target_func]

    for fn_name in functions_to_check:
        spec = store.load_spec(driver_name, fn_name)
        if spec is not None:
            specs[fn_name] = spec
        else:
            print(f"  Warning: no spec found for '{fn_name}' — skipping")

    if not specs:
        print("No specs to check. Run 'amc generate' first.")
        return 1

    funcs = {
        name: parsed.get_function_info(name)
        for name in specs
        if parsed.get_function_info(name) is not None
    }

    print(f"\nRunning BMC on {len(funcs)} function(s)...")
    verdicts = engine.check_all(funcs, specs, parsed, driver_name)

    # Print summary
    print(f"\nResults:")
    verified_count = 0
    failed_count = 0
    error_count = 0
    for fn_name, verdict in sorted(verdicts.items()):
        if verdict.error and "not found" in verdict.error.lower():
            status = "SKIPPED (cbmc not installed)"
            error_count += 1
        elif verdict.error:
            status = f"ERROR: {verdict.error}"
            error_count += 1
        elif verdict.verified:
            status = "VERIFIED"
            verified_count += 1
        else:
            status = f"FAILED ({len(verdict.counterexamples)} counterexample(s))"
            failed_count += 1
        print(f"  {fn_name:30s}  {status}")

    print(f"\nSummary: {verified_count} verified, {failed_count} failed, {error_count} errors/skipped")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    """Phase 4: Run AMC and baselines on a corpus of C programs."""
    from bmc_agent.config import Config
    from bmc_agent.evaluation.corpus import Corpus
    from bmc_agent.evaluation.runner import EvaluationRunner

    config = Config.from_env()
    corpus = Corpus(args.corpus)
    runner = EvaluationRunner(config)

    print(f"Running evaluation on corpus: {args.corpus}")
    print(f"Output directory: {args.output}")
    print(f"Run baselines: {args.baselines}")

    summary = runner.run_corpus(
        corpus=corpus,
        output_dir=args.output,
        run_baselines=args.baselines,
    )

    print(f"\n=== Evaluation Summary ===")
    print(f"  Total drivers:      {summary.total_drivers}")
    print(f"  Total functions:    {summary.total_functions}")
    print(f"  Total bugs found:   {summary.total_bugs_found}")
    print(f"  Avg spec coverage:  {summary.avg_spec_coverage * 100:.1f}%")
    print(f"  Avg FP rate:        {summary.avg_false_positive_rate * 100:.1f}%")
    print(f"\nReports saved to: {args.output}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """Phase 4: Generate a summary report from evaluation artifacts."""
    import json
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.evaluation.metrics import DriverMetrics, EvaluationSummary
    from bmc_agent.evaluation.report import ReportGenerator

    eval_dir = Path(args.eval_dir)
    summary_json = eval_dir / "eval_summary.json"

    if not summary_json.exists():
        print(
            f"Error: no eval_summary.json found in {args.eval_dir}. "
            "Run 'amc eval' first.",
            file=sys.stderr,
        )
        return 1

    with summary_json.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    # Reconstruct summary
    per_driver = [
        DriverMetrics(
            driver_name=d["driver_name"],
            total_functions=d["total_functions"],
            functions_specified=d["functions_specified"],
            functions_checked=d["functions_checked"],
            functions_verified=d["functions_verified"],
            counterexamples_found=d["counterexamples_found"],
            real_bugs_confirmed=d["real_bugs_confirmed"],
            spurious_cex_count=d["spurious_cex_count"],
            false_positive_rate=d["false_positive_rate"],
            refinement_iterations=[],
            avg_refinement_iters=d["avg_refinement_iters"],
            spec_coverage=d["spec_coverage"],
            runtime_seconds=d["runtime_seconds"],
            token_cost=d["token_cost"],
            bugs_by_type=d["bugs_by_type"],
        )
        for d in data.get("per_driver", [])
    ]
    summary = EvaluationSummary(
        total_drivers=data["total_drivers"],
        total_functions=data["total_functions"],
        total_bugs_found=data["total_bugs_found"],
        avg_false_positive_rate=data["avg_false_positive_rate"],
        avg_spec_coverage=data["avg_spec_coverage"],
        avg_refinement_iters=data["avg_refinement_iters"],
        total_token_cost=data["total_token_cost"],
        bugs_by_type=data.get("bugs_by_type", {}),
        per_driver=per_driver,
        amc_unique_bugs=data.get("amc_unique_bugs", 0),
        baseline_unique_bugs=data.get("baseline_unique_bugs", {}),
    )

    from bmc_agent.artifacts import ArtifactStore

    store = ArtifactStore(str(eval_dir))
    gen = ReportGenerator(store)
    md = gen.generate_summary_report(summary)

    output = args.output
    if output:
        Path(output).write_text(md, encoding="utf-8")
        print(f"Report written to: {output}")
    else:
        print(md)

    return 0


def _cmd_corpus_generate(args: argparse.Namespace) -> int:
    """Phase 4: Use LLM to generate synthetic C programs for the corpus."""
    from bmc_agent.config import Config
    from bmc_agent.evaluation.corpus import Corpus
    from bmc_agent.llm import LLMClient

    config = Config.from_env()
    llm = LLMClient(config)
    corpus = Corpus(args.output)

    print(f"Generating {args.count} synthetic corpus entries into: {args.output}")
    entries = corpus.generate_synthetic_corpus(
        output_dir=args.output,
        llm=llm,
        count=args.count,
    )
    print(f"Generated {len(entries)} entries:")
    for e in entries:
        print(f"  {e.name} ({e.driver_type}): {len(e.ground_truth_bugs)} known bug(s)")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Full pipeline: generate specs + run BMC + validate counterexamples (Phase 3)."""
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline

    config = Config.from_env()
    if hasattr(args, "output") and args.output:
        config.artifact_dir = args.output
    if hasattr(args, "include_dir") and args.include_dir:
        config.include_dirs = args.include_dir
        config.preprocess = True
    if getattr(args, "skip_refinement", False):
        config.skip_refinement = True
    if getattr(args, "enable_realism_check", False):
        config.enable_realism_check = True
    if getattr(args, "enable_realism_thinking", False):
        config.enable_realism_thinking = True
    if getattr(args, "enable_flag_selection", False):
        config.enable_flag_selection = True
    if getattr(args, "enable_dynamic_validation", False):
        config.enable_dynamic_validation = True
    if getattr(args, "threat_model", None):
        config.threat_model = args.threat_model
    _apply_model_arg(config, args)

    domain_knowledge = _resolve_domain_knowledge(args.domain_knowledge) if (hasattr(args, "domain_knowledge") and args.domain_knowledge) else ""

    print(f"Full verification pipeline for: {args.source}")
    print(f"Driver: {args.driver}")
    print(f"Artifact dir: {config.artifact_dir}")
    if config.skip_refinement:
        print("Mode: FilteringOnly (skip_refinement=True) — RQ3 ablation baseline")
    if config.preprocess:
        print(f"Include dirs: {config.include_dirs}")
    if config.enable_realism_check:
        thinking_tag = " (extended thinking)" if config.enable_realism_thinking else ""
        print(f"Realism check: enabled{thinking_tag}")

    pipeline = AMCPipeline(config)
    bug_reports = pipeline.run(
        source_file=args.source,
        driver_name=args.driver,
        domain_knowledge=domain_knowledge,
    )

    print(f"\n=== Results ===")
    if not bug_reports:
        print("No bugs confirmed.")
    else:
        print(f"Confirmed bugs: {len(bug_reports)}")
        for report in bug_reports:
            print(f"\n  [{report.bug_type.upper()}] {report.function_name}")
            print(f"    Property: {report.violated_property}")
            print(f"    Confidence: {report.confidence}")
            if report.call_chain:
                print(f"    Call chain: {' → '.join(report.call_chain)}")

    # Print summary from reporter
    summary = pipeline.reporter.generate_summary(args.driver)
    print(f"\n{summary}")

    return 0


def _cmd_baseline(args: argparse.Namespace) -> int:
    """Run CBMC-alone baseline on a C source file (no LLM, no spec generation)."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.evaluation.baselines import CBMCAloneBaseline

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output

    store = ArtifactStore(config.artifact_dir)
    baseline = CBMCAloneBaseline()

    print(f"CBMC-alone baseline for: {args.source}")
    print(f"Driver:       {args.driver}")
    print(f"Artifact dir: {config.artifact_dir}")

    result = baseline.run(
        source_file=args.source,
        driver_name=args.driver,
        config=config,
        store=store,
    )

    if result.error:
        print(f"\nWarning: {result.error}", file=sys.stderr)

    print(f"\n=== CBMC-Alone Baseline Results ===")
    if not result.bugs_found:
        print("No bugs found by CBMC alone.")
    else:
        print(f"Findings ({len(result.bugs_found)} total):")
        for bug in result.bugs_found:
            print(f"  {bug}")

    print(f"\nRuntime: {result.runtime_seconds:.1f}s")
    return 0


def _cmd_ablation_baseline(args: argparse.Namespace) -> int:
    """Run AMC-ablation baseline: bottom-up spec generation (no caller context)."""
    from bmc_agent.config import Config
    from bmc_agent.evaluation.baselines import AMCAblationBaseline

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output
    _apply_model_arg(config, args)

    baseline = AMCAblationBaseline()

    print(f"AMC-ablation baseline for: {args.source}")
    print(f"Driver:       {args.driver}")
    print(f"Artifact dir: {config.artifact_dir}")
    print(f"Model:        {config.llm_model}")
    print("Mode: bottom-up spec generation (no caller context)")

    result = baseline.run(
        source_file=args.source,
        driver_name=args.driver,
        config=config,
    )

    if result.error:
        print(f"\nWarning: {result.error}", file=sys.stderr)

    print(f"\n=== AMC-Ablation Baseline Results ===")
    if not result.bugs_found:
        print("No bugs found.")
    else:
        print(f"Findings ({len(result.bugs_found)} total):")
        for bug in result.bugs_found:
            print(f"  {bug}")

    print(f"\nRuntime: {result.runtime_seconds:.1f}s")
    return 0


def _cmd_verify_dir(args: argparse.Namespace) -> int:
    """Run the full pipeline on every .c file in a directory."""
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output
    if getattr(args, "skip_refinement", False):
        config.skip_refinement = True
    if getattr(args, "enable_dynamic_validation", False):
        config.enable_dynamic_validation = True
    if getattr(args, "enable_realism_check", False):
        config.enable_realism_check = True
    if getattr(args, "enable_realism_thinking", False):
        config.enable_realism_thinking = True
    if getattr(args, "enable_flag_selection", False):
        config.enable_flag_selection = True
    if getattr(args, "threat_model", None):
        config.threat_model = args.threat_model
    _apply_model_arg(config, args)

    include_dirs = args.include_dir or []

    domain_knowledge = _resolve_domain_knowledge(args.domain_knowledge) if args.domain_knowledge else ""

    exclude = args.exclude or []

    print(f"Verifying directory: {args.source_dir}")
    print(f"Driver prefix:       {args.driver}")
    print(f"Include dirs:        {include_dirs or '(none)'}")
    print(f"Artifact dir:        {config.artifact_dir}")
    if exclude:
        print(f"Excluded patterns:   {exclude}")

    pipeline = AMCPipeline(config)
    results = pipeline.run_directory(
        source_dir=args.source_dir,
        driver_name=args.driver,
        include_dirs=include_dirs,
        domain_knowledge=domain_knowledge,
        exclude_patterns=exclude,
    )

    print(f"\n=== Summary ===")
    total = sum(len(v) for v in results.values())
    print(f"Files processed: {len(results)}")
    print(f"Total bugs confirmed: {total}")
    for fname, bugs in sorted(results.items()):
        print(f"  {fname}: {len(bugs)} bug(s)")
        for report in bugs:
            print(f"    [{report.bug_type.upper()}] {report.function_name} — {report.violated_property}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmc-agent",
        description="BMC-Agent: Agentic Model Checking for C programs (part of AProver)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # Shared --model argument added to commands that call the LLM
    def _add_model_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--model",
            default="",
            metavar="MODEL_ID",
            help=(
                "Override the LLM model (e.g. claude-opus-4-7, claude-sonnet-4-6). "
                "Defaults to BMC_AGENT_LLM_MODEL env var or claude-sonnet-4-6."
            ),
        )

    # --- generate ---
    gen = subparsers.add_parser(
        "generate",
        help="Generate specs for all functions in a C source file (Phase 1)",
    )
    gen.add_argument("--source", required=True, help="Path to a C (.c/.h) or Rust (.rs) source file")
    gen.add_argument("--driver", required=True, help="Driver name (used for artifact storage)")
    gen.add_argument("--output", default="artifacts", help="Artifact output directory")
    gen.add_argument(
        "--domain-knowledge",
        default="",
        metavar="TEXT_OR_FILE",
        help="Domain knowledge string or path to a file containing domain knowledge",
    )
    _add_model_arg(gen)
    gen.set_defaults(func=_cmd_generate)

    # --- check ---
    chk = subparsers.add_parser(
        "check",
        help="Run BMC on previously generated specs (Phase 2)",
    )
    chk.add_argument("--source", required=True, help="Path to a C (.c/.h) or Rust (.rs) source file")
    chk.add_argument("--driver", required=True, help="Driver name")
    chk.add_argument("--output", default="artifacts", help="Artifact directory")
    chk.add_argument("--function", default="", help="Specific function to check (optional)")
    chk.set_defaults(func=_cmd_check)

    # --- verify ---
    ver = subparsers.add_parser(
        "verify",
        help="Run full pipeline: generate + check + validate (Phase 3)",
    )
    ver.add_argument("--source", required=True, help="Path to a C (.c/.h) or Rust (.rs) source file")
    ver.add_argument("--driver", required=True, help="Driver name")
    ver.add_argument("--output", default="artifacts", help="Artifact directory")
    ver.add_argument(
        "--domain-knowledge",
        default="",
        metavar="TEXT_OR_FILE",
        help="Domain knowledge string or path to a file containing domain knowledge",
    )
    ver.add_argument(
        "--include-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Add an include directory for C preprocessing (repeatable)",
    )
    ver.add_argument(
        "--skip-refinement",
        action="store_true",
        default=False,
        help="FilteringOnly mode: classify counterexamples but skip spec update and caller re-queue (RQ3 ablation)",
    )
    ver.add_argument(
        "--enable-dynamic-validation",
        action="store_true",
        default=False,
        help="Stage 3: compile and run a GCC harness to confirm bugs at runtime (confirmed_dynamic tier)",
    )
    ver.add_argument(
        "--enable-realism-check",
        action="store_true",
        default=False,
        help="Run LLM realism audit on every REAL_BUG finding to reduce false positives",
    )
    ver.add_argument(
        "--enable-realism-thinking",
        action="store_true",
        default=False,
        help="Use extended thinking in the realism checker (higher quality, slower, implies --enable-realism-check)",
    )
    ver.add_argument(
        "--enable-flag-selection",
        action="store_true",
        default=False,
        help="Phase 1.5: LLM selects per-function CBMC flags (unsigned/signed overflow, conversion, pointer overflow)",
    )
    ver.add_argument(
        "--threat-model",
        choices=["security", "safety", "functional"],
        default="security",
        help="Threat model: shapes CBMC baseline flags, spec prompts, and realism context (default: security)",
    )
    _add_model_arg(ver)
    ver.set_defaults(func=_cmd_verify)

    # --- baseline ---
    bl = subparsers.add_parser(
        "baseline",
        help="Run CBMC-alone baseline on a C source file (no LLM, no spec generation)",
    )
    bl.add_argument("--source", required=True, help="Path to a C (.c/.h) or Rust (.rs) source file")
    bl.add_argument("--driver", required=True, help="Driver name (used for artifact storage)")
    bl.add_argument("--output", default="artifacts", help="Artifact output directory")
    bl.set_defaults(func=_cmd_baseline)

    # --- ablation-baseline ---
    ab = subparsers.add_parser(
        "ablation-baseline",
        help="Run AMC-ablation baseline: bottom-up spec generation without caller context",
    )
    ab.add_argument("--source", required=True, help="Path to a C (.c/.h) or Rust (.rs) source file")
    ab.add_argument("--driver", required=True, help="Driver name (used for artifact storage)")
    ab.add_argument("--output", default="artifacts", help="Artifact output directory")
    _add_model_arg(ab)
    ab.set_defaults(func=_cmd_ablation_baseline)

    # --- verify-dir ---
    vd = subparsers.add_parser(
        "verify-dir",
        help="Run full pipeline on every .c file in a directory",
    )
    vd.add_argument("--source-dir", required=True, help="Directory containing .c files")
    vd.add_argument("--driver", required=True, help="Driver name prefix")
    vd.add_argument("--output", default="artifacts", help="Artifact directory")
    vd.add_argument(
        "--include-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Add an include directory for C preprocessing (repeatable)",
    )
    vd.add_argument(
        "--domain-knowledge",
        default="",
        metavar="TEXT_OR_FILE",
        help="Domain knowledge string or path to a file",
    )
    vd.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Glob pattern of filenames to skip (repeatable)",
    )
    vd.add_argument(
        "--skip-refinement",
        action="store_true",
        default=False,
        help="FilteringOnly mode: classify counterexamples but skip spec update and caller re-queue (RQ3 ablation)",
    )
    vd.add_argument(
        "--enable-dynamic-validation",
        action="store_true",
        default=False,
        help="Stage 3: compile and run a GCC harness to confirm bugs at runtime (confirmed_dynamic tier)",
    )
    vd.add_argument(
        "--enable-realism-check",
        action="store_true",
        default=False,
        help="Run LLM realism audit on every REAL_BUG finding to reduce false positives",
    )
    vd.add_argument(
        "--enable-realism-thinking",
        action="store_true",
        default=False,
        help="Use extended thinking in the realism checker (higher quality, slower)",
    )
    vd.add_argument(
        "--enable-flag-selection",
        action="store_true",
        default=False,
        help="Phase 1.5: LLM selects per-function CBMC flags (unsigned/signed overflow, conversion, pointer overflow)",
    )
    vd.add_argument(
        "--threat-model",
        choices=["security", "safety", "functional"],
        default="security",
        help="Threat model: shapes CBMC baseline flags, spec prompts, and realism context (default: security)",
    )
    _add_model_arg(vd)
    vd.set_defaults(func=_cmd_verify_dir)

    # --- eval ---
    ev = subparsers.add_parser(
        "eval",
        help="Run AMC + baselines on a corpus of C programs (Phase 4)",
    )
    ev.add_argument("--corpus", required=True, help="Path to corpus directory")
    ev.add_argument("--output", default="artifacts/eval", help="Output directory for results")
    ev.add_argument(
        "--baselines",
        action="store_true",
        default=False,
        help="Also run CBMC-alone and AMC-ablation baselines",
    )
    ev.set_defaults(func=_cmd_eval)

    # --- report ---
    rpt = subparsers.add_parser(
        "report",
        help="Generate a summary report from evaluation artifacts (Phase 4)",
    )
    rpt.add_argument("--eval-dir", required=True, help="Directory produced by 'amc eval'")
    rpt.add_argument("--output", default="", help="Output file path (default: stdout)")
    rpt.set_defaults(func=_cmd_report)

    # --- corpus ---
    corpus_p = subparsers.add_parser(
        "corpus",
        help="Corpus management commands (Phase 4)",
    )
    corpus_sub = corpus_p.add_subparsers(dest="corpus_command", metavar="SUBCOMMAND")
    corpus_sub.required = True

    cg = corpus_sub.add_parser(
        "generate",
        help="Use LLM to generate synthetic C programs",
    )
    cg.add_argument("--output", required=True, help="Output directory for generated corpus")
    cg.add_argument("--count", type=int, default=5, help="Number of programs to generate")
    cg.set_defaults(func=_cmd_corpus_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
