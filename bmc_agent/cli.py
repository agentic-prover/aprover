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
import re
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


def _resolve_threat_model_context(value: "str | None") -> str:
    """Resolve a ``--threat-model-context`` argument to its text. Accepts either
    a path to a file (read it) or an inline string (used verbatim). Returns "" for
    a missing/empty value. Keeps the CLI ergonomic: usually a path, occasionally
    a short inline note.
    """
    if not value:
        return ""
    try:
        from pathlib import Path as _P
        p = _P(value)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass
    return value.strip()


def _apply_threat_model_context(config: "object", args: argparse.Namespace) -> None:
    """Apply ``--threat-model-context`` (path or inline text) onto the config."""
    note = _resolve_threat_model_context(getattr(args, "threat_model_context", None))
    if note:
        config.threat_model_context = note  # type: ignore[attr-defined]


def _apply_provider_args(config: "object", args: argparse.Namespace) -> None:
    """Apply the agentic / provider-routing flags.

    These are CLI conveniences over the existing env-var routing
    (``BMC_AGENT_LLM_PROVIDER`` and ``BMC_AGENT_LLM_<ROLE>_PROVIDER``):

      --agentic                  GENERAL agentic stack: every agent role becomes
                                 an investigating agent, but each is instantiated
                                 by WHATEVER backend its routing says — a per-role
                                 BMC_AGENT_LLM_<ROLE>_PROVIDER override, else the
                                 global default provider (--provider / env), else
                                 the auto-resolved provider. So roles can be a mix
                                 of API / claude-code / codex / etc. Turns on the
                                 soundness gate, agentic harness repair, split
                                 spec-gen and component gating — WITHOUT forcing
                                 any particular backend.
      --agentic-claude-code      Like --agentic, but FORCES every agent role onto
                                 the local Claude Code CLI provider (the previous
                                 --agentic behaviour). A per-role env override
                                 still wins, so individual agents can be repointed.
      --provider X               sets the global default provider for every role.
      --specs-via-claude-code    routes ONLY the spec_gen + refinement roles to
                                 the Claude Code CLI provider (your local
                                 ``claude`` login; no API key), leaving every
                                 other role on the global default.
      --claude-code-agentic      lets the claude-code provider use read-only
                                 tools (Read/Grep/Glob) to explore the source
                                 tree while drafting/refining, instead of a
                                 one-shot text completion.

    Merges (rather than replaces) any role override already present from env so
    explicit ``BMC_AGENT_LLM_*`` settings still compose.
    """
    agentic = getattr(args, "agentic", False)
    agentic_cc = getattr(args, "agentic_claude_code", False)
    agentic_refine = getattr(args, "agentic_refine", False)

    provider = getattr(args, "provider", "") or ""
    if provider:
        config.llm_provider = provider  # type: ignore[attr-defined]

    # Every LLM-driven agent role. Under --agentic ALL of them default to the
    # Claude Code agent (strongest, code-reading). The conventional core (CBMC,
    # deterministic harness translation, compile+run dynamic validation) has no
    # LLM and is unaffected.
    ALL_AGENT_ROLES = (
        "spec_gen", "refinement", "realism", "triage",
        "disagreement_diagnose", "feedback_distill", "classifier",
        "dynamic_repro", "dynval_triage", "cbmc_driver",
    )
    # Which roles are FORCED onto the Claude Code CLI:
    #   --agentic-claude-code -> EVERY agent role (the "all claude-code" preset)
    #   --agentic-refine      -> refinement only (LEAN; rest stay on the default
    #                            provider — recommended for batches)
    #   --specs-via-claude-code -> spec_gen + refinement (explicit)
    #   --agentic (general)   -> forces NOTHING; each role keeps its per-role /
    #                            default / resolved provider (API, claude-code, …).
    cc_roles: set[str] = set()
    if agentic_cc:
        cc_roles |= set(ALL_AGENT_ROLES)
    if getattr(args, "specs_via_claude_code", False):
        cc_roles |= {"spec_gen", "refinement"}
    if agentic_refine:
        cc_roles |= {"refinement"}

    want_cc_tools = (agentic or agentic_cc or agentic_refine
                     or getattr(args, "claude_code_agentic", False))

    if cc_roles:
        overrides = getattr(config, "llm_role_overrides", None)
        if overrides is None:
            overrides = {}
            config.llm_role_overrides = overrides  # type: ignore[attr-defined]
        for role in sorted(cc_roles):
            merged = dict(overrides.get(role, {}))
            # The flag is the DEFAULT; an explicit per-agent env override
            # (BMC_AGENT_LLM_<ROLE>_PROVIDER=...) WINS so any agent can be
            # re-pointed to a cheaper/faster LLM.
            if not merged.get("provider"):
                merged["provider"] = "claude-code"
            overrides[role] = merged

    if want_cc_tools:
        config.claude_code_agentic = True  # type: ignore[attr-defined]
        # Scope the read-only file access to the source tree: the source file's
        # directory + any --include-dir. cwd is always readable regardless.
        dirs: list[str] = list(getattr(config, "claude_code_add_dirs", None) or [])
        src = getattr(args, "source", "") or ""
        if src:
            dirs.append(str(Path(src).resolve().parent))
        for inc in getattr(args, "include_dir", None) or []:
            dirs.append(str(Path(inc).resolve()))
        # de-dupe, preserve order
        seen: set[str] = set()
        config.claude_code_add_dirs = [d for d in dirs if not (d in seen or seen.add(d))]  # type: ignore[attr-defined]

    if agentic or agentic_cc or agentic_refine:
        # The verify/verify-dir-only guards. Harmless on commands that don't use
        # them (generate, etc.) — the fields just go unread.
        config.enable_soundness_gate = True  # type: ignore[attr-defined]
        config.enable_agentic_harness_repair = True  # type: ignore[attr-defined]
        # Split spec gen: contract-only precondition (pass 2 runs on whatever
        # provider spec_gen is on — agentic under --agentic, flat under
        # --agentic-refine; the contract POLICY applies either way).
        config.enable_split_spec_gen = True  # type: ignore[attr-defined]

    if agentic or agentic_cc:
        # Component gating (both agentic presets). The CLASSIFIER stays ON: it drives
        # the spurious→refinement→soundness-gate loop (the agentic centerpiece),
        # so it must NOT be disabled by default. The dynamic reproducer is ON.
        # Each layer is independently overridable; an explicit --enable-* / --no-*
        # wins via these arg guards, regardless of flag order.
        if not getattr(args, "no_dynamic_validation", False):
            config.enable_dynamic_validation = True  # type: ignore[attr-defined]
        # Realism: keep a LIGHTWEIGHT (single-LLM-call, non-tool) realism check ON
        # by default — it cheaply filters obvious modelling artifacts and is the
        # one judgment layer worth its cost. The expensive multi-turn TOOL-USE
        # augmentation is OPT-IN via --enable-realism-tools. Disable realism
        # entirely with --no-realism-check. Set authoritatively here (not via
        # `if not`) so the resolution is independent of arg-handling order.
        config.enable_realism_check = not getattr(args, "no_realism_check", False)  # type: ignore[attr-defined]
        config.enable_realism_tools = bool(getattr(args, "enable_realism_tools", False))  # type: ignore[attr-defined]
        # Triage (severity tiering) stays OFF by default; opt in with --enable-triage.
        if not getattr(args, "enable_triage", False):
            config.enable_phase_3e_triage = False  # type: ignore[attr-defined]
        # Agentic CBMC driver: the agent decides how to configure CBMC by reading
        # the code — per-function checks + unwind (flag selector) and which callees
        # to inline vs stub (inlining advisor). Both become code-reading agents
        # under --agentic (their system prompts get the investigation framing) and
        # CBMC keeps --unwinding-assertions on, so an agent-chosen unwind that's
        # too low is FLAGGED, not silently unsound.
        if not getattr(args, "no_flag_selection", False):
            config.enable_flag_selection = True  # type: ignore[attr-defined]
        if not getattr(args, "no_inlining_advisor", False):
            config.enable_inlining_advisor = True  # type: ignore[attr-defined]


def _print_ai_layers(config) -> None:
    """Print the active AI layers so the operator can see what's running.

    The default-on AI layers are listed here; --no-* / --minimal flags
    individually disable. Visible at startup so users can audit what
    a given run actually exercises.
    """
    layers = [
        ("realism check",        getattr(config, "enable_realism_check", False)),
        ("dynamic validation",   getattr(config, "enable_dynamic_validation", False)),
        ("flag selection",       getattr(config, "enable_flag_selection", False)),
        ("feedback loop",        getattr(config, "enable_feedback_loop", False)),
        ("spec refiner",         getattr(config, "enable_spec_refiner", False)),
        ("inlining advisor",     getattr(config, "enable_inlining_advisor", False)),
        ("spec-gen tools (v2.2)",getattr(config, "enable_spec_gen_tools", False)),
        ("realism tools",        getattr(config, "enable_realism_tools", False)),
    ]
    on  = [n for n, v in layers if v]
    off = [n for n, v in layers if not v]
    if on:
        thinking_tag = (
            " (extended thinking)"
            if getattr(config, "enable_realism_thinking", False)
            and getattr(config, "enable_realism_check", False)
            else ""
        )
        print(f"AI layers ON:  {', '.join(on)}{thinking_tag}")
    if off:
        print(f"AI layers OFF: {', '.join(off)}")


