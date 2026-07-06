"""
Phase 2: Compositional BMC Engine [CONVENTIONAL].

Deterministic tool invocation — not agentic. Exposes one interface:
  check(function, spec, callee_specs) -> {verified, counterexample}
The agentic layers use this interface; swapping CBMC for another backend
changes only the harness-synthesis-and-invocation code here.
"""

from __future__ import annotations

import dataclasses
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from bmc_agent.artifacts import ArtifactStore
from bmc_agent.backends import BMCBackend, CBMCBackend
from bmc_agent.cbmc import CBMCResult, Counterexample, run_cbmc
from bmc_agent.config import Config
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, ParsedCFile
from bmc_agent.spec import Spec

logger = get_logger("bmc_engine")


# CBMC error substrings that indicate the harness failed to BUILD (parse /
# convert / type-check) rather than a verification outcome (property failure,
# unwind-bound, timeout). Used to trigger the agentic harness-repair fallback.
_HARNESS_BUILD_ERROR_MARKERS = (
    "conversion error",
    "incomplete type",
    "parsing error",
    "redefinition",
    "conflicting types",
    "syntax error",
    "cannot convert",
    "type mismatch",
    "expected ',' or ';'",
    "undeclared",
)


import re as _re

# Widths recorded by the harness generator's string-copy SOURCE widening comments:
#   /* copy-sink source 'p': widened to 256 chars ... */          (param)
#   /* copy-source source field 'f': widened to 256 chars */      (struct field)
#   /* copy-source RETURN modeling: 'f' ... widen it (256 chars... */ (stub return)
_COPY_WIDEN_RE = _re.compile(r"widened to (\d+) chars|widen it \((\d+) chars")


def _copy_widen_floor_from_harness(harness_src: "str | None") -> int:
    """Per-function unwind floor implied by the string-copy SOURCE widths the
    harness actually applied: the copy loop must unroll past the widest source
    (max width + 2) for the fixed-buffer overflow to be reachable. 0 when none.

    Reads the harness TEXT so the floor is consistent with the modeling even
    when func.body still carries an unexpanded macro destination size."""
    if not harness_src:
        return 0
    widest = 0
    for m in _COPY_WIDEN_RE.finditer(harness_src):
        w = int(m.group(1) or m.group(2))
        if w > widest:
            widest = w
    return (widest + 2) if widest else 0


def _is_harness_build_error(err: "str | None") -> bool:
    """True iff a CBMC error string looks like a harness BUILD failure (parse /
    conversion / incomplete-type), as opposed to a verification result or a
    resource limit (unwind bound, timeout)."""
    if not err:
        return False
    e = err.lower()
    # Never treat resource/verification outcomes as build errors.
    if "unwind" in e or "timed out" in e or "timeout" in e:
        return False
    return any(m in e for m in _HARNESS_BUILD_ERROR_MARKERS)


def _harness_entry_of(harness_path) -> "str | None":
    """Read the `/* Harness entry: NAME */` header tag, if present. Returns the
    entry function name CBMC should use via --function, or None for main()."""
    try:
        with open(harness_path, "r") as _hf:
            for _line in _hf:
                if "Harness entry:" in _line:
                    _name = _line.split("Harness entry:", 1)[1].strip().rstrip("*/").strip()
                    return _name if _name and _name != "main" else None
                if not _line.startswith("/*") and "include" in _line:
                    break  # past the header
    except Exception:
        pass
    return None


@dataclass
class BMCVerdict:
    """Result of running BMC on a single function."""

    function_name: str
    verified: bool                          # True = verified up to bound k
    counterexamples: list[Counterexample] = field(default_factory=list)
    harness_path: str = ""
    cbmc_result: Optional[CBMCResult] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        # CBMCResult and Counterexample are already handled by asdict
        return d


