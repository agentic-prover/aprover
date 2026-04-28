"""
CBMC subprocess wrapper for BMC-Agent.

Runs CBMC with --json-ui output and parses the result into structured
data classes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Counterexample:
    """A single counterexample from a CBMC verification failure."""

    failing_property: str
    variable_assignments: dict[str, str] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)


@dataclass
class CBMCResult:
    """
    Result of a CBMC verification run.

    Attributes:
        verified:         True iff CBMC reported VERIFICATION SUCCESSFUL.
        counterexamples:  List of parsed counterexamples (empty on success).
        raw_output:       Raw CBMC stdout (JSON string or plain text).
        error:            Non-None if CBMC could not be run or timed out.
    """

    verified: bool
    counterexamples: list[Counterexample] = field(default_factory=list)
    raw_output: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_cbmc(
    harness_path: str | Path,
    unwind: int = 4,
    timeout: int = 120,
    cbmc_path: str = "cbmc",
    include_dirs: list[str] | None = None,
) -> CBMCResult:
    """
    Run CBMC on *harness_path* and return a structured result.

    Parameters
    ----------
    harness_path:
        Path to the C harness file.
    unwind:
        Loop unwinding bound (``--unwind N``).
    timeout:
        Maximum wall-clock seconds before the process is killed.
    cbmc_path:
        Path / name of the CBMC executable.

    Returns
    -------
    CBMCResult
        On CBMC-not-found: ``CBMCResult(verified=False, error="cbmc not found")``.
        On timeout:        ``CBMCResult(verified=False, error="cbmc timed out")``.
    """
    harness_path = Path(harness_path)

    # 1. Locate CBMC
    if not shutil.which(cbmc_path):
        return CBMCResult(
            verified=False,
            error="cbmc not found",
        )

    # 2. Build command
    cmd: list[str] = [
        cbmc_path,
        str(harness_path),
        "--json-ui",
        f"--unwind",
        str(unwind),
        "--unwinding-assertions",
    ]
    for d in (include_dirs or []):
        cmd += ["-I", d]

    # 3. Execute
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CBMCResult(
            verified=False,
            error=f"cbmc timed out after {timeout}s",
        )
    except FileNotFoundError:
        return CBMCResult(
            verified=False,
            error="cbmc not found",
        )
    except OSError as exc:
        return CBMCResult(
            verified=False,
            error=f"cbmc OS error: {exc}",
        )

    raw = proc.stdout or ""
    stderr = proc.stderr or ""

    # 4. Parse output
    return _parse_cbmc_output(raw, stderr, proc.returncode)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _parse_cbmc_output(raw: str, stderr: str, returncode: int) -> CBMCResult:
    """Parse CBMC JSON output into a CBMCResult."""

    # CBMC exit codes: 0 = verified, 10 = failed (property violated), other = error
    if returncode not in (0, 10):
        # Probably a parse / compile error
        err_msg = stderr.strip() or f"cbmc exited with code {returncode}"
        return CBMCResult(verified=False, raw_output=raw, error=err_msg)

    verified = returncode == 0
    counterexamples: list[Counterexample] = []

    # Try JSON parse
    try:
        messages = json.loads(raw)
        counterexamples = _extract_counterexamples(messages)
    except (json.JSONDecodeError, TypeError):
        # Fall back: look for plain-text markers
        if "VERIFICATION SUCCESSFUL" in raw:
            verified = True
        elif "VERIFICATION FAILED" in raw:
            verified = False

    return CBMCResult(
        verified=verified,
        counterexamples=counterexamples,
        raw_output=raw,
    )


def _extract_counterexamples(messages: list | dict) -> list[Counterexample]:
    """Walk the CBMC JSON message list and collect counterexamples."""
    cexes: list[Counterexample] = []

    if isinstance(messages, dict):
        messages = [messages]

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        # CBMC JSON-UI emits objects with a "result" key for property checks
        results = msg.get("result", [])
        if not isinstance(results, list):
            continue

        for item in results:
            if not isinstance(item, dict):
                continue
            status = item.get("status", "")
            if status != "FAILURE":
                continue

            prop = item.get("property", {})
            prop_id = prop if isinstance(prop, str) else prop.get("id", "unknown")

            # Extract trace
            trace_lines: list[str] = []
            var_assignments: dict[str, str] = {}
            for trace_entry in item.get("trace", []):
                if not isinstance(trace_entry, dict):
                    continue
                step_type = trace_entry.get("stepType", "")
                if step_type == "assignment":
                    lhs = trace_entry.get("lhs", "")
                    rhs_value = trace_entry.get("value", {})
                    rhs_str = (
                        rhs_value.get("data", str(rhs_value))
                        if isinstance(rhs_value, dict)
                        else str(rhs_value)
                    )
                    var_assignments[lhs] = rhs_str
                    trace_lines.append(f"{lhs} = {rhs_str}")
                elif step_type == "output":
                    io_val = trace_entry.get("io-args", [])
                    trace_lines.append(f"output: {io_val}")
                else:
                    loc = trace_entry.get("sourceLocation", {})
                    line = loc.get("line", "?")
                    func = loc.get("function", "?")
                    trace_lines.append(f"{step_type} at {func}:{line}")

            cexes.append(
                Counterexample(
                    failing_property=prop_id,
                    variable_assignments=var_assignments,
                    trace=trace_lines,
                )
            )

    return cexes