def _cmd_generate(args: argparse.Namespace) -> int:
    """Phase 1: Generate specs for all functions in a C source file."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.spec_generator import SpecGenerator

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output
    if getattr(args, "include_dir", None):
        config.include_dirs = args.include_dir
        config.preprocess = True
    if getattr(args, "defines", None):
        config.cbmc_defines = list(args.defines)
    _apply_model_arg(config, args)
    _apply_provider_args(config, args)

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
    # Thread build include-dirs / defines into CBMC so the per-function harness
    # can preprocess build-config headers (config.h, etc.) — mirrors `verify`.
    # Without this, `check --function X` fails on missing config.h.
    if getattr(args, "include_dir", None):
        config.include_dirs = args.include_dir
        config.preprocess = True
    if getattr(args, "defines", None):
        config.cbmc_defines = list(args.defines)

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
        err_lower = (verdict.error or "").lower()
        if verdict.error and "not found" in err_lower:
            status = "SKIPPED (verifier not installed)"
            error_count += 1
        elif verdict.error and ("unwind" in err_lower or "timed out" in err_lower):
            # Inconclusive: BMC bound exhausted or timeout — not a found
            # CEx, not a verified property either.
            status = f"INCONCLUSIVE ({verdict.error})"
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


def _run_standalone(args: argparse.Namespace, config: "object") -> int:
    """Standalone whole-program verification (see bmc_agent.standalone)."""
    from bmc_agent.standalone import verify_standalone

    entry = getattr(args, "entry", None) or "main"
    unwind = int(getattr(args, "standalone_unwind", None) or 64)
    if getattr(args, "include_dir", None):
        config.include_dirs = args.include_dir   # type: ignore[attr-defined]
        config.preprocess = True                 # type: ignore[attr-defined]
    if getattr(args, "defines", None):
        config.cbmc_defines = list(args.defines)  # type: ignore[attr-defined]

    print(f"Standalone whole-program verification: {args.source}")
    print(f"Entry: {entry}   unwind: {unwind}")
    print("Checks: bounds, pointer, signed-overflow, pointer-overflow, "
          "div-by-zero, unwinding-assertions")

    result, n_acsl = verify_standalone(
        args.source, config, entry=entry, unwind=unwind,
        timeout=int(getattr(config, "cbmc_timeout", 0) or 300),
    )

    print("\n=== Standalone result ===")
    if result.error and not result.counterexamples:
        print(f"INVALID / CBMC ERROR: {result.error}")
        return 2
    if result.verified:
        extra = f" (incl. {n_acsl} //@ assert)" if n_acsl else ""
        print(f"VERIFICATION SUCCESSFUL — the program as written is safe{extra}.")
        return 0
    print(f"VERIFICATION FAILED — {len(result.counterexamples)} property violation(s):")
    for ce in result.counterexamples[:25]:
        loc = ce.failure_location or {}
        where = f"{loc.get('file','?')}:{loc.get('line','?')}" if loc else ""
        print(f"  - {ce.failing_property or '?'}  {where}  {ce.description}".rstrip())
    return 1


def _run_assert_synth(args: argparse.Namespace, config: "object") -> int:
    """Assertion-driven spec synthesis (see bmc_agent.assert_driven_specs)."""
    from bmc_agent.assert_driven_specs import synthesize
    from bmc_agent.llm import LLMClient

    entry = getattr(args, "entry", None) or "main"
    if getattr(args, "include_dir", None):
        config.include_dirs = args.include_dir   # type: ignore[attr-defined]
        config.preprocess = True                 # type: ignore[attr-defined]
    if getattr(args, "defines", None):
        config.cbmc_defines = list(args.defines)  # type: ignore[attr-defined]

    print(f"Assertion-driven spec synthesis: {args.source}")
    print(f"Entry: {entry}   (refine postconditions until //@ asserts hold; "
          "with no goal, mine + soundly verify a contract)")
    r = synthesize(args.source, config, LLMClient(config), entry=entry)
    if getattr(r, "entry", "") and r.entry != entry:   # auto-resolved to the goal-bearing fn
        print(f"  (entry auto-resolved to '{r.entry}' — the function containing the asserts)")
        entry = r.entry

    # --- verification-gated overflow rigor (Frama-C oracle) ---------------------
    # A synthesized `ensures \result == E` with signed arithmetic is only sound for
    # mathematical integers — the C body computing E can overflow (UB). Add the
    # no-overflow precondition `MIN <= E <= MAX` and re-verify with RTE ON; adopt it
    # (into the displayed + confirmed contract) ONLY if WP still discharges every
    # goal. Otherwise fall back to the math-int (RTE-off) contract below. This makes
    # the contract path overflow-rigorous wherever it provably can be.
    _ovf_used = False
    _ovf_wp = None
    if (r.ok and getattr(config, "oracle", "cbmc") == "frama-c"
            and getattr(args, "overflow_rigor", True)):
        try:
            from bmc_agent.frama_c import (insert_contract_acsl, run_wp,
                                           function_assigns_clause,
                                           function_frame_precondition)
            from bmc_agent.assert_driven_specs import (
                _conjoin_precondition, overflow_preconditions,
                overflow_preconditions_from_body)
            _src0 = open(str(args.source)).read()
            _fc = getattr(config, "frama_c_path", "frama-c")
            ovf = overflow_preconditions(_src0, r.postconditions or {})
            # Postcondition-shape extraction (`result == E`) misses arithmetic
            # buried in guards/comparisons (e.g. `a+b>c`). Under --math-ints the
            # functional proof already assumes no overflow, so ALSO enumerate
            # every body overflow site via RTE and bound it — making the math-int
            # contract machine-int sound wherever WP can still discharge it.
            if getattr(config, "math_ints", False):
                body_ovf = overflow_preconditions_from_body(
                    _src0, list((r.postconditions or {}).keys()), frama_c_path=_fc)
                for fn, term in body_ovf.items():
                    if fn not in ovf:
                        ovf[fn] = term
                    elif term not in ovf[fn]:
                        ovf[fn] = f"({ovf[fn]}) && ({term})"
            if ovf:
                _assigns0 = {fn: function_assigns_clause(_src0, fn)
                             for fn in (r.postconditions or {})}
                aug_pre = dict(r.preconditions or {})
                for fn, term in ovf.items():
                    aug_pre[fn] = _conjoin_precondition(aug_pre.get(fn, "true"), term)
                for fn, assigns in _assigns0.items():
                    frame_pre = function_frame_precondition(_src0, fn, assigns)
                    if frame_pre:
                        aug_pre[fn] = _conjoin_precondition(aug_pre.get(fn, "true"), frame_pre)
                annotated = "#include <limits.h>\n" + _src0
                for fn, p in (r.postconditions or {}).items():
                    annotated = insert_contract_acsl(
                        annotated, fn, requires=aug_pre.get(fn, "true"), ensures=p,
                        assigns=_assigns0.get(fn, ""))
                wp = run_wp(annotated, frama_c_path=getattr(config, "frama_c_path", "frama-c"),
                            rte=True, exclude_terminates=True)
                if wp.available and wp.n_total and wp.n_proved == wp.n_total:
                    r.preconditions = aug_pre   # adopt the rigorous contract for display
                    _ovf_used, _ovf_wp = True, wp
                    print("overflow-rigor: added no-overflow precondition(s) "
                          f"for {', '.join(ovf)} — re-verified with RTE on")
        except Exception as exc:   # never let the rigor attempt mask the result
            print(f"overflow-rigor: skipped ({exc}); math-int contract stands.")

    # Render the synthesized contracts in ACSL (the benchmark output format) and
    # fold them into the artifact alongside the DSL form.
    from bmc_agent.acsl import contract_to_acsl
    from bmc_agent.assert_driven_specs import _conjoin_precondition
    from bmc_agent.frama_c import function_assigns_clause, function_frame_precondition
    _src_text = open(str(args.source)).read()
    fn_assigns = {fn: function_assigns_clause(_src_text, fn)
                  for fn in (r.postconditions or {})}
    fn_requires = {}
    for fn in (r.postconditions or {}):
        req = (r.preconditions or {}).get(fn, "true")
        frame_pre = function_frame_precondition(_src_text, fn, fn_assigns.get(fn, ""))
        if frame_pre:
            req = _conjoin_precondition(req, frame_pre)
        fn_requires[fn] = req

    if getattr(config, "oracle", "cbmc") == "frama-c" and (r.postconditions or {}) and not _ovf_used:
        from bmc_agent.assert_driven_specs import _split_conjuncts
        from bmc_agent.frama_c import insert_contract_block, run_wp

        def _annotated_for(posts):
            annotated_src = _src_text
            for _fn, _p in (posts or {}).items():
                _block = contract_to_acsl(fn_requires.get(_fn, "true"), _p,
                                          assigns=fn_assigns.get(_fn, ""))
                annotated_src = insert_contract_block(
                    annotated_src, _fn, _block.rstrip() + "\n")
            return annotated_src

        math_ints = bool(getattr(config, "math_ints", False))
        posts = dict(r.postconditions or {})
        wp0 = run_wp(_annotated_for(posts),
                     frama_c_path=getattr(config, "frama_c_path", "frama-c"),
                     rte=not math_ints, exclude_terminates=True)
        if wp0.available and wp0.n_total and wp0.n_proved == wp0.n_total:
            if not r.ok:
                r.ok = True
                r.failing_asserts = []
                r.note = ("Frama-C/WP confirmed the rendered ACSL contract "
                          "(engine did not prove it)")
        if wp0.available and wp0.n_total and wp0.n_proved < wp0.n_total:
            best_gap = wp0.n_total - wp0.n_proved
            best_posts = dict(posts)
            pruned = []
            changed = True
            trials = 0
            max_trials = 24

            def _prune_priority(item):
                _idx, _clause = item
                clause = _clause.strip()
                if re.fullmatch(r"\\old\s*\([^)]+\)\s*==\s*-?\d+\b", clause):
                    return 0
                if re.fullmatch(r"[A-Za-z_]\w*\s*==\s*-?\d+\b", clause):
                    return 1
                if "\\old" in clause:
                    return 2
                return 3

            while changed and best_gap > 0:
                changed = False
                for fn in list(posts):
                    clauses = _split_conjuncts(posts.get(fn, ""))
                    if len(clauses) <= 1:
                        continue
                    ordered = sorted(enumerate(list(clauses)), key=_prune_priority)
                    for idx, clause in ordered:
                        if trials >= max_trials:
                            break
                        trials += 1
                        trial_clauses = clauses[:idx] + clauses[idx + 1:]
                        trial_posts = dict(posts)
                        trial_posts[fn] = " && ".join(trial_clauses) or "true"
                        wp = run_wp(
                            _annotated_for(trial_posts),
                            frama_c_path=getattr(config, "frama_c_path", "frama-c"),
                            rte=not math_ints,
                            exclude_terminates=True,
                        )
                        if not (wp.available and wp.n_total):
                            continue
                        gap = wp.n_total - wp.n_proved
                        if gap < best_gap:
                            posts, best_gap = trial_posts, gap
                            best_posts = dict(trial_posts)
                            pruned.append(f"{fn}: {clause}")
                            changed = True
                            break
                    if trials >= max_trials:
                        break
                    if changed:
                        break
            if pruned and best_gap == 0:
                r.postconditions = best_posts
                was_ok = r.ok
                r.ok = True
                r.failing_asserts = []
                msg = "WP-pruned unproved postcondition conjunct(s): " + "; ".join(pruned)
                if was_ok:
                    r.note += "; " + msg
                else:
                    r.note = "Frama-C/WP confirmed the pruned ACSL contract; " + msg

    acsl_blocks = {}
    for fn, p in (r.postconditions or {}).items():
        block = contract_to_acsl(fn_requires.get(fn, "true"), p,
                                 assigns=fn_assigns.get(fn, ""))
        if block:
            acsl_blocks[fn] = block

    # Persist the synthesized contracts + per-assert status to a stable artifact.
    import json as _json, os as _os
    out_dir = getattr(config, "artifact_dir", None) or "."
    _os.makedirs(out_dir, exist_ok=True)
    _base = _os.path.splitext(_os.path.basename(str(args.source)))[0] or "out"
    out_path = _os.path.join(out_dir, f"synthesized_specs_{_base}.json")
    payload = {
        "source": str(args.source),
        "entry": entry,
        "satisfied": bool(r.ok),
        "na": bool(getattr(r, "no_goals", False)),   # no proof target → N/A, not pass/fail
        "iterations": r.iterations,
        "asserts": list(r.asserts or []),
        "failing_asserts": list(r.failing_asserts or []),
        "synthesized_specs": {
            fn: {"requires": fn_requires.get(fn, "true"), "ensures": p}
            for fn, p in (r.postconditions or {}).items()
        },
        "note": r.note,
    }
    try:
        with open(out_path, "w") as fh:
            _json.dump(payload, fh, indent=2)
    except OSError as exc:
        print(f"(warning: could not write {out_path}: {exc})")

    payload["acsl"] = acsl_blocks
    for fn, a in fn_assigns.items():           # record the frame in the DSL artifact too
        if a:
            payload["synthesized_specs"].get(fn, {})["assigns"] = a
    try:
        with open(out_path, "w") as fh:
            _json.dump(payload, fh, indent=2)
    except OSError:
        pass

    from bmc_agent.dsl_to_cbmc import fully_parenthesize
    print("\n=== Synthesized specs (DSL) ===")
    for fn, p in (r.postconditions or {}).items():
        # Display with explicit &&/|| grouping so the printed DSL is read the
        # same way the renderers (ACSL/CBMC) interpret it — no precedence guesswork.
        req = fn_requires.get(fn, "true")
        print(f"  {fn}:  requires {fully_parenthesize(req)}")
        if fn_assigns.get(fn):
            print(f"  {' ' * len(fn)}   assigns  {fn_assigns[fn]}")
        print(f"  {' ' * len(fn)}   ensures  {fully_parenthesize(p)}")
    print("\n=== Synthesized specs (ACSL) ===")
    if _ovf_used:
        print("#include <limits.h>")
    for fn, block in acsl_blocks.items():
        print(f"// contract for {fn}")
        print(block)
    print(f"\nasserts: {len(r.asserts)}   iterations: {r.iterations}")
    print(f"written: {out_path}")

    # --oracle frama-c: deductively CONFIRM the CBMC-synthesized contract with
    # Frama-C/WP. The gen-refine loop above (CBMC) does the synthesis; WP then
    # discharges the contract + the //@ assert goals over mathematical integers.
    # Degrades cleanly when frama-c is absent (the CBMC verdict still stands).
    if r.ok and _ovf_used and _ovf_wp is not None:
        # Already deductively confirmed above WITH RTE on (overflow checked).
        print(f"Frama-C/WP: CONFIRMED — proved {_ovf_wp.n_proved}/{_ovf_wp.n_total} "
              "goals (RTE on; signed overflow checked).")
    elif r.ok and getattr(config, "oracle", "cbmc") == "frama-c":
        from bmc_agent.frama_c import insert_contract_acsl, run_wp
        try:
            annotated = _src_text
            for fn, p in (r.postconditions or {}).items():
                # A frame clause is ESSENTIAL for modular WP: without `assigns
                # \nothing` on a pure callee, WP assumes the call may clobber any
                # memory (e.g. add(&a,&b) "could" change a/b), so post-call asserts
                # about the caller's variables fail — even though CBMC, which inlines,
                # proves them. fn_assigns (computed above) emits \nothing for callees
                # with no escaping store.
                annotated = insert_contract_acsl(
                    annotated, fn,
                    requires=fn_requires.get(fn, "true"), ensures=p,
                    assigns=fn_assigns.get(fn, ""))
            # Match the loop oracle's semantics: partial correctness (no @terminates)
            # and mathematical integers under --math-ints (no overflow RTE).
            math_ints = bool(getattr(config, "math_ints", False))
            wp = run_wp(annotated, frama_c_path=getattr(config, "frama_c_path", "frama-c"),
                        rte=not math_ints, exclude_terminates=True)
            if not wp.available:
                print(f"Frama-C/WP: requested but unavailable ({wp.error}); contract "
                      "validated by CBMC only — install frama-c + alt-ergo for WP confirmation.")
            elif wp.n_total and wp.n_proved == wp.n_total:
                print(f"Frama-C/WP: CONFIRMED — proved {wp.n_proved}/{wp.n_total} goals.")
            else:
                print(f"Frama-C/WP: proved {wp.n_proved}/{wp.n_total} goals; "
                      f"unproved: {', '.join(wp.unproved) or '?'}")
        except Exception as exc:  # never let the WP confirmation mask the CBMC result
            print(f"Frama-C/WP: confirmation step errored ({exc}); CBMC verdict stands.")

    if getattr(r, "no_goals", False):
        print(f"RESULT: N/A — {r.note}.")
        print("  (No assertion target, and no non-trivial contract could be mined "
              "from a function body. This is NOT a pass — add a //@ assert / assert / "
              "__VERIFIER_assert stating the expected result to make it a real target.)")
        return 2
    if r.ok:
        if r.asserts:
            print("RESULT: SATISFIED — all //@ asserts are provable from the synthesized specs.")
        else:
            # Goal-free mining: a non-trivial contract was synthesized AND the
            # implementation was proved to satisfy it (sound). Non-vacuous.
            print("RESULT: SATISFIED — synthesized function contract(s) from the body "
                  "and proved the implementation satisfies them (no explicit goal needed).")
        return 0
    print(f"RESULT: NOT SATISFIED — {r.note}")
    for a in (r.failing_asserts or []):
        print(f"  unprovable: {a}")
    return 1


def _run_specs_bench(args: argparse.Namespace, config: "object") -> int:
    """One-flag Specification-Synthesis benchmark runner. Turns on the
    mathematical-integer semantics these benchmarks assume, then dispatches by
    program content: loops → loop-invariant synthesis; otherwise → function-
    contract synthesis. Both emit ACSL."""
    config.math_ints = True                                   # type: ignore[attr-defined]
    # Oracle for bench mode: an explicit --oracle always wins; otherwise prefer
    # Frama-C/WP (the correct oracle for specification benchmarks — native ACSL,
    # mathematical integers, unbounded + aggregate goals) when it is installed,
    # falling back to CBMC so a run is never a no-op where frama-c is absent.
    if getattr(args, "oracle", None):
        config.oracle = args.oracle                           # type: ignore[attr-defined]
    else:
        from bmc_agent.frama_c import frama_c_available
        fc_path = getattr(config, "frama_c_path", "frama-c")
        config.oracle = "frama-c" if frama_c_available(fc_path) else "cbmc"  # type: ignore[attr-defined]
        print(f"specs-bench: oracle auto-selected → {config.oracle}"
              + ("" if config.oracle == "frama-c"
                 else " (frama-c not on PATH; install frama-c + alt-ergo to use WP)"))
    from bmc_agent.loop_invariants import find_loops, brace_braceless_loops
    src = ""
    try:
        with open(args.source) as fh:
            src = fh.read()
    except OSError as exc:
        print(f"error: cannot read {args.source}: {exc}")
        return 2
    # Normalise brace-less loops so a single-statement loop body still dispatches
    # to loop-invariant synthesis (not the contract path).
    if find_loops(brace_braceless_loops(src)):
        print("specs-bench: loops present → loop-invariant synthesis (+ --math-ints)")
        return _run_loop_invariant_synth(args, config)
    print("specs-bench: no loops → function-contract synthesis")
    return _run_assert_synth(args, config)


def _run_loop_invariant_synth(args: argparse.Namespace, config: "object") -> int:
    """Specification-synthesis mode: synthesize loop invariants (ACSL) that are
    inductive and sufficient to prove the program's goals (see
    bmc_agent.loop_invariants)."""
    from bmc_agent.loop_invariants import synthesize_loop_invariants
    from bmc_agent.llm import LLMClient

    entry = getattr(args, "entry", None) or "main"
    if getattr(args, "include_dir", None):
        config.include_dirs = args.include_dir          # type: ignore[attr-defined]
        config.preprocess = True                        # type: ignore[attr-defined]
    if getattr(args, "defines", None):
        config.cbmc_defines = list(args.defines)        # type: ignore[attr-defined]
    unwind = int(getattr(args, "standalone_unwind", 0) or 0)   # 0 => auto from loop bound
    # Honor the explicit --math-ints flag, a preset default already on config,
    # AND the --specs-bench preset itself. The dispatcher routes --synth-loop-
    # invariants here BEFORE _run_specs_bench runs, so passing both flags would
    # otherwise silently drop the preset's math-ints default (IC3-style benchmarks
    # assume mathematical-integer semantics — without it correct invariants get
    # masked by signed-overflow RTE goals).
    if (getattr(args, "math_ints", False)
            or getattr(args, "specs_bench", False)
            or getattr(config, "math_ints", False)):
        config.math_ints = True                          # type: ignore[attr-defined]
    if getattr(args, "oracle", None):
        config.oracle = args.oracle                      # type: ignore[attr-defined]

    print(f"Loop-invariant synthesis: {args.source}")
    print(f"Entry: {entry}   (propose → CBMC validity+adequacy → refine)")
    r = synthesize_loop_invariants(args.source, config, LLMClient(config),
                                   entry=entry, unwind=unwind)

    import json as _json, os as _os
    out_dir = getattr(config, "artifact_dir", None) or "."
    _os.makedirs(out_dir, exist_ok=True)
    base = _os.path.splitext(_os.path.basename(str(args.source)))[0] or "out"
    out_path = _os.path.join(out_dir, f"synthesized_loop_invariants_{base}.json")
    inst_path = _os.path.join(out_dir, f"{base}_instrumented.c")
    log_path = _os.path.join(out_dir, f"{base}_cbmc.log")
    payload = {
        "source": str(args.source), "entry": entry,
        "satisfied": bool(r.ok),
        "na": bool(getattr(r, "no_goals", False)),   # no proof target → N/A, not pass/fail
        "iterations": r.iterations,
        "goals": list(r.goals or []),
        "loop_invariants": {str(o): invs for o, invs in (r.annotations or {}).items()},
        "acsl": r.acsl, "note": r.note,
        "instrumented_source": _os.path.relpath(inst_path) if r.instrumented else "",
        "cbmc_log": _os.path.relpath(log_path) if r.cbmc_log else "",
    }
    try:
        with open(out_path, "w") as fh:
            _json.dump(payload, fh, indent=2)
        if r.instrumented:                      # (b) the source CBMC actually checked
            with open(inst_path, "w") as fh:
                fh.write(r.instrumented)
        if r.cbmc_log:                          # (b) raw CBMC output of the final check
            with open(log_path, "w") as fh:
                fh.write(r.cbmc_log)
    except OSError as exc:
        print(f"(warning: could not write artifacts: {exc})")

    print("\n=== Synthesized loop invariants (ACSL) ===")
    print(r.acsl or "  (none)")
    print(f"\ngoals: {len(r.goals)}   iterations: {r.iterations}")
    print(f"written: {out_path}")
    if r.instrumented:
        print(f"harness: {inst_path}")
    if r.cbmc_log:
        print(f"cbmc log: {log_path}")
    if getattr(r, "no_goals", False):
        print(f"RESULT: N/A — {r.note}.")
        print("  (No assertion target, so nothing was proved. This is NOT a pass — add a "
              "//@ assert / assert / __VERIFIER_assert stating the expected result.)")
        return 2
    if r.ok:
        # --- machine-int overflow recheck (Frama-C oracle) ---------------------
        # The math-int proof above ran with RTE off, so a body site like `x = x + y`
        # that genuinely overflows int is assumed away. Re-run WP over the SAME
        # invariant set with RTE ON and report whether the result is also machine-
        # int sound. Additive: this NEVER flips the math-int SATISFIED verdict — it
        # only annotates it (the loop path's analogue of the contract path's
        # overflow-rigor pass). The loop entry is usually a parameterless `main`, so
        # there's nowhere to add a no-overflow precondition; the honest outcome is
        # to state whether machine-int overflow is provably absent at the bound.
        if (getattr(config, "oracle", "cbmc") == "frama-c"
                and getattr(config, "math_ints", False)
                and getattr(args, "overflow_rigor", True)
                and r.annotations):
            try:
                from bmc_agent.loop_invariants import check_loop_invariants_wp
                _src = open(str(args.source)).read()
                _chk = check_loop_invariants_wp(_src, r.annotations, config, entry,
                                                force_rte=True)
                _wp = getattr(_chk, "result", None)
                if _wp is not None and _wp.available:
                    _ovf = [g for g in _wp.unproved if "overflow" in g.lower()]
                    if _wp.proved:
                        print("overflow-rigor: machine-int sound — re-verified with RTE on "
                              "(signed overflow provably absent at the loop bound).")
                    elif _ovf:
                        print(f"overflow-rigor: math-int only — {len(_ovf)} signed-overflow "
                              "site(s) in the loop body are NOT machine-int safe at this bound; "
                              "the result holds under mathematical-integer semantics (--math-ints).")
                    else:
                        print("overflow-rigor: math-int only — the invariants are not preserved "
                              "under machine-int (wrapping) semantics; the result holds under "
                              "mathematical-integer semantics (--math-ints).")
            except Exception as exc:   # never let the rigor recheck mask the result
                print(f"overflow-rigor: recheck skipped ({exc}); math-int result stands.")
        print("RESULT: SATISFIED — invariants are inductive and prove all goals.")
        return 0
    print(f"RESULT: NOT SATISFIED — {r.note}")
    return 1


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
    if getattr(args, "real_libc", False):
        # Real-libc supersedes Python preprocessing: CBMC handles all
        # preprocessing via -I so we don't expand glibc internals into
        # text that CBMC's frontend can't re-parse.
        config.cbmc_real_libc = True
        config.preprocess = False
    if getattr(args, "strict_dsl", False):
        config.strict_dsl = True
    if getattr(args, "raw_bytes", False):
        config.raw_bytes = True
    if getattr(args, "defines", None):
        config.cbmc_defines = list(args.defines)
    if getattr(args, "skip_refinement", False):
        config.skip_refinement = True
    if getattr(args, "enable_realism_check", False):
        config.enable_realism_check = True
    if getattr(args, "enable_realism_thinking", False):
        config.enable_realism_thinking = True
    if getattr(args, "enable_flag_selection", False):
        config.enable_flag_selection = True
    if getattr(args, "enable_feedback_loop", False):
        config.enable_feedback_loop = True
    if getattr(args, "feedback_max_iters", None) is not None:
        config.feedback_max_iters = int(args.feedback_max_iters)
    if getattr(args, "enable_dynamic_validation", False):
        config.enable_dynamic_validation = True
    if getattr(args, "threat_model", None):
        config.threat_model = args.threat_model
    _apply_threat_model_context(config, args)
    config.reachability_grounding = getattr(args, "reachability_grounding", "off")  # type: ignore[attr-defined]
    config.harness_refinement = getattr(args, "harness_refinement", "off")  # type: ignore[attr-defined]
    if getattr(args, "lite_mode", False):
        config.lite_mode = True
    if getattr(args, "legacy_spec_gen", False):
        config.use_legacy_spec_gen = True
    if getattr(args, "enable_spec_refiner", False):
        config.enable_spec_refiner = True
    if getattr(args, "enable_soundness_gate", False):
        config.enable_soundness_gate = True
    if getattr(args, "enforce_spec_refiner_retier", False):
        config.enforce_spec_refiner_retier = True
    if getattr(args, "enable_agentic_harness_repair", False):
        config.enable_agentic_harness_repair = True
    if getattr(args, "enable_classifier", False):
        config.enable_classifier = True
    if getattr(args, "enable_triage", False):
        config.enable_phase_3e_triage = True
    if getattr(args, "enable_inlining_advisor", False):
        config.enable_inlining_advisor = True
    # --no-<flag> escape hatches (override the default-on AI layers).
    if getattr(args, "no_realism_check", False):
        config.enable_realism_check = False
    if getattr(args, "no_dynamic_validation", False):
        config.enable_dynamic_validation = False
    if getattr(args, "no_flag_selection", False):
        config.enable_flag_selection = False
    if getattr(args, "no_feedback_loop", False):
        config.enable_feedback_loop = False
    if getattr(args, "no_global_invariants", False):
        config.enable_global_invariants = False
    if getattr(args, "per_function_time_budget", None) is not None:
        config.per_function_time_budget_s = int(args.per_function_time_budget)
    if getattr(args, "no_spec_refiner", False):
        config.enable_spec_refiner = False
    if getattr(args, "no_inlining_advisor", False):
        config.enable_inlining_advisor = False
    if getattr(args, "no_spec_gen_tools", False):
        config.enable_spec_gen_tools = False
    if getattr(args, "no_realism_tools", False):
        config.enable_realism_tools = False
    if getattr(args, "minimal", False):
        config.enable_realism_check = False
        config.enable_dynamic_validation = False
        config.enable_flag_selection = False
        config.enable_feedback_loop = False
        config.enable_spec_refiner = False
        config.enable_inlining_advisor = False
        config.enable_spec_gen_tools = False
        config.enable_realism_tools = False
    _apply_model_arg(config, args)
    _apply_provider_args(config, args)

    # Standalone (whole-program) mode short-circuits the compositional pipeline:
    # verify the program AS WRITTEN from its real entry point, no harness
    # synthesis, no nondet injection. Answers "is THIS program safe?" rather
    # than "is each function safe for any caller?".
    if getattr(args, "standalone", False):
        return _run_standalone(args, config)

    # Assertion-driven spec synthesis: refine function postconditions until the
    # program's //@ assert clauses are provable (and sound w.r.t. the bodies).
    if getattr(args, "specs_from_asserts", False):
        return _run_assert_synth(args, config)

    if getattr(args, "synth_loop_invariants", False):
        return _run_loop_invariant_synth(args, config)

    if getattr(args, "specs_bench", False):
        return _run_specs_bench(args, config)

    domain_knowledge = _resolve_domain_knowledge(args.domain_knowledge) if (hasattr(args, "domain_knowledge") and args.domain_knowledge) else ""

    print(f"Full verification pipeline for: {args.source}")
    print(f"Driver: {args.driver}")
    print(f"Artifact dir: {config.artifact_dir}")
    if config.skip_refinement:
        print("Mode: FilteringOnly (skip_refinement=True) — RQ3 ablation baseline")
    if config.preprocess:
        print(f"Include dirs: {config.include_dirs}")
    _print_ai_layers(config)

    pipeline = AMCPipeline(config)
    bug_reports = pipeline.run(
        source_file=args.source,
        driver_name=args.driver,
        domain_knowledge=domain_knowledge,
    )

    # Filter out bug_reports whose realism check returned UNREALISTIC
    # OR whose final classification (saved to classification.json) was
    # later re-marked as ``spurious`` (e.g. by the feedback loop's
    # CEGAR re-verification with a tighter precondition). Without this
    # filter, the printed list contains stale ``REAL BUG confirmed``
    # entries that the system itself has since rejected, wasting
    # triage time and giving a false impression of the run's success.
    #
    # classification.json is overwritten per-CEX, so a function with N
    # counterexamples ends up holding only the last one's verdict. Match
    # on ``failing_property`` so we only suppress when the persisted
    # verdict describes the same CEX the report came from — otherwise a
    # later spurious unwind-artifact would mask an earlier real bug.
    def _final_classification_is_spurious(driver_name: str, fn_name: str, violated_property: str) -> bool:
        import json, os
        path = os.path.join(config.artifact_dir, driver_name, fn_name, "classification.json")
        try:
            with open(path) as f:
                data = json.load(f)
            cls = data.get("classification") or {}
            saved_prop = (cls.get("counterexample") or {}).get("failing_property")
            if saved_prop and violated_property and saved_prop != violated_property:
                return False
            return cls.get("outcome") == "spurious"
        except Exception:
            return False

    def _realism_was_unrealistic(report) -> bool:
        rc = getattr(report, "realism_check", None) or {}
        if isinstance(rc, dict):
            return (rc.get("verdict") or "").lower() == "unrealistic"
        return getattr(rc, "verdict", "").lower() == "unrealistic" if rc else False

    survivors = [
        r for r in bug_reports
        if not _realism_was_unrealistic(r)
        and not _final_classification_is_spurious(args.driver, r.function_name, getattr(r, "violated_property", "") or "")
    ]
    dropped = len(bug_reports) - len(survivors)

    print(f"\n=== Results ===")
    if dropped > 0:
        print(
            f"(suppressed {dropped} stale finding(s): rejected by realism check "
            f"or re-classified as spurious after refinement)"
        )
    if not survivors:
        print("No bugs confirmed.")
    else:
        print(f"Confirmed bugs: {len(survivors)}")
        for report in survivors:
            print(f"\n  [{report.bug_type.upper()}] {report.function_name}")
            print(f"    Property: {report.violated_property}")
            print(f"    Confidence: {report.confidence}")
            if report.call_chain:
                print(f"    Call chain: {' → '.join(report.call_chain)}")

    # Latent findings: panics reachable on the pub API but no in-tree
    # caller produces the CEx state. Separate severity tier from reachable
    # bugs — cargo-fuzz / future-caller risk, not an active crash path.
    latent_reports = getattr(pipeline, "latent_reports", None) or []
    if latent_reports:
        print(f"\nLatent panics on pub API: {len(latent_reports)}")
        print(
            "  (panic reachable via cargo-fuzz / future-caller, "
            "but no in-tree call site produces the state)"
        )
        for report in latent_reports:
            print(f"\n  [{(report.bug_type or 'panic').upper()}] {report.function_name}")
            print(f"    Property: {report.violated_property}")

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
    _apply_provider_args(config, args)

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


def _cmd_judge_dir(args: argparse.Namespace) -> int:
    """Simple LLM-as-judge mode. No multi-stage pipeline."""
    from pathlib import Path
    from bmc_agent.config import Config
    from bmc_agent.judge_pipeline import run_judge_pipeline

    config = Config.from_env()
    if getattr(args, "model", None):
        config.llm_model = args.model
    if getattr(args, "enable_flag_selection", False):
        config.enable_flag_selection = True
    if getattr(args, "enable_feedback_loop", False):
        config.enable_feedback_loop = True
    if getattr(args, "agentic_harness", False):
        config.enable_agentic_harness = True
    if getattr(args, "refine_rounds", None) is not None:
        config.agentic_refine_rounds = int(args.refine_rounds)

    summary = run_judge_pipeline(
        config=config,
        source_dir=Path(args.source_dir),
        driver=args.driver,
        output=Path(args.output),
        exclude_patterns=list(getattr(args, "exclude", None) or []),
        include_dirs=list(getattr(args, "include_dir", None) or []),
        defines=list(getattr(args, "defines", None) or []),
        cbmc_unwind=int(getattr(args, "cbmc_unwind", 4)),
        cbmc_timeout=int(getattr(args, "cbmc_timeout", 60)),
        max_functions=getattr(args, "max_functions", None),
        only_files=getattr(args, "only_file", None),
    )

    # Top-line summary to stdout
    total_real = 0
    total_unreal = 0
    total_uncertain = 0
    total_adj = 0
    for stem, pf in summary.get("per_file", {}).items():
        v = pf.get("verdicts", {})
        total_real += v.get("realistic", 0)
        total_unreal += v.get("unrealistic", 0)
        total_uncertain += v.get("uncertain", 0)
        for fn_name, rec in (pf.get("functions") or {}).items():
            for cex_rec in (rec.get("cexs") or []):
                total_adj += len((cex_rec.get("judge") or {}).get("adjacent_bugs") or [])
    print(f"\n=== judge-dir summary ===")
    print(f"  files parsed:   {summary.get('n_files_parsed', 0)}")
    print(f"  files skipped:  {summary.get('n_files_skipped', 0)}")
    print(f"  verdict counts: realistic={total_real}  unrealistic={total_unreal}  uncertain={total_uncertain}")
    print(f"  adjacent-bug hypotheses: {total_adj}")
    print(f"  summary.json: {Path(args.output) / args.driver / 'summary.json'}")
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
    if getattr(args, "enable_feedback_loop", False):
        config.enable_feedback_loop = True
    if getattr(args, "feedback_max_iters", None) is not None:
        config.feedback_max_iters = int(args.feedback_max_iters)
    if getattr(args, "real_libc", False):
        config.cbmc_real_libc = True
        config.preprocess = False
    if getattr(args, "strict_dsl", False):
        config.strict_dsl = True
    if getattr(args, "raw_bytes", False):
        config.raw_bytes = True
    if getattr(args, "defines", None):
        config.cbmc_defines = list(args.defines)
    if getattr(args, "threat_model", None):
        config.threat_model = args.threat_model
    _apply_threat_model_context(config, args)
    config.reachability_grounding = getattr(args, "reachability_grounding", "off")  # type: ignore[attr-defined]
    config.harness_refinement = getattr(args, "harness_refinement", "off")  # type: ignore[attr-defined]
    if getattr(args, "lite_mode", False):
        config.lite_mode = True
    if getattr(args, "legacy_spec_gen", False):
        config.use_legacy_spec_gen = True
    if getattr(args, "enable_spec_refiner", False):
        config.enable_spec_refiner = True
    if getattr(args, "enable_soundness_gate", False):
        config.enable_soundness_gate = True
    if getattr(args, "enforce_spec_refiner_retier", False):
        config.enforce_spec_refiner_retier = True
    if getattr(args, "enable_agentic_harness_repair", False):
        config.enable_agentic_harness_repair = True
    if getattr(args, "enable_classifier", False):
        config.enable_classifier = True
    if getattr(args, "enable_triage", False):
        config.enable_phase_3e_triage = True
    if getattr(args, "enable_inlining_advisor", False):
        config.enable_inlining_advisor = True
    if getattr(args, "enable_phase_3e_triage", False):
        config.enable_phase_3e_triage = True
    # --no-<flag> escape hatches (override the default-on AI layers).
    if getattr(args, "no_realism_check", False):
        config.enable_realism_check = False
    if getattr(args, "no_dynamic_validation", False):
        config.enable_dynamic_validation = False
    if getattr(args, "no_flag_selection", False):
        config.enable_flag_selection = False
    if getattr(args, "no_feedback_loop", False):
        config.enable_feedback_loop = False
    if getattr(args, "no_global_invariants", False):
        config.enable_global_invariants = False
    if getattr(args, "per_function_time_budget", None) is not None:
        config.per_function_time_budget_s = int(args.per_function_time_budget)
    if getattr(args, "no_spec_refiner", False):
        config.enable_spec_refiner = False
    if getattr(args, "no_inlining_advisor", False):
        config.enable_inlining_advisor = False
    if getattr(args, "no_spec_gen_tools", False):
        config.enable_spec_gen_tools = False
    if getattr(args, "no_realism_tools", False):
        config.enable_realism_tools = False
    if getattr(args, "no_phase_3e_triage", False):
        config.enable_phase_3e_triage = False
    if getattr(args, "minimal", False):
        config.enable_realism_check = False
        config.enable_dynamic_validation = False
        config.enable_flag_selection = False
        config.enable_feedback_loop = False
        config.enable_spec_refiner = False
        config.enable_inlining_advisor = False
        config.enable_spec_gen_tools = False
        config.enable_realism_tools = False
        config.enable_phase_3e_triage = False
    _apply_model_arg(config, args)
    _apply_provider_args(config, args)

    include_dirs = args.include_dir or []
    if include_dirs:
        config.include_dirs = include_dirs
        # In real-libc mode we deliberately DON'T also enable Python-side
        # preprocessing; CBMC handles all -I expansion itself. In every
        # other mode, include_dirs implies Python preprocessing is wanted.
        if not config.cbmc_real_libc:
            config.preprocess = True

    domain_knowledge = _resolve_domain_knowledge(args.domain_knowledge) if args.domain_knowledge else ""

    exclude = args.exclude or []

    print(f"Verifying directory: {args.source_dir}")
    print(f"Driver prefix:       {args.driver}")
    print(f"Include dirs:        {include_dirs or '(none)'}")
    print(f"Artifact dir:        {config.artifact_dir}")
    if exclude:
        print(f"Excluded patterns:   {exclude}")

    pipeline = AMCPipeline(config)
    only_functions = None
    _fns = getattr(args, "functions", "") or ""
    if _fns.strip():
        only_functions = {f.strip() for f in _fns.split(",") if f.strip()}
        print(f"Only functions:      {sorted(only_functions)} (cross-file gen+refinement)")
    results = pipeline.run_directory(
        source_dir=args.source_dir,
        driver_name=args.driver,
        include_dirs=include_dirs,
        domain_knowledge=domain_knowledge,
        exclude_patterns=exclude,
        only_functions=only_functions,
    )

    # Filter out reports the realism check downgraded to "unlikely" —
    # these are findings where the realism LLM judged the witness
    # UNREALISTIC (e.g. it violated an active stub contract). The
    # bug-reporter still PERSISTS the report (the audit trail lives in
    # bug_report.json) but the verify-dir summary should not present
    # them as confirmed bugs, otherwise the "Total bugs confirmed"
    # number is misleading. Mirrors the suppression logic that
    # ``_cmd_verify`` already has.
    def _is_unlikely(report) -> bool:
        return (getattr(report, "confidence", "") or "").lower() == "unlikely"

    filtered_results: dict = {}
    suppressed_total = 0
    for fname, bugs in results.items():
        kept = [b for b in bugs if not _is_unlikely(b)]
        filtered_results[fname] = kept
        suppressed_total += len(bugs) - len(kept)

    print(f"\n=== Summary ===")
    total = sum(len(v) for v in filtered_results.values())
    print(f"Files processed: {len(results)}")
    print(f"Total bugs confirmed: {total}")
    if suppressed_total > 0:
        print(
            f"(suppressed {suppressed_total} finding(s) downgraded by realism "
            f"check to 'unlikely' — see per-file bug_report.json for the audit trail)"
        )
    for fname, bugs in sorted(filtered_results.items()):
        if not bugs:
            continue
        print(f"  {fname}: {len(bugs)} bug(s)")
        for report in bugs:
            print(f"    [{report.bug_type.upper()}] {report.function_name} — {report.violated_property}")

    # ------------------------------------------------------------------
    # Adjacent-bug follow-up rounds
    # ------------------------------------------------------------------
    follow_n = int(getattr(args, "follow_adjacent_rounds", 0) or 0)
    if follow_n > 0:
        from pathlib import Path as _Path
        from bmc_agent.adjacent_follower import follow_rounds
        sweep_output = _Path(config.artifact_dir) / args.driver
        source_dir = _Path(args.source_dir)
        print(f"\n=== Adjacent-bug follow-up: up to {follow_n} round(s) ===")
        rounds_out = follow_rounds(
            source_dir=source_dir,
            sweep_output=sweep_output,
            config=config,
            rounds=follow_n,
        )
        for round_num, drivers in sorted(rounds_out.items()):
            total = sum(len(bs) for bs in drivers.values())
            print(f"  Round {round_num}: {len(drivers)} driver(s), {total} new bug(s)")
            for drv, bs in sorted(drivers.items()):
                if not bs:
                    continue
                for r in bs:
                    print(f"    [{r.bug_type.upper()}] {drv}/{r.function_name} — {r.violated_property}")

    # ------------------------------------------------------------------
    # Per-bug markdown report generation
    # ------------------------------------------------------------------
    # For every realism-confirmed function (ANY of its per-CEx records had
    # realism=realistic AND confidence!=unlikely), write a human-readable
    # markdown report to <output>/reports/<function>.md plus an index.md.
    # Always runs at end of verify-dir so reviewers don't need to grep JSON.
    try:
        from bmc_agent.report_generator import generate_reports
        rerun_cmd = (
            f"python -m bmc_agent.cli verify-dir --source-dir {args.source_dir} "
            f"--driver {args.driver} --output <new_output> "
            f"--exclude 'test_*'  # (re-supply the include/defines/flags you used)"
        )
        written = generate_reports(
            sweep_output=config.artifact_dir,
            driver=args.driver,
            rerun_cmd=rerun_cmd,
        )
        if written:
            print(f"\n=== Per-bug reports: {len(written) - 1} finding(s) + index ===")
            for p in written:
                print(f"  {p}")
        else:
            print("\n=== Per-bug reports: 0 realism-confirmed findings ===")
    except Exception as exc:
        print(f"\nReport generation failed: {exc}")

    return 0


def _cmd_self_patch_review(args: argparse.Namespace) -> int:
    """Walk a sweep's proposed_patches/ dir and digest every staged proposal."""
    import json as _json
    from pathlib import Path as _Path

    root = _Path(args.proposals_dir)
    if not root.exists():
        print(f"path not found: {root}", file=__import__("sys").stderr)
        return 2

    meta_files = sorted(root.rglob("*.meta.json"))
    if not meta_files:
        print(f"No staged proposals found under {root}")
        return 0

    print(f"=== Staged self-patch proposals: {root} ===\n")
    print(f"Found {len(meta_files)} proposal(s)\n")

    for mf in meta_files:
        try:
            with mf.open() as f:
                meta = _json.load(f)
        except Exception as exc:
            print(f"  ! failed to read {mf}: {exc}")
            continue

        diff_path = mf.with_suffix("").with_suffix(".diff")
        test_path = mf.with_suffix("").with_suffix(".test.py")
        # Above twice-stripped: e.g. ``foo.meta.json`` → ``foo``, suffix
        # ``.diff`` produces ``foo.diff``.

        round_dir = mf.parent.name
        print(f"--- [{round_dir}] {mf.stem.replace('.meta', '')} ---")
        print(f"  Status:           {meta.get('status')}")
        print(f"  Error class:      {meta.get('error_class')}")
        print(f"  Error target:     {meta.get('error_target') or '(none)'}")
        print(f"  Files touched:    {meta.get('files_touched')}")
        print(f"  Lines changed:    {meta.get('lines_changed')}")
        rt_name = meta.get("regression_test_name", "(unknown)")
        rt_path = meta.get("regression_test_path", "(unknown)")
        print(f"  Regression test:  {rt_path}::{rt_name}")
        rationale = (meta.get("rationale") or "").strip()
        if rationale:
            print(f"  Rationale:        {rationale}")
        review = (meta.get("review_instructions") or "").strip()
        if review:
            print("  Manual apply:")
            for line in review.splitlines():
                print(f"    {line}")
        if args.show_diff and diff_path.exists():
            print(f"\n  Diff ({diff_path}):\n")
            for line in diff_path.read_text().splitlines():
                print(f"    {line}")
        print()

    return 0


