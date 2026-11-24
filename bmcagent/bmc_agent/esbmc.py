"""ESBMC 8.3 bounded-checking adapter (no induction or LLM calls).

ESBMC's report.json is a trace array, not CBMC's --json-ui protocol. Each
invocation has its own working directory so parallel checks cannot mix traces.
Only tested intrinsic/flag translations are accepted. Unsupported CBMC features
are errors, never an implicit successful verification.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from bmc_agent.cbmc import CBMCResult, Counterexample


_INTRINSICS = {
    "__CPROVER_assume": "__ESBMC_assume",
    "__CPROVER_assert": "__ESBMC_assert",
}
_C_NONCODE = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)


def _code_only(source: str) -> str:
    return _C_NONCODE.sub(lambda m: " " * len(m[0]), source)


def _omit_unused_void_externs(source: str, defines: list[str]) -> tuple[str, list[str]]:
    """Omit unused GNU void objects that ESBMC initializes as NONDET(void).

    Only standalone, simple declarations are eligible. Count names in the raw
    text so comments, strings and asm references conservatively prevent removal.
    Includes or preprocessing that can hide/splice references prevent removal.
    """
    code = _code_only(source)
    preprocessing = code + "\n" + "\n".join(defines)
    if ("##" in preprocessing or "%:%:" in preprocessing or "\\" in preprocessing
            or re.search(r"(?:#|%:)[ \t]*(?:include|include_next|import)\b", code)):
        return source, []
    pattern = re.compile(r"(?m)^[ \t]*extern[ \t]+void[ \t]+([A-Za-z_]\w*)[ \t]*;[ \t]*(?=\r?$)")
    raw = source + "\n" + "\n".join(defines)
    omitted = []
    replacements = []
    for match in pattern.finditer(source):
        name = match[1]
        if (code[match.start():match.end()] == match[0]
                and len(re.findall(r"\b" + re.escape(name) + r"\b", raw)) == 1):
            omitted.append(name)
            replacements.append(match.span())
    for start, end in reversed(replacements):
        source = source[:start] + " " * (end - start) + source[end:]
    return source, omitted


def _intrinsic_defines(source: str) -> list[str]:
    """Preserve explicitly defined benchmark helpers; reject unmapped intrinsics.

    Archived AWS harnesses already define several __CPROVER_* helpers as C
    functions. These definitions are reused verbatim. Other unknown names must
    not become opaque external calls (ESBMC can otherwise report success).
    """
    code = _code_only(source)
    names = set(re.findall(r"\b__CPROVER_\w+", code))
    defined = set(re.findall(r"\b(__CPROVER_\w+)\s*\([^;{}]*\)\s*\{", code))
    unsupported = names - _INTRINSICS.keys() - defined - {"__CPROVER_allocate"}
    if unsupported:
        raise ValueError("unsupported CBMC intrinsics: " + ", ".join(sorted(unsupported)))
    # Always define these, including for assertions in local included headers.
    defines = [f"{old}={new}" for old, new in _INTRINSICS.items()]
    if "__CPROVER_allocate" in names - defined:
        # The LDV generator requests fresh, nonzeroed, non-failing heap objects.
        # __builtin_malloc becomes ESBMC's native malloc operation, but only if
        # a source-defined malloc does not shadow it. Rename that source symbol
        # and ALL its uses together, retaining its existing benchmark semantics.
        # The nonnull constraint is local to the intrinsic, not a global malloc
        # policy. Zero-initialized/variable modes are explicitly unsupported.
        from bmc_agent.parser import parse_c_file
        if "malloc" in parse_c_file("esbmc_input.c", source_text=source).functions:
            if "__aprover_benchmark_malloc" in code:
                raise ValueError("allocation bridge symbol already defined: __aprover_benchmark_malloc")
            defines.append("malloc=__aprover_benchmark_malloc")
        defines += [
            '__CPROVER_allocate(n,z)=({ _Static_assert((z)==0, "ESBMC adapter supports only nonzeroed allocation"); '
            'void *__aprover_alloc_p = __builtin_malloc(n); '
            '__ESBMC_assume(__aprover_alloc_p != 0); __aprover_alloc_p; })',
        ]
    return defines


def _limits(unwind: int, timeout: int) -> tuple[int, int]:
    """Same experiment budget policy as run_cbmc, including the final cap."""
    if os.environ.get("SVCOMP_PROP"):
        unwind = max(unwind, int(os.environ.get("SVCOMP_UNWIND", "64")))
        timeout = max(timeout, int(os.environ.get("SVCOMP_TIMEOUT", "800")))
    cap = os.environ.get("BMC_UNWIND_CAP")
    if cap:
        try:
            unwind = min(unwind, int(cap))
        except ValueError:
            pass
    return unwind, timeout


def _counterexamples(report) -> list[Counterexample]:
    if not isinstance(report, list):
        raise ValueError("ESBMC report must be a trace array")
    result = []
    for witness in report:
        assignments = {str(name): str(value["value"])
                       for name, value in witness.get("initial_values", {}).items()
                       if isinstance(value, dict) and "value" in value}
        trace = []
        for step in witness.get("steps", []):
            loc = {k: str(step[k]) for k in ("file", "line", "function") if k in step}
            where = ":".join(loc.get(k, "") for k in ("file", "line", "function"))
            assignment = step.get("assignment")
            if assignment:
                name, value = str(assignment.get("lhs", "")), str(assignment.get("rhs", ""))
                assignments[name] = value
                trace.append(f"{where}: {name} = {value}")
            else:
                trace.append(f"{where}: {step.get('message', step.get('type', ''))}")
        for step in witness.get("steps", []):
            assertion = step.get("assertion", {})
            if step.get("type") != "violation" or not assertion.get("violated"):
                continue
            description = str(assertion.get("comment", step.get("message", "assertion")))
            loc = {k: str(step[k]) for k in ("file", "line", "function") if k in step}
            kind = _property_kind(description)
            # Keep the function prefix and unwind marker used by the pipeline's
            # existing property filters. This is a normalized ID, not a CBMC ID.
            identity = json.dumps([loc, description, assertion.get("guard")], sort_keys=True)
            suffix = str(int(hashlib.sha256(identity.encode()).hexdigest()[:12], 16))
            result.append(Counterexample(
                failing_property=f"{loc.get('function', 'unknown')}.{kind}.{suffix}",
                variable_assignments=dict(assignments), trace=list(trace),
                description=description, failure_location=loc,
            ))
    return result


def _property_kind(description: str) -> str:
    # These names are consumed by the pipeline's unwind, bug-type and dynamic
    # validation filters. In particular, arithmetic UB must not look like an
    # assertion crash and unwinding IDs must retain a trailing numeric index.
    lower = description.lower()
    for needles, kind in (
        (("unwinding assertion",), "unwind"),
        (("arithmetic overflow", "arithmetic underflow"), "overflow"),
        (("division by zero",), "div-by-zero"),
        (("array bounds",), "bounds"),
        (("dereference failure", "invalid pointer",), "pointer_dereference"),
        (("undefined shift", "undefined behavior on shift"), "shift"),
    ):
        if any(needle in lower for needle in needles):
            return kind
    return "assertion"


def checks_for_counterexample(counterexample: Counterexample) -> dict[str, bool]:
    """Re-enable the failed check when validating a witness with real callees.

    A feasibility harness fixes inputs and inlines implementations, but does
    not necessarily contain an explicit assertion for an intrinsic safety check.
    """
    parts = counterexample.failing_property.split(".")
    family = parts[-2] if len(parts) >= 2 else parts[0]
    flags = {
        "bounds": ("bounds_check",),
        "pointer_dereference": ("pointer_check",),
        "div-by-zero": ("div_by_zero_check",),
        "shift": ("undefined_shift_check",),
        # ESBMC uses the same description for signed/unsigned arithmetic.
        "overflow": ("signed_overflow_check", "unsigned_overflow_check"),
    }
    return {flag: True for flag in flags.get(family, ())}


def _normalize_ldv_target(result: CBMCResult, source: Path, source_text: str) -> None:
    """Give the preserved single reach_error assertion a query-stable identity.

    ESBMC's generated locations contain the temporary feasibility filename and
    shifted line numbers. They cannot identify the same assertion across queries.
    Only the unique assertion in the preserved reach_error body is normalized;
    other assertions, safety checks and ambiguous bodies keep their native ID.
    Raw output, trace and source location are retained unchanged.
    """
    targets = [c for c in result.counterexamples
               if c.failure_location.get("function") == "reach_error"
               and _property_kind(c.description) == "assertion"]
    if not targets:
        return
    from bmc_agent.parser import parse_c_file
    body = parse_c_file(source, source_text=source_text).function_bodies.get("reach_error", "")
    assertions = re.findall(r"\b(?:assert|__assert_fail|__CPROVER_assert|__ESBMC_assert)\s*\(", _code_only(body))
    if len(assertions) == 1:
        for cex in targets:
            cex.failing_property = "reach_error.assertion.1"


def parse_esbmc_result(returncode: int, raw: str, report=None) -> CBMCResult:
    """Fail closed on crashes, missing witnesses, ignored intrinsics and vacuity."""
    capped = raw if len(raw) <= 65536 else raw[:4096] + "\n... truncated ...\n" + raw[-61440:]
    def error(message):
        return CBMCResult(verified=False, raw_output=capped, error=message)

    # Check the FULL log before truncation. A no-body CPROVER/ESBMC call is not
    # nondeterministic user code: it is a lost modeling/verification operation.
    if re.search(r"[^\n]*(?:no body|not found)[^\n]*__(?:CPROVER|ESBMC)_", raw, re.I):
        return error("esbmc unsupported or unresolved verification intrinsic")
    frontend = raw.split("Starting Bounded Model Checking", 1)[0]
    if "PARSING ERROR" in frontend or "CONVERSION ERROR" in frontend or re.search(r"\berror:", frontend):
        return error("esbmc frontend error: " + raw[-3000:])
    if returncode not in (0, 1):
        return error(f"esbmc exited with code {returncode}: " + raw[-2000:])
    if returncode == 1 and "VERIFICATION FAILED" in raw:
        try:
            cexs = _counterexamples(report)
        except (ValueError, TypeError, AttributeError) as exc:
            return error(f"esbmc invalid counterexample report: {exc}")
        if not cexs:
            return error("esbmc verification failed without a structured counterexample")
        return CBMCResult(verified=False, counterexamples=cexs, raw_output=capped)
    if returncode == 0 and "VERIFICATION SUCCESSFUL" in raw:
        counts = re.findall(r"Generated (\d+) VCC\(s\)", raw)
        if not counts or all(int(count) == 0 for count in counts):
            return error("esbmc produced no verification conditions; result is inconclusive")
        if report:
            return error("esbmc success contradicts nonempty violation report")
        return CBMCResult(verified=True, raw_output=capped)
    return error("esbmc returned no recognized verification verdict: " + raw[-2000:])


def run_esbmc(
    harness_path: str | Path, unwind: int = 4, timeout: int = 120,
    esbmc_path: str = "esbmc", include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    unsigned_overflow_check: bool = False, signed_overflow_check: bool = False,
    conversion_check: bool = False, pointer_overflow_check: bool = False,
    undefined_shift_check: bool = False, pointer_check: bool = False,
    bounds_check: bool = False, div_by_zero_check: bool = False,
    object_bits: int | None = None, auto_scale_object_bits: bool = True,
    function: str | None = None, *, arch: str | None = None,
    partial_loops: bool | None = None, apply_env_policy: bool = True,
    evidence_dir: str | Path | None = None,
    preserve_ldv_property: bool = False,
) -> CBMCResult:
    """Run a bounded query; preserve the selected checks, architecture and budget.

    ``object_bits`` and its retry ladder control CBMC's SAT object encoding;
    ESBMC uses a different encoding. An explicit object_bits is rejected.
    The default auto_scale_object_bits=True has no ESBMC counterpart or effect.
    Unknown/unsupported requests are returned as errors, with no CBMC fallback.
    Evidence output is optional and must be unique per call.
    """
    unsupported = [name for name, enabled in (
        ("conversion_check", conversion_check),
        ("pointer_overflow_check", pointer_overflow_check),
        ("object_bits", object_bits is not None),
    ) if enabled]
    if unsupported:
        return CBMCResult(False, error="esbmc unsupported options: " + ", ".join(unsupported))
    binary = shutil.which(esbmc_path)
    if not binary:
        return CBMCResult(False, error="esbmc not found")
    binary = str(Path(binary).resolve())
    source = Path(harness_path).resolve()
    try:
        source_text = source.read_text()
        intrinsic_defines = _intrinsic_defines(source_text)
    except (OSError, UnicodeError, ValueError) as exc:
        return CBMCResult(False, error=f"esbmc input error: {exc}")
    if apply_env_policy:
        unwind, timeout = _limits(unwind, timeout)
    arch = (arch or os.environ.get("SVCOMP_ARCH", "ILP32")).upper()
    if arch not in ("ILP32", "LP64"):
        return CBMCResult(False, error=f"esbmc unsupported architecture: {arch}")
    if unwind < 1 or timeout <= 0:
        return CBMCResult(False, error="esbmc requires positive unwind and timeout")
    if partial_loops is None:
        partial_loops = bool(os.environ.get("SVCOMP_PROP") and (
            os.environ.get("BMC_CONE_SLICE") or os.environ.get("BMC_BUGHUNT")))
    cmd = [binary, str(source), "--generate-json-report", "--multi-property",
           "--unwind", str(unwind), "--32" if arch == "ILP32" else "--64"]
    # ESBMC checks unwinding assertions by default. Preserve the historical
    # bounded bug-finding profile explicitly, without enabling k-induction.
    if partial_loops:
        cmd += ["--partial-loops", "--no-unwinding-assertions"]
    if function and function != "main":
        cmd += ["--function", function]
    # Unlike CBMC, ESBMC enables several checks by default. Do not silently
    # broaden a query intended to check only the benchmark's target assertions.
    cmd += ["--no-align-check", "--no-vla-size-check", "--no-unlimited-scanf-check",
            "--no-pointer-relation-check"]
    for enabled, flag in ((bounds_check, "--no-bounds-check"),
                          (pointer_check, "--no-pointer-check"),
                          (div_by_zero_check, "--no-div-by-zero-check")):
        if not enabled:
            cmd.append(flag)
    for enabled, flag in ((signed_overflow_check, "--overflow-check"),
                          (unsigned_overflow_check, "--unsigned-overflow-check"),
                          (undefined_shift_check, "--ub-shift-check")):
        if enabled:
            cmd.append(flag)
    for directory in [str(source.parent), *(include_dirs or [])]:
        cmd += ["-I", str(Path(directory).resolve())]
    for define in [*(defines or []), *intrinsic_defines]:
        cmd += ["-D", define]
    with tempfile.TemporaryDirectory(prefix="aprover-esbmc-") as work:
        solver_text, omitted_void_externs = source_text, []
        if preserve_ldv_property:
            solver_text, omitted_void_externs = _omit_unused_void_externs(
                source_text, [*(defines or []), *intrinsic_defines])
        if omitted_void_externs:
            solver_source = Path(work) / source.name
            solver_source.write_text(solver_text)
            cmd[1] = str(solver_source)
        raw, report, returncode = "", None, None
        try:
            proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=timeout)
            returncode = proc.returncode
            raw = proc.stdout + "\n" + proc.stderr
            report_path = Path(work) / "report.json"
            if report_path.exists():
                try:
                    report = json.loads(report_path.read_text())
                except (ValueError, OSError) as exc:
                    result = CBMCResult(False, raw_output=raw[-65536:], error=f"esbmc malformed report: {exc}")
                else:
                    result = parse_esbmc_result(returncode, raw, report)
            else:
                result = parse_esbmc_result(returncode, raw)
        except subprocess.TimeoutExpired as exc:
            def decoded(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
            raw = decoded(exc.stdout) + decoded(exc.stderr)
            result = CBMCResult(False, raw_output=raw[-65536:], error=f"esbmc timed out after {timeout}s")
        except OSError as exc:
            result = CBMCResult(False, error=f"esbmc OS error: {exc}")
        if preserve_ldv_property:
            _normalize_ldv_target(result, source, source_text)
        if evidence_dir is not None:
            dest = Path(evidence_dir)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "output.log").write_text(raw)
            (dest / "report.json").write_text(json.dumps(report, indent=2))
            invocation = {
                "backend": "esbmc", "command": cmd, "timeout_s": timeout,
                "returncode": returncode, "harness_path": str(source),
                "harness_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "partial_loops": partial_loops, "arch": arch,
            }
            if omitted_void_externs:
                (dest / "solver_input.c").write_text(solver_text)
                invocation.update(
                    solver_input_sha256=hashlib.sha256(solver_text.encode()).hexdigest(),
                    omitted_unused_void_externs=omitted_void_externs,
                )
            (dest / "invocation.json").write_text(json.dumps(invocation, indent=2))
        return result
