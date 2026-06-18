"""Java/JML specification-benchmark support.

This module is intentionally an evaluation backend: it asks the configured
LLM to insert JML annotations into a Java source file, then validates the
annotated file with OpenJML.  It does not change the existing Java/JBMC safety
pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bmc_agent.llm import LLMClient


_JML_BLOCK_RE = re.compile(r"/\*@.*?@\*/", re.DOTALL)
_JML_LINE_RE = re.compile(r"^[ \t]*//@.*(?:\n|$)", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"```(?:java)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class OpenJMLResult:
    """Result of one OpenJML invocation."""

    status: str
    passed: bool
    returncode: int | None
    runtime_s: float
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    command: list[str] | None = None


@dataclass
class JMLIteration:
    """One generate/refine attempt."""

    iteration: int
    annotated_source: str
    annotated_path: str
    openjml_output_path: str
    source_preserved: bool
    source_preservation_error: str
    openjml: OpenJMLResult


@dataclass
class JMLSpecBenchResult:
    """Top-level Java specs-bench report."""

    source: str
    driver: str
    model: str
    provider: str
    openjml_path: str
    status: str
    passed: bool
    iterations: list[JMLIteration]
    final_annotated_path: str
    report_path: str
    prompt_hash: str
    jml_clause_counts: dict[str, int]
    runtime_s: float
    error: str = ""


def default_openjml_path() -> str:
    """Return the configured OpenJML path or the executable name."""

    env = os.environ.get("BMC_AGENT_OPENJML_PATH", "")
    if env:
        return env
    return "openjml"


def extract_java_source(reply: str) -> str:
    """Extract Java source from an LLM reply.

    Prefer a fenced Java/code block.  If no fence exists, use the raw reply so
    providers that already return plain source still work.
    """

    text = (reply or "").strip()
    match = _CODE_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


def strip_jml_comments(source: str) -> str:
    """Remove JML annotations while leaving ordinary Java comments alone."""

    without_blocks = _JML_BLOCK_RE.sub(" ", source)
    return _JML_LINE_RE.sub("", without_blocks)


def java_without_jml_fingerprint(source: str) -> str:
    """Normalize Java source after deleting JML comments."""

    return re.sub(r"\s+", " ", strip_jml_comments(source)).strip()


def source_code_preserved(original: str, annotated: str) -> tuple[bool, str]:
    """Return whether annotations changed only JML comments."""

    if java_without_jml_fingerprint(original) == java_without_jml_fingerprint(annotated):
        return True, ""
    return False, "generated source changes executable Java code after removing JML comments"


def count_jml_clauses(source: str) -> dict[str, int]:
    """Count common JML clause kinds in an annotated Java source."""

    counts = {
        "requires": 0,
        "ensures": 0,
        "assignable": 0,
        "maintaining": 0,
        "decreases": 0,
        "assert": 0,
        "spec_public": 0,
    }
    for key in counts:
        counts[key] = len(re.findall(rf"\b{re.escape(key)}\b", source))
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts


def _is_jml_line(line: str) -> bool:
    return line.lstrip().startswith("//@")


def _is_loop_annotation(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("//@") and any(
        f" {kw}" in f" {stripped}" for kw in ("maintaining", "decreases", "decreasing", "loop_invariant")
    )


def _line_indent(line: str) -> str:
    return re.match(r"\s*", line).group(0)  # type: ignore[union-attr]


def normalize_jml_annotation_placement(source: str) -> str:
    """Fix common placement-only JML syntax mistakes.

    The LLM sometimes inserts method contracts between a method signature and
    its opening brace, or inserts loop annotations just inside the loop body.
    OpenJML rejects both.  Moving those annotations to the valid adjacent
    location preserves executable Java code and is generic across benchmarks.
    """

    src = re.sub(r"(^[ \t]*//@\s*)decreasing\b", r"\1decreases", source, flags=re.MULTILINE)
    lines = src.splitlines()

    # Move loop annotations from the start of a loop body to immediately before
    # the loop statement.
    out: list[str] = []
    i = 0
    loop_re = re.compile(r"\b(?:for|while)\s*\(.*\)\s*\{?\s*$")
    while i < len(lines):
        line = lines[i]
        if loop_re.search(line):
            j = i + 1
            moved: list[str] = []
            while j < len(lines) and _is_loop_annotation(lines[j]):
                moved.append(_line_indent(line) + lines[j].strip())
                j += 1
            if moved:
                out.extend(moved)
                out.append(line)
                i = j
                continue
        out.append(line)
        i += 1

    lines = out

    # Move method contracts placed between a signature line and the opening
    # brace to the line before the signature.
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            ")" in line
            and "{" not in line
            and not line.lstrip().startswith("//")
            and not line.rstrip().endswith(";")
        ):
            j = i + 1
            moved = []
            while j < len(lines) and _is_jml_line(lines[j]):
                moved.append(_line_indent(line) + lines[j].strip())
                j += 1
            if moved and j < len(lines) and lines[j].lstrip().startswith("{"):
                out.extend(moved)
                out.append(line)
                i = j
                continue
        out.append(line)
        i += 1

    return "\n".join(out).rstrip() + ("\n" if source.endswith("\n") else "")


def build_openjml_command(openjml_path: str, source_path: str | Path, timeout_s: int) -> list[str]:
    """Build the OpenJML ESC command used by the SpecGen artifact."""

    return [
        openjml_path,
        "--esc",
        "--esc-max-warnings",
        "1",
        "--arithmetic-failure=quiet",
        "--nonnull-by-default",
        "--quiet",
        "-nowarn",
        "--prover=cvc4",
        "--timeout",
        str(timeout_s),
        str(source_path),
    ]


def run_openjml(
    source_path: str | Path,
    *,
    openjml_path: str = "openjml",
    timeout_s: int = 200,
    cwd: str | Path | None = None,
) -> OpenJMLResult:
    """Run OpenJML and classify its output using SpecGen's pass convention."""

    resolved = openjml_path
    if not Path(openjml_path).exists() and shutil.which(openjml_path) is None:
        return OpenJMLResult(
            status="tool_missing",
            passed=False,
            returncode=None,
            runtime_s=0.0,
            error=f"openjml not found: {openjml_path}",
            command=build_openjml_command(openjml_path, source_path, timeout_s),
        )

    cmd = build_openjml_command(resolved, source_path, timeout_s)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout_s + 5,
        )
    except subprocess.TimeoutExpired as exc:
        runtime = time.monotonic() - start
        return OpenJMLResult(
            status="timeout",
            passed=False,
            returncode=None,
            runtime_s=runtime,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=f"openjml timed out after {timeout_s}s",
            command=cmd,
        )
    except OSError as exc:
        runtime = time.monotonic() - start
        return OpenJMLResult(
            status="tool_error",
            passed=False,
            returncode=None,
            runtime_s=runtime,
            error=f"openjml OS error: {exc}",
            command=cmd,
        )

    runtime = time.monotonic() - start
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    passed = proc.returncode == 0 and output == ""
    output_lower = output.lower()
    if passed:
        status = "passed"
    elif proc.returncode == 0 or "verify:" in output_lower:
        status = "verification_failed"
    else:
        status = "tool_error" if "error:" not in output_lower else "annotation_error"
    return OpenJMLResult(
        status=status,
        passed=passed,
        returncode=proc.returncode,
        runtime_s=runtime,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        command=cmd,
    )