def _cmd_autonomous(args: argparse.Namespace) -> int:
    """Phase 2 of autonomous mode: round-based verify-dir with convergence.

    Each round runs the full verify-dir pipeline with the autonomous-mode
    defaults (lite_mode + realism + dynamic + Phase 2b auto-retry +
    feedback loop). After each round, the loop computes a fingerprint
    (coverage, total_bugs, total_errors, total_uncertain) and stops on:

      * coverage ≥ ``--target-coverage`` (default 0.80)
      * fingerprint matches a previous round (fixed point)
      * ``--max-rounds`` reached (default 3)

    Between rounds, knobs are adjusted based on the previous round's
    output:

      * Many UNCERTAIN realism verdicts → enable
        ``enable_realism_thinking`` for the next round.
      * Many post-retry CBMC errors that all share the same identifier
        → promote into ``session_strip_typedefs`` for the next round
        (already done in-round by Phase 2b, but persisted across rounds
        here so round-2 starts with round-1's wins).

    Per-round artifact: ``<output>/autonomous/round_<N>.json`` with the
    summary, the strip-set deltas applied, and the convergence verdict.
    """
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline
    import json as _json
    import time as _time
    from pathlib import Path as _Path

    config = Config.from_env()
    if args.output:
        config.artifact_dir = args.output

    # Apply the standard autonomous-mode defaults. The user can still
    # override individual flags via the CLI; we only force the defaults
    # when the corresponding flag wasn't passed.
    config.lite_mode = True
    config.enable_realism_check = True
    config.enable_feedback_loop = True
    if config.feedback_max_iters in (None, 3):
        config.feedback_max_iters = 3
    if getattr(args, "enable_dynamic_validation", False):
        config.enable_dynamic_validation = True
    if getattr(args, "real_libc", False):
        config.cbmc_real_libc = True
        config.preprocess = False
    if getattr(args, "raw_bytes", False):
        config.raw_bytes = True
    if getattr(args, "defines", None):
        config.cbmc_defines = list(args.defines)
    if getattr(args, "threat_model", None):
        config.threat_model = args.threat_model
    _apply_threat_model_context(config, args)
    if getattr(args, "allow_self_patch", None):
        config.allow_self_patch = args.allow_self_patch
    _apply_model_arg(config, args)
    _apply_provider_args(config, args)

    include_dirs = args.include_dir or []
    if include_dirs:
        config.include_dirs = include_dirs
        if not config.cbmc_real_libc:
            config.preprocess = True

    domain_knowledge = _resolve_domain_knowledge(args.domain_knowledge) if args.domain_knowledge else ""
    exclude = args.exclude or []

    max_rounds = int(args.max_rounds)
    target_coverage = float(args.target_coverage)

    print(f"=== AUTONOMOUS MODE ===")
    print(f"Source dir:      {args.source_dir}")
    print(f"Driver prefix:   {args.driver}")
    print(f"Include dirs:    {include_dirs or '(none)'}")
    print(f"Artifact dir:    {config.artifact_dir}")
    print(f"Max rounds:      {max_rounds}")
    print(f"Target coverage: {target_coverage:.0%}")
    print(f"Defaults: lite_mode=True, realism=True, feedback_loop=True,")
    print(f"          auto_retry_max_rounds={config.auto_retry_max_rounds}")
    print()

    autonomous_dir = _Path(config.artifact_dir) / "autonomous"
    autonomous_dir.mkdir(parents=True, exist_ok=True)

    round_summaries: list[dict] = []
    seen_fingerprints: set[tuple] = set()

    for round_idx in range(max_rounds):
        print(f"\n===== AUTONOMOUS ROUND {round_idx + 1} / {max_rounds} =====")
        t0 = _time.time()

        # Snapshot the session-strip sets at the start of the round so we
        # can report the round's deltas (Phase 2b mutates them in-place).
        strip_typedefs_before = list(config.session_strip_typedefs)
        strip_structs_before = list(config.session_strip_structs)
        opaque_before = list(config.session_opaque_param_structs)

        pipeline = AMCPipeline(config)
        results = pipeline.run_directory(
            source_dir=args.source_dir,
            driver_name=args.driver,
            include_dirs=include_dirs,
            domain_knowledge=domain_knowledge,
            exclude_patterns=exclude,
        )
        elapsed = _time.time() - t0

        summary = _summarize_autonomous_round(
            config=config,
            args_driver=args.driver,
            results=results,
            elapsed_s=elapsed,
            strip_typedefs_before=strip_typedefs_before,
            strip_structs_before=strip_structs_before,
            opaque_before=opaque_before,
        )
        summary["round"] = round_idx + 1
        round_summaries.append(summary)

        # Persist per-round artifact.
        (autonomous_dir / f"round_{round_idx + 1}.json").write_text(
            _json.dumps(summary, indent=2)
        )

        print(_format_round_summary(summary))

        fingerprint = (
            summary["total_files"],
            summary["total_functions"],
            summary["cbmc_verdicts"],
            summary["cbmc_errors"],
            summary["confirmed_bugs"],
        )
        if summary["coverage"] >= target_coverage:
            print(
                f"\n✓ Converged: coverage {summary['coverage']:.1%} ≥ target "
                f"{target_coverage:.0%}"
            )
            break
        if fingerprint in seen_fingerprints:
            print(
                f"\n✓ Converged: fixed-point fingerprint reached "
                f"(no progress this round)"
            )
            break
        seen_fingerprints.add(fingerprint)

        # Knob adjustments for the next round.
        next_knobs: list[str] = []
        if summary["uncertain_count"] > max(1, summary["confirmed_bugs"]) * 0.5:
            if not config.enable_realism_thinking:
                config.enable_realism_thinking = True
                next_knobs.append("enable_realism_thinking=True")

        # Phase 4b: scan the round's bug-report tree for recurring FP
        # patterns and inject a constrained skepticism hint into the
        # next round's realism prompt. No-op when no pattern crosses
        # the threshold (the realism prompt runs unchanged).
        from bmc_agent.realism_hint_injector import collect_hints, persist_hints
        driver_root = _Path(config.artifact_dir) / args.driver
        if driver_root.exists():
            hint_bundle = collect_hints(driver_root)
            persist_hints(hint_bundle, autonomous_dir, round_idx)
            if hint_bundle.text:
                config.realism_extra_skepticism = hint_bundle.text
                patterns = ", ".join(
                    f"{k}={v}" for k, v in hint_bundle.patterns_observed.items()
                    if v
                )
                next_knobs.append(f"realism_extra_skepticism (patterns: {patterns})")

        if next_knobs:
            print(f"  Adjusting for round {round_idx + 2}: {', '.join(next_knobs)}")
    else:
        print(f"\n× Stopped: max rounds ({max_rounds}) reached without convergence")

    # Cumulative summary.
    summary_md = _format_autonomous_summary(round_summaries)
    (autonomous_dir / "summary.md").write_text(summary_md)
    print(f"\nAutonomous artifacts: {autonomous_dir}")

    return 0


