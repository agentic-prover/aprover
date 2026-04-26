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

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from amc.config import Config
from amc.logger import get_logger
from amc.parser import FunctionInfo, ParsedCFile

if TYPE_CHECKING:
    from amc.cbmc import Counterexample
    from amc.harness_generator import HarnessGenerator

logger = get_logger("dynamic_validator")


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

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "signal_name": self.signal_name,
            "compile_error": self.compile_error,
            "run_error": self.run_error,
            "reasoning": self.reasoning,
        }


# ---------------------------------------------------------------------------
# DynamicValidator
# ---------------------------------------------------------------------------


class DynamicValidator:
    """Compiles and executes dynamic harnesses to confirm BMC counterexamples."""

    def __init__(self, config: Config, harness_gen: "HarnessGenerator") -> None:
        self.config = config
        self.harness_gen = harness_gen

    def validate(
        self,
        entry_func: FunctionInfo,
        counterexample: "Counterexample",
        parsed_file: ParsedCFile,
        all_funcs: Optional[dict] = None,
        all_specs: Optional[dict] = None,
        caller_path: Optional[list[str]] = None,
    ) -> DynamicValidationResult:
        """
        Attempt to confirm the counterexample by compiling and running a dynamic harness.

        Strategy:
        1. Generate a harness with global state injection (with_globals=True).
        2. Compile it.  If compilation fails, retry without globals (with_globals=False).
        3. Run the compiled binary and parse stdout for DYNAMIC:CONFIRMED / NOT_TRIGGERED.
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

        # --- Attempt 1: with global state injection ---
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