def _initial_system_prompt() -> str:
    return (
        "You are a JML specification generator for Java programs. Insert JML "
        "annotations so OpenJML ESC can prove the program. Return only the "
        "complete Java source code. Do not modify executable Java code, imports, "
        "class names, method names, statements, or literals; only insert JML "
        "comments. Prefer method contracts (`requires`, `ensures`, `assignable`) "
        "and loop annotations (`maintaining`, `decreases`). Use `spec_public` "
        "for private fields when needed. Loop annotations must appear immediately "
        "before the corresponding `for`, `while`, or `do` statement; never place "
        "loop annotations inside the loop body. Do not add runtime Java assertions."
    )


def _initial_user_prompt(source: str) -> str:
    return (
        "Please generate JML specifications for this Java program.\n\n"
        "Requirements:\n"
        "- Output the full Java source, not a patch and not an explanation.\n"
        "- Preserve all executable Java code exactly; insert only JML comments.\n"
        "- Generate `ensures` clauses for methods when possible.\n"
        "- Generate `maintaining` and `decreases` clauses for loops.\n"
        "- Place all loop annotations immediately before the loop statement, not inside the loop body.\n"
        "- Add overflow/domain preconditions when OpenJML needs them.\n\n"
        "Java source:\n"
        "```java\n"
        f"{source}\n"
        "```"
    )


def _refine_user_prompt(annotated: str, verifier_output: str, source_error: str = "") -> str:
    extra = ""
    if source_error:
        extra = (
            "\nThe previous output also changed executable Java code. You must "
            "preserve the original Java code exactly and only insert JML comments.\n"
            f"Source-preservation error: {source_error}\n"
        )
    return (
        "The current JML-annotated Java source did not pass validation."
        f"{extra}\n\n"
        "Current annotated source:\n"
        "```java\n"
        f"{annotated}\n"
        "```\n\n"
        "OpenJML output:\n"
        "```\n"
        f"{verifier_output[:6000]}\n"
        "```\n\n"
        "Please refine the JML annotations so OpenJML can verify the program. "
        "Return the complete Java source only, preserving all executable Java code. "
        "Every loop annotation must be immediately before its loop statement."
    )