def _summarize_autonomous_round(
    config,
    args_driver: str,
    results: dict,
    elapsed_s: float,
    strip_typedefs_before: list,
    strip_structs_before: list,
    opaque_before: list,
) -> dict:
    """Build a one-round summary dict from the run_directory output.

    Reads per-file coverage_diagnostics.json (written by Phase 2b) and
    classification.json files (written by Phase 3) to compute aggregate
    counts. Pure file-scan; never re-runs CBMC.
    """
    import json as _json
    from pathlib import Path as _Path

    artifact_dir = _Path(config.artifact_dir)
    driver_root = artifact_dir / args_driver

    total_runs = 0
    total_verdicts = 0
    total_errors = 0
    files_with_verdicts = 0
    files_all_errored = 0
    for cov in driver_root.rglob("coverage_diagnostics.json"):
        try:
            with cov.open() as f:
                d = _json.load(f)
            runs = int(d.get("total_cbmc_runs", 0))
            verd = int(d.get("produced_verdict", 0))
            fail = int(d.get("failed_before_verdict", 0))
            total_runs += runs
            total_verdicts += verd
            total_errors += fail
            if runs and fail == runs:
                files_all_errored += 1
            elif runs:
                files_with_verdicts += 1
        except Exception:
            pass

    # Phase 3 outcomes.
    outcome_counts: dict[str, int] = {}
    realism_counts: dict[str, int] = {}
    for cls in driver_root.rglob("classification.json"):
        try:
            with cls.open() as f:
                cd = _json.load(f)
            c = cd.get("classification") or {}
            outcome_counts[c.get("outcome", "unknown")] = outcome_counts.get(c.get("outcome", "unknown"), 0) + 1
        except Exception:
            pass
    for br in driver_root.rglob("bug_report.json"):
        try:
            with br.open() as f:
                bd = _json.load(f)
            rc = bd.get("realism_check") or {}
            v = (rc.get("verdict") if isinstance(rc, dict) else None) or "n/a"
            realism_counts[v] = realism_counts.get(v, 0) + 1
        except Exception:
            pass

    confirmed_bugs = sum(len(b) for b in results.values())
    coverage = (total_verdicts / total_runs) if total_runs else 0.0

    return {
        "elapsed_s": round(elapsed_s, 1),
        "total_files": len(results),
        "total_functions": total_runs,
        "cbmc_verdicts": total_verdicts,
        "cbmc_errors": total_errors,
        "coverage": coverage,
        "files_with_verdicts": files_with_verdicts,
        "files_all_errored": files_all_errored,
        "outcome_counts": outcome_counts,
        "realism_counts": realism_counts,
        "uncertain_count": int(realism_counts.get("UNCERTAIN", 0)),
        "unrealistic_count": int(realism_counts.get("UNREALISTIC", 0)),
        "realistic_count": int(realism_counts.get("REALISTIC", 0)),
        "confirmed_bugs": confirmed_bugs,
        "session_strip_typedefs_added": [
            t for t in config.session_strip_typedefs
            if t not in strip_typedefs_before
        ],
        "session_strip_structs_added": [
            s for s in config.session_strip_structs
            if s not in strip_structs_before
        ],
        "session_opaque_param_structs_added": [
            o for o in config.session_opaque_param_structs
            if o not in opaque_before
        ],
    }


