"""
CBMC subprocess wrapper for BMC-Agent.

Runs CBMC with --json-ui output and parses the result into structured
data classes.
"""

from __future__ import annotations

import json
import re
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
    undefined_shift_check: bool = False,
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

    # SV-COMP unwind floor (env SVCOMP_UNWIND, default 64 when SVCOMP_PROP set):
    # whole-program harnesses call builtin loops (strlen/memcmp/...) that the
    # per-function bmc-config agent under-sizes (e.g. unwind=12), stalling on a
    # strlen.unwind artifact before reaching reach_error. Clamp up here so EVERY
    # CBMC call path (config agent, flag-selection, harness-repair) is covered.
    import os as _os0
    if _os0.environ.get("SVCOMP_PROP"):
        _svc_floor = int(_os0.environ.get("SVCOMP_UNWIND", "64"))
        if (unwind or 0) < _svc_floor:
            unwind = _svc_floor
        # SV-COMP timeout floor: the per-fn bmc-config agent caps cbmc at
        # ~120-240s, but the SV-COMP budget is 900s; let CBMC use it so heavy
        # tasks (hash tables) get decided instead of auto-timing-out.
        _svc_to = int(_os0.environ.get("SVCOMP_TIMEOUT", "800"))
        if (timeout or 0) < _svc_to:
            timeout = _svc_to
    # BUG-FINDING unwind CAP (env BMC_UNWIND_CAP): SVCOMP_UNWIND above is only a
    # FLOOR (it raises unwind, never lowers it), so the bmc-config agent's pick
    # (often 8) wins and heavy ldv dispatch loops still explode (10^k paths). For
    # the bounded bug-finding pass we want to CAP the dispatch depth: any
    # reach_error found within the cap is a REAL bug (sound); a clean run is NOT
    # a proof (bounded) and must be scored unknown. Applied last so it overrides
    # both the floor and the agent's pick, at the single cbmc call gate.
    _cap = _os0.environ.get("BMC_UNWIND_CAP")
    if _cap:
        try:
            if (unwind or 0) > int(_cap):
                unwind = int(_cap)
        except ValueError:
            pass

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
    ]
    import os as _osbh
    if (_osbh.environ.get("BMC_CONE_SLICE") or _osbh.environ.get("BMC_BUGHUNT")) and _osbh.environ.get("SVCOMP_PROP"):
        # BUG-FINDING pass (cone-slice mode): --partial-loops cuts loops at the
        # bound and we DROP --unwinding-assertions. Neither is needed to REACH
        # reach_error; both exist only to PROVE safety. Dropping them removes the
        # per-loop unwinding-assertion VCCs and lets bounded loop bodies stop at
        # the bound -> far smaller SAT formula, so heavy ldv drivers finish at
        # k=8 instead of timing out. SOUND for bug-finding: any reach_error found
        # is a real (downstream feasibility-checked) bug. A CLEAN run here is NOT
        # a proof (loops were cut) and MUST be scored unknown, never "true".
        cmd.append("--partial-loops")
        # MEMORY: large inlined kernel cones blow up the eager SAT encoding
        # ("SAT checker ran out of memory" -> cbmc exit 6). --refine uses lazy/
        # iterative SAT refinement (sound + complete) that encodes only what is
        # needed, and --slice-formula drops VCCs irrelevant to the property.
        # Together they let heavy USB/net driver cones (e.g. p54usb, ~7.6k LOC
        # inlined) REACH reach_error within the memory cap instead of OOMing.
        # Verified: p54usb VERIFICATION FAILED reach_error.assertion.1 @ uw4
        # with --refine vs OOM without. Toggle off with BMC_REFINE=0.
        if _osbh.environ.get("BMC_REFINE", "1") != "0":
            cmd.append("--refine")
            cmd.append("--slice-formula")
    else:
        cmd.append("--unwinding-assertions")
    import os as _os
    if _os.environ.get("SVCOMP_ARCH", "ILP32").upper() == "ILP32":
        cmd.append("--32")
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
    if undefined_shift_check:
        cmd.append("--undefined-shift-check")
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
        # Probably a parse / compile error. Prefer stderr, but if stderr is
        # empty CBMC may have routed the parse error to stdout (some versions
        # do this on --json-ui mode) — include a snippet of stdout so the
        # error log isn't just "cbmc exited with code N" with no context.
        err_msg = stderr.strip()
        if not err_msg:
            stdout_snippet = (raw or "").strip()[:1500]
            if stdout_snippet:
                err_msg = f"cbmc exited with code {returncode}; stdout: {stdout_snippet}"
            else:
                err_msg = f"cbmc exited with code {returncode} (no stderr or stdout)"
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

    # Vacuity guard (SOUNDNESS): a "SUCCESSFUL" with ZERO verification conditions
    # means CBMC checked NOTHING — typically the function under test had no body
    # (declared extern / not linked), so its code was never analysed. Reporting
    # that as verified=True is a soundness false-negative (it silently passes a
    # function whose bugs were never examined). Demote any such pass to a hard
    # error so the pipeline treats it as INVALID/unresolved, never as safe.
    if verified and not counterexamples:
        m = re.search(r"Generated\s+(\d+)\s+VCC", raw)
        if m and int(m.group(1)) == 0:
            # 0 VCCs is vacuous ONLY if NO properties were actually enumerated.
            # CBMC constant-folds fully-concrete assertions to `true` (0 VCCs reach
            # the solver) yet still REPORTS them as checked results — that is a
            # genuine proof, not an un-analysed body. Demote only when CBMC reported
            # zero property results (extern/unlinked body = nothing to check).
            n_props = 0
            try:
                for msg in json.loads(raw):
                    if isinstance(msg, dict) and isinstance(msg.get("result"), list):
                        n_props += len(msg["result"])
            except (json.JSONDecodeError, TypeError):
                mm = re.search(r"\bof\s+(\d+)\s+failed", raw)
                n_props = int(mm.group(1)) if mm else 0
            if n_props == 0:
                return CBMCResult(
                    verified=False,
                    raw_output=_cap_raw(raw),
                    error="vacuous verification: CBMC generated 0 VCCs and reported 0 "
                          "properties — the function body was not analysed (likely "
                          "declared extern / not linked). A safe verdict here would be "
                          "unsound; treating as INVALID.",
                )

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