def run_jml_specs_bench(
    source_path: str | Path,
    *,
    driver: str,
    config: Any,
    llm: LLMClient,
    output_dir: str | Path,
    openjml_path: str | None = None,
    openjml_timeout: int = 200,
    max_iterations: int = 3,
) -> JMLSpecBenchResult:
    """Generate JML for one Java source and validate with OpenJML."""

    source_file = Path(source_path)
    original = source_file.read_text(encoding="utf-8")
    artifact_dir = (Path(output_dir) / driver).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    input_path = artifact_dir / "input.java"
    input_path.write_text(original, encoding="utf-8")

    oj_path = openjml_path or getattr(config, "openjml_path", "") or default_openjml_path()
    max_iter = max(1, int(max_iterations))
    provider = getattr(config, "resolved_provider", lambda: getattr(config, "llm_provider", ""))()
    model = getattr(config, "llm_model", "")
    prompt_seed = _initial_system_prompt() + "\n" + _initial_user_prompt(original)
    prompt_hash = hashlib.sha256(prompt_seed.encode("utf-8")).hexdigest()[:16]

    iterations: list[JMLIteration] = []
    current_annotated = ""
    verifier_output = ""
    source_error = ""
    start = time.monotonic()

    for i in range(1, max_iter + 1):
        if i == 1:
            user_prompt = _initial_user_prompt(original)
        else:
            user_prompt = _refine_user_prompt(current_annotated, verifier_output, source_error)
        reply = llm.complete(
            _initial_system_prompt(),
            user_prompt,
            max_tokens=8192,
            temperature=0.1,
            role="spec_gen",
        )
        current_annotated = normalize_jml_annotation_placement(extract_java_source(reply))
        preserved, source_error = source_code_preserved(original, current_annotated)

        iter_dir = artifact_dir / f"iter_{i}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        # Public Java classes must be verified from a file with the class name.
        annotated_path = iter_dir / source_file.name
        annotated_path.write_text(current_annotated, encoding="utf-8")

        if preserved:
            openjml = run_openjml(
                annotated_path,
                openjml_path=oj_path,
                timeout_s=int(openjml_timeout),
                cwd=artifact_dir,
            )
            verifier_output = ((openjml.stdout or "") + (openjml.stderr or "")).strip()
        else:
            openjml = OpenJMLResult(
                status="source_changed",
                passed=False,
                returncode=None,
                runtime_s=0.0,
                error=source_error,
                command=[],
            )
            verifier_output = source_error

        openjml_output_path = artifact_dir / f"openjml_iter_{i}.out"
        out_text = ((openjml.stdout or "") + (openjml.stderr or ""))
        if openjml.error:
            out_text = (out_text + "\n" + openjml.error).strip() + "\n"
        openjml_output_path.write_text(out_text, encoding="utf-8")

        iterations.append(
            JMLIteration(
                iteration=i,
                annotated_source=current_annotated,
                annotated_path=str(annotated_path),
                openjml_output_path=str(openjml_output_path),
                source_preserved=preserved,
                source_preservation_error=source_error,
                openjml=openjml,
            )
        )
        if preserved and openjml.passed:
            break

    runtime = time.monotonic() - start
    final = iterations[-1] if iterations else None
    passed = bool(final and final.source_preserved and final.openjml.passed)
    if passed:
        status = "passed"
    elif final and not final.source_preserved:
        status = "source_changed"
    elif final:
        status = final.openjml.status
    else:
        status = "error"

    result = JMLSpecBenchResult(
        source=str(source_file),
        driver=driver,
        model=model,
        provider=provider,
        openjml_path=oj_path,
        status=status,
        passed=passed,
        iterations=iterations,
        final_annotated_path=final.annotated_path if final else "",
        report_path=str(artifact_dir / "jml_result.json"),
        prompt_hash=prompt_hash,
        jml_clause_counts=count_jml_clauses(final.annotated_source if final else ""),
        runtime_s=runtime,
        error="" if passed else (final.openjml.error if final else "no iterations completed"),
    )

    def encode(obj: Any) -> Any:
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        return obj

    (artifact_dir / "jml_result.json").write_text(
        json.dumps(encode(result), indent=2),
        encoding="utf-8",
    )
    return result
