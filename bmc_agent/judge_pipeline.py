"""
Simple LLM-as-judge pipeline.

Skips the multi-stage classifier → realism → refinement → feedback-loop
pipeline and instead, for each CBMC counterexample, hands everything to a
single tool-using LLM judge (bmc_agent.llm_judge.JudgeAgent). The judge
decides REALISTIC / UNREALISTIC / UNCERTAIN and on UNREALISTIC verdicts
also searches for adjacent bugs in the same function or nearby code.

Per-CEx result lands in:
  ``<output>/<driver>/<file_stem>/<function>/judge_<property>.json``

A summary report is written to ``<output>/<driver>/summary.json``.

This intentionally does NOT use spec_gen / refinement / feedback_loop / the
pattern-detector filters — see [[feedback-llm-as-judge]] in user memory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from bmc_agent.cbmc import run_cbmc
from bmc_agent.config import Config
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.llm_judge import JudgeAgent, JudgeResult
from bmc_agent.logger import get_logger
from bmc_agent.parser import parse_c_file, ParsedCFile
from bmc_agent.preprocessor import preprocess
from bmc_agent.spec import Spec, SpecStatus

# Use the project logger so INFO lines reach the sweep.log file/console
# handlers configured by bmc_agent.logger.get_logger. The previous
# `logging.getLogger("judge_pipeline")` bypassed those handlers and
# silently dropped every INFO call this module makes.
logger = get_logger("judge_pipeline")


# Confidence levels that warrant a BMC confirmation pass for an adjacent bug.
_ADJACENT_CONFIRM_CONFIDENCE = {"high", "medium"}

# Maximum number of adjacent_bugs to BMC-confirm per CEx. The LLM sometimes
# emits 5-7 hypotheses; processing all of them serializes BMC re-runs and
# slows the sweep dramatically. Cap to top-3 by confidence (high > medium).
_MAX_ADJACENT_TO_CONFIRM = 3

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "": 0, None: 0}

# Default location for the libarchive build (used by dynamic validation when
# the user hasn't supplied a libarchive-build-dir override).
_DEFAULT_LIBARCHIVE_BUILD = "/tmp/libarchive_bench/libarchive/build/libarchive"
_DEFAULT_LIBARCHIVE_INC = "/tmp/libarchive_bench/libarchive/libarchive"
_DYN_VAL_TIMEOUT_S = 20

# Default CBMC unwind when re-verifying an adjacent-bug hypothesis. The
# primary CBMC run typically uses unwind=4 to keep the per-function cost
# low. But LLM-hypothesized bugs (integer overflow on long inputs, many-
# iteration overruns, etc.) often need more loop depth than 4 to surface.
# Empirically: LLM flagged size_t overflow in archive_acl_text_len's digit
# loop; unwind=4 hit an unwinding assertion before reaching the overflow
# point. Bumping to 16 by default; can be overridden per-call.
_ADJACENT_CONFIRM_UNWIND_DEFAULT = 16

# Refinement harness re-runs use a larger budget than the primary CBMC pass.
# When the LLM closes a havoc'd-extern gap (e.g., wcslen returning UINT64-1)
# by adding stubs or wider assumes, the new code typically exercises loops
# that the primary unwind=4/60s can't cover. Empirically 3 of 3 refinements
# on append_entry_w timed out at 60s with the primary budget.
_REFINE_CBMC_UNWIND_DEFAULT = 16
_REFINE_CBMC_TIMEOUT_DEFAULT = 180

# Pseudocode tokens the LLM emits that CBMC cannot compile. Learned
# constraints containing any of these are dropped before conjoining
# into the harness PRE (otherwise the next harness compile errors out).
# Detected at conjoin time rather than at persist time so the
# learned_constraints.json still records what the LLM said, but the
# unsafe clauses are skipped on application.
_PSEUDOCODE_TOKENS = (
    "valid(",          # not a C function; CBMC uses __CPROVER_valid_pointer
    "assume(",         # would re-wrap an already-implicit assume
    "require(",        # contract DSL, not C
    "forall ",         # quantifier, not C boolean
    "exists ",
    "ensures(",
    "invariant(",
    "old(",
    "result",          # contract-style $result placeholder
    "\\old",
    "\\result",
    "\\valid",
)


def _is_safe_clause(clause: str) -> bool:
    """Reject clauses that contain pseudocode or contract-DSL tokens
    CBMC cannot compile. Conservative: when in doubt, drop the clause."""
    if not clause or clause.strip() in {"", "true", "1"}:
        return False
    low = clause.lower()
    for tok in _PSEUDOCODE_TOKENS:
        if tok.lower() in low:
            return False
    # Must contain at least one C-shaped token (an identifier or operator)
    # to look like a real boolean expression.
    return any(ch in clause for ch in "=<>!&|") or "==" in clause or "NULL" in clause


def _prop_type(prop: str) -> str:
    """Extract property type from a CBMC failing-property string.
    ``next_field.pointer_dereference.83`` -> ``pointer_dereference``"""
    if not prop or "." not in prop:
        return prop or "unknown"
    parts = prop.split(".")
    return parts[-2] if parts[-1].isdigit() else parts[-1]


def _dedup_counterexamples(cexs, max_per_type: int = 3):
    seen: dict[str, int] = {}
    out = []
    for cex in cexs or []:
        t = _prop_type(getattr(cex, "failing_property", "") or "")
        if seen.get(t, 0) >= max_per_type:
            continue
        seen[t] = seen.get(t, 0) + 1
        out.append(cex)
    return out


def _safe_filename(text: str, max_len: int = 100) -> str:
    out = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)
    return out[:max_len] or "unnamed"


def _extract_function_from_location(loc: str, known_fns: set) -> Optional[str]:
    """Best-effort: parse 'archive_acl.c:1911-1913 (function archive_acl_from_text_nl)'
    or 'archive_acl.c:2105-2139 (next_field)' or 'foo (bar.c:N)' and return the
    function name when it's one of the known fns in the corpus.
    """
    if not loc or not known_fns:
        return None
    # Pattern: "(function fname)" or "(fname)"
    m = re.search(r"\(\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[:)]", loc)
    if m and m.group(1) in known_fns:
        return m.group(1)
    # Pattern: "fname (..." at the start
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", loc)
    if m and m.group(1) in known_fns:
        return m.group(1)
    # Pattern: bare identifier somewhere matching a known function
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", loc):
        if tok in known_fns:
            return tok
    return None


def _dynamic_validate_bug(
    *,
    func,
    attacker_scenario: str,
    parsed_file: ParsedCFile,
    libarchive_build_dir: Optional[str] = None,
    libarchive_include_dir: Optional[str] = None,
    llm=None,
    out_dir: Path,
) -> dict:
    """For a realism-realistic bug, run the iterative reproducer loop:
    generate a C reproducer, compile with ASan+UBSan, run, and on
    no-crash re-prompt the LLM with the stderr/exit-code feedback up to
    MAX_ATTEMPTS times. Returns the strongest outcome across attempts
    plus a per-attempt history (see reproducer_loop.run_reproducer_loop).
    """
    if llm is None:
        return {"outcome": "skipped", "reason": "no LLM client provided"}

    libarchive_build = libarchive_build_dir or _DEFAULT_LIBARCHIVE_BUILD
    libarchive_inc = libarchive_include_dir or _DEFAULT_LIBARCHIVE_INC

    try:
        from bmc_agent.reproducer_loop import run_reproducer_loop
        return run_reproducer_loop(
            func=func,
            attacker_scenario=attacker_scenario,
            parsed_file=parsed_file,
            llm=llm,
            out_dir=out_dir,
            libarchive_build=libarchive_build,
            libarchive_inc=libarchive_inc,
        )
    except Exception as exc:
        return {"outcome": "skipped", "reason": f"reproducer_loop exc: {exc}"}


def _refine_harness_loop(
    *,
    config: Config,
    parsed_files: dict,
    all_funcs_global: dict,
    source_dir: Path,
    include_dirs: list,
    defines: list,
    func,
    initial_harness: str,
    initial_cex,
    initial_judge,
    cbmc_unwind: int,
    cbmc_timeout: int,
    max_rounds: int,
    flag_extras: Optional[dict] = None,
) -> list[dict]:
    """Bounded refinement: hand UNREALISTIC verdict + harness + witness
    back to the agentic generator, re-run CBMC, re-judge. Repeat until
    REALISTIC, clean, or max_rounds reached. Returns a list of round
    records (one per refinement attempt).
    """
    from bmc_agent.agentic_harness_gen import AgenticHarnessGen
    from bmc_agent.llm_judge import JudgeAgent

    rounds: list[dict] = []
    current_harness = initial_harness
    current_cex = initial_cex
    current_judge = initial_judge
    flag_extras = flag_extras or {}

    for round_idx in range(1, max_rounds + 1):
        try:
            agen = AgenticHarnessGen(
                config=config,
                parsed_files=parsed_files,
                corpus_root=source_dir,
            )
            refined = agen.refine(
                func=func,
                all_funcs_global=all_funcs_global,
                prior_harness=current_harness,
                failing_property=getattr(current_cex, "failing_property", "") or "",
                judge_verdict=current_judge.verdict,
                judge_reasoning=current_judge.reasoning or "",
                witness=dict(getattr(current_cex, "variable_assignments", {}) or {}),
                cbmc_trace_excerpt=list(getattr(current_cex, "trace", []) or [])[:80],
                include_dirs=include_dirs,
                defines=defines,
            )
        except Exception as exc:
            rounds.append({
                "round": round_idx,
                "outcome": "refine_error",
                "error": str(exc)[:200],
            })
            return rounds

        if not refined.harness or refined.last_compile_error:
            rounds.append({
                "round": round_idx,
                "outcome": "refine_failed",
                "compile_error": (refined.last_compile_error or "")[:400],
                "rationale": (refined.rationale or "")[:400],
            })
            return rounds

        new_harness = refined.harness
        no_change = new_harness.strip() == current_harness.strip()

        # Re-run CBMC on the refined harness.
        # Larger budget than the primary pass: refinements that close a
        # havoc'd-extern gap (e.g., wcslen returning UINT64-1) frequently
        # add stubs / wider assumes that exercise loops the primary
        # unwind=4 / 60s can't cover.
        # If the refining LLM emitted its own cbmc_budget, honour it (still
        # floored by the refine defaults so we don't shrink the budget the
        # initial CBMC had).
        refine_unwind = max(cbmc_unwind, _REFINE_CBMC_UNWIND_DEFAULT)
        refine_timeout = max(cbmc_timeout, _REFINE_CBMC_TIMEOUT_DEFAULT)
        refined_budget = getattr(refined, "cbmc_budget", None) or {}
        if "unwind" in refined_budget:
            refine_unwind = max(refine_unwind, int(refined_budget["unwind"]))
        if "timeout_s" in refined_budget:
            refine_timeout = max(refine_timeout, int(refined_budget["timeout_s"]))
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".c", delete=False,
        ) as f:
            f.write(new_harness)
            hp = f.name
        try:
            try:
                new_cbmc = run_cbmc(
                    harness_path=hp,
                    unwind=refine_unwind, timeout=refine_timeout,
                    include_dirs=include_dirs or [], defines=defines or [],
                    bounds_check=True, pointer_check=True,
                    signed_overflow_check=True, div_by_zero_check=True,
                    unsigned_overflow_check=bool(flag_extras.get("unsigned_overflow_check")),
                    conversion_check=bool(flag_extras.get("conversion_check")),
                    pointer_overflow_check=bool(flag_extras.get("pointer_overflow_check")),
                )
            except Exception as exc:
                rounds.append({
                    "round": round_idx,
                    "outcome": "cbmc_error",
                    "error": str(exc)[:200],
                    "rationale": (refined.rationale or "")[:400],
                    "no_harness_change": no_change,
                })
                return rounds
        finally:
            try: os.unlink(hp)
            except OSError: pass

        if new_cbmc.error:
            rounds.append({
                "round": round_idx,
                "outcome": "cbmc_error",
                "error": new_cbmc.error[:200],
                "rationale": (refined.rationale or "")[:400],
                "no_harness_change": no_change,
            })
            return rounds

        if new_cbmc.verified:
            rounds.append({
                "round": round_idx,
                "outcome": "verified_clean",
                "rationale": (refined.rationale or "")[:400],
                "no_harness_change": no_change,
            })
            return rounds

        new_cexs = _dedup_counterexamples(
            new_cbmc.counterexamples, max_per_type=2,
        )
        if not new_cexs:
            rounds.append({
                "round": round_idx,
                "outcome": "no_cex_after_dedup",
                "rationale": (refined.rationale or "")[:400],
                "no_harness_change": no_change,
            })
            return rounds

        # Pick the CEx whose failing_property matches the prior one (same
        # bug-class) if possible, else the first.
        prior_prop = getattr(current_cex, "failing_property", "") or ""
        next_cex = next(
            (c for c in new_cexs if c.failing_property == prior_prop),
            new_cexs[0],
        )

        # Re-judge using the same simple JudgeAgent (no refinement-specific
        # prompt — the judge sees the new harness + CEx fresh).
        try:
            j = JudgeAgent(
                config=config,
                parsed_files=parsed_files,
                corpus_root=source_dir,
                harness_source=new_harness,
                cbmc_rerun_callback=None,
            )
            new_judge = j.judge(func=func, counterexample=next_cex, cbmc_result=new_cbmc)
        except Exception as exc:
            rounds.append({
                "round": round_idx,
                "outcome": "judge_error",
                "error": str(exc)[:200],
                "rationale": (refined.rationale or "")[:400],
                "no_harness_change": no_change,
            })
            return rounds

        rounds.append({
            "round": round_idx,
            "outcome": (
                "realistic" if new_judge.verdict == "realistic"
                else "still_unrealistic" if new_judge.verdict == "unrealistic"
                else new_judge.verdict
            ),
            "rationale": (refined.rationale or "")[:400],
            "no_harness_change": no_change,
            "new_failing_property": next_cex.failing_property or "",
            "new_judge": {
                "verdict": new_judge.verdict,
                "confidence": new_judge.confidence,
                "reasoning": (new_judge.reasoning or "")[:1200],
            },
            "n_cex": len(new_cexs),
        })

        if new_judge.verdict == "realistic":
            return rounds
        if no_change:
            # LLM re-emitted the prior harness; no point looping further.
            return rounds

        # Advance to next round
        current_harness = new_harness
        current_cex = next_cex
        current_judge = new_judge

    return rounds


def _confirm_adjacent_via_bmc(
    adjacent_bug: dict,
    *,
    config: Config,
    parsed_files: dict,
    all_funcs_global: dict,
    parsed_for_target: ParsedCFile,
    target_fn_name: str,
    source_dir: Path,
    include_dirs: list,
    defines: list,
    cbmc_unwind: int,
    cbmc_timeout: int,
    flag_selection: Optional[dict] = None,
    primary_fn_name: Optional[str] = None,
    primary_verdict: Optional[str] = None,
    primary_reasoning: Optional[str] = None,
) -> dict:
    """Re-run BMC on the function the LLM flagged as containing an adjacent
    bug, then re-judge any CEx that surfaces. Returns a dict:
      {confirmed: bool, verdict, reasoning, n_cex, target_function, ...}
    """
    target_func = parsed_for_target.get_function_info(target_fn_name)
    if target_func is None:
        return {"confirmed": False, "reason": f"target function '{target_fn_name}' not parsed"}

    spec = Spec(
        function_name=target_fn_name,
        precondition="true", postcondition="true",
        status=SpecStatus.GENERATED,
    )

    try:
        gen = HarnessGenerator(config)
        harness_text = gen.generate_harness(
            func=target_func, spec=spec, parsed_file=parsed_for_target,
            all_funcs=all_funcs_global,
        )
    except Exception as exc:
        return {"confirmed": False, "reason": f"harness gen failed: {exc}"}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
        f.write(harness_text)
        harness_path = f.name
    extra_flags = flag_selection or {}
    try:
        cbmc_res = run_cbmc(
            harness_path=harness_path,
            unwind=cbmc_unwind, timeout=cbmc_timeout,
            include_dirs=include_dirs or [], defines=defines or [],
            bounds_check=True, pointer_check=True,
            signed_overflow_check=True, div_by_zero_check=True,
            unsigned_overflow_check=bool(extra_flags.get("unsigned_overflow_check")),
            conversion_check=bool(extra_flags.get("conversion_check")),
            pointer_overflow_check=bool(extra_flags.get("pointer_overflow_check")),
        )
    except Exception as exc:
        return {"confirmed": False, "reason": f"cbmc failed: {exc}"}
    finally:
        try: os.unlink(harness_path)
        except OSError: pass

    if cbmc_res.error:
        return {"confirmed": False, "reason": f"cbmc error: {cbmc_res.error[:200]}"}
    if cbmc_res.verified:
        return {
            "confirmed": False, "reason": "function verifies clean against the hypothesis",
            "n_cex": 0, "target_function": target_fn_name,
        }

    # CBMC found CExes. Hand the strongest-typed CEx to JudgeAgent with a
    # focused prompt: "the LLM hypothesized X; does this CEx confirm?".
    cexs = _dedup_counterexamples(cbmc_res.counterexamples, max_per_type=2)
    if not cexs:
        return {"confirmed": False, "reason": "no CEx after dedup", "target_function": target_fn_name}

    # Pick the CEx whose property type matches the hypothesized bug type
    hypothesis_type = (adjacent_bug.get("bug_type") or "").lower()
    def _rank(c):
        prop = (c.failing_property or "").lower()
        score = 0
        for kw in ("pointer", "overflow", "deref", "arithmetic", "bounds", "subtraction", "null"):
            if kw in hypothesis_type and kw in prop:
                score += 2
        for kw in ("pointer_dereference", "pointer_arithmetic", "array_bounds", "overflow"):
            if kw in prop:
                score += 1
        return score
    cex = sorted(cexs, key=_rank, reverse=True)[0]

    judge = JudgeAgent(
        config=config,
        parsed_files=parsed_files,
        corpus_root=source_dir,
        harness_source=harness_text,
        cbmc_rerun_callback=None,
    )
    # Inject the hypothesis context into the initial user prompt by stashing
    # a synthetic counterexample-description that mentions the LLM hypothesis.
    # JudgeAgent's _build_initial_context reads getattr(cex, 'description', '').
    #
    # When a primary verdict exists for the function the adjacent bug was
    # surfaced from, include it as PRIOR CONTEXT so the adjacent judge can
    # recognise shared harness-artifact patterns (e.g., the primary CEx on
    # `append_entry` ruled the 5-byte buffer unrealistic; the adjacent CEx on
    # `append_id` previously contradicted that, lacking the caller-chain
    # context the primary judge had assembled).
    prior_block = ""
    if primary_verdict and primary_reasoning:
        prior_block = (
            f"\n\n[PRIOR PRIMARY-CEX VERDICT for related function "
            f"'{primary_fn_name or '?'}']\n"
            f"  Primary verdict: {primary_verdict}\n"
            f"  Primary reasoning: {primary_reasoning}\n"
            f"  Use this as context. Judge this counterexample on its own "
            f"evidence — but if the witness exhibits the same harness gap "
            f"(undersized buffer, unconstrained external return, etc.) that "
            f"the primary judge already classified, weigh that.\n"
        )
    setattr(cex, "description",
            (getattr(cex, "description", "") or "") +
            prior_block +
            f"\n\n[ADJACENT-BUG CONFIRMATION] LLM hypothesis: {adjacent_bug.get('bug_type','')}. "
            f"Attacker scenario: {adjacent_bug.get('attacker_scenario','')}. "
            f"Does this CBMC counterexample confirm the hypothesis?")
    result = judge.judge(func=target_func, counterexample=cex, cbmc_result=cbmc_res)
    confirmed = (result.verdict == "realistic")
    return {
        "confirmed": confirmed,
        "verdict": result.verdict,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "attacker_scenario": result.attacker_scenario,
        "n_cex": len(cexs),
        "target_function": target_fn_name,
        "failing_property": cex.failing_property,
    }


def run_judge_pipeline(
    config: Config,
    source_dir: Path,
    driver: str,
    output: Path,
    exclude_patterns: list = None,
    include_dirs: list = None,
    defines: list = None,
    cbmc_unwind: int = 4,
    cbmc_timeout: int = 60,
    max_functions: Optional[int] = None,
    only_files: Optional[list] = None,
) -> dict:
    """Run the simple judge pipeline across all .c files in ``source_dir``.

    Returns a summary dict with per-function verdicts.
    """
    output = Path(output)
    out_root = output / driver
    out_root.mkdir(parents=True, exist_ok=True)

    src_dir = Path(source_dir)
    files = sorted(src_dir.glob("*.c"))
    if only_files:
        files = [f for f in files if f.name in set(only_files) or f.stem in set(only_files)]
    if exclude_patterns:
        from fnmatch import fnmatch
        files = [f for f in files if not any(fnmatch(f.name, p) for p in exclude_patterns)]

    logger.info("judge_pipeline: %d .c files in %s", len(files), src_dir)

    # Pass 1: preprocess + parse all files (so JudgeAgent has cross-file context)
    parsed_files: dict[str, ParsedCFile] = {}
    for f in files:
        logger.info("Parsing %s", f)
        try:
            expanded = preprocess(
                f, include_dirs=include_dirs or [], defines=defines or [],
            )
            parsed = parse_c_file(f, source_text=expanded)
            parsed_files[str(f)] = parsed
        except Exception as exc:
            logger.warning("Parse failed for %s: %s", f, exc)
            continue

    # Aggregated all_funcs across files (for harness gen's cross-file callees)
    all_funcs_global: dict = {}
    for path, p in parsed_files.items():
        for name in p.functions:
            info = p.get_function_info(name)
            if info is not None:
                all_funcs_global.setdefault(name, info)

    # Pass 1.5: per-function CBMC flag selection (when enabled).
    # The LLM is asked, per function, whether to enable the extra
    # unsigned-overflow / signed-overflow / conversion / pointer-overflow
    # checks. Returns dict[fn_name] -> FlagSelection (bool fields).
    #
    # SKIPPED when --agentic-harness is on: the agentic harness LLM picks
    # the same flags as part of its emit_harness call, using the same
    # function-body analysis it already does. Saves an upfront LLM pass per
    # function (10-20 min on a 200-function corpus). The deterministic-gen
    # fallback path still respects this pass when both flags are set.
    flag_selections_global: dict = {}
    _skip_flag_pass = (
        getattr(config, "enable_agentic_harness", False)
        and getattr(config, "enable_flag_selection", False)
    )
    if _skip_flag_pass:
        logger.info(
            "Flag selection: deferred to agentic harness gen "
            "(--agentic-harness owns per-function flag picks)"
        )
    if getattr(config, "enable_flag_selection", False) and not _skip_flag_pass:
        try:
            from bmc_agent.flag_selector import FlagSelector
            from bmc_agent.llm import LLMClient
            flag_llm = LLMClient(config)
            selector = FlagSelector(config=config, llm=flag_llm)
            # Use the global function map so each function is selected once
            # across files (harnesses still get the per-function dict below).
            flag_selections_global = selector.select_all(all_funcs_global)
            logger.info(
                "Flag selection done: %d functions analyzed", len(flag_selections_global)
            )
        except Exception as exc:
            logger.warning(
                "Flag selection failed (%s) — proceeding with baseline flags", exc
            )
            flag_selections_global = {}

    # Feedback-loop state (when enabled): a LearnedConstraintsStore
    # backed by <output>/learned_constraints.json. Persisted clauses
    # are applied to subsequent harness gens via spec.precondition.
    constraints_store = None
    if getattr(config, "enable_feedback_loop", False):
        try:
            from bmc_agent.feedback_loop import LearnedConstraintsStore
            constraints_store = LearnedConstraintsStore(out_root)
            logger.info(
                "Feedback loop enabled: persisting to %s",
                constraints_store.path,
            )
        except Exception as exc:
            logger.warning(
                "Feedback loop init failed (%s) — disabling for this run", exc
            )
            constraints_store = None

    summary = {
        "driver": driver,
        "source_dir": str(src_dir),
        "n_files_parsed": len(parsed_files),
        "n_files_skipped": len(files) - len(parsed_files),
        "per_file": {},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    fn_count = 0
    for src_path, parsed in parsed_files.items():
        file_stem = Path(src_path).stem
        file_out = out_root / file_stem
        file_out.mkdir(parents=True, exist_ok=True)

        per_file = {
            "n_functions": len(parsed.functions),
            "n_verified": 0, "n_unverified": 0, "n_cbmc_error": 0,
            "n_cex_total": 0, "n_judged": 0,
            "verdicts": {"realistic": 0, "unrealistic": 0, "uncertain": 0},
            "functions": {},
        }
        summary["per_file"][file_stem] = per_file

        for fn_name in sorted(parsed.functions.keys()):
            if max_functions and fn_count >= max_functions:
                logger.info("max_functions cap reached (%d)", max_functions)
                break
            fn_count += 1
            func = parsed.get_function_info(fn_name)
            if func is None:
                continue

            fn_out = file_out / fn_name
            fn_out.mkdir(parents=True, exist_ok=True)

            # Apply learned constraints (feedback loop): conjoin any
            # per-function clauses + project-wide clauses into the
            # harness PRE. Skip clauses containing contract-DSL /
            # pseudocode tokens (valid(), assume(), \result, etc.)
            # that the LLM occasionally emits — CBMC can't compile
            # them and they'd break harness generation.
            pre_clauses = ["true"]
            dropped: list[str] = []
            if constraints_store is not None:
                for clause in constraints_store.function_clauses(fn_name):
                    if _is_safe_clause(clause):
                        pre_clauses.append(clause)
                    elif clause:
                        dropped.append(clause)
                for clause in constraints_store.project_clauses():
                    if _is_safe_clause(clause):
                        pre_clauses.append(clause)
                    elif clause:
                        dropped.append(clause)
            if dropped:
                logger.info(
                    "  Skipping %d unsafe learned clause(s) for '%s' "
                    "(contain pseudocode/contract-DSL): %r",
                    len(dropped), fn_name, [d[:80] for d in dropped],
                )
            precondition = " && ".join(f"({c})" for c in pre_clauses) if len(pre_clauses) > 1 else "true"

            spec = Spec(
                function_name=fn_name,
                precondition=precondition,
                postcondition="true",
                status=SpecStatus.GENERATED,
            )

            # Generate harness.
            # When --agentic-harness is enabled, ask the LLM to write the
            # harness directly (deciding per-callee stub-vs-inline + grounding
            # parameter shapes in real callers). Falls back to the
            # deterministic generator if the LLM cannot produce something
            # CBMC parses within the retry budget.
            agentic_meta: Optional[dict] = None
            harness_text = None
            if getattr(config, "enable_agentic_harness", False):
                try:
                    from bmc_agent.agentic_harness_gen import AgenticHarnessGen
                    ag = AgenticHarnessGen(
                        config=config,
                        parsed_files=parsed_files,
                        corpus_root=src_dir,
                    )
                    ag_res = ag.generate(
                        func=func,
                        all_funcs_global=all_funcs_global,
                        include_dirs=include_dirs or [],
                        defines=defines or [],
                    )
                    agentic_meta = ag_res.to_dict()
                    if ag_res.harness and not ag_res.last_compile_error:
                        harness_text = ag_res.harness
                        logger.info(
                            "  agentic harness OK for %s (turns=%d retries=%d): %s",
                            fn_name, ag_res.turns_used, ag_res.retries,
                            (ag_res.rationale or "")[:160],
                        )
                    else:
                        logger.warning(
                            "  agentic harness failed for %s "
                            "(turns=%d retries=%d err=%s) — falling back to deterministic gen",
                            fn_name, ag_res.turns_used, ag_res.retries,
                            (ag_res.last_compile_error or "")[:200],
                        )
                except Exception as exc:
                    logger.warning(
                        "  agentic harness raised for %s: %s — falling back",
                        fn_name, exc,
                    )

            try:
                if harness_text is None:
                    gen = HarnessGenerator(config)
                    harness_text = gen.generate_harness(
                        func=func, spec=spec, parsed_file=parsed,
                        all_funcs=all_funcs_global,
                    )
            except Exception as exc:
                logger.warning("harness gen failed for %s: %s", fn_name, exc)
                per_file["functions"][fn_name] = {"error": f"harness gen: {exc}"}
                continue
            (fn_out / "harness.c").write_text(harness_text)

            # Per-function CBMC flag selection.
            # Source priority:
            #   1. agentic_meta["cbmc_flags"] when the agentic harness was
            #      used successfully — same LLM context as the harness gen,
            #      no extra round-trip
            #   2. flag_selections_global from the upfront FlagSelector pass
            #      (deterministic-gen path, or agentic disabled)
            #   3. {} = baseline checks only
            extra_flags: dict = {}
            if agentic_meta is not None and agentic_meta.get("cbmc_flags"):
                extra_flags = dict(agentic_meta.get("cbmc_flags") or {})
            else:
                fn_flag_sel = flag_selections_global.get(fn_name)
                if fn_flag_sel is not None:
                    extra_flags = fn_flag_sel.to_dict()

            # Per-function CBMC budget: the agentic LLM may have picked
            # unwind / timeout_s appropriate to the harness it wrote (loop
            # bounds, callee depth). Use those when present; else CLI default.
            fn_unwind = cbmc_unwind
            fn_timeout = cbmc_timeout
            if agentic_meta is not None and agentic_meta.get("cbmc_budget"):
                budget = agentic_meta.get("cbmc_budget") or {}
                if "unwind" in budget:
                    fn_unwind = int(budget["unwind"])
                if "timeout_s" in budget:
                    fn_timeout = int(budget["timeout_s"])
                if fn_unwind != cbmc_unwind or fn_timeout != cbmc_timeout:
                    logger.info(
                        "  agentic CBMC budget for %s: unwind=%d timeout=%ds "
                        "(CLI defaults: unwind=%d timeout=%ds)",
                        fn_name, fn_unwind, fn_timeout,
                        cbmc_unwind, cbmc_timeout,
                    )

            # Run CBMC
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".c", delete=False,
            ) as f:
                f.write(harness_text)
                harness_path = f.name
            try:
                cbmc_res = run_cbmc(
                    harness_path=harness_path,
                    unwind=fn_unwind, timeout=fn_timeout,
                    include_dirs=include_dirs or [],
                    defines=defines or [],
                    bounds_check=True, pointer_check=True,
                    signed_overflow_check=True, div_by_zero_check=True,
                    unsigned_overflow_check=bool(extra_flags.get("unsigned_overflow_check")),
                    conversion_check=bool(extra_flags.get("conversion_check")),
                    pointer_overflow_check=bool(extra_flags.get("pointer_overflow_check")),
                )
            except Exception as exc:
                logger.warning("CBMC failed for %s: %s", fn_name, exc)
                per_file["n_cbmc_error"] += 1
                per_file["functions"][fn_name] = {"error": f"cbmc: {exc}"}
                continue
            finally:
                try: os.unlink(harness_path)
                except OSError: pass

            (fn_out / "cbmc_result.json").write_text(json.dumps({
                "verified": cbmc_res.verified,
                "n_counterexamples": len(cbmc_res.counterexamples or []),
                "error": cbmc_res.error,
            }, indent=2))

            if cbmc_res.error:
                per_file["n_cbmc_error"] += 1
                per_file["functions"][fn_name] = {"error": f"cbmc error: {cbmc_res.error[:200]}"}
                continue
            if cbmc_res.verified:
                per_file["n_verified"] += 1
                per_file["functions"][fn_name] = {"verified": True}
                continue
            per_file["n_unverified"] += 1

            # Dedup + judge each surviving CEx
            cexs = _dedup_counterexamples(
                cbmc_res.counterexamples,
                max_per_type=config.dedup_max_per_type,
            )
            per_file["n_cex_total"] += len(cexs)

            fn_record = {"verified": False, "cexs": []}
            if agentic_meta is not None:
                fn_record["agentic_harness"] = agentic_meta
            judge = JudgeAgent(
                config=config,
                parsed_files=parsed_files,
                corpus_root=src_dir,
                harness_source=harness_text,
                cbmc_rerun_callback=None,
            )

            for cex in cexs:
                logger.info(
                    "Judging '%s' / %s",
                    fn_name, cex.failing_property,
                )
                t0 = time.time()
                try:
                    result = judge.judge(
                        func=func, counterexample=cex, cbmc_result=cbmc_res,
                    )
                except Exception as exc:
                    logger.warning("Judge raised for %s: %s", fn_name, exc)
                    continue
                dt = time.time() - t0
                per_file["verdicts"][result.verdict] = per_file["verdicts"].get(result.verdict, 0) + 1
                per_file["n_judged"] += 1

                record = {
                    "failing_property": cex.failing_property,
                    "judge": result.to_dict(),
                    "elapsed_s": round(dt, 1),
                    "adjacent_confirmations": [],
                    "primary_dynamic_validation": None,
                    "feedback_distillation": None,
                }
                fn_record["cexs"].append(record)

                logger.info(
                    "  → verdict=%s confidence=%s turns=%d adjacent_bugs=%d",
                    result.verdict, result.confidence,
                    result.turns_used, len(result.adjacent_bugs or []),
                )

                # ----------------------------------------------------------
                # Feedback loop: on UNREALISTIC / UNCERTAIN verdicts, ask
                # the LLM to distill a learned constraint. Persist it to
                # <output>/learned_constraints.json; the next harness gen
                # for this function will conjoin it into the precondition.
                # ----------------------------------------------------------
                if (
                    constraints_store is not None
                    and result.verdict in ("unrealistic", "uncertain")
                ):
                    try:
                        from bmc_agent.feedback_loop import (
                            learn_from_rejection,
                        )
                        from bmc_agent.realism_checker import (
                            RealismCheckResult, RealismVerdict,
                        )
                        from bmc_agent.llm import LLMClient
                        v_map = {
                            "realistic": RealismVerdict.REALISTIC,
                            "unrealistic": RealismVerdict.UNREALISTIC,
                            "uncertain": RealismVerdict.UNCERTAIN,
                        }
                        realism_adapter = RealismCheckResult(
                            verdict=v_map[result.verdict],
                            reasoning=result.reasoning or "",
                            key_concern=(result.reasoning or "")[:200],
                            llm_confidence=result.confidence or "",
                        )
                        fb_llm = LLMClient(config)
                        remediation = learn_from_rejection(
                            config=config,
                            llm=fb_llm,
                            func=func,
                            counterexample=cex,
                            realism=realism_adapter,
                            existing_project_clauses=constraints_store.project_clauses(),
                        )
                        changed = constraints_store.record(
                            fn_name, remediation,
                            source_property=cex.failing_property or "",
                        )
                        record["feedback_distillation"] = {
                            "remediation": remediation.to_dict(),
                            "persisted": changed,
                        }
                        logger.info(
                            "  Feedback: scope=%s clause=%r persisted=%s",
                            remediation.scope.value,
                            (remediation.clause or "")[:80],
                            changed,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Feedback distillation failed for %s: %s",
                            fn_name, exc,
                        )
                        record["feedback_distillation"] = {
                            "error": str(exc)[:200],
                        }

                # ----------------------------------------------------------
                # Agentic harness REFINEMENT loop: when the agentic harness
                # generator is active and the judge ruled this CEx UNREALISTIC
                # / UNCERTAIN, hand the verdict reasoning + harness + witness
                # back to the agentic generator so it can rewrite the harness.
                # Each refinement round runs CBMC on the new harness and
                # re-judges one CEx. Stops on REALISTIC verdict, clean
                # verification, or budget exhaustion.
                # ----------------------------------------------------------
                refine_budget = int(getattr(config, "agentic_refine_rounds", 0) or 0)
                if (
                    refine_budget > 0
                    and getattr(config, "enable_agentic_harness", False)
                    and result.verdict in ("unrealistic", "uncertain")
                ):
                    refinement_chain = _refine_harness_loop(
                        config=config,
                        parsed_files=parsed_files,
                        all_funcs_global=all_funcs_global,
                        source_dir=src_dir,
                        include_dirs=include_dirs or [],
                        defines=defines or [],
                        func=func,
                        initial_harness=harness_text,
                        initial_cex=cex,
                        initial_judge=result,
                        cbmc_unwind=cbmc_unwind,
                        cbmc_timeout=cbmc_timeout,
                        max_rounds=refine_budget,
                        flag_extras=extra_flags,
                    )
                    if refinement_chain:
                        record["refinement_chain"] = refinement_chain
                        last = refinement_chain[-1]
                        logger.info(
                            "    refinement chain: %d round(s), final=%s",
                            len(refinement_chain),
                            last.get("outcome", "?"),
                        )

                # ----------------------------------------------------------
                # Dynamic validation for PRIMARY-REALISTIC verdicts.
                # If the judge says the original CBMC counterexample is a
                # real bug, generate a C reproducer for THIS function +
                # compile with ASan/UBSan + run. The bug is on `func` itself.
                # ----------------------------------------------------------
                if result.verdict == "realistic":
                    primary_scenario = (
                        result.attacker_scenario
                        or result.reasoning  # fall back to reasoning if scenario empty
                    )
                    dyn_dir = fn_out / "dynamic" / fn_name
                    logger.info(
                        "  Dynamic-validating PRIMARY realistic verdict on '%s'",
                        fn_name,
                    )
                    try:
                        from bmc_agent.llm import LLMClient
                        dyn_llm = LLMClient(config)
                        primary_dyn = _dynamic_validate_bug(
                            func=func,
                            attacker_scenario=primary_scenario,
                            parsed_file=parsed,
                            llm=dyn_llm,
                            out_dir=dyn_dir,
                        )
                    except Exception as exc:
                        primary_dyn = {"outcome": "skipped",
                                       "reason": f"dynamic-val exc: {exc}"}
                    record["primary_dynamic_validation"] = primary_dyn
                    logger.info(
                        "  → primary dynamic outcome=%s signal=%s",
                        primary_dyn.get("outcome"),
                        primary_dyn.get("signal_name"),
                    )

                # ----------------------------------------------------------
                # Step 4: BMC-confirm adjacent-bug hypotheses
                # ----------------------------------------------------------
                # For each adjacent_bug at confidence high/medium, locate
                # the target function, generate a focused harness, re-run
                # CBMC, and re-judge. If both BMC and judge agree → confirmed.
                known_fn_names = set(all_funcs_global.keys())
                # Sort adjacent bugs by confidence (high first), cap to
                # _MAX_ADJACENT_TO_CONFIRM. The LLM sometimes generates
                # 5-7 hypotheses per CEx and each BMC re-run + 2nd judge +
                # potential dyn-val is 2-5 min; sweep would otherwise drag.
                candidates = [
                    a for a in (result.adjacent_bugs or [])
                    if isinstance(a, dict)
                    and (a.get("confidence") or "").lower() in _ADJACENT_CONFIRM_CONFIDENCE
                ]
                candidates.sort(
                    key=lambda a: _CONFIDENCE_RANK.get(
                        (a.get("confidence") or "").lower(), 0
                    ),
                    reverse=True,
                )
                candidates = candidates[:_MAX_ADJACENT_TO_CONFIRM]
                if len(result.adjacent_bugs or []) > len(candidates):
                    logger.info(
                        "    Capping adjacent confirmations: %d hypotheses → top %d by confidence",
                        len(result.adjacent_bugs or []), len(candidates),
                    )
                for adj in candidates:
                    conf = (adj.get("confidence") or "").lower()
                    loc = str(adj.get("location") or "")
                    target_fn_name = _extract_function_from_location(loc, known_fn_names)
                    if not target_fn_name:
                        record["adjacent_confirmations"].append({
                            "adjacent": adj,
                            "confirmed": False,
                            "reason": "could not extract a known function from location",
                        })
                        continue

                    # Find which ParsedCFile owns this target
                    target_parsed = None
                    for p in parsed_files.values():
                        if target_fn_name in p.functions:
                            target_parsed = p
                            break
                    if target_parsed is None:
                        record["adjacent_confirmations"].append({
                            "adjacent": adj,
                            "confirmed": False,
                            "reason": f"function '{target_fn_name}' not in any parsed file",
                        })
                        continue

                    logger.info(
                        "    BMC-confirming adjacent bug in '%s' (hypothesis: %s)",
                        target_fn_name, (adj.get("bug_type") or "")[:80],
                    )
                    adj_flag_sel = flag_selections_global.get(target_fn_name)
                    adj_flags = adj_flag_sel.to_dict() if adj_flag_sel is not None else {}
                    conf_result = _confirm_adjacent_via_bmc(
                        adjacent_bug=adj,
                        config=config,
                        parsed_files=parsed_files,
                        all_funcs_global=all_funcs_global,
                        parsed_for_target=target_parsed,
                        target_fn_name=target_fn_name,
                        source_dir=src_dir,
                        include_dirs=include_dirs or [],
                        defines=defines or [],
                        # Adjacent confirmations use a larger unwind than the
                        # primary BMC run (4 → 16). LLM-flagged bugs often
                        # need deeper loop unwinding to surface.
                        cbmc_unwind=max(cbmc_unwind, _ADJACENT_CONFIRM_UNWIND_DEFAULT),
                        cbmc_timeout=cbmc_timeout,
                        flag_selection=adj_flags,
                        primary_fn_name=fn_name,
                        primary_verdict=result.verdict,
                        primary_reasoning=result.reasoning,
                    )
                    confirmation_record = {
                        "adjacent": adj,
                        **conf_result,
                    }

                    # Dynamic validation: if BMC re-confirmed and the LLM
                    # supplied an attacker_scenario, generate a C reproducer
                    # against the real libarchive .so, compile with ASan+UBSan,
                    # run, and record whether it crashed at runtime.
                    if conf_result.get("confirmed"):
                        target_func = None
                        target_parsed_ = None
                        for p in parsed_files.values():
                            if target_fn_name in p.functions:
                                target_func = p.get_function_info(target_fn_name)
                                target_parsed_ = p
                                break
                        if target_func is not None:
                            attacker_scenario = (
                                conf_result.get("attacker_scenario")
                                or adj.get("attacker_scenario")
                                or ""
                            )
                            dyn_dir = fn_out / "dynamic" / target_fn_name
                            logger.info(
                                "    Dynamic-validating confirmed bug in %s",
                                target_fn_name,
                            )
                            try:
                                from bmc_agent.llm import LLMClient
                                dyn_llm = LLMClient(config)
                                dyn_result = _dynamic_validate_bug(
                                    func=target_func,
                                    attacker_scenario=attacker_scenario,
                                    parsed_file=target_parsed_,
                                    llm=dyn_llm,
                                    out_dir=dyn_dir,
                                )
                            except Exception as exc:
                                dyn_result = {"outcome": "skipped",
                                              "reason": f"dynamic-val exc: {exc}"}
                            confirmation_record["dynamic_validation"] = dyn_result
                            logger.info(
                                "    → dynamic outcome=%s signal=%s",
                                dyn_result.get("outcome"),
                                dyn_result.get("signal_name"),
                            )

                    record["adjacent_confirmations"].append(confirmation_record)
                    logger.info(
                        "    → adjacent confirmed=%s verdict=%s reason=%s",
                        conf_result.get("confirmed"),
                        conf_result.get("verdict"),
                        (conf_result.get("reason") or conf_result.get("reasoning") or "")[:120],
                    )

                # Persist per-CEx judge result (including adjacent_confirmations)
                safe_prop = _safe_filename(cex.failing_property or "unknown")
                (fn_out / f"judge_{safe_prop}.json").write_text(
                    json.dumps(record, indent=2)
                )

            per_file["functions"][fn_name] = fn_record

            # Persist updated summary every function (so partial runs are useful)
            (out_root / "summary.json").write_text(json.dumps(summary, indent=2))

    summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary
