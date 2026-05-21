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
    # CBMC's textual description for the failing property
    # (e.g. "dereference failure: NULL pointer").
    description: str = ""
    # Source location of the failing property, populated from CBMC's
    # JSON output. Keys typically include "file", "line", "function".
    # Empty when CBMC didn't emit a sourceLocation for the result item.
    failure_location: dict[str, str] = field(default_factory=dict)


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
    defines: list[str] | None = None,
    unsigned_overflow_check: bool = False,
    signed_overflow_check: bool = False,
    conversion_check: bool = False,
    pointer_overflow_check: bool = False,
    pointer_check: bool = False,
    bounds_check: bool = False,
    div_by_zero_check: bool = False,
    object_bits: int | None = None,
    auto_scale_object_bits: bool = True,
    function: str | None = None,
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
    # If the harness function isn't `main`, tell CBMC which one to verify.
    # Used by real-libc mode when the included source already defines its
    # own `main()` (e.g. llm.c's train_gpt2.c is a training program, not
    # a library).
    if function and function != "main":
        cmd += ["--function", function]
    if unsigned_overflow_check:
        cmd.append("--unsigned-overflow-check")
    if signed_overflow_check:
        cmd.append("--signed-overflow-check")
    if conversion_check:
        cmd.append("--conversion-check")
    if pointer_overflow_check:
        cmd.append("--pointer-overflow-check")
    if pointer_check:
        cmd.append("--pointer-check")
    if bounds_check:
        cmd.append("--bounds-check")
    if div_by_zero_check:
        cmd.append("--div-by-zero-check")
    if object_bits is not None:
        cmd += ["--object-bits", str(object_bits)]
    for d in (include_dirs or []):
        cmd += ["-I", d]
    for d in (defines or []):
        cmd += ["-D", d]

    # 3. Execute (with auto-scale retry on "too many addressed objects")
    # CBMC's default `--object-bits 8` allows 2^8=256 distinct objects.
    # State-heavy parser files (libxml2 HTMLparser.c, etc.) blow past
    # this; retry with progressively higher object-bits up to 16
    # (= 2^16 = 65k objects, which is the practical ceiling).
    retry_bits_ladder = [12, 16] if auto_scale_object_bits and object_bits is None else []
    current_cmd = cmd
    raw = ""
    stderr = ""
    returncode = -1
    for tier in [None] + retry_bits_ladder:
        if tier is not None:
            # Drop any previous --object-bits and append the new tier.
            stripped: list[str] = []
            skip_next = False
            for a in current_cmd:
                if skip_next:
                    skip_next = False
                    continue
                if a == "--object-bits":
                    skip_next = True
                    continue
                stripped.append(a)
            current_cmd = stripped + ["--object-bits", str(tier)]
        try:
            proc = subprocess.run(
                current_cmd,
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
        returncode = proc.returncode
        if not _is_too_many_objects(raw, stderr):
            break
        # Retry with a larger object-bits in the ladder; if exhausted,
        # fall through with the last error result.

    # 4. Parse output
    return _parse_cbmc_output(raw, stderr, returncode)


def _is_too_many_objects(raw: str, stderr: str) -> bool:
    """Detect CBMC's 'too many addressed objects' error."""
    needle = "too many addressed objects"
    return needle in (raw or "") or needle in (stderr or "")


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _parse_cbmc_output(raw: str, stderr: str, returncode: int) -> CBMCResult:
    """Parse CBMC JSON output into a CBMCResult."""

    # CBMC exit codes: 0 = verified, 10 = failed (property violated), other = error
    if returncode not in (0, 10):
        # Probably a parse / compile error
        err_msg = stderr.strip() or f"cbmc exited with code {returncode}"
        return CBMCResult(verified=False, raw_output=_cap_raw(raw), error=err_msg)

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
        raw_output=_cap_raw(raw),
    )


# Cap raw CBMC output before stashing on CBMCResult. CBMC's --json-ui dumps
# the entire trace including full struct/array snapshots per step. For a
# kernel TU (preprocessed ~3MB, struct neuron_device with 28 fields), the
# JSON output can hit 9+ GB and explode bug_report.json. We only keep
# raw_output for diagnostic context (verification status, top-of-trace
# preview), so cap to 64KB head + 4KB tail with an elision marker.
_RAW_OUTPUT_HEAD = 64 * 1024
_RAW_OUTPUT_TAIL = 4 * 1024


def _cap_raw(raw: str) -> str:
    if raw is None:
        return ""
    if len(raw) <= _RAW_OUTPUT_HEAD + _RAW_OUTPUT_TAIL + 128:
        return raw
    head = raw[:_RAW_OUTPUT_HEAD]
    tail = raw[-_RAW_OUTPUT_TAIL:]
    omitted = len(raw) - _RAW_OUTPUT_HEAD - _RAW_OUTPUT_TAIL
    return f"{head}\n/* ... {omitted} bytes elided ... */\n{tail}"


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

            # CBMC's top-level description + source location for the failure.
            description = str(item.get("description", "")).strip()
            top_loc = item.get("sourceLocation") or {}
            if not isinstance(top_loc, dict):
                top_loc = {}
            failure_location: dict[str, str] = {}
            if top_loc:
                for k in ("file", "line", "function", "column"):
                    v = top_loc.get(k)
                    if v is not None:
                        failure_location[k] = str(v)

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
                    if isinstance(rhs_value, dict):
                        # Scalar leaf values have a ``data`` field; use it
                        # verbatim. Struct/array values have ``members`` /
                        # ``elements`` and no ``data`` — for those, emit a
                        # one-line summary rather than ``str(dict)`` of the
                        # nested state. A full struct stringification for a
                        # 28-field kernel ``neuron_device`` can hit megabytes
                        # per assignment (observed: ts_nq_destroy produced a
                        # 264MB bug_report.json because every assignment was
                        # a stringified struct). That blob then poisoned the
                        # realism / reproducer prompts (OpenRouter rejects
                        # requests >8MB).
                        if "data" in rhs_value:
                            rhs_str = str(rhs_value["data"])
                        elif "members" in rhs_value:
                            n = len(rhs_value.get("members") or [])
                            rhs_str = f"<struct: {n} members>"
                        elif "elements" in rhs_value:
                            n = len(rhs_value.get("elements") or [])
                            rhs_str = f"<array: {n} elements>"
                        else:
                            # Last-ditch: cap stringified form so a missing
                            # key doesn't reintroduce the blow-up.
                            rhs_str = str(rhs_value)[:512]
                    else:
                        rhs_str = str(rhs_value)[:512]
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

            # If the result item didn't carry a sourceLocation, fall back to
            # the last trace step that did — usually the failing assert.
            if not failure_location:
                for trace_entry in reversed(item.get("trace", [])):
                    if not isinstance(trace_entry, dict):
                        continue
                    loc = trace_entry.get("sourceLocation") or {}
                    if isinstance(loc, dict) and loc.get("line"):
                        for k in ("file", "line", "function", "column"):
                            v = loc.get(k)
                            if v is not None:
                                failure_location[k] = str(v)
                        break

            cexes.append(
                Counterexample(
                    failing_property=prop_id,
                    variable_assignments=var_assignments,
                    trace=trace_lines,
                    description=description,
                    failure_location=failure_location,
                )
            )

    return cexes