class BMCEngine:
    """Runs CBMC on function harnesses to check specs."""

    def __init__(
        self,
        config: Config,
        store: ArtifactStore,
        backend: "BMCBackend | None" = None,
    ) -> None:
        self.config = config
        self.store = store
        self.harness_gen = HarnessGenerator(config)  # kept for backward compat
        self.backend: BMCBackend = backend or CBMCBackend(config)
        # Cumulative CBMC wall-clock spent per function name, across ALL phases
        # (initial check, auto-retry, Phase-3c refinement, spec_refiner re-verify).
        # Enforces config.per_function_time_budget_s so a single pathological
        # function (e.g. a path/parser fn the flag-selector unwinds deeply and
        # gives a 600s timeout) can't grind the whole sweep for hours — see the
        # check_function wrapper below.
        self._fn_cumulative_time: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_function(
        self,
        func: FunctionInfo,
        spec: Spec,
        parsed_file: ParsedCFile,
        driver_name: str,
        all_funcs: "dict | None" = None,
        flag_selection: "object | None" = None,
    ) -> BMCVerdict:
        """Per-function time-budget wrapper around the real check.

        All CBMC invocations for a function (across every phase) funnel through
        here, so this is the one chokepoint that can bound a function's TOTAL
        wall-clock. Once the cumulative CBMC time for ``func.name`` reaches
        ``config.per_function_time_budget_s`` (0 = unlimited), further checks
        short-circuit to an errored verdict (verified=False, error set, no
        counterexamples) which the pipeline already routes to UNRESOLVED — never
        a false "verified clean". The worst-case overshoot is one in-flight CBMC
        call (bounded by its own timeout).
        """
        budget = int(getattr(self.config, "per_function_time_budget_s", 0) or 0)
        fn_name = func.name
        if budget > 0:
            used = self._fn_cumulative_time.get(fn_name, 0.0)
            if used >= budget:
                logger.warning(
                    "Per-function time budget exhausted for '%s' "
                    "(%.0fs used >= %ds budget) — skipping further CBMC, "
                    "recording UNRESOLVED (timeout) instead of grinding.",
                    fn_name, used, budget,
                )
                return BMCVerdict(
                    function_name=fn_name,
                    verified=False,
                    counterexamples=[],
                    error=(f"per-function-time-budget-exhausted: {used:.0f}s "
                           f">= {budget}s budget (unresolved/timeout)"),
                )
        t0 = time.monotonic()
        try:
            return self._check_function_impl(
                func, spec, parsed_file, driver_name,
                all_funcs=all_funcs, flag_selection=flag_selection,
            )
        finally:
            if budget > 0:
                self._fn_cumulative_time[fn_name] = (
                    self._fn_cumulative_time.get(fn_name, 0.0)
                    + (time.monotonic() - t0)
                )

    def _check_function_impl(
        self,
        func: FunctionInfo,
        spec: Spec,
        parsed_file: ParsedCFile,
        driver_name: str,
        all_funcs: "dict | None" = None,
        flag_selection: "object | None" = None,
    ) -> BMCVerdict:
        """
        Check a single function against its spec using CBMC.

        Steps:
        1. Generate a CBMC harness.
        2. Save harness to the artifact directory.
        3. Run CBMC.
        4. Return a structured BMCVerdict.
        """
        fn_name = func.name
        logger.info("Checking function '%s' (driver '%s')", fn_name, driver_name)

        # ---- Step 1: generate harness ----
        try:
            harness_src = self.backend.generate_harness(func, spec, {}, parsed_file, all_funcs=all_funcs)
        except Exception as exc:
            # Distinguish unresolvable-type skips from genuine generator failures.
            # Unresolvable-type cases (impl-method functions referencing types
            # imported from sibling files Kani can't see) are an expected skip,
            # not an error -- they're noise if logged at ERROR level. Log them
            # at INFO and use a typed error marker so downstream filters can
            # exclude them from the harness-compile-failure tally.
            from bmc_agent.backends.kani_backend import HarnessUnresolvableTypes
            if isinstance(exc, HarnessUnresolvableTypes):
                logger.info(
                    "Skipping harness for '%s' — unresolvable types: %s",
                    fn_name,
                    ", ".join(exc.unresolved_types),
                )
                return BMCVerdict(
                    function_name=fn_name,
                    verified=False,
                    error=f"harness-skipped-unresolvable-types: {', '.join(exc.unresolved_types)}",
                )
            logger.error("Harness generation failed for '%s': %s", fn_name, exc)
            return BMCVerdict(
                function_name=fn_name,
                verified=False,
                error=f"Harness generation failed: {exc}",
            )

        # ---- Step 2: save harness ----
        harness_path = self._save_harness(driver_name, fn_name, harness_src)
        logger.debug("Harness saved to: %s", harness_path)

        # ---- Step 3: run the backend verifier ----
        # The CBMC path threads threat-model + per-function flag selection
        # through to run_cbmc.  Non-C backends (Kani for Rust) don't take
        # those CBMC-specific flags, so for them we use the polymorphic
        # backend.check() method and ignore flag selection.
        if self.backend.language == "c":
            threat_model = getattr(self.config, "threat_model", "security")
            pointer_check    = threat_model in ("security", "safety")
            bounds_check     = threat_model in ("security", "safety")
            div_by_zero_check = threat_model == "safety"

            unsigned_overflow_check = bool(getattr(flag_selection, "unsigned_overflow_check", False))
            signed_overflow_check   = bool(getattr(flag_selection, "signed_overflow_check", False))
            conversion_check        = bool(getattr(flag_selection, "conversion_check", False))
            pointer_overflow_check  = bool(getattr(flag_selection, "pointer_overflow_check", False))
            undefined_shift_check   = bool(getattr(flag_selection, "undefined_shift_check", False))
            import os as _os_ms
            if _os_ms.environ.get("BMC_MEMSAFE_ONLY"):
                # Memory-safety-focused: bounds + pointer checks ONLY. Drop the
                # conversion / arithmetic-overflow / pointer-overflow checks, which
                # flag benign idioms (intentional narrowing, one-past-end pointers)
                # as violations -> false alarms the realism filter cannot reliably
                # clear. Matches the clean single-shot check set (cex FA ~1/10).
                pointer_check = bounds_check = True
                unsigned_overflow_check = signed_overflow_check = False
                conversion_check = pointer_overflow_check = undefined_shift_check = False
            # --- SV-COMP deterministic per-property check override (env SVCOMP_PROP) ---
            import os as _os
            _svp = _os.environ.get("SVCOMP_PROP", "")
            # Per-function property class (PlanAgent code-shape inference) overrides the global
            # token: memsafety everywhere, "all" (adds overflow) on functions with size/ptr
            # arithmetic. Read-only map -> thread-safe under parallel check_all.
            _fpm = _os.environ.get("BMC_FUNC_PROP_MAP")
            if _fpm:
                try:
                    import json as _json_fpm
                    _svp = _json_fpm.loads(_fpm).get(getattr(func, "name", ""), _svp) or _svp
                except Exception:
                    pass
            if _svp == "no-overflow":
                signed_overflow_check = True
                unsigned_overflow_check = conversion_check = pointer_overflow_check = undefined_shift_check = False
                pointer_check = bounds_check = div_by_zero_check = False
            elif _svp == "memsafety":
                pointer_check = bounds_check = True
                div_by_zero_check = False
                unsigned_overflow_check = signed_overflow_check = conversion_check = pointer_overflow_check = undefined_shift_check = False
            elif _svp == "unreach":
                pointer_check = bounds_check = div_by_zero_check = False
                unsigned_overflow_check = signed_overflow_check = conversion_check = pointer_overflow_check = undefined_shift_check = False
            elif _svp == "all":
                # goal=all: every built-in memory-safety AND arithmetic check ON (max coverage;
                # accepts the higher FP rate -> triage/refinement sort them).
                pointer_check = bounds_check = div_by_zero_check = True
                unsigned_overflow_check = signed_overflow_check = True
                conversion_check = pointer_overflow_check = undefined_shift_check = True
            # Per-function unwind override (None = use global default).
            unwind_for_this_run     = getattr(flag_selection, "unwind_override", None) or self.config.cbmc_unwind
            # SV-COMP: whole-program harnesses bound their own inputs but call
            # builtin loops (strlen/memcmp/...) the per-function config agent
            # cannot size, so its low unwind guess (e.g. 12) stalls on a
            # strlen.unwind artifact before reaching reach_error. Force a
            # competition-grade unwind floor so CBMC reaches the real property.
            if _svp:
                _svc_unwind = int(_os.environ.get("SVCOMP_UNWIND", "64"))
                if _svc_unwind > (unwind_for_this_run or 0):
                    unwind_for_this_run = _svc_unwind
            # Couple the unwind floor to widened string-copy SOURCES: if the
            # harness modeled an input as a long string feeding a strcpy/strcat
            # SINK, the copy loop must unroll past it (max_len + 2) for the
            # fixed-buffer overflow to be reachable — else the source widening
            # is wasted and --unwinding-assertions would flag the function
            # incomplete rather than clean. Targeted: only fires when the body
            # has a qualifying copy sink, so it doesn't inflate other functions.
            if getattr(self.config, "enable_string_copy_source_modeling", True):
                # Derive the copy-source unwind floor from the GENERATED HARNESS
                # TEXT, not from func.body: a fixed dest like malloc(VFS_MAX_PATH)
                # only resolves to its literal size (256) after the preprocessing
                # the harness assembly applies, whereas func.body (and even
                # parsed_file.function_bodies) may still carry the raw macro and
                # under-resolve. The harness's widening comments record the EXACT
                # width applied to each copy source, so reading them keeps the
                # unwind floor consistent with the modeling (the strcpy loop must
                # unroll past the widened source to reach the overflow).
                _copy_floor = _copy_widen_floor_from_harness(harness_src)
                if _copy_floor > unwind_for_this_run:
                    unwind_for_this_run = _copy_floor
            # Per-function CBMC timeout override (None = use global default).
            timeout_for_this_run    = getattr(flag_selection, "timeout_override", None) or self.config.cbmc_timeout

            if flag_selection and flag_selection.any_enabled():
                logger.debug(
                    "Flag selection for '%s': %s (%s)",
                    fn_name,
                    ", ".join(flag_selection.enabled_flags()),
                    getattr(flag_selection, "reasoning", ""),
                )
            # Extract the harness entry function name from the harness
            # file header (real-libc mode tags it with `/* Harness entry:
            # NAME */`). When the source already defines `main`, the
            # harness uses a different function name and CBMC needs
            # --function to pick it.
            harness_entry = _harness_entry_of(harness_path)

            def _run_c_cbmc(_hpath, _hentry):
                return run_cbmc(
                    harness_path=_hpath,
                    unwind=unwind_for_this_run,
                    timeout=timeout_for_this_run,
                    cbmc_path=self.config.cbmc_path,
                    include_dirs=getattr(self.config, "include_dirs", None),
                    defines=getattr(self.config, "cbmc_defines", None),
                    unsigned_overflow_check=unsigned_overflow_check,
                    signed_overflow_check=signed_overflow_check,
                    conversion_check=conversion_check,
                    pointer_overflow_check=pointer_overflow_check,
                    undefined_shift_check=undefined_shift_check,
                    pointer_check=pointer_check,
                    bounds_check=bounds_check,
                    div_by_zero_check=div_by_zero_check,
                    object_bits=getattr(self.config, "cbmc_object_bits", None),
                    auto_scale_object_bits=getattr(
                        self.config, "cbmc_auto_scale_object_bits", True
                    ),
                    function=_hentry,
                )

            cbmc_result = _run_c_cbmc(harness_path, harness_entry)

            # Agentic harness-repair fallback (opt-in): the DETERMINISTIC harness
            # failed to BUILD (conversion / incomplete-type / parse error). Let the
            # code-reading AgenticHarnessGen rebuild it, then re-run. Fires only on
            # a build error, so there's no soundness downside — a non-building
            # harness yields no verdict either way.
            if (
                getattr(self.config, "enable_agentic_harness_repair", False)
                and cbmc_result.error
                and _is_harness_build_error(cbmc_result.error)
            ):
                repaired_path = self._agentic_repair_harness(
                    func, parsed_file, all_funcs, driver_name,
                    build_error=cbmc_result.error,
                    spec=spec,
                )
                if repaired_path is not None:
                    repaired_result = _run_c_cbmc(
                        repaired_path, _harness_entry_of(repaired_path)
                    )
                    if not (
                        repaired_result.error
                        and _is_harness_build_error(repaired_result.error)
                    ):
                        logger.info(
                            "agentic harness repair resolved the build error for "
                            "'%s' (verified=%s, cex=%d)",
                            fn_name, repaired_result.verified,
                            len(repaired_result.counterexamples),
                        )
                        harness_path = repaired_path
                        cbmc_result = repaired_result
                    else:
                        logger.info(
                            "agentic harness repair did NOT resolve the build "
                            "error for '%s' — keeping original verdict", fn_name,
                        )
        else:
            # Rust / Kani path: the backend wraps its own verifier
            # invocation and returns a CBMCResult-shaped object.
            cbmc_result = self.backend.check(
                harness_path,
                harness_name=f"check_{fn_name}",
                source_path=getattr(parsed_file, "path", None),
            )

            # Timeout retry: complex Rust functions (UTF-8 validation,
            # allocator-driven Vec/String code) can blow past Kani's
            # 120-s timeout at the default slice_bound=4. Regenerate
            # the harness with progressively smaller buffer bounds
            # (4 → 2 → 1) and a tighter loop unwind, then re-check.
            # Each retry overwrites the saved harness so the artifact
            # reflects the configuration that actually produced the
            # verdict. Regression: CCC encoding.rs run 2026-05-19 —
            # bytes_to_string and encode_non_utf8 timed out at default
            # bound=4; encode_non_utf8 verifies clean at bound=1 in ~40s.
            # Unwind-exhausted retry: when Kani reports "loop unwind
            # bound exhausted" the result is inconclusive — the function
            # would have verified clean if the solver had been allowed
            # more loop iterations. Bump unwind 4 → 16 (one retry) and
            # try again; keep slice_bound the same. Distinct from the
            # timeout-retry path below, which shrinks state.
            if (
                cbmc_result.error
                and "unwind bound exhausted" in cbmc_result.error
                and getattr(self.backend, "generate_harness", None) is not None
            ):
                bumped_unwind = max(self.config.kani_unwind * 4, 16)
                logger.info(
                    "Kani unwind-exhausted for '%s' — retrying with unwind=%d",
                    fn_name, bumped_unwind,
                )
                cbmc_result = self.backend.check(
                    harness_path,
                    harness_name=f"check_{fn_name}",
                    source_path=getattr(parsed_file, "path", None),
                    unwind_override=bumped_unwind,
                )
                if not cbmc_result.error or "unwind bound" not in cbmc_result.error:
                    logger.info(
                        "Kani unwind-bump succeeded for '%s' at unwind=%d",
                        fn_name, bumped_unwind,
                    )

            if (
                cbmc_result.error
                and "timed out" in cbmc_result.error
                and getattr(self.backend, "generate_harness", None) is not None
            ):
                # Use a shorter wall-clock for retries: the whole point of
                # shrinking the bound is to reduce state-space, so if
                # bound=1 doesn't land in 60s it almost certainly won't
                # land in 120s. Keeps total per-function budget bounded
                # (worst case: 120 + 60 + 60 ≈ 4 min, vs 6 min before).
                retry_timeout = min(60, self.config.kani_timeout)
                for retry_bound, retry_unwind in [(2, 4), (1, 2)]:
                    logger.info(
                        "Kani timed out for '%s' — retrying with "
                        "slice_bound=%d, unwind=%d, timeout=%ds",
                        fn_name, retry_bound, retry_unwind, retry_timeout,
                    )
                    try:
                        retry_src = self.backend.generate_harness(
                            func, spec, {}, parsed_file,
                            all_funcs=all_funcs,
                            slice_bound_override=retry_bound,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Retry harness gen failed for '%s' at bound=%d: %s",
                            fn_name, retry_bound, exc,
                        )
                        break
                    retry_path = self._save_harness(driver_name, fn_name, retry_src)
                    cbmc_result = self.backend.check(
                        retry_path,
                        harness_name=f"check_{fn_name}",
                        source_path=getattr(parsed_file, "path", None),
                        unwind_override=retry_unwind,
                        timeout_override=retry_timeout,
                    )
                    harness_path = retry_path
                    if not cbmc_result.error or "timed out" not in cbmc_result.error:
                        logger.info(
                            "Kani retry succeeded for '%s' at slice_bound=%d",
                            fn_name, retry_bound,
                        )
                        break

        # ---- Step 4: build verdict ----
        if cbmc_result.error:
            logger.warning(
                "CBMC error for '%s': %s", fn_name, cbmc_result.error
            )
            verdict = BMCVerdict(
                function_name=fn_name,
                verified=False,
                counterexamples=cbmc_result.counterexamples,
                harness_path=str(harness_path),
                cbmc_result=cbmc_result,
                error=cbmc_result.error,
            )
        else:
            logger.info(
                "CBMC verdict for '%s': verified=%s, counterexamples=%d",
                fn_name,
                cbmc_result.verified,
                len(cbmc_result.counterexamples),
            )
            verdict = BMCVerdict(
                function_name=fn_name,
                verified=cbmc_result.verified,
                counterexamples=cbmc_result.counterexamples,
                harness_path=str(harness_path),
                cbmc_result=cbmc_result,
                error=None,
            )

        # ---- Save results to artifact store ----
        try:
            self.store.save_cbmc_result(driver_name, fn_name, cbmc_result)
            self.store.save_bug_report(driver_name, fn_name, verdict.to_dict())
        except Exception as exc:
            logger.warning("Failed to save artifacts for '%s': %s", fn_name, exc)

        return verdict

    def _agentic_repair_harness(
        self,
        func: FunctionInfo,
        parsed_file: ParsedCFile,
        all_funcs: "dict | None",
        driver_name: str,
        build_error: str,
        spec=None,
    ) -> "str | None":
        """Rebuild a non-compiling harness with the agentic, code-reading
        generator (``AgenticHarnessGen``), which reads the real structs/headers
        and compile-checks with retry. Returns the path to a freshly saved
        harness on success, or None. Fail-safe: any error returns None and the
        caller keeps the original (failed) verdict.
        """
        try:
            from pathlib import Path as _Path
            from bmc_agent.agentic_harness_gen import AgenticHarnessGen

            src_path = getattr(parsed_file, "path", "") or ""
            parsed_files = {src_path: parsed_file} if src_path else {}
            corpus_root = _Path(src_path).parent if src_path else _Path(".")
            logger.info(
                "agentic harness repair: rebuilding harness for '%s' "
                "(build error: %s)",
                func.name, (build_error or "")[:140],
            )
            agen = AgenticHarnessGen(
                config=self.config,
                parsed_files=parsed_files,
                corpus_root=corpus_root,
            )
            gen_kwargs = dict(
                func=func,
                all_funcs_global=all_funcs or {},
                include_dirs=list(getattr(self.config, "include_dirs", None) or []),
                defines=list(getattr(self.config, "cbmc_defines", None) or []),
            )
            # Route by the RESOLVED PROVIDER, not just the claude_code_agentic
            # flag: under --agentic the user may pin the agentic roles to an API
            # (e.g. OpenRouter) via per-role overrides. Honour that — use the
            # Claude Code agent ONLY when the resolved provider is actually
            # claude-code; otherwise use bmc's in-process tool loop, which runs on
            # the openai-compatible API. (Previously this hardcoded claude-code
            # whenever claude_code_agentic was set, so "--agentic with API" still
            # fell back to the claude-code subscription for harness repair.)
            try:
                rs = self.config.role_settings("spec_gen") if hasattr(self.config, "role_settings") else None
                prov = (rs or {}).get("provider") or self.config.resolved_provider()
            except Exception:
                prov = "claude-code" if getattr(self.config, "claude_code_agentic", False) else ""
            _precond = ""
            try:
                _precond = (getattr(spec, "precondition", "") or "").strip()
            except Exception:
                _precond = ""
            if prov == "claude-code":
                logger.info("agentic harness repair: using Claude Code agent for '%s'", func.name)
                res = agen.generate_via_claude_code(**gen_kwargs, spec_preconditions=_precond)
            else:
                logger.info("agentic harness repair: using in-process tool loop (provider=%s) for '%s'",
                            prov or "?", func.name)
                res = agen.generate(**gen_kwargs, spec_preconditions=_precond)
            harness = getattr(res, "harness", None)
            if harness and not getattr(res, "last_compile_error", None):
                return str(self._save_harness(driver_name, func.name, harness))
            logger.info(
                "agentic harness repair produced no clean harness for '%s' "
                "(last_compile_error=%s)",
                func.name, str(getattr(res, "last_compile_error", ""))[:140],
            )
        except Exception as exc:
            logger.warning(
                "agentic harness repair failed for '%s': %s", func.name, exc
            )
        return None

    def check_all(
        self,
        funcs: dict[str, FunctionInfo],
        specs: dict[str, Spec],
        parsed_file: ParsedCFile,
        driver_name: str,
        all_funcs: "dict | None" = None,
        flag_selections: "dict | None" = None,
        progress_cb: "Callable[..., None] | None" = None,
    ) -> dict[str, BMCVerdict]:
        """
        Check all functions in parallel (ThreadPoolExecutor).

        Parameters
        ----------
        funcs:
            Mapping function_name → FunctionInfo.
        specs:
            Mapping function_name → Spec.
        parsed_file:
            The parsed C file object.
        driver_name:
            Driver name for artifact storage.
        progress_cb:
            Optional structured-progress sink (the pipeline passes
            ``AMCPipeline._emit``). Called with one ``type="function"`` event as
            each function settles, so the workbench's per-function counter and
            the ETA estimate update live through this (often longest) phase
            instead of only at its end.

        Returns
        -------
        Mapping function_name → BMCVerdict.
        """
        verdicts: dict[str, BMCVerdict] = {}

        # Only check functions that have both a FunctionInfo and a Spec
        to_check = {
            name: funcs[name]
            for name in funcs
            if name in specs
        }
        if not to_check:
            logger.warning("No functions to check in driver '%s'", driver_name)
            return verdicts

        # CBMC is CPU-bound: cap at the configured worker count AND the CPU
        # count so we don't oversubscribe cores with concurrent solver runs.
        import os as _os
        max_workers = min(
            len(to_check),
            getattr(self.config, "max_workers", 8),
            (_os.cpu_count() or 8),
        )
        logger.info(
            "Checking %d functions in driver '%s' with %d workers",
            len(to_check),
            driver_name,
            max_workers,
        )

        import contextvars
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Run each task in a fresh copy of the current context so the web
            # runner's per-run log sink (a ContextVar) reaches the worker threads;
            # they'd otherwise start with an empty context and their logs would be
            # dropped from the live run view. A per-task copy is required (one
            # Context can't be entered concurrently).
            future_to_name = {
                executor.submit(
                    contextvars.copy_context().run,
                    self.check_function,
                    func,
                    specs[name],
                    parsed_file,
                    driver_name,
                    all_funcs,
                    (flag_selections or {}).get(name),
                ): name
                for name, func in to_check.items()
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    verdict = future.result()
                    verdicts[name] = verdict
                except Exception as exc:
                    logger.error(
                        "Unexpected error checking '%s': %s", name, exc
                    )
                    verdicts[name] = BMCVerdict(
                        function_name=name,
                        verified=False,
                        error=f"Unexpected error: {exc}",
                    )
                if progress_cb is not None:
                    v = verdicts[name]
                    cex = getattr(v, "counterexamples", None)
                    status = ("verified" if getattr(v, "verified", False)
                              else ("counterexample" if cex else "unresolved"))
                    progress_cb(
                        type="function",
                        name=name,
                        phase="bmc",
                        status=status,
                        n_counterexamples=len(cex) if cex else 0,
                        # Why an "unresolved" verdict couldn't be decided (timeout,
                        # vacuous, harness failure, …) — shown as the workbench bmc
                        # chip tooltip. Empty for verified/counterexample. The full
                        # BMCVerdict.error stays in artifacts + logs.
                        detail=(self._unresolved_reason(v) if status == "unresolved" else ""),
                        # The CBMC harness is the proof artifact. Carry its source
                        # in the event so the web workbench can show it after the
                        # run's scratch dir (where harness_path lives) is removed;
                        # see web.runner._stream_pipeline cleanup.
                        harness=self._read_harness_src(getattr(v, "harness_path", "")),
                        harness_lang=self.backend.language,
                    )

        return verdicts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unresolved_reason(verdict) -> str:
        """One-line, human reason for an unresolved verdict — shown as the bmc
        chip tooltip in the workbench. The full BMCVerdict.error (which can be a
        multi-line CBMC dump) stays on the verdict, in artifacts and logs."""
        err = (getattr(verdict, "error", "") or "").strip()
        if not err:
            return "BMC could not prove or disprove this function"
        low = err.lower()
        if "timed out" in low or "timeout" in low or "time-budget" in low:
            return "CBMC timed out before a proof or counterexample"
        if "vacuous" in low:
            return "function body not analysed (likely extern / not linked)"
        if "unresolvable-types" in low or "unresolved_types" in low:
            return "input types couldn't be modeled for checking"
        # CBMC build errors all echo the `harness.c` filename, so a bare
        # "harness" match is too broad — surface the SPECIFIC cause first.
        # Undeclared types / missing headers are the common case for code with
        # external deps (e.g. wlroots/wayland headers not in the upload), which
        # the harness models as opaque; say so rather than "harness build failed".
        if (
            "no such file" in low
            or "undeclared" in low
            or "unknown type" in low
            or "incomplete type" in low
        ):
            return "references external types/headers not in the project (modeled opaquely)"
        if "harness generation failed" in low:
            return "harness generation failed"
        if "harness" in low:
            return "harness build failed"
        if "unexpected error" in low:
            return "internal error during the check"
        first = err.splitlines()[0]
        return first if len(first) <= 160 else first[:157] + "…"

    # Cap on harness source carried in a progress event, so a pathological
    # generated harness can't bloat the web job's retained event log.
    _MAX_HARNESS_BYTES = 200_000

    def _read_harness_src(self, harness_path: str) -> str:
        """Best-effort read of a saved harness for the progress event.

        Returns "" when no harness was produced (generation skipped/failed),
        the file is unreadable, or it exceeds ``_MAX_HARNESS_BYTES``."""
        if not harness_path:
            return ""
        try:
            p = Path(harness_path)
            if p.stat().st_size > self._MAX_HARNESS_BYTES:
                return ""
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _save_harness(
        self,
        driver_name: str,
        func_name: str,
        harness_src: str,
    ) -> Path:
        """
        Save the harness source to
        ``{artifact_dir}/{driver_name}/{func_name}/harness.{c,rs}``.

        The extension follows ``self.backend.language`` so Rust harnesses
        get a ``.rs`` extension (required for Kani to parse them) and
        artifact inspection tools can rely on filename to language.
        """
        ext = "rs" if self.backend.language == "rust" else "c"
        fn_dir = (
            Path(self.config.artifact_dir) / driver_name / func_name
        )
        fn_dir.mkdir(parents=True, exist_ok=True)
        harness_path = fn_dir / f"harness.{ext}"
        harness_path.write_text(harness_src, encoding="utf-8")
        return harness_path
