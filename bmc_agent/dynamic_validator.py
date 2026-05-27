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

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "signal_name": self.signal_name,
            "compile_error": self.compile_error,
            "run_error": self.run_error,
            "reasoning": self.reasoning,
            "harness_source": self.harness_source,
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
        logger.info(
            "Dynamic validation for '%s': %s%s",
            entry_func.name,
            result.outcome.value,
            f" signal={result.signal_name}" if result.signal_name else "",
        )
        return result

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
        include_dirs = getattr(self.config, "include_dirs", None) or []
        for d in include_dirs:
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
        for line in stdout.splitlines():
            if line.startswith("DYNAMIC:CONFIRMED"):
                sig_name = None
                if "signal=" in line:
                    sig_name = line.split("signal=", 1)[1].strip()
                return DynamicValidationResult(
                    outcome=DynamicOutcome.CONFIRMED,
                    signal_name=sig_name,
                    reasoning=f"Dynamic harness confirmed fault: {line.strip()}",
                )
            if "DYNAMIC:NOT_TRIGGERED" in line:
                return DynamicValidationResult(
                    outcome=DynamicOutcome.NOT_TRIGGERED,
                    reasoning="Dynamic harness ran to completion without triggering a fault.",
                )

        # On Linux/macOS, a process killed by signal N exits with returncode = -N
        # in Python subprocess.  Detect this as a confirmed fault even when the
        # in-process signal handler did not fire (e.g. bare-metal signal() stub).
        _sig_names = {-11: "SIGSEGV", -6: "SIGABRT", -8: "SIGFPE", -4: "SIGILL"}
        if proc.returncode in _sig_names:
            sig = _sig_names[proc.returncode]
            return DynamicValidationResult(
                outcome=DynamicOutcome.CONFIRMED,
                signal_name=sig,
                reasoning=(
                    f"Process killed by {sig} (exit code {proc.returncode}); "
                    "fault confirmed at runtime."
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
