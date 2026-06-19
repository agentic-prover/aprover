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


_JML_BLOCK_RE = re.compile(r"/\*@.*?(?:@\*/|\*/)", re.DOTALL)
_JML_LINE_RE = re.compile(r"^[ \t]*//@.*(?:\n|$)", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"```(?:java)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_JAVA_TOKEN_RE = re.compile(
    r"""
    /\*.*?\*/|//[^\n]*|
    "(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|
    [A-Za-z_$][A-Za-z0-9_$]*|
    0[xX][0-9A-Fa-f_]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[fFdDlL]?|
    >>>=|>>=|<<=|>>>|>>|<<|==|!=|<=|>=|\+\+|--|&&|\|\||\+=|-=|\*=|/=|%=|&=|\|=|\^=|->|::|
    [\[\]{}().,;:?~!%^&*+\-/=<>|]
    """,
    re.DOTALL | re.VERBOSE,
)
_JML_RANGE_QUANTIFIER_RE = re.compile(
    r"(\\(?:sum|forall|exists)\s+(?:int|integer|long)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*;\s*)"
    r"\2\s+in\s+([^;\n]+?)\s*\.\.\s*([^;\n]+?)\s*;",
)


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


def _as_text(value: Any) -> str:
    """Best-effort subprocess output normalization."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def java_without_jml_fingerprint(source: str) -> str:
    """Normalize Java source after deleting JML comments."""

    return " ".join(java_executable_tokens(source))


def java_executable_tokens(source: str) -> list[str]:
    """Tokenize executable Java while ignoring whitespace and comments.

    Source preservation should reject executable changes, but not harmless
    pretty-printing such as ``n-1`` becoming ``n - 1``.  A lightweight lexical
    token stream is enough for the benchmark adapter: it preserves identifiers,
    literals, operators, and punctuation while ignoring formatting and comments.
    """

    stripped = strip_jml_comments(source)
    tokens: list[str] = []
    for match in _JAVA_TOKEN_RE.finditer(stripped):
        tok = match.group(0)
        if not tok or tok.isspace() or tok.startswith("//") or tok.startswith("/*"):
            continue
        tokens.append(tok)
    return tokens


def source_code_preserved(original: str, annotated: str) -> tuple[bool, str]:
    """Return whether annotations changed only JML comments."""

    original_tokens = java_executable_tokens(original)
    annotated_tokens = java_executable_tokens(annotated)
    if original_tokens == annotated_tokens:
        return True, ""
    detail = ""
    for idx, (a, b) in enumerate(zip(original_tokens, annotated_tokens)):
        if a != b:
            detail = f" first token difference at {idx}: original={a!r}, generated={b!r}"
            break
    if not detail and len(original_tokens) != len(annotated_tokens):
        detail = f" token-count differs: original={len(original_tokens)}, generated={len(annotated_tokens)}"
    return False, "generated source changes executable Java code after removing JML comments;" + detail


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
        f" {kw}" in f" {stripped}"
        for kw in ("maintaining", "decreases", "decreasing", "loop_invariant", "loop_variant", "assignable")
    )


def _line_indent(line: str) -> str:
    return re.match(r"\s*", line).group(0)  # type: ignore[union-attr]


_LOOP_START_RE = re.compile(r"\b(?:for|while|do)\b")
_FOR_DECL_VAR_RE = re.compile(
    r"\bfor\s*\(\s*(?:final\s+)?(?:int|long|short|byte|char|boolean)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
)


def _normalise_jml_clause_keyword(line: str) -> str:
    line = re.sub(r"(^[ \t]*(?://@|/?\*?)\s*)decreasing\b", r"\1decreases", line)
    line = re.sub(r"(^[ \t]*(?://@|/?\*?)\s*)loop_variant\b", r"\1decreases", line)
    line = re.sub(r"(^[ \t]*(?://@|/?\*?)\s*)loop_invariant\b", r"\1maintaining", line)
    return line


def _normalise_jml_range_quantifiers(source: str) -> str:
    """Rewrite common non-OpenJML range syntax in quantifiers.

    Some LLMs emit mathematical shorthand such as ``\\sum int k; k in 0..i;``.
    OpenJML expects the range as a boolean predicate after the first semicolon.
    This is syntax normalization only; it does not invent new specifications.
    """

    def repl(match: re.Match[str]) -> str:
        prefix, var, low, high = match.groups()
        return f"{prefix}{low.strip()} <= {var} && {var} <= {high.strip()};"

    return _JML_RANGE_QUANTIFIER_RE.sub(repl, source)


def _strip_simple_old_in_loop_clause(line: str) -> str:
    """Remove simple ``\\old(x)`` wrappers from loop annotations.

    OpenJML's ``\\old`` is meaningful in method postconditions, but LLMs often
    use it inside loop invariants where local variables are not method-entry
    values.  Restrict this repair to simple variable/field names in loop specs
    so method-level postconditions retain their intended two-state meaning.
    """

    return re.sub(
        r"\\old\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*)\s*\)",
        lambda m: re.sub(r"\s+", "", m.group(1)),
        line,
    )


def _normalise_conditional_ensures(line: str) -> str:
    """Rewrite a common malformed conditional postcondition shape.

    LLMs often write ``ensures \result == expr && cond;`` when they mean
    ``ensures cond ==> \result == expr;``.  The former requires ``cond`` at every
    method exit and is usually inconsistent across switch/branch-heavy code.
    This rewrite is deliberately narrow: it only fires when the left conjunct is
    a ``\result`` equality and the right conjunct does not mention ``\result``.
    """

    match = re.match(r"^(\s*(?://@|\*)?\s*ensures\s+)(.+?)\s*&&\s*(.+?)(;\s*)$", line)
    if not match:
        return line
    prefix, lhs, rhs, suffix = match.groups()
    if "\\result" not in lhs or "\\result" in rhs:
        return line
    if "==>" in lhs or "<==>" in lhs or "==>" in rhs or "<==>" in rhs:
        return line
    if not re.match(r"^\s*\\result\s*(?:==|!=|<=|>=|<|>)", lhs):
        return line
    return f"{prefix}{rhs.strip()} ==> {lhs.strip()}{suffix}"


def _jml_content_keyword(line: str) -> str:
    stripped = line.strip()
    stripped = stripped.removeprefix("//@").strip()
    stripped = stripped.removeprefix("*").strip()
    return stripped.split(None, 1)[0].rstrip(";") if stripped else ""


def _method_param_names(signature_line: str) -> set[str] | None:
    """Extract Java parameter names from a simple method signature line."""

    if "(" not in signature_line or ")" not in signature_line:
        return None
    if not re.search(r"\b(?:public|protected|private|static|final|synchronized|native|abstract|strictfp)\b", signature_line):
        # Package-private methods are allowed, but avoid treating control-flow
        # statements as method signatures.
        if not re.search(r"\w+\s+\w+\s*\(", signature_line):
            return None
    params = signature_line[signature_line.find("(") + 1: signature_line.rfind(")")].strip()
    if not params:
        return set()
    names: set[str] = set()
    for part in params.split(","):
        toks = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", part)
        if toks:
            names.add(toks[-1])
    return names


def _jml_bound_variables(line: str) -> set[str]:
    return set(re.findall(r"\\(?:forall|exists|sum)\s+(?:int|integer|long|short|byte|char|boolean)\s+([A-Za-z_$][A-Za-z0-9_$]*)", line))


def _jml_identifiers(line: str) -> set[str]:
    cleaned = re.sub(r"'(?:\\.|[^'\\])*'", " ", line)
    cleaned = re.sub(r'"(?:\\.|[^"\\])*"', " ", cleaned)
    return set(re.findall(r"(?<!\\)\b[A-Za-z_$][A-Za-z0-9_$]*\b", cleaned))


_METHOD_CONTRACT_ALLOWED_IDS = {
    "requires",
    "ensures",
    "assignable",
    "assigns",
    "also",
    "pure",
    "true",
    "false",
    "null",
    "int",
    "integer",
    "long",
    "short",
    "byte",
    "char",
    "boolean",
    "String",
    "Integer",
    "Long",
    "Short",
    "Byte",
    "Character",
    "Boolean",
    "Math",
    "MIN_VALUE",
    "MAX_VALUE",
    "length",
    "charAt",
    "toCharArray",
    "old",
    "result",
}


def _method_contract_unknown_ids(line: str, params: set[str]) -> set[str]:
    ids = _jml_identifiers(line)
    ids -= _jml_bound_variables(line)
    ids -= _METHOD_CONTRACT_ALLOWED_IDS
    ids -= params
    return ids


def _filter_method_contract_scope(lines: list[str]) -> list[str]:
    """Drop method-contract clauses that reference local variables.

    Method pre/postconditions are scoped over parameters, fields, ``this``, and
    JML built-ins.  A generated clause such as ``ensures result == area1`` is
    invalid when ``area1`` is a local variable declared inside the method body.
    Dropping only those invalid clauses preserves executable Java and converts
    syntax errors into either weaker valid specs or genuine proof failures.
    """

    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _is_jml_line(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        j = i
        group: list[str] = []
        while j < len(lines) and _is_jml_line(lines[j]):
            group.append(lines[j])
            j += 1
        sig_idx = _next_nonempty_index(lines, j)
        params = _method_param_names(lines[sig_idx]) if sig_idx is not None else None
        if params is not None:
            filtered: list[str] = []
            for line in group:
                keyword = _jml_content_keyword(line)
                if keyword in {"requires", "ensures"} and _method_contract_unknown_ids(line, params):
                    continue
                filtered.append(line)
            group = filtered
        out.extend(group)
        i = j
    return out


def _normalize_jml_lines(lines: list[str]) -> list[str]:
    return [_normalise_conditional_ensures(_normalise_jml_clause_keyword(line)) for line in lines]


def _brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def _add_division_requires(lines: list[str]) -> list[str]:
    """Add non-zero preconditions for direct division/modulo by parameters.

    OpenJML checks Java arithmetic well-definedness.  If a method body contains
    ``/ p`` or ``% p`` for parameter ``p`` and no matching precondition exists,
    verification can fail before the generated functional spec matters.
    """

    out = list(lines)
    i = 0
    insertions: dict[int, list[str]] = {}
    while i < len(out):
        params = _method_param_names(out[i])
        if params is None or "{" not in out[i]:
            i += 1
            continue
        depth = _brace_delta(out[i])
        j = i + 1
        body: list[str] = []
        while j < len(out) and depth > 0:
            body.append(out[j])
            depth += _brace_delta(out[j])
            j += 1
        body_text = "\n".join(strip_jml_comments("\n".join(body)).splitlines())
        denoms = {
            m.group(1)
            for m in re.finditer(r"(?:/|%)\s*([A-Za-z_$][A-Za-z0-9_$]*)\b", body_text)
            if m.group(1) in params
        }
        if denoms:
            group_start = i
            while group_start > 0 and _is_jml_line(out[group_start - 1]):
                group_start -= 1
            existing = "\n".join(out[group_start:i])
            reqs = []
            indent = _line_indent(out[i])
            for denom in sorted(denoms):
                if not re.search(rf"\b{re.escape(denom)}\s*!=\s*0\b", existing):
                    reqs.append(f"{indent}//@ requires {denom} != 0;")
            if reqs:
                insertions.setdefault(group_start, []).extend(reqs)
        i = j

    rebuilt: list[str] = []
    for idx, line in enumerate(out):
        if idx in insertions:
            rebuilt.extend(insertions[idx])
        rebuilt.append(line)
    if len(out) in insertions:
        rebuilt.extend(insertions[len(out)])
    return rebuilt


def _prune_reported_postcondition(source: str, verifier_output: str) -> tuple[str, bool]:
    """Remove the generated ``ensures`` line OpenJML reports as unproved."""

    # For an unproved method postcondition, OpenJML reports two locations: the
    # ``Postcondition`` location points at the contract clause, while the
    # ``Associated declaration`` often points at the return statement.  Prefer
    # the former so pruning removes only generated contract text.
    matches = re.findall(r"\(Postcondition: [^)\n]*?\.java:(\d+):\)", verifier_output)
    if not matches:
        matches = re.findall(r"Associated declaration: [^\n]*?\.java:(\d+):", verifier_output)
    if not matches:
        return source, False
    lines = source.splitlines()
    for raw in matches:
        idx = int(raw) - 1
        if 0 <= idx < len(lines) and "ensures" in lines[idx]:
            del lines[idx]
            return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), True
    return source, False


def _next_nonempty_is_loop(lines: list[str], index: int) -> bool:
    j = index
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j < len(lines) and bool(_LOOP_START_RE.search(lines[j]))


def _next_nonempty_index(lines: list[str], index: int) -> int | None:
    j = index
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j if j < len(lines) else None


def _first_nested_for_decl(lines: list[str], loop_index: int) -> tuple[int, str] | None:
    """Return the first nested ``for (int v ...)`` inside a loop body."""

    depth = 0
    seen_body = False
    for idx in range(loop_index, len(lines)):
        line = lines[idx]
        if idx > loop_index and depth <= 0 and seen_body:
            return None
        if idx > loop_index:
            match = _FOR_DECL_VAR_RE.search(line)
            if match and depth > 0:
                return idx, match.group(1)
        depth += line.count("{") - line.count("}")
        if "{" in line:
            seen_body = True
    return None


def _relocate_inner_loop_annotations(lines: list[str]) -> list[str]:
    """Move misplaced inner-loop line annotations to the inner loop.

    A common LLM mistake for nested loops is:

    ``//@ maintaining ... i ...``
    ``//@ maintaining ... j ...``
    ``for (int i ...) {``
    ``    for (int j ...) {``

    The ``j`` clauses are not in scope before the outer loop.  If the next loop
    body immediately contains a nested ``for`` declaration, move only the lines
    referencing that nested loop variable to the nested loop.  This is purely a
    placement repair; executable Java remains untouched.
    """

    removals: set[int] = set()
    insertions: dict[int, list[str]] = {}
    i = 0
    while i < len(lines):
        if not _is_jml_line(lines[i]):
            i += 1
            continue
        j = i
        group_indices: list[int] = []
        while j < len(lines) and _is_jml_line(lines[j]):
            group_indices.append(j)
            j += 1
        loop_idx = _next_nonempty_index(lines, j)
        if loop_idx is not None and _LOOP_START_RE.search(lines[loop_idx]):
            nested = _first_nested_for_decl(lines, loop_idx)
            if nested:
                nested_idx, nested_var = nested
                nested_indent = _line_indent(lines[nested_idx])
                moved: list[str] = []
                var_re = re.compile(rf"\b{re.escape(nested_var)}\b")
                for line_idx in group_indices:
                    line = lines[line_idx]
                    if var_re.search(line):
                        moved.append(nested_indent + _normalise_conditional_ensures(line.strip()))
                        removals.add(line_idx)
                if moved:
                    insertions.setdefault(nested_idx, []).extend(moved)
        i = j

    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in insertions:
            out.extend(insertions[idx])
        if idx not in removals:
            out.append(line)
    return out


def _normalise_loop_jml_groups(lines: list[str]) -> list[str]:
    """Normalize line-style JML groups that directly annotate loops.

    OpenJML accepts ``maintaining`` and ``decreases`` before a loop in this
    setup.  It rejects method-frame clauses such as ``assignable`` in a loop
    spec group, so those are dropped only when the following statement is a loop.
    """

    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _is_jml_line(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        j = i
        group: list[str] = []
        while j < len(lines) and _is_jml_line(lines[j]):
            group.append(_normalise_conditional_ensures(_normalise_jml_clause_keyword(lines[j])))
            j += 1
        if _next_nonempty_is_loop(lines, j):
            filtered = []
            for line in group:
                if _jml_content_keyword(line) in {"assignable", "assigns"}:
                    continue
                line = _strip_simple_old_in_loop_clause(line)
                filtered.append(line)
            group = filtered
        out.extend(group)
        i = j
    return out


def _normalise_loop_jml_blocks(source: str) -> str:
    """Normalize block-style JML comments that directly annotate loops."""

    lines = source.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if "/*@" not in lines[i]:
            out.append(lines[i])
            i += 1
            continue

        block: list[str] = []
        while i < len(lines):
            block.append(lines[i])
            if "*/" in lines[i]:
                i += 1
                break
            i += 1
        is_loop_block = _next_nonempty_is_loop(lines, i)
        new_block: list[str] = []
        for raw in block:
            line = _normalise_conditional_ensures(_normalise_jml_clause_keyword(raw))
            if is_loop_block and _jml_content_keyword(line) in {"assignable", "assigns"}:
                continue
            if is_loop_block:
                line = _strip_simple_old_in_loop_clause(line)
            new_block.append(line)
        out.extend(new_block)
    return "\n".join(out)


def normalize_jml_annotation_placement(source: str) -> str:
    """Fix common placement-only JML syntax mistakes.

    The LLM sometimes inserts method contracts between a method signature and
    its opening brace, or inserts loop annotations just inside the loop body.
    OpenJML rejects both.  Moving those annotations to the valid adjacent
    location preserves executable Java code and is generic across benchmarks.
    """

    src = _normalise_jml_range_quantifiers(source)
    src = "\n".join(_normalize_jml_lines(src.splitlines()))
    src = _normalise_loop_jml_blocks(src)
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
                moved.append(_line_indent(line) + _normalise_conditional_ensures(lines[j].strip()))
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
                moved.append(_line_indent(line) + _normalise_conditional_ensures(lines[j].strip()))
                j += 1
            if moved and j < len(lines) and lines[j].lstrip().startswith("{"):
                out.extend(moved)
                out.append(line)
                i = j
                continue
        out.append(line)
        i += 1

    out = _normalise_loop_jml_groups(out)
    out = _relocate_inner_loop_annotations(out)
    out = _filter_method_contract_scope(out)
    out = _add_division_requires(out)
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
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
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
        "loop annotations inside the loop body, and never put inner-loop annotations "
        "before an outer loop. Use only OpenJML loop keywords `maintaining` and "
        "`decreases`; do not use `loop_invariant`, `loop_variant`, `decreasing`, "
        "or loop-level `assignable`. Loop invariants may mention only variables "
        "that are in scope immediately before the loop; do not use `\\old` in loop "
        "invariants. Do not add runtime Java assertions."
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
        "- For nested loops, place inner-loop annotations immediately before the inner loop, not before the outer loop.\n"
        "- Do not use loop-level `assignable`; OpenJML rejects it in loop specs.\n"
        "- Use `maintaining` and `decreases`, not `loop_invariant`, `loop_variant`, or `decreasing`.\n"
        "- Loop invariants may mention only variables in scope immediately before the annotated loop; do not use `\\old` in loop invariants.\n"
        "- If using JML quantifiers or sums, use semicolon-separated predicates such as `\\sum int k; 0 <= k && k < i; expr`; do not use `k in 0..i` shorthand.\n"
        "- Add overflow/domain preconditions when OpenJML needs them.\n\n"
        "Java source:\n"
        "```java\n"
        f"{source}\n"
        "```"
    )


def _refine_user_prompt(
    annotated: str,
    verifier_output: str,
    source_error: str = "",
    original_source: str = "",
) -> str:
    extra = ""
    if source_error:
        extra = (
            "\nThe previous output also changed executable Java code. You must "
            "preserve the original Java token stream exactly and only insert JML comments.\n"
            f"Source-preservation error: {source_error}\n"
            "Start from this original Java source and add comments only:\n"
            "```java\n"
            f"{original_source}\n"
            "```\n"
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
        "Every loop annotation must be immediately before its loop statement. "
        "If OpenJML reports an annotation syntax error, fix the JML syntax without "
        "changing Java code. If it reports an out-of-scope variable, move or remove "
        "that annotation so every referenced variable is in scope. If it reports a "
        "LoopInvariant failure, replace the failing invariant with "
        "one that is true before the loop and preserved by the loop body. "
        "Do not add loop-level `assignable`, `loop_invariant`, `loop_variant`, "
        "`decreasing`, or `k in a..b` range shorthand."
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
    last_preserved_annotated = ""
    repeated_timeout_count = 0
    start = time.monotonic()

    for i in range(1, max_iter + 1):
        if i == 1:
            user_prompt = _initial_user_prompt(original)
        else:
            user_prompt = _refine_user_prompt(current_annotated, verifier_output, source_error, original)
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
            last_preserved_annotated = current_annotated
            openjml = run_openjml(
                annotated_path,
                openjml_path=oj_path,
                timeout_s=int(openjml_timeout),
                cwd=artifact_dir,
            )
            prune_rounds = 0
            while not openjml.passed and openjml.status == "verification_failed" and prune_rounds < 3:
                combined_output = ((openjml.stdout or "") + (openjml.stderr or "") + (("\n" + openjml.error) if openjml.error else ""))
                pruned, changed = _prune_reported_postcondition(current_annotated, combined_output)
                if not changed:
                    break
                current_annotated = normalize_jml_annotation_placement(pruned)
                annotated_path.write_text(current_annotated, encoding="utf-8")
                openjml = run_openjml(
                    annotated_path,
                    openjml_path=oj_path,
                    timeout_s=int(openjml_timeout),
                    cwd=artifact_dir,
                )
                prune_rounds += 1
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
        if preserved and openjml.status == "timeout":
            repeated_timeout_count += 1
            if repeated_timeout_count >= 2:
                break
        elif preserved:
            repeated_timeout_count = 0
        if not preserved and last_preserved_annotated:
            current_annotated = last_preserved_annotated

    runtime = time.monotonic() - start
    final = iterations[-1] if iterations else None
    effective_final = final
    if final and not final.source_preserved:
        for candidate in reversed(iterations):
            if candidate.source_preserved:
                effective_final = candidate
                break
    passed = bool(effective_final and effective_final.source_preserved and effective_final.openjml.passed)
    if passed:
        status = "passed"
    elif effective_final and not effective_final.source_preserved:
        status = "source_changed"
    elif effective_final:
        status = effective_final.openjml.status
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
        final_annotated_path=effective_final.annotated_path if effective_final else "",
        report_path=str(artifact_dir / "jml_result.json"),
        prompt_hash=prompt_hash,
        jml_clause_counts=count_jml_clauses(effective_final.annotated_source if effective_final else ""),
        runtime_s=runtime,
        error="" if passed else (effective_final.openjml.error if effective_final else "no iterations completed"),
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
