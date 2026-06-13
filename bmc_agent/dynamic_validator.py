"""
Phase 3 Stage 3: Dynamic CEx Validation.

Compiles a GCC-based harness and executes it to confirm that a BMC counterexample
triggers a real fault at runtime.  The harness wraps the entry function (highest
available caller in the call chain) with signal handlers that catch SIGSEGV /
SIGABRT / SIGFPE / SIGILL and reports whether the fault occurred.

Outcomes:
  CONFIRMED     — the harness triggered a signal (fault confirmed at runtime)
  NOT_TRIGGERED — the harness ran to completion without faulting
  INCONCLUSIVE  — compilation or execution failed (tool unavailable, timeout, etc.)
  SKIPPED       — dynamic validation disabled or not applicable
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bmc_agent.config import Config
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, ParsedCFile

if TYPE_CHECKING:
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.llm import LLMClient

logger = get_logger("dynamic_validator")


# ---------------------------------------------------------------------------
# Link-flag detection from #include'd public headers
# ---------------------------------------------------------------------------

# Header → linker library mapping for OSS projects bmc-agent has been
# calibrated on. The reproducer LLM is required to #include a project
# public header (cex_validator's _reproducer_uses_public_api gate); we
# use that include to derive the linker flag so the GCC build actually
# resolves the public-API symbols against the project's installed .so.
_HEADER_TO_LIB: dict[str, str] = {
    # libarchive
    "archive.h": "archive",
    "archive_entry.h": "archive",
    # libcurl
    "curl/curl.h": "curl",
    # libxml2
    "libxml/parser.h": "xml2",
    "libxml/tree.h": "xml2",
    "libxml/xmlmemory.h": "xml2",
    "libxml/HTMLparser.h": "xml2",
    # openssl
    "openssl/ssl.h": "ssl",
    "openssl/crypto.h": "crypto",
    "openssl/evp.h": "crypto",
    "openssl/x509.h": "crypto",
    # zlib / bzip2 / lzma
    "zlib.h": "z",
    "bzlib.h": "bz2",
    "lzma.h": "lzma",
    # nghttp2
    "nghttp2/nghttp2.h": "nghttp2",
}


_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)


# Standard C / POSIX headers an LLM reproducer may legitimately include; these
# resolve against the system toolchain and are never stripped.
_STD_HEADERS = frozenset({
    "assert.h", "ctype.h", "errno.h", "fenv.h", "float.h", "inttypes.h",
    "limits.h", "locale.h", "math.h", "setjmp.h", "signal.h", "stdarg.h",
    "stdbool.h", "stddef.h", "stdint.h", "stdio.h", "stdlib.h", "string.h",
    "tgmath.h", "time.h", "wchar.h", "wctype.h", "stdatomic.h", "stdnoreturn.h",
    "unistd.h", "fcntl.h", "sys/types.h", "sys/stat.h", "sys/mman.h",
    "sys/wait.h", "sys/socket.h", "netinet/in.h", "arpa/inet.h", "pthread.h",
    "dlfcn.h", "dirent.h", "poll.h", "memory.h", "malloc.h", "alloca.h",
})

_INCLUDE_LINE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"]+)[>"].*$',
                              re.MULTILINE)


def _sanitize_reproducer_includes(source: str, config: "Config") -> str:
    """Comment out ``#include`` directives an LLM reproducer cannot compile.

    LLM-generated reproducers are prompted to ``#include`` the project's public
    header — fine for an installed, hosted library (libarchive), but for a
    freestanding / bare-metal target (e.g. VibeOS) the named header is either
    hallucinated (``#include <archive.h>`` for a dtb parser) or a kernel header
    that doesn't resolve in a hosted GCC build. Either way GCC fails with
    ``fatal error: X: No such file or directory`` and the whole reproducer is
    wasted (and ``_detect_link_flags`` would add a bogus ``-l``).

    Keep standard C/POSIX headers and any header that actually resolves in the
    configured ``-I`` dirs; comment out the rest so the compile can proceed on
    the reproducer's own (often self-contained) code. If a stripped header was
    genuinely needed, the build still fails on the missing symbol and falls
    back to the unit-level harness — no worse than the fatal include, minus the
    wasted link attempt.
    """
    include_dirs = [str(d) for d in (getattr(config, "include_dirs", None) or [])]

    def _resolves(hdr: str) -> bool:
        if hdr in _STD_HEADERS:
            return True
        # Quoted/angle project header that exists on an -I path.
        return any(os.path.exists(os.path.join(d, hdr)) for d in include_dirs)

    dropped: list[str] = []

    def _repl(m: "re.Match") -> str:
        hdr = m.group(1)
        if _resolves(hdr):
            return m.group(0)
        dropped.append(hdr)
        return f"/* AMC: dropped unresolved #include <{hdr}> */"

    out = _INCLUDE_LINE_RE.sub(_repl, source)
    if dropped:
        logger.info(
            "Reproducer: dropped %d unresolved #include(s) before compile: %s",
            len(dropped), ", ".join(sorted(set(dropped))),
        )
    return out


def _detect_link_flags(source: str, config: "Config") -> list[str]:
    """Derive ``-l<libname>`` (and optional ``-L<dir>``) flags from the
    project public headers a reproducer ``#include``s.

    The system-entry reproducer (LLM-generated, gated by
    ``_reproducer_uses_public_api``) drives the FUT through the real
    public API. For the call to actually resolve at link time, the
    GCC build must link against the project's ``.so`` — otherwise we
    get ``undefined reference to archive_match_new`` on every
    libarchive reproducer and the entire dynamic-validation channel
    is silent.

    Strategy: scan the source for ``#include <header>`` lines, map known
    project headers to their library name via ``_HEADER_TO_LIB``, and
    emit ``-l<name>``. ``-L<dir>`` flags from ``BMC_AGENT_DYN_LIB_DIRS``
    (colon-separated) precede the ``-l`` flags so the linker checks the
    user-provided paths before the system default. Returns an empty
    list when no known header is included.
    """
    libs: list[str] = []
    seen: set[str] = set()
    for inc in _INCLUDE_RE.findall(source):
        lib = _HEADER_TO_LIB.get(inc)
        if lib is None:
            # Also try header basename — covers libarchive's
            # ``archive.h`` regardless of include style.
            base = inc.split("/")[-1]
            lib = _HEADER_TO_LIB.get(base)
        if lib and lib not in seen:
            libs.append(lib)
            seen.add(lib)

    flags: list[str] = []
    lib_dirs = os.environ.get("BMC_AGENT_DYN_LIB_DIRS", "")
    if lib_dirs:
        for d in lib_dirs.split(":"):
            d = d.strip()
            if d:
                flags += ["-L", d, f"-Wl,-rpath,{d}"]
    for lib in libs:
        flags.append(f"-l{lib}")
    return flags


# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------


class DynamicOutcome(Enum):
    CONFIRMED     = "confirmed"
    NOT_TRIGGERED = "not_triggered"
    INCONCLUSIVE  = "inconclusive"
    SKIPPED       = "skipped"


@dataclass
class DynamicValidationResult:
    outcome: DynamicOutcome
    signal_name: Optional[str] = None   # e.g., "SIGSEGV", "SIGABRT"
    compile_error: Optional[str] = None
    run_error: Optional[str] = None
    reasoning: str = ""
    harness_source: Optional[str] = None  # the C source that was compiled and run
    # Which harness produced this outcome — the EVIDENCE-QUALITY signal:
    #   "system_entry" — driven through a real caller path (LLM system-entry
    #                    reproducer / scenario reproducer): a crash here is
    #                    reachability-meaningful (strong evidence).
    #   "unit"         — unit-level harness with nondet args: a crash only proves
    #                    "this code faults on SOME input" (weak evidence — barely
    #                    stronger than the BMC counterexample it validates).
    harness_kind: str = "unit"
    # Step A — fault-site classification. Possible values:
    #   "in_fut"      — fault fired inside or after the FUT call
    #                   (real-bug-shaped signal)
    #   "in_setup"    — fault fired in harness setup BEFORE the FUT was
    #                   reached (harness-artifact; NOT a real-bug signal)
    #   "unknown"     — fault site could not be determined (e.g., process
    #                   killed by OS signal without our handler running,
    #                   or stripped binary)
    #   None          — no fault fired; field not applicable
    fault_site: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "signal_name": self.signal_name,
            "compile_error": self.compile_error,
            "run_error": self.run_error,
            "reasoning": self.reasoning,
            "harness_source": self.harness_source,
            "fault_site": self.fault_site,
        }


# ---------------------------------------------------------------------------
# DynamicValidator
# ---------------------------------------------------------------------------


class DynamicValidator:
    """Compiles and executes dynamic harnesses to confirm BMC counterexamples."""

    def __init__(
        self,
        config: Config,
        harness_gen: "HarnessGenerator",
        llm: Optional["LLMClient"] = None,
    ) -> None:
        self.config = config
        self.harness_gen = harness_gen
        # Optional LLM client — when supplied, the system-entry reproducer
        # path retries on compile failure by feeding the GCC error back to
        # the LLM and asking for a corrected reproducer. None disables the
        # retry (e.g. unit tests with mocked dyn-val).
        self._llm: Optional["LLMClient"] = llm
        # Bounded to keep token cost in check; each iteration is one
        # LLM call + one GCC compile.
        self._reproducer_retry_max = int(
            os.environ.get("BMC_AGENT_DYN_REPRODUCER_RETRY_MAX", "2")
        )
        # Step B — input-realism triage on CONFIRMED outcomes. Off by
        # default because it costs one LLM call per CONFIRMED CEx. Set
        # BMC_AGENT_DYNVAL_INPUT_TRIAGE=1 to enable.
        self._input_triage_enabled = (
            os.environ.get("BMC_AGENT_DYNVAL_INPUT_TRIAGE", "0")
            .lower() not in ("0", "false", "off", "")
        )
        # Step C — iterative regen on harness-artifact signals. Off by
        # default; depends on Step B's triage signal. Capped to keep
        # cost bounded.
        self._artifact_regen_max = int(
            os.environ.get("BMC_AGENT_DYNVAL_ARTIFACT_REGEN_MAX", "2")
        )

    def validate(
        self,
        entry_func: FunctionInfo,
        counterexample: "Counterexample",
        parsed_file: ParsedCFile,
        all_funcs: Optional[dict] = None,
        all_specs: Optional[dict] = None,
        caller_path: Optional[list[str]] = None,
        system_entry_reproducer: Optional[str] = None,
    ) -> DynamicValidationResult:
        """
        Attempt to confirm the counterexample by compiling and running a dynamic harness.

        Strategy:
        1. If system_entry_reproducer is provided (LLM-generated C from the system entry),
           try to compile and run it first — this exercises the real call chain.
        2. Generate a unit-level harness with global state injection (with_globals=True).
        3. Compile it.  If compilation fails, retry without globals (with_globals=False).
        4. Run the compiled binary and parse stdout for DYNAMIC:CONFIRMED / NOT_TRIGGERED.
        """
        if not self.config.enable_dynamic_validation:
            return DynamicValidationResult(
                outcome=DynamicOutcome.SKIPPED,
                reasoning="Dynamic validation is disabled (enable_dynamic_validation=False).",
            )

        cc = self.config.dynamic_cc_path
        if not shutil.which(cc):
            return DynamicValidationResult(
                outcome=DynamicOutcome.INCONCLUSIVE,
                reasoning=f"C compiler '{cc}' not found on PATH — skipping dynamic validation.",
            )

        # winning_harness: the C source that successfully compiled (for realism checker)
        winning_harness: Optional[str] = None

        # --- Attempt 0: system-entry reproducer (LLM-generated, call chain intact) ---
        if system_entry_reproducer and _looks_like_c_code(system_entry_reproducer):
            current_reproducer = system_entry_reproducer
            last_compile_err: Optional[str] = None
            for retry_n in range(self._reproducer_retry_max + 1):
                # Drop #includes that won't resolve in a hosted build (bogus or
                # bare-metal project headers) so a hallucinated/freestanding
                # header doesn't fatal the compile (and doesn't drive a bogus
                # -l link flag). Standard + resolvable project headers are kept.
                current_reproducer = _sanitize_reproducer_includes(
                    current_reproducer, self.config,
                )
                se_harness = _wrap_reproducer_with_signal_handlers(current_reproducer)
                # Derive -l<libname> from #include'd project headers so the
                # public-API call chain actually resolves at link time.
                # Otherwise libarchive sweeps systematically fail with
                # "undefined reference to archive_match_new" → INCONCLUSIVE.
                link_flags = _detect_link_flags(current_reproducer, self.config)
                binary_path_se, compile_err = self._compile(
                    se_harness, cc, extra_flags=link_flags or None,
                )
                if binary_path_se is not None:
                    try:
                        result = self._run(binary_path_se)
                    finally:
                        _unlink(binary_path_se)
                    result.harness_source = se_harness
                    result.harness_kind = "system_entry"
                    logger.info(
                        "System-entry dynamic validation for '%s': %s%s%s",
                        entry_func.name,
                        result.outcome.value,
                        f" signal={result.signal_name}" if result.signal_name else "",
                        f" (after {retry_n} LLM regen retr{'y' if retry_n == 1 else 'ies'})" if retry_n else "",
                    )
                    return result
                last_compile_err = compile_err
                # Compile failed. If we have an LLM and budget, ask it to
                # fix the reproducer based on the compile error. If not,
                # fall through to the unit-level harness.
                if (
                    retry_n < self._reproducer_retry_max
                    and self._llm is not None
                    and compile_err
                    and not _is_link_only_error(compile_err)
                ):
                    fixed = self._regenerate_reproducer_with_error(
                        current_reproducer, compile_err, entry_func.name,
                    )
                    if fixed and fixed != current_reproducer:
                        logger.info(
                            "System-entry reproducer compile failed for '%s' "
                            "(retry %d/%d) — LLM produced a corrected version, "
                            "trying again",
                            entry_func.name, retry_n + 1,
                            self._reproducer_retry_max,
                        )
                        current_reproducer = fixed
                        continue
                # No more retries possible — fall out.
                break
            logger.info(
                "System-entry reproducer compilation failed for '%s' — "
                "falling back to unit-level harness%s",
                entry_func.name,
                f" (compile error: {(last_compile_err or '')[:120]!r})" if last_compile_err else "",
            )

        # --- Attempt 1: unit-level harness with global state injection ---
        harness_src = self._generate(
            entry_func, counterexample, parsed_file, all_funcs, all_specs,
            with_globals=True,
        )
        if harness_src is None:
            return DynamicValidationResult(
                outcome=DynamicOutcome.INCONCLUSIVE,
                reasoning="Harness generation failed.",
            )

        binary_path, compile_err = self._compile(harness_src, cc)
        if binary_path is not None:
            winning_harness = harness_src

        if binary_path is None:
            # --- Attempt 2: without global state injection ---
            logger.info(
                "Dynamic harness (with_globals) compile failed for '%s' — retrying without globals",
                entry_func.name,
            )
            harness_src2 = self._generate(
                entry_func, counterexample, parsed_file, all_funcs, all_specs,
                with_globals=False,
            )
            if harness_src2 is None:
                return DynamicValidationResult(
                    outcome=DynamicOutcome.INCONCLUSIVE,
                    compile_error=compile_err,
                    reasoning="Harness generation failed on second attempt.",
                )
            binary_path, compile_err2 = self._compile(harness_src2, cc)
            if binary_path is not None:
                winning_harness = harness_src2
            if binary_path is None:
                # --- Attempt 3: relax linker — ignore undefined external symbols ---
                # Bare-metal functions often reference globals from other translation
                # units (e.g. fb_base from fb.c).  Allow undefined references so the
                # harness still runs; unresolved globals default to address 0, which
                # is likely to trigger the same fault the CEx predicts.
                if compile_err2 and "undefined reference" in compile_err2:
                    logger.info(
                        "Dynamic harness has undefined external refs for '%s' — "
                        "retrying with --allow-unresolved-symbols",
                        entry_func.name,
                    )
                    binary_path, compile_err3 = self._compile(
                        harness_src2, cc,
                        extra_flags=["-Wl,--unresolved-symbols=ignore-all"],
                    )
                    if binary_path is not None:
                        winning_harness = harness_src2
                else:
                    compile_err3 = compile_err2
                    binary_path = None
                if binary_path is None:
                    # --- Attempt 4: agentic harness repair (opt-in) ---
                    # Backstop mirroring bmc_engine's CBMC-harness repair: the
                    # DETERMINISTIC dynamic harness won't GCC-compile. Ask the
                    # LLM to fix it from the compile error, recompile, and run.
                    # Build-error-only -> no soundness downside (a non-building
                    # harness yields no verdict anyway). Guarded: a repaired
                    # harness is accepted only if it still carries the
                    # DYNAMIC:CONFIRMED signal-oracle marker, so the LLM can't
                    # silently strip the fault reporter and yield a bogus
                    # NOT_TRIGGERED.
                    cur_err = compile_err3 or compile_err2
                    if (
                        getattr(self.config, "enable_agentic_harness_repair", False)
                        and self._llm is not None
                        and cur_err
                        and not _is_link_only_error(cur_err)
                    ):
                        cur_harness = harness_src2
                        for rn in range(self._reproducer_retry_max + 1):
                            fixed = self._regenerate_reproducer_with_error(
                                cur_harness, cur_err, entry_func.name,
                            )
                            if not fixed or fixed == cur_harness:
                                break
                            fixed = _sanitize_reproducer_includes(fixed, self.config)
                            if "DYNAMIC:CONFIRMED" not in fixed:
                                logger.info(
                                    "agentic harness repair: rejected rebuilt harness for "
                                    "'%s' (lost the DYNAMIC:CONFIRMED oracle)", entry_func.name,
                                )
                                break
                            logger.info(
                                "agentic harness repair: dynamic harness rebuilt for '%s' "
                                "(retry %d/%d) — recompiling",
                                entry_func.name, rn + 1, self._reproducer_retry_max,
                            )
                            cur_harness = fixed
                            binary_path, cur_err = self._compile(cur_harness, cc)
                            if binary_path is not None:
                                winning_harness = cur_harness
                                logger.info(
                                    "agentic harness repair resolved the dynamic-harness "
                                    "build error for '%s'", entry_func.name,
                                )
                                break
                if binary_path is None:
                    err_snippet = (compile_err3 or compile_err2 or "unknown")[:300]
                    return DynamicValidationResult(
                        outcome=DynamicOutcome.INCONCLUSIVE,
                        compile_error=compile_err2,
                        reasoning=(
                            f"Dynamic harness compilation failed even without global state "
                            f"injection for '{entry_func.name}'. Error: {err_snippet}"
                        ),
                    )

        try:
            result = self._run(binary_path)
        finally:
            _unlink(binary_path)

        result.harness_source = winning_harness
        result.harness_kind = "unit"
        # Step B — input-realism triage on CONFIRMED outcomes. Off by
        # default; gate via BMC_AGENT_DYNVAL_INPUT_TRIAGE=1. Catches
        # cases that Step A's fault_site check couldn't (the FUT WAS
        # called and the signal fired in or after it, but the witness
        # inputs are unreachable from real callers).
        result = self._post_confirm_triage(
            result=result,
            entry_func=entry_func,
            counterexample=counterexample,
        )
        logger.info(
            "Dynamic validation for '%s': %s%s%s",
            entry_func.name,
            result.outcome.value,
            f" signal={result.signal_name}" if result.signal_name else "",
            f" fault_site={result.fault_site}" if result.fault_site else "",
        )
        return result

    def _post_confirm_triage(
        self,
        result: DynamicValidationResult,
        entry_func: FunctionInfo,
        counterexample: "Counterexample",
    ) -> DynamicValidationResult:
        """Step B + C: when a CONFIRMED outcome's fault_site is not
        already disqualified by Step A, optionally run an LLM-driven
        input-realism audit (Step B). If the agent flags the witness
        as harness-artifact / unbounded-input AND retries remain,
        regenerate the harness with the artifact diagnosis as
        guidance, re-compile, re-run, and re-triage (Step C). After
        the regen budget is exhausted with the artifact verdict
        unchanged, reclassify CONFIRMED → INCONCLUSIVE.

        Returns the final result after up to ``_artifact_regen_max``
        regeneration attempts. Pass-through when
        - the feature flag is off,
        - no LLM client is configured,
        - the outcome isn't CONFIRMED, or
        - Step A already disqualified the signal.
        """
        if not self._input_triage_enabled:
            return result
        if self._llm is None:
            return result
        if result.outcome != DynamicOutcome.CONFIRMED:
            return result
        if result.fault_site == "in_setup":
            # Step A has already reclassified upstream; nothing to do.
            return result

        # ---------------- triage / regen loop ----------------
        # attempt=0 is the initial triage on the existing harness.
        # attempts 1..N are post-regen re-triage.
        last_triage = None
        for attempt in range(self._artifact_regen_max + 1):
            triage = self._run_dynval_triage(
                result=result,
                entry_func=entry_func,
                counterexample=counterexample,
            )
            if triage is None:
                # Agent failure → keep original CONFIRMED verdict.
                return result
            last_triage = triage

            from bmc_agent.agents.dyn_val_triage import DynValTriageVerdict
            if triage.verdict == DynValTriageVerdict.REAL_BUG_SHAPED:
                result.reasoning = (
                    (result.reasoning or "")
                    + f"\n[Step B attempt {attempt}] DynValTriageAgent: "
                    + f"real_bug_shaped ({triage.confidence}). "
                    + f"{triage.reasoning[:200]}"
                )
                return result
            if triage.verdict == DynValTriageVerdict.UNCERTAIN:
                # Don't reclassify on uncertain — preserves recall.
                result.reasoning = (
                    (result.reasoning or "")
                    + f"\n[Step B attempt {attempt}] DynValTriageAgent: "
                    + f"uncertain ({triage.confidence}). "
                    + f"{triage.reasoning[:200]}"
                )
                return result

            # HARNESS_ARTIFACT or UNBOUNDED_INPUT.
            # If we have regen retries remaining (Step C), try to fix
            # the harness with the artifact diagnosis. Otherwise fall
            # through to reclassification below.
            if attempt >= self._artifact_regen_max:
                break

            new_result = self._regen_harness_with_artifact_diagnosis(
                result=result,
                triage=triage,
                entry_func=entry_func,
            )
            if new_result is None:
                # Regen failed (UNREPRODUCIBLE, no change, compile
                # error). Fall through to reclassification.
                break
            if new_result.outcome != DynamicOutcome.CONFIRMED:
                # The regenerated harness didn't fire — record and
                # return. This is *evidence* the original signal was
                # a harness artifact (a tighter input doesn't reach
                # the fault). The new outcome tells the consumer what
                # the tightened harness actually did.
                new_result.reasoning = (
                    (new_result.reasoning or "")
                    + f"\n[Step C attempt {attempt + 1}] Regenerated "
                    + f"harness with artifact diagnosis "
                    + f"({triage.artifact_class}); new outcome="
                    + f"{new_result.outcome.value}. Original CONFIRMED "
                    + f"signal was likely a harness artifact."
                )
                return new_result
            # Still CONFIRMED. Update result and loop to re-triage.
            new_result.reasoning = (
                (new_result.reasoning or "")
                + f"\n[Step C attempt {attempt + 1}] Regenerated harness "
                + f"with artifact diagnosis ({triage.artifact_class}); "
                + f"new harness ALSO fires. Re-triaging."
            )
            result = new_result

        # Exhausted regen budget with artifact verdict still standing.
        # Reclassify CONFIRMED → INCONCLUSIVE.
        triage = last_triage
        tag = triage.verdict.value
        cls = triage.artifact_class or "unspecified"
        logger.info(
            "Step B/C reclassified '%s' CONFIRMED → INCONCLUSIVE after %d "
            "regen attempt(s) (triage=%s class=%s)",
            entry_func.name, self._artifact_regen_max, tag, cls,
        )
        result.outcome = DynamicOutcome.INCONCLUSIVE
        result.reasoning = (
            (result.reasoning or "")
            + f"\n[Step B+C] DynValTriageAgent: {tag} "
            + f"(class={cls}, conf={triage.confidence}) persists after "
            + f"{self._artifact_regen_max} regen attempt(s). "
            + f"Reclassified CONFIRMED → INCONCLUSIVE. "
            + f"Final triage: {triage.reasoning[:300]}"
        )
        return result

    def _run_dynval_triage(
        self,
        result: DynamicValidationResult,
        entry_func: FunctionInfo,
        counterexample: "Counterexample",
    ) -> "Optional[Any]":
        """Helper: invoke DynValTriageAgent on the current result.
        Returns the DynValTriageResult or None on agent failure.
        """
        try:
            from bmc_agent.agents.dyn_val_triage import DynValTriageAgent
            agent = DynValTriageAgent(config=self.config, llm=self._llm)
            va = (counterexample.variable_assignments or {})
            witness_lines = []
            for k, v in va.items():
                if k.startswith("__CPROVER_"):
                    continue
                if k.startswith("rb_ops"):
                    continue
                witness_lines.append(f"  {k} = {v}")
                if len(witness_lines) > 60:
                    witness_lines.append("  ...")
                    break
            witness_text = "\n".join(witness_lines)
            outcome = agent.run(
                func_name=entry_func.name,
                func_source=(entry_func.body or "")[:3000],
                harness=(result.harness_source or "")[:3000],
                witness=witness_text,
                run_output=(result.reasoning or "")[:1000],
                signal_name=result.signal_name or "unknown",
                fault_site=result.fault_site or "unknown",
            )
            if outcome is None:
                return None
            return outcome.output
        except Exception as exc:
            logger.debug(
                "DynValTriageAgent raised on '%s': %s — pass-through",
                entry_func.name, exc,
            )
            return None

    def _regen_harness_with_artifact_diagnosis(
        self,
        result: DynamicValidationResult,
        triage,
        entry_func: FunctionInfo,
    ) -> "Optional[DynamicValidationResult]":
        """Step C: ask DynamicReproAgent to regenerate the harness with
        the artifact diagnosis as guidance, then compile + run.
        Returns the new DynamicValidationResult, or None when:
          - the agent returned UNREPRODUCIBLE / no change
          - the regenerated harness failed to compile
        """
        try:
            from bmc_agent.agents.dynamic_repro import DynamicReproAgent
            agent = DynamicReproAgent(config=self.config, llm=self._llm)
            outcome = agent.run(
                previous_reproducer=(result.harness_source or ""),
                func_name=entry_func.name,
                artifact_class=triage.artifact_class or "unspecified",
                triage_reasoning=triage.reasoning,
                signal_name=result.signal_name or "unknown",
            )
        except Exception as exc:
            logger.debug(
                "DynamicReproAgent (artifact mode) raised on '%s': %s",
                entry_func.name, exc,
            )
            return None

        if outcome is None or not outcome.output:
            return None
        new_src = outcome.output
        if new_src == result.harness_source:
            return None
        if "UNREPRODUCIBLE" in new_src:
            logger.debug(
                "Step C: agent returned UNREPRODUCIBLE for '%s'",
                entry_func.name,
            )
            return None

        # Compile + run the regenerated harness.
        cc = self.config.dynamic_cc_path
        binary_path, compile_err = self._compile(new_src, cc)
        if binary_path is None:
            logger.debug(
                "Step C: regenerated harness for '%s' failed to compile: %s",
                entry_func.name, (compile_err or "")[:200],
            )
            return None
        try:
            new_result = self._run(binary_path)
        finally:
            _unlink(binary_path)
        new_result.harness_source = new_src
        new_result.harness_kind = "system_entry"
        return new_result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate(
        self,
        entry_func: FunctionInfo,
        counterexample: "Counterexample",
        parsed_file: ParsedCFile,
        all_funcs: Optional[dict],
        all_specs: Optional[dict],
        with_globals: bool,
    ) -> "str | None":
        try:
            return self.harness_gen.generate_dynamic_harness(
                entry_func=entry_func,
                counterexample=counterexample,
                parsed_file=parsed_file,
                all_funcs=all_funcs or {},
                all_specs=all_specs,
                with_globals=with_globals,
            )
        except Exception as exc:
            logger.warning(
                "Dynamic harness generation failed for '%s': %s",
                entry_func.name, exc,
            )
            return None

    def _regenerate_reproducer_with_error(
        self,
        previous_reproducer: str,
        compile_error: str,
        func_name: str,
    ) -> Optional[str]:
        """Ask the LLM to fix the previous reproducer based on the GCC
        compile error. Returns the corrected C source, or None if the
        LLM declined / errored.

        Delegates to ``DynamicReproAgent`` (C2 step 9, commit 68c815d).
        The agent owns the prompt template, the response parser, and
        the routing role (``dynamic_repro`` — previously this call
        piggybacked on ``role="realism"`` which conflated two distinct
        LLM tasks under a single env-var override). The cex_validator's
        downstream ``_reproducer_uses_public_api`` gate still re-runs
        on whatever we return; UNREPRODUCIBLE marker pass-through is
        preserved by the agent's parse path.
        """
        if self._llm is None:
            return None
        from bmc_agent.agents.dynamic_repro import DynamicReproAgent
        agent = DynamicReproAgent(config=self.config, llm=self._llm)
        result = agent.run(
            previous_reproducer=previous_reproducer,
            compile_error=compile_error,
            func_name=func_name,
        )
        if not result.ok:
            if result.error:
                logger.warning(
                    "DynamicReproAgent reproducer regeneration failed for "
                    "'%s': %s",
                    func_name, result.error,
                )
            return None
        return result.output

    def _compile(
        self, harness_src: str, cc: str, extra_flags: "list[str] | None" = None
    ) -> "tuple[str | None, str | None]":
        """Write harness to a temp file and compile it.  Returns (binary_path, error)."""
        with tempfile.NamedTemporaryFile(
            suffix=".c", delete=False, mode="w", encoding="utf-8"
        ) as src_f:
            src_f.write(harness_src)
            src_path = src_f.name

        with tempfile.NamedTemporaryFile(suffix="", delete=False) as bin_f:
            bin_path = bin_f.name

        cmd = [cc, "-g", "-fno-builtin", "-w", "-o", bin_path, src_path]
        # Propagate the configured -I paths so dynamic harnesses can resolve
        # project-internal headers (e.g. libxml.h, openssl/foo.h). Without this,
        # any harness that #includes the source file via real-libc mode fails
        # to compile because the GCC frontend can't find the project headers.
        #
        # BUT: freestanding targets (e.g. VibeOS kernel/libc) ship their own
        # stub <signal.h>/<stdio.h>/<stdlib.h> that, on an -I dir, SHADOW the
        # real system headers. Those stubs have no sig_atomic_t (compile fails)
        # and a no-op signal()/printf() (the SIGSEGV oracle never installs or
        # prints) — defeating dynamic validation. For the hosted GCC harness we
        # need the REAL libc, so skip any include dir that ships stub standard
        # headers. Soundness is unaffected: this is the runtime replay compile,
        # not the CBMC verification.
        import os as _os
        def _is_stub_libc_dir(d: str) -> bool:
            return any(_os.path.exists(_os.path.join(d, h))
                       for h in ("signal.h", "stdio.h", "stdlib.h"))
        include_dirs = getattr(self.config, "include_dirs", None) or []
        for d in include_dirs:
            if _is_stub_libc_dir(str(d)):
                logger.debug("Dynamic compile: skipping stub-libc include dir %s "
                             "(would shadow real <signal.h>/<stdio.h>)", d)
                continue
            cmd += ["-I", str(d)]
        defines = getattr(self.config, "cbmc_defines", None) or []
        for d in defines:
            cmd += ["-D", str(d)]
        if extra_flags:
            cmd.extend(extra_flags)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            _unlink(src_path)
            _unlink(bin_path)
            return None, "compilation timed out"
        except Exception as exc:
            _unlink(src_path)
            _unlink(bin_path)
            return None, str(exc)
        finally:
            _unlink(src_path)

        if proc.returncode != 0:
            _unlink(bin_path)
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            logger.debug("Dynamic harness compile error for: %s", err[:200])
            return None, err

        return bin_path, None

    def _run(self, binary_path: str) -> DynamicValidationResult:
        """Execute the compiled harness and parse its stdout."""
        try:
            proc = subprocess.run(
                [binary_path],
                capture_output=True,
                text=True,
                timeout=self.config.dynamic_validation_timeout,
            )
        except subprocess.TimeoutExpired:
            return DynamicValidationResult(
                outcome=DynamicOutcome.INCONCLUSIVE,
                run_error="execution timed out",
                reasoning=(
                    f"Dynamic harness timed out after "
                    f"{self.config.dynamic_validation_timeout}s."
                ),
            )
        except Exception as exc:
            return DynamicValidationResult(
                outcome=DynamicOutcome.INCONCLUSIVE,
                run_error=str(exc),
                reasoning=f"Dynamic harness execution raised: {exc}",
            )

        stdout = proc.stdout or ""
        # Step A — observe the fut_called checkpoint marker as it's printed
        # to stdout BEFORE the FUT call. If the line "DYNAMIC:CHECKPOINT" is
        # ever required as a separate marker, this scan supports it; for
        # the in-process signal-handler path we extract the fut_called=N
        # token from the CONFIRMED line directly.
        for line in stdout.splitlines():
            if line.startswith("DYNAMIC:CONFIRMED"):
                sig_name = None
                if "signal=" in line:
                    # signal=<NAME> may be followed by additional tokens
                    tail = line.split("signal=", 1)[1]
                    sig_name = tail.split()[0].strip() if tail.split() else tail.strip()
                # Parse fut_called=0/1 — emitted by the Step A
                # instrumented signal handler. Absent on older harnesses,
                # in which case we default to "unknown".
                fault_site: Optional[str] = "unknown"
                if "fut_called=" in line:
                    flag = line.split("fut_called=", 1)[1].split()[0].strip()
                    if flag == "1":
                        fault_site = "in_fut"
                    elif flag == "0":
                        fault_site = "in_setup"
                outcome = DynamicOutcome.CONFIRMED
                reasoning = f"Dynamic harness confirmed fault: {line.strip()}"
                # Step A reclassification: signal fired in harness setup
                # (the FUT was never reached) → not a real-bug-shaped
                # signal. Reclassify as INCONCLUSIVE with a tagged reason.
                # Feature-flagged via the BMC_AGENT_DYNVAL_STRICT_FAULT_SITE
                # env var (default: "1" — on, since the cost is negligible
                # and the FP-reduction is direct).
                strict = os.environ.get(
                    "BMC_AGENT_DYNVAL_STRICT_FAULT_SITE", "1"
                ).lower() not in ("0", "false", "off", "")
                if strict and fault_site == "in_setup":
                    outcome = DynamicOutcome.INCONCLUSIVE
                    reasoning = (
                        f"Signal {sig_name} fired in harness setup before the "
                        f"function under test was called (fut_called=0). "
                        f"This is a harness-artifact signal, not a real-bug "
                        f"signal — reclassified from CONFIRMED to INCONCLUSIVE. "
                        f"Raw line: {line.strip()}"
                    )
                return DynamicValidationResult(
                    outcome=outcome,
                    signal_name=sig_name,
                    reasoning=reasoning,
                    fault_site=fault_site,
                )
            if "DYNAMIC:NOT_TRIGGERED" in line:
                return DynamicValidationResult(
                    outcome=DynamicOutcome.NOT_TRIGGERED,
                    reasoning="Dynamic harness ran to completion without triggering a fault.",
                )

        # On Linux/macOS, a process killed by signal N exits with returncode = -N
        # in Python subprocess.  Detect this as a confirmed fault even when the
        # in-process signal handler did not fire (e.g. bare-metal signal() stub).
        # When this branch fires, we don't know the fault-site value (the
        # handler that prints fut_called was bypassed); record as "unknown".
        _sig_names = {-11: "SIGSEGV", -6: "SIGABRT", -8: "SIGFPE", -4: "SIGILL"}
        if proc.returncode in _sig_names:
            sig = _sig_names[proc.returncode]
            return DynamicValidationResult(
                outcome=DynamicOutcome.CONFIRMED,
                signal_name=sig,
                fault_site="unknown",
                reasoning=(
                    f"Process killed by {sig} (exit code {proc.returncode}); "
                    "fault confirmed at runtime. (fault_site unknown — in-process "
                    "signal handler did not run, so the Step A checkpoint marker "
                    "was not emitted.)"
                ),
            )

        # Other non-zero exit with no DYNAMIC: line
        if proc.returncode != 0:
            return DynamicValidationResult(
                outcome=DynamicOutcome.INCONCLUSIVE,
                run_error=f"exit code {proc.returncode}; stdout={stdout[:200]}",
                reasoning=(
                    f"Dynamic harness exited with code {proc.returncode} but "
                    "produced no DYNAMIC: output line."
                ),
            )

        return DynamicValidationResult(
            outcome=DynamicOutcome.INCONCLUSIVE,
            reasoning="Dynamic harness produced no recognizable output.",
        )

    def refine_and_revalidate(
        self,
        winning_harness: "str | None",
        sibling_sources: "dict[str, str]",
        referenced_idents: "set[str] | None" = None,
    ):
        """Phase 1 (realism-enforcement): re-run the dynamic harness with
        boot-init-trusted EXTERN globals MATERIALIZED, to tell a NULL-default
        harness artifact apart from a real fault. See ``harness_refiner``.

        Returns ``(DynamicValidationResult, [TrustedGlobal])`` for the refined
        run, or ``None`` when refinement does not apply (harness links cleanly
        already, no trusted externs to materialize, or the refined harness
        won't build). Pure analysis: never mutates the original finding.

        Soundness: the materialization is ``calloc(1, sizeof(*g))`` (smallest
        non-NULL object) — a real out-of-bounds access still faults on the
        1-element buffer, so a genuine bug re-crashes and the caller keeps it.
        """
        cc = self.config.dynamic_cc_path
        if not winning_harness or not shutil.which(cc):
            return None
        from bmc_agent import harness_refiner as _hr

        # 1. Clean compile (no unresolved-symbols relaxation) to surface the
        #    externs the unit harness left to default-to-0/NULL.
        binp, err = self._compile(winning_harness, cc)
        if binp is not None:
            _unlink(binp)
            return None  # already links clean -> not the unresolved-extern artifact
        plan = _hr.plan_refinement(err, sibling_sources, referenced_idents)
        if not plan:
            return None  # nothing boot-init-trusted to materialize -> keep finding

        block = _hr.synthesize_materialization(plan)
        refined = _hr.inject_materialization(winning_harness, block)
        binp2, err2 = self._compile(refined, cc)
        if binp2 is None:
            # Other (untrusted) externs may still be undefined — let THOSE
            # default to 0 so the run proceeds; the trusted globals we model are
            # already materialized, which is the only change that matters.
            binp2, _err3 = self._compile(
                refined, cc, extra_flags=["-Wl,--unresolved-symbols=ignore-all"],
            )
        if binp2 is None:
            return None
        try:
            res = self._run(binp2)
        finally:
            _unlink(binp2)
        res.harness_source = refined
        res.harness_kind = "unit_refined"
        return res, plan


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _unlink(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _looks_like_c_code(text: str) -> bool:
    """Return True if text looks like compilable C rather than pseudocode/stub."""
    if not text or len(text) < 20:
        return False
    # Must contain a main function and at least one C statement
    return "main" in text and ("{" in text and "}" in text)


_LINK_ERR_HINTS = (
    "undefined reference",      # gcc / binutils ld
    "undefined symbol",          # musl-libc loader
    "could not find library",
    "cannot find -l",
    "library not found for",     # macOS-style
)


def _is_link_only_error(err: str) -> bool:
    """Heuristic: is this compile error purely a linker failure (i.e.,
    the source compiled fine but symbols couldn't resolve)?

    LLM regeneration can't fix linker errors — the source is already
    correct, the build is just missing ``-l<libname>``. Skipping the
    LLM round-trip in that case saves a useless token spend; the
    ``_detect_link_flags`` helper is what addresses link errors.

    True iff every hint line in the error matches a known link pattern
    AND no obvious C compile error (file/line ``: error:`` style)
    appears earlier. Conservative: when in doubt, return False so the
    LLM gets a shot.
    """
    if not err:
        return False
    lines = [L.strip() for L in err.splitlines() if L.strip()]
    if not lines:
        return False
    has_compile_error_marker = any(
        re.search(r":\d+:\d+:\s*(error|fatal error):", L) for L in lines
    )
    if has_compile_error_marker:
        return False
    has_link_hint = any(
        any(hint in L for hint in _LINK_ERR_HINTS)
        for L in lines
    )
    return has_link_hint


def _wrap_reproducer_with_signal_handlers(reproducer_code: str) -> str:
    """
    Wrap an LLM-generated C reproducer with AMC signal handlers.

    The reproducer already has its own main().  We use a #define trick to rename
    it to _amc_original_main(), then our main() installs signal handlers before
    calling it so faults are caught and reported in the standard AMC format.
    """
    preamble = (
        "/* AMC Dynamic Validation Harness — system-entry reproducer */\n"
        "#include <signal.h>\n"
        "#include <stdio.h>\n"
        "#include <string.h>\n"
        "#include <stdlib.h>\n"
        "#include <stddef.h>\n"
        "#include <stdint.h>\n"
        "\n"
        "static volatile const char *_amc_signal_name = \"UNKNOWN\";\n"
        "static void _amc_handler(int sig) {\n"
        "    if (sig == 11) _amc_signal_name = \"SIGSEGV\";\n"
        "    else if (sig == 6)  _amc_signal_name = \"SIGABRT\";\n"
        "    else if (sig == 8)  _amc_signal_name = \"SIGFPE\";\n"
        "    else if (sig == 4)  _amc_signal_name = \"SIGILL\";\n"
        "    printf(\"DYNAMIC:CONFIRMED signal=%s\\n\", (const char *)_amc_signal_name);\n"
        "    fflush(stdout);\n"
        "    _Exit(1);\n"
        "}\n"
        "\n"
        "/* Rename main() in reproducer so we can wrap it */\n"
        "#define main _amc_reproducer_main\n"
    )
    suffix = (
        "\n#undef main\n"
        "\n"
        "int main(void) {\n"
        "    signal(11, _amc_handler);  /* SIGSEGV */\n"
        "    signal(6,  _amc_handler);  /* SIGABRT */\n"
        "    signal(8,  _amc_handler);  /* SIGFPE  */\n"
        "    signal(4,  _amc_handler);  /* SIGILL  */\n"
        "    _amc_reproducer_main();\n"
        "    puts(\"DYNAMIC:NOT_TRIGGERED\");\n"
        "    return 0;\n"
        "}\n"
    )
    return preamble + reproducer_code + suffix
