"""
Kani (Rust bounded model checker) subprocess wrapper for BMC-Agent.

Mirrors :mod:`bmc_agent.cbmc`: runs Kani on a Rust harness file, parses its
output, and returns a :class:`bmc_agent.cbmc.CBMCResult`.  Reusing the
existing result type means the rest of the pipeline (CEx classifier,
artifact store, bug reporter) is backend-agnostic.

Kani is a separate tool that itself uses CBMC under the hood.  Its output
format is similar to CBMC's but not identical, so the parser here looks
for both Kani-specific markers (``VERIFICATION:- SUCCESSFUL``,
``Failed Checks:``) and CBMC-style ones.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from bmc_agent.cbmc import CBMCResult, Counterexample


# Kani's standalone CLI invocation form:
#   kani <file.rs> [--harness <name>] [--default-unwind N]
# When the path is a Cargo project we'd use `cargo kani` instead, but the
# pipeline drops single .rs files into a working dir per function so single-file
# mode is the natural fit.
#
# We use Kani's default ("regular") output format, not "old": the old format
# emits per-assertion reachability_check rows that report FAILURE when the
# assertion *was reached* (i.e. healthy proofs), and its per-harness summary
# prints "VERIFICATION FAILED" for any harness containing such a row. That
# inverts the verdict and is essentially unparseable. Regular format suppresses
# reachability checks and prints "VERIFICATION:- SUCCESSFUL/FAILED" cleanly.


def run_kani(
    harness_path: str | Path,
    harness_name: str | None = None,
    unwind: int = 4,
    timeout: int = 120,
    kani_path: str = "kani",
) -> CBMCResult:
    """
    Run Kani on *harness_path* and return a structured result.

    Parameters
    ----------
    harness_path:
        Path to a self-contained Rust harness file containing one or more
        ``#[kani::proof]`` functions.
    harness_name:
        Optional name of a single ``#[kani::proof]`` function to verify.
        When omitted, Kani verifies every proof it finds.
    unwind:
        Default loop unwinding bound (``--default-unwind N``).
    timeout:
        Maximum wall-clock seconds before the process is killed.
    kani_path:
        Path / name of the Kani executable.

    Returns
    -------
    CBMCResult
        On Kani-not-found: ``CBMCResult(verified=False, error="kani not found")``.
        On timeout:        ``CBMCResult(verified=False, error="kani timed out")``.
        Otherwise a parsed verdict with any counterexamples extracted from
        the textual output.
    """
    harness_path = Path(harness_path)

    if not shutil.which(kani_path):
        return CBMCResult(verified=False, error="kani not found")

    cmd: list[str] = [
        kani_path,
        str(harness_path),
        "--default-unwind",
        str(unwind),
    ]
    if harness_name:
        cmd += ["--harness", harness_name]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CBMCResult(verified=False, error=f"kani timed out after {timeout}s")
    except FileNotFoundError:
        return CBMCResult(verified=False, error="kani not found")
    except OSError as exc:
        return CBMCResult(verified=False, error=f"kani OS error: {exc}")

    raw = proc.stdout or ""
    stderr = proc.stderr or ""
    return _parse_kani_output(raw, stderr, proc.returncode)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


# Kani's regular (default) output format. The per-harness verdict is a single
# "VERIFICATION:- SUCCESSFUL" or "VERIFICATION:- FAILED" line; each property is
# a multi-line "Check N: <id> / Status: ... / Description: ..." block.
_VERDICT_SUCCESS_RE = re.compile(r"^VERIFICATION:-\s*SUCCESSFUL", re.MULTILINE)
_VERDICT_FAILED_RE = re.compile(r"^VERIFICATION:-\s*FAILED", re.MULTILINE)

# Per-check failure block in regular format. The `Check N:` header line is
# followed by indented "- Status:" and "- Description:" lines. Status FAILURE
# is the genuine failure signal — reachability_check rows are not present in
# regular output.
_REGULAR_CHECK_FAILURE_RE = re.compile(
    r"^Check\s+\d+:\s*(?P<prop>\S+)\s*\n"
    r"\s*-\s*Status:\s*FAILURE\s*\n"
    r"\s*-\s*Description:\s*\"?(?P<desc>[^\"\n]*)\"?",
    re.MULTILINE,
)

# Legacy/old-format per-property failure row, kept for backward compatibility
# with synthetic test fixtures and other Kani output variants.
_LEGACY_PROPERTY_FAILURE_RE = re.compile(
    r"^\s*\[([^\]]+)\]\s*(.*?):\s*(?:FAILURE|FAILED)\s*$",
    re.MULTILINE,
)

# Kani sometimes summarises failures as "Failed Checks: <description>".
_FAILED_CHECKS_RE = re.compile(r"^Failed Checks:\s*(.+)$", re.MULTILINE)


def _parse_kani_output(raw: str, stderr: str, returncode: int) -> CBMCResult:
    """Parse Kani text output into a CBMCResult.

    Kani's exit codes match CBMC's convention: 0 = verified, non-zero = failed
    or error.  When the run produced no verdict marker at all we treat it as
    an error.
    """
    has_success = bool(_VERDICT_SUCCESS_RE.search(raw))
    has_failure = bool(_VERDICT_FAILED_RE.search(raw))

    if not has_success and not has_failure:
        # No verdict at all → treat as error and surface stderr.
        err = (stderr or "").strip() or f"kani exited with code {returncode}"
        return CBMCResult(verified=False, raw_output=raw, error=err)

    verified = has_success and not has_failure
    counterexamples = _extract_counterexamples(raw)
    return CBMCResult(verified=verified, counterexamples=counterexamples, raw_output=raw)


def _extract_counterexamples(raw: str) -> list[Counterexample]:
    """Pull failing properties out of Kani's text output.

    Tries the regular-format multi-line ``Check N:`` blocks first, then falls
    back to the legacy ``[id] desc: FAILURE`` row form, then to a single
    ``Failed Checks:`` summary line. Reachability_check rows (only present in
    --output-format old) report FAILURE when the assertion *was reached* and
    so must not be treated as genuine failures; the regex below targets only
    the regular-format "Status: FAILURE" lines.
    """
    cexes: list[Counterexample] = []
    seen: set[str] = set()

    for match in _REGULAR_CHECK_FAILURE_RE.finditer(raw):
        prop_id = match.group("prop").strip()
        desc = match.group("desc").strip()
        if prop_id in seen:
            continue
        seen.add(prop_id)
        trace = [f"property {prop_id}: {desc}"] if desc else []
        cexes.append(
            Counterexample(
                failing_property=prop_id,
                variable_assignments={},
                trace=trace,
            )
        )

    if not cexes:
        for match in _LEGACY_PROPERTY_FAILURE_RE.finditer(raw):
            prop_id = match.group(1).strip()
            # Skip reachability_check pseudo-failures in old-format output.
            if ".reachability_check." in prop_id:
                continue
            desc = match.group(2).strip()
            if prop_id in seen:
                continue
            seen.add(prop_id)
            trace = [f"property {prop_id}: {desc}"] if desc else []
            cexes.append(
                Counterexample(
                    failing_property=prop_id,
                    variable_assignments={},
                    trace=trace,
                )
            )

    if not cexes:
        m = _FAILED_CHECKS_RE.search(raw)
        if m:
            cexes.append(
                Counterexample(
                    failing_property="failed_checks",
                    variable_assignments={},
                    trace=[m.group(1).strip()],
                )
            )

    return cexes
