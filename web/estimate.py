"""
Pre-run cost/token estimate for a selected verification scope.

The workbench shows this on the Scope screen and in the run-confirm dialog so a
user can decide before spending anything. It is **provider agnostic and makes no
LLM calls**: it enumerates the functions in scope (the same parser the pipeline
uses), tokenizes representative prompts locally with ``tiktoken``, and models the
pipeline as *tokens-per-request × number-of-requests* the way the pipeline
actually spends.

The pipeline's cost splits in two:

* **Deterministic per function** — Phase 1 spec-gen is ~1 request/function with a
  knowable prompt (spec system prompt + function body + a caller-context
  allowance), plus optional flag selection. We tokenize these exactly.
* **Variable per counterexample** — Phase 3 (classify / realism / refinement /
  reproducer) runs *per counterexample*, and CBMC's counterexample count isn't
  knowable before running. We parameterize it (assumed CEx/function, capped by
  ``dedup_max_per_type``) and surface it as the upper end of a **low / expected /
  high** range.

All the fudge factors live in the CONSTANTS block below so the estimate can be
recalibrated against the live spend meter without touching the logic.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from bmc_agent.prompts import spec_system_prompt_for
from bmc_agent.source_parser import (
    CODE_EXTENSIONS,
    detect_language,
    parse_source_file,
)
from web import pricing

# ---------------------------------------------------------------------------
# CONSTANTS — calibration knobs. Tune these against real runs (see the
# verification notes in the plan); they don't change the model's shape.
# ---------------------------------------------------------------------------
# o200k_base undercounts Claude/other tokenizers; bump every count by this.
_TOKEN_CALIBRATION = 1.15
# Function body is truncated to this many chars in the real spec-gen prompt
# (spec_generator_v2.py: ``(func_info.body or "")[:4000]``).
_BODY_CHAR_CAP = 4000

# Output caps actually passed to the model per request type (from the pipeline).
_SPEC_MAX_TOKENS = 1200
_FLAG_MAX_TOKENS = 512
_CLASSIFY_MAX_TOKENS = 16384
_REALISM_MAX_TOKENS = 4096
_REFINE_MAX_TOKENS = 4096

# Fraction of the output cap the model typically actually emits. Classify/refine
# carry a huge cap but emit a short verdict, so they get a low fraction.
_OUTPUT_UTILIZATION = 0.55
_CLASSIFY_OUTPUT_UTILIZATION = 0.15

# Approximate sizes (tokens) for prompt pieces we don't assemble verbatim:
_SPEC_USER_OVERHEAD = 280       # instruction text wrapping body + callers
_CALLER_CONTEXT_TOKENS = 600    # up to ~5 caller sites + callee specs
_FLAG_SYS_TOKENS = 1600         # flag/bmc-config agent system prompt
_FLAG_AGENT_TURNS = 2           # multi-turn bmc-config agent, modeled flat
_CEX_SYS_TOKENS = 2400          # classify/realism/refine system prompt
_CEX_TRACE_TOKENS = 900         # counterexample trace + surrounding context
_FLAG_OVERHEAD = 200

# Per-counterexample request counts per scenario knob.
_CEX_PER_FN_EXPECTED = 1.0      # avg counterexamples to validate per function
_REFINE_PROB_EXPECTED = 0.5     # fraction of CExs that trigger a refinement
# Reproducer (dynamic-validation) tool agent — only folded into the high end.
_REPRODUCER_TURNS_HIGH = 3
_REPRODUCER_IN_TOKENS = 1800
_REPRODUCER_OUT_TOKENS = 1200


def _bool_env(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() == "true"


def _pipeline_flags(options: dict | None = None) -> dict:
    """Resolve the request-count-affecting flags the way the WEB run resolves
    them, so the estimate matches what ``web.runner._make_config`` actually does.

    The web inherits the CLI/Config defaults (``_make_config`` no longer pins any
    knob before the options overlay), so ``realism_check`` and
    ``dynamic_validation`` default ON here too and only the run options turn them
    off. Every flag reads option-first, then the Config default.
    """
    opts = options or {}
    ai = opts.get("ai_layers") or {}
    harness = opts.get("harness") or {}
    depth = opts.get("depth") or {}
    return {
        "lite_mode": bool(harness.get("lite_mode", _bool_env("BMC_AGENT_LITE_MODE", False))),
        "enable_flag_selection": bool(
            ai.get("enable_flag_selection", _bool_env("BMC_AGENT_ENABLE_FLAG_SELECTION", True))
        ),
        # On by default (CLI/Config parity) unless the run options turn them off.
        "enable_realism_check": bool(ai.get("enable_realism_check", True)),
        "enable_dynamic_validation": bool(ai.get("enable_dynamic_validation", True)),
        "dedup_max_per_type": int(
            depth.get("dedup_max_per_type",
                      int(os.environ.get("BMC_AGENT_DEDUP_MAX_PER_TYPE", "3")))
        ),
    }


# --- tokenizer -------------------------------------------------------------

_enc = None
_enc_tried = False


def _encoder():
    global _enc, _enc_tried
    if _enc_tried:
        return _enc
    _enc_tried = True
    try:
        import tiktoken
        _enc = tiktoken.get_encoding("o200k_base")
    except Exception:
        _enc = None  # fall back to a char heuristic
    return _enc


def _ntokens(text: str) -> int:
    """Calibrated token count for ``text`` (local, offline)."""
    if not text:
        return 0
    enc = _encoder()
    raw = len(enc.encode(text)) if enc is not None else max(1, len(text) // 4)
    return int(round(raw * _TOKEN_CALIBRATION))


# --- scope enumeration -----------------------------------------------------

def _iter_code_files(target: Path, is_dir: bool) -> Iterable[Path]:
    if not is_dir:
        yield target
        return
    for p in sorted(target.rglob("*")):
        if p.is_symlink() or any(part.startswith(".") for part in p.relative_to(target).parts):
            continue
        if p.is_file() and p.suffix.lower() in CODE_EXTENSIONS:
            yield p


def _function_body_tokens(
    target: Path, is_dir: bool, only_functions: "set[str] | None" = None,
) -> tuple[list[int], int, set[str]]:
    """Return (per-function body token counts, n_files, languages-in-scope).

    When ``only_functions`` is given (single-file per-function picker), only the
    named functions are counted so the estimate reflects the chosen subset."""
    body_toks: list[int] = []
    n_files = 0
    langs: set[str] = set()
    for path in _iter_code_files(target, is_dir):
        try:
            lang = detect_language(path)
        except Exception:
            continue
        try:
            parsed = parse_source_file(str(path))
        except Exception:
            continue
        funcs = getattr(parsed, "functions", None) or {}
        if only_functions is not None:
            funcs = {n: i for n, i in funcs.items() if n in only_functions}
        if not funcs:
            continue
        n_files += 1
        langs.add(lang)
        for info in funcs.values():
            body = (getattr(info, "body", "") or "")[:_BODY_CHAR_CAP]
            body_toks.append(_ntokens(body))
    return body_toks, n_files, langs


# --- the request model -----------------------------------------------------

def _sys_tokens_for(langs: set[str]) -> int:
    """Spec-gen system prompt size — the largest in-scope language's prompt."""
    best = 0
    for lang in (langs or {"c"}):
        try:
            best = max(best, _ntokens(spec_system_prompt_for(lang)))
        except Exception:
            continue
    return best or _ntokens(spec_system_prompt_for("c"))