def _format_round_summary(s: dict) -> str:
    """One-screen round summary for stdout."""
    lines = []
    lines.append(f"  Elapsed: {s['elapsed_s']:.1f}s")
    lines.append(f"  Files: {s['total_files']} ({s['files_with_verdicts']} with verdicts, {s['files_all_errored']} all-errored)")
    lines.append(f"  Functions: {s['total_functions']}")
    lines.append(f"  CBMC verdicts: {s['cbmc_verdicts']}, errors: {s['cbmc_errors']}, coverage: {s['coverage']:.1%}")
    lines.append(f"  Phase 3 outcomes: {s['outcome_counts']}")
    lines.append(f"  Realism: REALISTIC={s['realistic_count']}, UNCERTAIN={s['uncertain_count']}, UNREALISTIC={s['unrealistic_count']}")
    lines.append(f"  Confirmed bugs (post-realism): {s['confirmed_bugs']}")
    if s.get("session_strip_typedefs_added"):
        lines.append(f"  Auto-retry added typedefs: {s['session_strip_typedefs_added']}")
    if s.get("session_strip_structs_added"):
        lines.append(f"  Auto-retry added structs: {s['session_strip_structs_added']}")
    if s.get("session_opaque_param_structs_added"):
        lines.append(f"  Auto-retry forced opaque: {s['session_opaque_param_structs_added']}")
    return "\n".join(lines)


def _format_autonomous_summary(rounds: list[dict]) -> str:
    """Markdown summary across all rounds, written to summary.md."""
    lines = ["# Autonomous-mode sweep summary", ""]
    lines.append(f"Rounds run: {len(rounds)}")
    if not rounds:
        return "\n".join(lines)
    last = rounds[-1]
    lines.append(f"Final coverage: {last['coverage']:.1%}")
    lines.append(f"Final confirmed bugs: {last['confirmed_bugs']}")
    lines.append("")
    lines.append("| Round | Elapsed | Files | Functions | Verdicts | Errors | Coverage | Confirmed | UNCERTAIN |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rounds:
        lines.append(
            f"| {r['round']} | {r['elapsed_s']:.1f}s | {r['total_files']} | "
            f"{r['total_functions']} | {r['cbmc_verdicts']} | {r['cbmc_errors']} | "
            f"{r['coverage']:.1%} | {r['confirmed_bugs']} | {r['uncertain_count']} |"
        )
    lines.append("")
    # Auto-retry promotion candidates: typedefs/structs added across rounds.
    promoted_typedefs: list[str] = []
    promoted_structs: list[str] = []
    promoted_opaque: list[str] = []
    for r in rounds:
        promoted_typedefs.extend(r.get("session_strip_typedefs_added", []))
        promoted_structs.extend(r.get("session_strip_structs_added", []))
        promoted_opaque.extend(r.get("session_opaque_param_structs_added", []))
    if promoted_typedefs or promoted_structs or promoted_opaque:
        lines.append("## Auto-retry promotion candidates")
        lines.append("")
        lines.append("Review and consider adding to the static sets in `harness_generator.py`:")
        lines.append("")
        if promoted_typedefs:
            lines.append("- Typedefs added to session strip set: " + ", ".join(sorted(set(promoted_typedefs))))
        if promoted_structs:
            lines.append("- Structs added to session strip set: " + ", ".join(sorted(set(promoted_structs))))
        if promoted_opaque:
            lines.append("- Structs forced opaque: " + ", ".join(sorted(set(promoted_opaque))))
    return "\n".join(lines)


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

    # Shared provider-routing arguments (CLI sugar over BMC_AGENT_LLM_*_PROVIDER).
    def _add_provider_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--agentic",
            action=argparse.BooleanOptionalAction,
            default=True,
            dest="agentic",
            help=(
                "GENERAL agentic stack (DEFAULT ON; pass --no-agentic for the plain "
                "non-agentic core). Makes EVERY LLM agent role (spec-gen, "
                "refinement, realism, triage, disagreement, …) an investigating "
                "agent and enables the soundness gate + agentic harness-repair + "
                "split spec-gen + component gating — but DOES NOT force any backend. "
                "Each role is instantiated by whatever its routing says: a per-role "
                "BMC_AGENT_LLM_<ROLE>_PROVIDER/_MODEL override, else the global "
                "default (--provider / BMC_AGENT_LLM_DEFAULT_*), else the "
                "auto-resolved provider — so roles may be a mix of API / "
                "claude-code / codex / etc. The conventional core (CBMC, harness "
                "translation, compile+run) is unaffected. Use --agentic-claude-code "
                "to force every role onto the local Claude Code CLI; --no-agentic to "
                "disable the whole agentic stack."
            ),
        )
        p.add_argument(
            "--agentic-claude-code",
            action="store_true",
            default=False,
            dest="agentic_claude_code",
            help=(
                "Like --agentic, but FORCE every agent role onto the local Claude "
                "Code CLI provider (read-only code-exploration tools, your `claude` "
                "login, no API key). This was the original --agentic behaviour. A "
                "per-role BMC_AGENT_LLM_<ROLE>_PROVIDER override still wins, so an "
                "individual agent can be repointed to an API/codex backend. NOTE: "
                "claude-code is a serial subprocess — slow for high-volume roles."
            ),
        )
        p.add_argument(
            "--agentic-refine",
            action="store_true",
            default=False,
            dest="agentic_refine",
            help=(
                "LEAN agentic: route ONLY refinement (+ its soundness guard) to "
                "the Claude Code CLI with tools, and enable the guard / "
                "harness-repair / split-spec-gen — but keep SPEC GENERATION on "
                "the fast default provider. Recommended for batch runs (avoids "
                "slow per-function agentic spec-gen)."
            ),
        )
        p.add_argument(
            "--provider",
            default="",
            choices=["", "anthropic", "openai", "claude-code"],
            metavar="PROVIDER",
            help=(
                "LLM provider for all roles (default: auto-detect / env). "
                "'claude-code' shells out to the local Claude Code CLI, reusing "
                "your existing login — no API key required."
            ),
        )
        p.add_argument(
            "--specs-via-claude-code",
            action="store_true",
            default=False,
            dest="specs_via_claude_code",
            help=(
                "Route ONLY spec generation + refinement to the Claude Code CLI "
                "(reuses your Claude Code login; no API key). Every other role "
                "keeps the global/default provider."
            ),
        )
        p.add_argument(
            "--claude-code-agentic",
            action="store_true",
            default=False,
            dest="claude_code_agentic",
            help=(
                "When the claude-code provider is active, grant it read-only "
                "tools (Read/Grep/Glob) so it can explore the source tree while "
                "drafting/refining specs, instead of a one-shot text completion."
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
    gen.add_argument(
        "--include-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Add an include directory for C preprocessing (repeatable).",
    )
    gen.add_argument(
        "-D", "--define",
        action="append",
        default=[],
        dest="defines",
        help="Pass a preprocessor define (repeatable), e.g. -D HAVE_CONFIG_H.",
    )
    _add_model_arg(gen)
    _add_provider_args(gen)
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
    chk.add_argument(
        "--include-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Add an include directory for C preprocessing (repeatable). Required when "
             "the harness pulls in build-config headers (e.g. config.h).",
    )
    chk.add_argument(
        "-D", "--define",
        action="append",
        default=[],
        dest="defines",
        help="Pass a preprocessor define to CBMC (repeatable), e.g. -D HAVE_CONFIG_H.",
    )
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
        "--enable-feedback-loop",
        action="store_true",
        default=False,
        help="Distill UNREALISTIC realism rejections into learned constraints (function-spec or project invariant) "
             "or code-change TODOs. Persists to <output>/learned_constraints.json so subsequent sweeps stop "
             "re-producing the same artifact pattern.",
    )
    ver.add_argument(
        "--feedback-max-iters",
        type=int,
        default=None,
        help="In-sweep convergence: after distilling a clause, re-run CBMC on the same function up to N times "
             "until the function verifies clean, a REALISTIC verdict emerges, or the same CE class repeats. "
             "Default 3 (set via Config.feedback_max_iters).",
    )
    ver.add_argument(
        "--real-libc",
        action="store_true",
        default=False,
        help="Real-libc mode: harness #includes the source .c file and lets CBMC do all preprocessing via -I. Required for real-world glibc-using OSS (jq, curl, OpenSSL, …); leave off for bare-metal targets like VibeOS.",
    )
    ver.add_argument(
        "--strict-dsl",
        action="store_true",
        default=False,
        help="Strict-formal Phase 1 prompts: pre/post must be a single C boolean expression (no natural language). Required for bounty/CVE work — prose-mixed specs translate to comments and produce vacuous verifications.",
    )
    ver.add_argument(
        "--raw-bytes",
        action="store_true",
        default=False,
        help="Treat single char* / const char* params as raw byte buffers (no NUL termination) in the harness. Required for wire-format parsers (protobuf upb, length-prefixed blobs) that read N raw bytes regardless of NULs.",
    )
    ver.add_argument(
        "-D", "--define",
        action="append",
        default=[],
        dest="defines",
        help="Pass a preprocessor define to CBMC (repeatable). Use NAME or NAME=VALUE form, e.g. -D HAVE_CONFIG_H -D BUILDING_LIBCURL. Required for build-config-driven C codebases (curl, OpenSSL) where headers gate on autoconf-style flags.",
    )
    ver.add_argument(
        "--standalone",
        action="store_true",
        default=False,
        help="Whole-program mode: verify the program AS WRITTEN from its real entry point (no per-function harness synthesis, no nondet injection). Answers 'is THIS program safe?' Loops with concrete bounds unwind fully; //@ assert annotations are checked. Runs CBMC directly with the full memory-safety + overflow check set.",
    )
    ver.add_argument(
        "--entry",
        default="main",
        help="Entry function for --standalone / --specs-from-asserts mode (default: main).",
    )
    ver.add_argument(
        "--specs-from-asserts",
        action="store_true",
        default=False,
        help="Assertion-driven spec synthesis: treat the program's //@ assert clauses as the goal and refine function postconditions until every assert is provable AND sound (implied by the body). Reports the synthesized contracts; flags any assert no sound spec can satisfy (i.e. genuinely false).",
    )
    ver.add_argument(
        "--synth-loop-invariants",
        action="store_true",
        default=False,
        help="Specification-synthesis mode: synthesize LOOP INVARIANTS (and render them in ACSL) that are inductive AND sufficient to prove the program's verification goals (assert / static_assert / __VERIFIER_assert / //@ assert). Reuses the gen+refine engine; CBMC validates each invariant per-iteration (Local Validity) and proves the goals (Global Adequacy) for unwindable loops.",
    )
    ver.add_argument(
        "--specs-bench",
        action="store_true",
        default=False,
        help="Specification-synthesis BENCHMARK preset (one flag). Reads the goals (assert/static_assert/__VERIFIER_assert///@ assert), dispatches by program content — loops → loop-invariant synthesis, otherwise → function-contract synthesis — turns on --math-ints (mathematical-integer semantics these benchmarks assume), and emits ACSL. Equivalent to picking the right synthesis mode + --math-ints automatically.",
    )
    ver.add_argument(
        "--math-ints",
        action="store_true",
        default=False,
        help="Loop-invariant synthesis (unbounded loops): assume the loop body's signed arithmetic does not overflow (mathematical-integer semantics, as IC3-style benchmarks and Frama-C/WP assume), so invariants like x>=1 under x=x+y are inductive. Off => machine-int (wrapping) semantics.",
    )
    ver.add_argument(
        "--no-overflow-rigor",
        dest="overflow_rigor",
        action="store_false",
        default=True,
        help="Disable the verification-gated overflow-rigor pass (Frama-C oracle, contract synthesis). On by default: even under --math-ints the synthesized contract is upgraded to machine-int soundness by enumerating every signed-overflow site in the body, bounding it INT_MIN<=e<=INT_MAX, and re-verifying with RTE on — adopted ONLY if WP still discharges every goal (additive; never turns a pass into a failure). Pass this to keep the PURE mathematical-integer contract (no synthesized no-overflow precondition), e.g. for exact-match against an IC3-style benchmark's golden math-int spec.",
    )
    ver.add_argument(
        "--oracle",
        choices=["cbmc", "frama-c"],
        default=None,
        help="Verification oracle for spec synthesis. Unset: 'cbmc' everywhere EXCEPT --specs-bench, which auto-prefers 'frama-c' when frama-c is on PATH (the correct oracle for specification benchmarks: native ACSL + mathematical integers + unbounded/aggregate goals) and falls back to 'cbmc' otherwise. 'cbmc': bounded model checking — unwinds bounded loops, machine integers. 'frama-c': Frama-C/WP deductive verification — consumes the synthesized ACSL loop invariants/contracts natively, mathematical integers, discharges base+preservation+goal for UNBOUNDED loops and aggregate (\\sum) invariants CBMC can't. Requires frama-c + an SMT prover (e.g. alt-ergo) on PATH. An explicit value always wins.",
    )
    ver.add_argument(
        "--standalone-unwind",
        type=int,
        default=0,
        help="Loop-unwinding bound for --standalone / --synth-loop-invariants modes. 0 (default) means: --standalone uses 64; --synth-loop-invariants auto-derives bound+2 from a literal loop bound. With --unwinding-assertions on, an undersized bound is reported, not silently assumed.",
    )
    ver.add_argument(
        "--threat-model",
        choices=["security", "safety", "functional"],
        default="security",
        help="Threat model: shapes CBMC baseline flags, spec prompts, and realism context (default: security)",
    )
    ver.add_argument(
        "--threat-model-context",
        default=None,
        metavar="PATH_OR_TEXT",
        help="Trust-boundary note for THIS target (path to a file, or inline text): which inputs are attacker-controlled vs. caller/hardware-guaranteed. Injected into spec-gen, refinement, classifier, dynamic-validation and realism so the precondition is shaped correctly at generation time. Conservative default (treat inputs as attacker-controlled) applies when omitted.",
    )
    ver.add_argument(
        "--lite-mode",
        action="store_true",
        default=False,
        help=(
            "bmc-agent-lite: skip the LLM spec_gen call for every function "
            "(every function gets a permissive pre=post=true spec) and also "
            "skip Pass 1.5 domain-knowledge extraction. CBMC's built-in checks "
            "(--bounds-check / --pointer-check / --signed-overflow-check) "
            "surface memory-safety bugs directly from nondet harness inputs. "
            "LLM budget shifts to realism + classifier in Phase 3, where the "
            "LLM adds real signal rather than parroting the function body. "
            "Pairs well with --raw-bytes."
        ),
    )
    ver.add_argument(
        "--legacy-spec-gen",
        action="store_true",
        default=False,
        help=(
            "Use the legacy v1 SpecGenerator instead of the default v2 "
            "(caller-grounded, evidence-tagged). v1 drafts from the function "
            "body alone; v2 reconciles body + observed call sites + doc "
            "annotations + signature patterns. Use this flag only for parity "
            "comparison against historical runs — v2 should otherwise be "
            "strictly better on internal functions."
        ),
    )
    ver.add_argument(
        "--enable-spec-refiner",
        action="store_true",
        default=False,
        help=(
            "Enable realism-feedback-driven in-sweep spec refinement. When "
            "realism rejects a CEx with verdict=UNREALISTIC + concrete "
            "key_concern, spec_refiner asks the LLM for the precise clause "
            "that would exclude the rejected CEx, re-runs BMC, and applies "
            "the soundness acceptance check (targeted CEx gone AND no "
            "previously-realistic CEx silently dropped). Opt-in."
        ),
    )
    ver.add_argument(
        "--enable-inlining-advisor",
        action="store_true",
        default=False,
        help=(
            "Enable LLM-driven inline-vs-stub decisions for callees the "
            "mechanical rule (file-local static, ≤30 LoC, no loops/alloc/"
            "recursion) marked STUB. The advisor reconsiders them per-caller "
            "in a batched LLM call; may PROMOTE small predicates / getters / "
            "accessors to inline when stubbing would produce stub-disconnect "
            "FPs. Bounded — never demotes; default STUB on uncertainty. "
            "Default ON; pass --no-inlining-advisor to disable."
        ),
    )
    # --no-<flag> escape hatches. The six AI layers
    # (realism / dynamic-validation / flag-selection / feedback-loop /
    # spec-refiner / inlining-advisor) are default-on as the recommended
    # bug-hunting pipeline. These flags turn them OFF individually for
    # ablations, parity comparisons, or zero-LLM-cost smoke runs.
    ver.add_argument("--no-realism-check", action="store_true", default=False,
                     help="Disable the LLM realism filter on CExs.")
    ver.add_argument("--no-dynamic-validation", action="store_true", default=False,
                     help="Disable building + running the GCC reproducer.")
    ver.add_argument("--no-flag-selection", action="store_true", default=False,
                     help="Disable per-function CBMC flag selection.")
    ver.add_argument("--no-feedback-loop", action="store_true", default=False,
                     help="Disable in-sweep realism-driven feedback loop.")
    ver.add_argument("--no-global-invariants", action="store_true", default=False,
                     help="Disable evidence-grounded global-invariant assumes "
                          "(bmc_agent/global_invariants.py, harness Step 1.5c).")
    ver.add_argument("--per-function-time-budget", type=int, default=None,
                     metavar="SECONDS",
                     help="Total CBMC wall-clock budget per function across all "
                          "phases (0 = unlimited; default 1200). Past it, further "
                          "checks short-circuit to unresolved (timeout) instead "
                          "of grinding on a pathological parser fn.")
    ver.add_argument("--no-spec-refiner", action="store_true", default=False,
                     help="Disable in-sweep realism-feedback-driven spec refiner.")
    ver.add_argument("--reachability-grounding", choices=["off", "shadow", "live", "uniform"],
                     default="off", dest="reachability_grounding",
                     help="Channel-guarded grounded-reachability on confirmed_dynamic findings. "
                          "'shadow' logs what it WOULD do (no verdict change); 'live' demotes "
                          "arg-driven, grounded-unreachable crashes to 'unlikely'; 'uniform' "
                          "applies the full new tier model (evidence-quality x reachability -> "
                          "confirmed|likely|unlikely), re-tiering weak unit-harness crashes off "
                          "the top tier. Fail-safe: never demotes channel-driven/uncertain bugs.")
    ver.add_argument("--harness-refinement", choices=["off", "shadow", "live"],
                     default="off", dest="harness_refinement",
                     help="Phase-1 harness-refinement (realism-enforcement). On a confirmed_dynamic "
                          "crash whose unit harness left a boot-init-trusted EXTERN global at its "
                          "NULL/0 default, MATERIALIZE that global (calloc(1,sizeof)) and RE-RUN: "
                          "refined harness clean => NULL-default artifact; still crashes => real. "
                          "'shadow' logs the would-be decision (no verdict change); 'live' demotes "
                          "a confirmed NULL-default artifact to 'unlikely'. Sound: a real OOB still "
                          "faults on the 1-element buffer, so a genuine bug is never demoted.")
    ver.add_argument("--enable-soundness-gate", action="store_true", default=False,
                     dest="enable_soundness_gate",
                     help="Caller-grounded soundness gate on refinement: block a "
                          "refiner clause that isn't caller-guaranteed (keeps the CEx "
                          "as a real-bug lead instead of assuming it away). Best with "
                          "--specs-via-claude-code --claude-code-agentic.")
    ver.add_argument("--enforce-spec-refiner-retier", action="store_true", default=False,
                     dest="enforce_spec_refiner_retier",
                     help="Soundness-policy compliance (realism-enforcement Phase 2): when the "
                          "spec-refiner's clause excludes the CEx but is NOT deterministically "
                          "caller-checked (the SoundnessAgent is agentic), RE-TIER the finding to "
                          "'unlikely' instead of deleting it (marking VERIFIED CLEAN). Strictly "
                          "more conservative for soundness; default off (does not change --agentic).")
    ver.add_argument("--enable-agentic-harness-repair", action="store_true", default=False,
                     dest="enable_agentic_harness_repair",
                     help="On a CBMC harness BUILD error (conversion / incomplete-type / "
                          "parse), rebuild the harness with the agentic code-reading "
                          "generator and re-run. Fires only on build errors.")
    ver.add_argument("--enable-classifier", action="store_true", default=False,
                     dest="enable_classifier",
                     help="Force the CEx classifier ON (REAL/SPURIOUS/UNRESOLVED + the "
                          "spurious->refinement->soundness-gate loop). On by default, "
                          "including under --agentic; this overrides "
                          "BMC_AGENT_ENABLE_CLASSIFIER=false.")
    ver.add_argument("--enable-triage", action="store_true", default=False,
                     dest="enable_triage",
                     help="Re-enable Phase-3e triage of UNRESOLVED counterexamples "
                          "(independent of classifier/realism).")
    ver.add_argument("--no-inlining-advisor", action="store_true", default=False,
                     help="Disable LLM inline-vs-stub advisor.")
    ver.add_argument("--no-spec-gen-tools", action="store_true", default=False,
                     help="Disable v2.2 spec_gen bounded tool-use branch.")
    ver.add_argument("--no-realism-tools", action="store_true", default=False,
                     help="Disable realism check's bounded tool-use augmentation.")
    ver.add_argument("--enable-realism-tools", action="store_true", default=False,
                     help="Opt in to the multi-turn tool-use realism augmentation "
                          "(under --agentic the realism check is lightweight/non-tool "
                          "by default; this re-enables the tool loop).")
    ver.add_argument("--minimal", action="store_true", default=False,
                     help=("Turn ALL default-on AI layers off (realism, "
                           "dyn-val, flag-selection, feedback-loop, spec-refiner, "
                           "inlining-advisor, spec-gen-tools). Equivalent to "
                           "passing every --no-* individually. For "
                           "zero-LLM-cost smoke runs."))
    _add_model_arg(ver)
    _add_provider_args(ver)
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
    _add_provider_args(ab)
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
        "--functions", default="",
        help="Comma-separated function names: build the cross-file call graph over the "
             "whole dir but VERIFY only these functions (coaudit audit-flagged path with "
             "full cross-file spec gen + refinement). Empty = verify all.",
    )
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
        "--enable-feedback-loop",
        action="store_true",
        default=False,
        help="Distill UNREALISTIC realism rejections into learned constraints or code-change TODOs. "
             "Persists to <output>/learned_constraints.json so subsequent sweeps stop re-producing "
             "the same artifact pattern.",
    )
    vd.add_argument(
        "--feedback-max-iters",
        type=int,
        default=None,
        help="In-sweep convergence: after distilling a clause, re-run CBMC on the same function up to N times "
             "until the function verifies clean, a REALISTIC verdict emerges, or the same CE class repeats. "
             "Default 3.",
    )
    vd.add_argument(
        "--real-libc",
        action="store_true",
        default=False,
        help="Real-libc mode: harness #includes the source .c file and lets CBMC do all preprocessing via -I. Required for real-world glibc-using OSS.",
    )
    vd.add_argument(
        "--strict-dsl",
        action="store_true",
        default=False,
        help="Strict-formal Phase 1 prompts: pre/post must be a single C boolean expression. Required for bounty/CVE work.",
    )
    vd.add_argument(
        "--raw-bytes",
        action="store_true",
        default=False,
        help="Treat single char* / const char* params as raw byte buffers (no NUL termination). Required for wire-format parsers.",
    )
    vd.add_argument(
        "-D", "--define",
        action="append",
        default=[],
        dest="defines",
        help="Pass a preprocessor define to CBMC (repeatable). Use NAME or NAME=VALUE. Required for build-config-driven C codebases.",
    )
    vd.add_argument(
        "--threat-model",
        choices=["security", "safety", "functional"],
        default="security",
        help="Threat model: shapes CBMC baseline flags, spec prompts, and realism context (default: security)",
    )
    vd.add_argument(
        "--threat-model-context",
        default=None,
        metavar="PATH_OR_TEXT",
        help="Trust-boundary note for THIS target (path to a file, or inline text): which inputs are attacker-controlled vs. caller/hardware-guaranteed. Injected into spec-gen, refinement, classifier, dynamic-validation and realism so the precondition is shaped correctly at generation time. Conservative default (treat inputs as attacker-controlled) applies when omitted.",
    )
    vd.add_argument(
        "--lite-mode",
        action="store_true",
        default=False,
        help=(
            "bmc-agent-lite: skip the LLM spec_gen call for every function "
            "(permissive pre=post=true spec) and skip Pass 1.5 domain extraction. "
            "CBMC built-in checks surface memory-safety bugs from nondet harness "
            "inputs; LLM budget shifts to realism + classifier in Phase 3. "
            "Pairs well with --raw-bytes."
        ),
    )
    vd.add_argument(
        "--legacy-spec-gen",
        action="store_true",
        default=False,
        help=(
            "Use the legacy v1 SpecGenerator instead of the default v2 "
            "(caller-grounded, evidence-tagged). See `verify --help` for the "
            "full explanation. Use only for parity comparison."
        ),
    )
    vd.add_argument(
        "--enable-spec-refiner",
        action="store_true",
        default=False,
        help="See `verify --help` for the full explanation. Opt-in.",
    )
    vd.add_argument(
        "--enable-inlining-advisor",
        action="store_true",
        default=False,
        help="Default ON; pass --no-inlining-advisor to disable.",
    )
    vd.add_argument(
        "--enable-phase-3e-triage",
        action="store_true",
        default=False,
        help=(
            "Phase 3e: in-pipeline TriageToolsAgent oracle on UNRESOLVED "
            "counterexamples. Promotes REAL_BUG/high verdicts to bug "
            "reports and writes per-CEx triage.json sidecars matching "
            "scripts/triage_unresolved.py. Default OFF (expensive)."
        ),
    )
    # --no-<flag> escape hatches (mirror --verify).
    vd.add_argument("--no-realism-check", action="store_true", default=False,
                    help="Disable the LLM realism filter on CExs.")
    vd.add_argument("--no-dynamic-validation", action="store_true", default=False,
                    help="Disable building + running the GCC reproducer.")
    vd.add_argument("--no-flag-selection", action="store_true", default=False,
                    help="Disable per-function CBMC flag selection.")
    vd.add_argument("--no-feedback-loop", action="store_true", default=False,
                    help="Disable in-sweep realism-driven feedback loop.")
    vd.add_argument("--no-global-invariants", action="store_true", default=False,
                    help="Disable evidence-grounded global-invariant assumes "
                         "(bmc_agent/global_invariants.py, harness Step 1.5c).")
    vd.add_argument("--per-function-time-budget", type=int, default=None,
                    metavar="SECONDS",
                    help="Total CBMC wall-clock budget per function across all "
                         "phases (0 = unlimited; default 1200). Past it, further "
                         "checks short-circuit to unresolved (timeout).")
    vd.add_argument("--no-spec-refiner", action="store_true", default=False,
                    help="Disable in-sweep realism-feedback-driven spec refiner.")
    vd.add_argument("--enable-soundness-gate", action="store_true", default=False,
                    dest="enable_soundness_gate",
                    help="Caller-grounded soundness gate on refinement: block a "
                         "refiner clause that isn't caller-guaranteed (keeps the CEx "
                         "as a real-bug lead instead of assuming it away). Best with "
                         "--specs-via-claude-code --claude-code-agentic.")
    vd.add_argument("--enforce-spec-refiner-retier", action="store_true", default=False,
                    dest="enforce_spec_refiner_retier",
                    help="Soundness-policy compliance (realism-enforcement Phase 2): RE-TIER "
                         "a spec-refiner accept to 'unlikely' instead of deleting it when the "
                         "clause is not deterministically caller-checked. Default off.")
    vd.add_argument("--enable-agentic-harness-repair", action="store_true", default=False,
                    dest="enable_agentic_harness_repair",
                    help="On a CBMC harness BUILD error (conversion / incomplete-type / "
                         "parse), rebuild the harness with the agentic code-reading "
                         "generator and re-run. Fires only on build errors.")
    vd.add_argument("--enable-classifier", action="store_true", default=False,
                    dest="enable_classifier",
                    help="Force the CEx classifier ON (REAL/SPURIOUS/UNRESOLVED + the "
                         "spurious->refinement->soundness-gate loop). On by default, "
                         "including under --agentic; this overrides "
                         "BMC_AGENT_ENABLE_CLASSIFIER=false.")
    vd.add_argument("--enable-triage", action="store_true", default=False,
                    dest="enable_triage",
                    help="Re-enable Phase-3e triage of UNRESOLVED counterexamples "
                         "(independent of classifier/realism).")
    vd.add_argument("--no-inlining-advisor", action="store_true", default=False,
                    help="Disable LLM inline-vs-stub advisor.")
    vd.add_argument("--no-spec-gen-tools", action="store_true", default=False,
                    help="Disable v2.2 spec_gen bounded tool-use branch.")
    vd.add_argument("--no-realism-tools", action="store_true", default=False,
                    help="Disable realism check's bounded tool-use augmentation.")
    vd.add_argument("--enable-realism-tools", action="store_true", default=False,
                    help="Opt in to the multi-turn tool-use realism augmentation "
                         "(under --agentic the realism check is lightweight/non-tool "
                         "by default; this re-enables the tool loop).")
    vd.add_argument("--no-phase-3e-triage", action="store_true", default=False,
                    help="Disable Phase 3e triage even if env BMC_AGENT_ENABLE_PHASE_3E_TRIAGE=true.")
    vd.add_argument("--minimal", action="store_true", default=False,
                    help=("Turn ALL default-on AI layers off. For "
                          "zero-LLM-cost smoke runs / ablations."))
    vd.add_argument(
        "--follow-adjacent-rounds",
        type=int,
        default=0,
        metavar="N",
        help=(
            "After the main sweep, harvest realism_check.adjacent_bugs[] from "
            "every bug_report.json and re-run the pipeline on each referenced "
            "function. Round-N outputs go to <output>/adjacent_round_N/. "
            "Default 0 (disabled). Recommended: 1 for MVP."
        ),
    )
    _add_model_arg(vd)
    _add_provider_args(vd)
    vd.set_defaults(func=_cmd_verify_dir)

    # --- autonomous ---
    au = subparsers.add_parser(
        "autonomous",
        help=(
            "Run verify-dir in a round-based loop with auto-retry, "
            "convergence detection, and per-round summaries. "
            "Implements Phase 2 of PLAN_autonomous_mode.md."
        ),
    )
    au.add_argument("--source-dir", required=True, help="Directory containing .c files")
    au.add_argument("--driver", required=True, help="Driver name prefix")
    au.add_argument("--output", default="artifacts", help="Artifact directory")
    au.add_argument(
        "--include-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Add an include directory for C preprocessing (repeatable)",
    )
    au.add_argument(
        "--domain-knowledge",
        default="",
        metavar="TEXT_OR_FILE",
        help="Domain knowledge string or path to a file",
    )
    au.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Glob pattern of filenames to skip (repeatable)",
    )
    au.add_argument(
        "--max-rounds",
        type=int,
        default=3,
        help="Maximum number of autonomous rounds before stopping (default: 3)",
    )
    au.add_argument(
        "--target-coverage",
        type=float,
        default=0.80,
        help="Stop early when per-file CBMC coverage reaches this fraction (default: 0.80)",
    )
    au.add_argument(
        "--enable-dynamic-validation",
        action="store_true",
        default=False,
        help="Phase 3: compile + run a GCC harness to confirm bugs at runtime",
    )
    au.add_argument(
        "--real-libc",
        action="store_true",
        default=False,
        help="Real-libc mode: harness #includes the source .c file and lets CBMC do all preprocessing via -I",
    )
    au.add_argument(
        "--raw-bytes",
        action="store_true",
        default=False,
        help="Treat single char* / const char* params as raw byte buffers (no NUL termination)",
    )
    au.add_argument(
        "-D", "--define",
        action="append",
        default=[],
        dest="defines",
        help="Pass a preprocessor define to CBMC (repeatable). NAME or NAME=VALUE.",
    )
    au.add_argument(
        "--threat-model",
        choices=["security", "safety", "functional"],
        default="security",
        help="Threat model: shapes CBMC baseline flags and realism context (default: security)",
    )
    au.add_argument(
        "--threat-model-context",
        default=None,
        metavar="PATH_OR_TEXT",
        help="Trust-boundary note for THIS target (path to a file, or inline text): which inputs are attacker-controlled vs. caller/hardware-guaranteed. Injected into spec-gen, refinement, classifier, dynamic-validation and realism so the precondition is shaped correctly at generation time. Conservative default (treat inputs as attacker-controlled) applies when omitted.",
    )
    au.add_argument(
        "--allow-self-patch",
        choices=["deny", "stage", "auto"],
        default="deny",
        help=(
            "Phase 3 (self-patch agent) mode. 'deny' (default): the "
            "agent is off and CBMC errors with no registered retry "
            "action stay errored. 'stage': when Phase 2b exhausts its "
            "actionable plans, the agent asks an LLM to propose a "
            "patch to bmc_agent/harness_generator.py (or preprocessor.py) "
            "plus a regression test; on passing all safety gates the "
            "diff is written to <output>/<driver>/proposed_patches/ "
            "for operator review (working tree stays clean). 'auto': "
            "same as stage plus git-apply + commit (only after every "
            "gate passes). See bmc_agent/self_patch_agent.py for the "
            "gate logic."
        ),
    )
    _add_model_arg(au)
    _add_provider_args(au)
    au.set_defaults(func=_cmd_autonomous)

    # --- self-patch-review ---
    spr = subparsers.add_parser(
        "self-patch-review",
        help=(
            "Walk a sweep's ``proposed_patches/`` directory and print "
            "a digest of every staged self-patch proposal — error "
            "class, rationale, files touched, manual-apply commands. "
            "Use this after an autonomous run with --allow-self-patch=stage."
        ),
    )
    spr.add_argument(
        "--proposals-dir",
        required=True,
        metavar="DIR",
        help=(
            "Path to the proposed_patches directory (e.g. "
            "<output>/<driver>/proposed_patches/)."
        ),
    )
    spr.add_argument(
        "--show-diff",
        action="store_true",
        default=False,
        help="Print the full unified diff for each proposal (default: just the digest)",
    )
    spr.set_defaults(func=_cmd_self_patch_review)

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

    # ------------------------------------------------------------------
    # judge-dir — simple LLM-as-judge mode (bypasses classifier/realism/
    # refinement/feedback-loop). See bmc_agent/judge_pipeline.py.
    # ------------------------------------------------------------------
    jd = subparsers.add_parser(
        "judge-dir",
        help=(
            "Simple LLM-as-judge mode: for each CBMC counterexample, hand "
            "everything to one tool-using LLM call. No multi-stage pipeline."
        ),
    )
    jd.add_argument("--source-dir", required=True, help="Directory containing .c files")
    jd.add_argument("--driver", required=True, help="Driver name prefix")
    jd.add_argument("--output", default="judge_out", help="Artifact directory")
    jd.add_argument("--include-dir", action="append", default=[],
                    help="Add include directory for preprocessing (repeatable)")
    jd.add_argument("-D", "--define", dest="defines", action="append", default=[],
                    help="Preprocessor define (repeatable)")
    jd.add_argument("--exclude", action="append", default=[],
                    help="Glob pattern of filenames to skip (repeatable)")
    jd.add_argument("--cbmc-unwind", type=int, default=4)
    jd.add_argument("--cbmc-timeout", type=int, default=60)
    jd.add_argument("--max-functions", type=int, default=None,
                    help="Cap total functions judged (for smoke testing)")
    jd.add_argument("--only-file", action="append", default=None,
                    help="Only run on these files (basename or stem; repeatable)")
    jd.add_argument(
        "--enable-flag-selection",
        action="store_true",
        default=False,
        help="Phase 1.5: per-function LLM-picked CBMC flags (unsigned-overflow / "
             "conversion / pointer-overflow). Default off.",
    )
    jd.add_argument(
        "--enable-feedback-loop",
        action="store_true",
        default=False,
        help="Distill UNREALISTIC/UNCERTAIN verdicts into learned constraints "
             "(persisted to <output>/<driver>/learned_constraints.json) "
             "and apply them to subsequent harness gens. WARNING: can quietly "
             "kill real bugs (see feedback_llm_as_judge memory).",
    )
    jd.add_argument(
        "--agentic-harness",
        action="store_true",
        default=False,
        help="Use the LLM-driven harness builder (bmc_agent/agentic_harness_gen.py) "
             "instead of the deterministic HarnessGenerator. The LLM reads "
             "callees/callers, decides per-callee stub-vs-inline, sizes buffers "
             "to match real callers, and emits a complete harness. Falls back "
             "to deterministic gen on failure.",
    )
    jd.add_argument(
        "--refine-rounds",
        type=int,
        default=0,
        metavar="N",
        help="When the judge rules a CEx UNREALISTIC/UNCERTAIN and "
             "--agentic-harness is on, hand verdict reasoning + harness + "
             "witness back to the agentic generator and re-run CBMC up to N "
             "rounds. Stops on REALISTIC, clean, or budget exhaustion. The "
             "LLM (not a regex) decides whether/how to incorporate the "
             "judge's reasoning, so this avoids the legacy feedback-loop "
             "failure mode of regex-distilled __CPROVER_assume killing real "
             "bugs. Default 0 (disabled). Recommended starting value: 1.",
    )
    _add_model_arg(jd)
    _add_provider_args(jd)
    jd.set_defaults(func=_cmd_judge_dir)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