def estimate_scope(target: Path, is_dir: bool, llm: dict,
                   max_files: int | None = None,
                   options: dict | None = None,
                   only_functions: "set[str] | None" = None) -> dict:
    """Estimate tokens + USD for verifying ``target``.

    ``llm`` is the dict from ``server._read_llm_config`` (backend, model,
    k2_backend). ``max_files`` overrides the reported file cap (per-run setting);
    None uses the env-default. ``options`` is the validated run-settings dict
    (``web.options.parse_options``) so the estimate reflects the knobs the run
    will actually use (e.g. turning realism on raises the figure).
    ``only_functions`` (single-file picker) restricts the estimate to the chosen
    functions. Returns a JSON-serializable dict with low/expected/high ranges.
    """
    flags = _pipeline_flags(options)
    body_toks, n_files, langs = _function_body_tokens(target, is_dir, only_functions)
    n_functions = len(body_toks)
    sys_tokens = _sys_tokens_for(langs) if n_functions else 0
    avg_body = (sum(body_toks) / n_functions) if n_functions else 0.0

    spec_out = _SPEC_MAX_TOKENS * _OUTPUT_UTILIZATION
    flag_out = _FLAG_MAX_TOKENS * _OUTPUT_UTILIZATION
    classify_out = _CLASSIFY_MAX_TOKENS * _CLASSIFY_OUTPUT_UTILIZATION
    realism_out = _REALISM_MAX_TOKENS * _OUTPUT_UTILIZATION
    refine_out = _REFINE_MAX_TOKENS * _OUTPUT_UTILIZATION

    # --- deterministic per-function cost (same in every scenario) ---
    det_reqs = 0
    det_in = 0.0
    det_out = 0.0
    if not flags["lite_mode"]:
        for bt in body_toks:
            # Phase 1: one spec-gen request.
            det_reqs += 1
            det_in += sys_tokens + _SPEC_USER_OVERHEAD + bt + _CALLER_CONTEXT_TOKENS
            det_out += spec_out
            # Phase 1.5: flag / bmc-config selection (multi-turn, modeled flat).
            if flags["enable_flag_selection"]:
                det_reqs += _FLAG_AGENT_TURNS
                det_in += _FLAG_AGENT_TURNS * (_FLAG_SYS_TOKENS + _FLAG_OVERHEAD + bt)
                det_out += _FLAG_AGENT_TURNS * flag_out

    def _cex_phase(cex_per_fn: float, refine_prob: float, with_reproducer: bool) -> tuple[float, float, float]:
        """(requests, input_tokens, output_tokens) for the per-CEx phase."""
        n_cex = n_functions * cex_per_fn
        if n_cex <= 0:
            return 0.0, 0.0, 0.0
        cex_in = _CEX_SYS_TOKENS + avg_body + _CEX_TRACE_TOKENS
        reqs = 0.0
        tin = 0.0
        tout = 0.0
        # classifier — always runs per CEx
        reqs += n_cex
        tin += n_cex * cex_in
        tout += n_cex * classify_out
        # realism audit
        if flags["enable_realism_check"]:
            reqs += n_cex
            tin += n_cex * cex_in
            tout += n_cex * realism_out
        # refinement (a fraction of CExs)
        reqs += n_cex * refine_prob
        tin += n_cex * refine_prob * cex_in
        tout += n_cex * refine_prob * refine_out
        # reproducer agent (high end only)
        if with_reproducer and flags["enable_dynamic_validation"]:
            reqs += n_cex * _REPRODUCER_TURNS_HIGH
            tin += n_cex * _REPRODUCER_TURNS_HIGH * _REPRODUCER_IN_TOKENS
            tout += n_cex * _REPRODUCER_TURNS_HIGH * _REPRODUCER_OUT_TOKENS
        return reqs, tin, tout

    # low: a clean run — no counterexamples to triage.
    # expected: ~1 CEx/function, realism on, half refine.
    # high: dedup cap CExs/function, refine every CEx, reproducer agents.
    scenarios = {
        "low": (0.0, 0.0, False),
        "expected": (_CEX_PER_FN_EXPECTED, _REFINE_PROB_EXPECTED, False),
        "high": (float(flags["dedup_max_per_type"]), 1.0, True),
    }

    # Pricing: presets only (custom/unknown → tokens only). K2 Think → free.
    is_free = bool(llm.get("k2_backend"))
    price = pricing.preset_price(llm.get("model", "") or "")
    priced = is_free or price is not None

    requests: dict[str, int] = {}
    prompt_tokens: dict[str, int] = {}
    completion_tokens: dict[str, int] = {}
    usd: dict[str, float | None] = {}
    for name, (cpf, rprob, repro) in scenarios.items():
        creqs, cin, cout = _cex_phase(cpf, rprob, repro)
        total_in = det_in + cin
        total_out = det_out + cout
        requests[name] = int(round(det_reqs + creqs))
        prompt_tokens[name] = int(round(total_in))
        completion_tokens[name] = int(round(total_out))
        if is_free:
            usd[name] = 0.0
        elif price is not None:
            pin, pout = price
            usd[name] = round((total_in * pin + total_out * pout) / 1_000_000.0, 2)
        else:
            usd[name] = None

    # The directory sweep only ever verifies the first N files (runner cap), so
    # surface it: the confirm dialog warns when n_files exceeds it. Use the
    # per-run override when set, else the env-default web cap.
    from web.limits import MAX_VERIFY_FILES as _DEFAULT_MAX_FILES

    return {
        "n_files": n_files,
        "max_files": max_files if max_files is not None else _DEFAULT_MAX_FILES,
        "n_functions": n_functions,
        # In-scope source languages ("c"/"rust"/"java") so the workbench can
        # language-gate the Run-settings panel (e.g. hide CBMC-only knobs for a
        # Rust repo).
        "languages": sorted(langs),
        "model": llm.get("model", ""),
        "free": is_free,
        "priced": priced,
        "requests": requests,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": {k: prompt_tokens[k] + completion_tokens[k] for k in requests},
        "usd": usd,
    }
