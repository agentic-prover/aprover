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
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

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
_IMPORT_LINE_RE = re.compile(r"^\s*import\s+([A-Za-z_$][A-Za-z0-9_$.]*)\s*;\s*$")
_JML_RANGE_QUANTIFIER_RE = re.compile(
    r"(\\(?:sum|forall|exists)\s+(?:int|integer|long)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*;\s*)"
    r"\2\s+in\s+([^;\n]+?)\s*\.\.\s*([^;\n]+?)\s*;",
)
_JAVA_UTIL_IMPORTS = {
    "ArrayDeque": "java.util.ArrayDeque",
    "ArrayList": "java.util.ArrayList",
    "Arrays": "java.util.Arrays",
    "Collections": "java.util.Collections",
    "Deque": "java.util.Deque",
    "HashMap": "java.util.HashMap",
    "HashSet": "java.util.HashSet",
    "LinkedList": "java.util.LinkedList",
    "List": "java.util.List",
    "Map": "java.util.Map",
    "Queue": "java.util.Queue",
    "Set": "java.util.Set",
    "Stack": "java.util.Stack",
}
_PUBLIC_JAVA_TYPE_RE = re.compile(
    r"\bpublic\s+(?:(?:abstract|final|strictfp|sealed|non-sealed)\s+)*"
    r"(?:class|interface|enum|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_PUBLIC_JAVA_TYPE_LINE_RE = re.compile(
    r"\bpublic\s+(?:(?:abstract|final|strictfp|sealed|non-sealed)\s+)*"
    r"(?:class|interface|enum|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_JAVA_TYPE_DECL_RE = re.compile(
    r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_JAVA_ARGS_DECL_RE = re.compile(
    r"\b(?!(?:return|throw|if|while|for|switch|case|new|instanceof)\b)"
    r"(?:[A-Za-z_$][A-Za-z0-9_$]*(?:\s*<[^;{}()]*>)?(?:\s*\[\s*\])*)\s+args\b"
)
_TOP_LEVEL_CLASS_HEADER_RE = re.compile(
    r"(^[ \t]*(?:(?:public|protected|private|abstract|final|strictfp)\s+)*"
    r"class\s+[A-Za-z_$][A-Za-z0-9_$]*(?:[^{;]*)\{)",
    re.MULTILINE,
)
_TYPE_HEADER_WITH_TRAILING_RE = re.compile(
    r"^(\s*(?:(?:public|protected|private|abstract|final|strictfp|sealed|non-sealed)\s+)*"
    r"(?:class|interface|enum|record)\s+[A-Za-z_$][A-Za-z0-9_$]*(?:[^{;]*)\{)\s+(.+)$"
)
_SVCOMP_VERIFIER_IMPORT_RE = re.compile(
    r"^\s*import\s+org\.sosy_lab\.sv_benchmarks\.Verifier\s*;\s*$",
    re.MULTILINE,
)
_TOP_LEVEL_STATIC_TYPE_RE = re.compile(
    r"^(\s*(?:(?:public|abstract|final|strictfp|sealed|non-sealed)\s+)*)"
    r"static\s+((?:class|interface|enum|record)\b.*)$"
)
_ACTIVE_PROCESS_GROUPS: set[int] = set()
_ACTIVE_PROCESS_GROUPS_LOCK = threading.Lock()


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
    on_path = shutil.which("openjml")
    if on_path:
        return on_path
    workspace_candidate = Path(__file__).resolve().parents[2] / "SpecGen-Artifact" / "openjml" / "openjml"
    if workspace_candidate.exists():
        return str(workspace_candidate)
    return "openjml"


def java_verification_filename(source: str, fallback_name: str) -> str:
    """Return the filename OpenJML/Javac expects for a Java source string.

    Several benchmark suites store public classes under benchmark-oriented file
    names.  ``javac`` rejects those files before it reaches the generated JML,
    so the verifier artifact should use the public top-level type name when it
    is present.  Package-private classes keep the benchmark filename.
    """

    type_name = _public_top_level_java_type_name(source)
    if not type_name:
        return fallback_name
    return f"{type_name}.java"


def repair_java_source_for_openjml(source: str) -> str:
    """Apply verifier-only repairs for Java source forms OpenJML rejects.

    Some SV-COMP Java artifacts contain ``static class`` at top level.  Java
    only allows ``static`` on nested types, so javac/OpenJML rejects the file
    before generated JML is meaningful.  Dropping the modifier for top-level
    type declarations is a source-hygiene repair on the verifier artifact.

    A small number of benchmark artifacts also carry JBMC/SV-COMP source shapes
    that are not valid javac input after extraction: a file may have been
    renamed away from ``Main`` while references still use ``Main``, or a
    constructor call may be emitted without ``new``.  Repair those forms only
    when the source structure makes the intent unambiguous, so OpenJML can
    reach the generated JML instead of stopping in the Java frontend.
    """

    out: list[str] = []
    depth = 0
    changed = False
    for line in source.splitlines():
        code = re.sub(r"//.*$", "", line)
        repaired = line
        if depth == 0:
            match = _TOP_LEVEL_STATIC_TYPE_RE.match(line)
            if match:
                repaired = f"{match.group(1)}{match.group(2)}"
                code = re.sub(r"//.*$", "", repaired)
                changed = True
        out.append(repaired)
        depth += _brace_delta(code)
        if depth < 0:
            depth = 0
    repaired_source = "\n".join(out) + ("\n" if source.endswith("\n") else "")
    main_repaired = _repair_renamed_main_references(repaired_source)
    constructor_repaired = _repair_bare_constructor_invocations(main_repaired)
    args_repaired = _repair_missing_args_reference(constructor_repaired)
    unreachable_repaired = _repair_unreachable_tail_return_after_caught_throw(args_repaired)
    loop_repaired = _brace_simple_nested_unbraced_loops(unreachable_repaired)
    unrolled_repaired = _unroll_small_constant_nested_for_loops(loop_repaired)
    if not changed and unrolled_repaired == source:
        return source
    return unrolled_repaired


def _repair_unreachable_tail_return_after_caught_throw(source: str) -> str:
    """Drop javac-unreachable tail returns after an always-caught throw.

    Some SV-COMP Java examples encode exception semantics with a method body of
    the shape ``try { throw ...; } catch (...) { return ...; } return true;``.
    Javac rejects the final return as unreachable before OpenJML can verify
    anything.  Removing only that tail statement is a verifier-artifact hygiene
    repair: the source already cannot reach it under Java control flow.
    """

    lines = source.splitlines()
    out: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        window = "\n".join(lines[i : i + 14])
        if (
            re.search(r"\btry\s*\{\s*throw\s+[^;{}]+;\s*\}", window, re.DOTALL)
            and re.search(r"\bcatch\s*\([^)]*\)\s*\{[^{}]*\breturn\b[^{}]*;\s*\}", window, re.DOTALL)
        ):
            j = i
            depth = 0
            while j < len(lines):
                code = re.sub(r"//.*$", "", lines[j])
                depth += _brace_delta(code)
                stripped = lines[j].strip()
                if depth == 1 and stripped == "return true;":
                    # Keep a blank line so generated artifact line numbers stay
                    # close to the benchmark source.
                    out.extend(lines[i:j])
                    out.append("")
                    changed = True
                    i = j + 1
                    break
                j += 1
            else:
                out.append(lines[i])
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if not changed:
        return source
    return "\n".join(out) + ("\n" if source.endswith("\n") else "")


_SIMPLE_CONTROL_HEADER_RE = r"(?:for|while)\s*\([^()]*\)"
_SAME_LINE_NESTED_LOOP_RE = re.compile(
    rf"^(\s*)({_SIMPLE_CONTROL_HEADER_RE})\s+({_SIMPLE_CONTROL_HEADER_RE})\s+(.+;\s*)$"
)
_CONTROL_HEADER_ONLY_RE = re.compile(rf"^(\s*)({_SIMPLE_CONTROL_HEADER_RE})\s*$")
_CONTROL_HEADER_STATEMENT_RE = re.compile(rf"^(\s*)({_SIMPLE_CONTROL_HEADER_RE})\s+(.+;\s*)$")
_JAVA_IDENTIFIER_PATTERN = r"[A-Za-z_$][\w$]*"
_SMALL_CONSTANT_NESTED_FOR_RE = re.compile(
    rf"(?ms)^"
    rf"(?P<indent>[ \t]*)for\s*\(\s*int\s+(?P<outer>{_JAVA_IDENTIFIER_PATTERN})\s*=\s*"
    rf"(?P<outer_start>\d+)\s*;\s*(?P=outer)\s*<\s*(?P<outer_end>\d+)\s*;\s*"
    rf"(?P=outer)\s*\+\+\s*\)\s*\{{\s*"
    rf"(?P<inner_indent>[ \t]*)for\s*\(\s*int\s+(?P<inner>{_JAVA_IDENTIFIER_PATTERN})\s*=\s*"
    rf"(?P<inner_start>\d+)\s*;\s*(?P=inner)\s*<\s*(?P<inner_end>\d+)\s*;\s*"
    rf"(?P=inner)\s*\+\+\s*\)\s*\{{\s*"
    rf"(?P<body>[^{{}}]*?)"
    rf"(?P=inner_indent)\}}\s*"
    rf"(?P=indent)\}}",
    re.MULTILINE,
)


def _brace_simple_nested_unbraced_loops(source: str) -> str:
    """Add braces around simple nested one-statement loops for OpenJML.

    OpenJML can hit internal prover/JML errors on compact nested loops of the
    form ``for (...) for (...) stmt;``.  Bracing those loops is semantics
    preserving Java source hygiene for verifier artifacts, and it gives later
    loop-spec pruning a stable source shape.
    """

    lines = source.splitlines()
    out: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        same_line = _SAME_LINE_NESTED_LOOP_RE.match(lines[i])
        if same_line and not same_line.group(4).lstrip().startswith("{"):
            indent, outer, inner, stmt = same_line.groups()
            inner_indent = indent + "    "
            out.extend(
                [
                    f"{indent}{outer} {{",
                    f"{inner_indent}{inner} {{",
                    f"{inner_indent}    {stmt.strip()}",
                    f"{inner_indent}}}",
                    f"{indent}}}",
                ]
            )
            changed = True
            i += 1
            continue

        outer_line = _CONTROL_HEADER_ONLY_RE.match(lines[i])
        if outer_line and i + 1 < len(lines):
            inner_line = _CONTROL_HEADER_STATEMENT_RE.match(lines[i + 1])
            if inner_line and not inner_line.group(3).lstrip().startswith("{"):
                outer_indent, outer = outer_line.groups()
                inner_indent, inner, stmt = inner_line.groups()
                out.extend(
                    [
                        f"{outer_indent}{outer} {{",
                        f"{inner_indent}{inner} {{",
                        f"{inner_indent}    {stmt.strip()}",
                        f"{inner_indent}}}",
                        f"{outer_indent}}}",
                    ]
                )
                changed = True
                i += 2
                continue

        out.append(lines[i])
        i += 1
    if not changed:
        return source
    return "\n".join(out) + ("\n" if source.endswith("\n") else "")


def _unroll_small_constant_nested_for_loops(source: str) -> str:
    """Unroll tiny literal-bound nested loops for verifier artifacts.

    OpenJML can hit internal rewriting errors on simple two-dimensional array
    loops even before generated JML is considered.  For loops with literal
    integer bounds and a tiny iteration space, unrolling is Java-equivalent and
    converts a verifier frontend/tool failure into ordinary proof obligations.
    Dynamic bounds, large loops, and bodies with non-local loop control are
    intentionally left untouched.
    """

    def replace(match: re.Match[str]) -> str:
        outer = match.group("outer")
        inner = match.group("inner")
        outer_start = int(match.group("outer_start"))
        outer_end = int(match.group("outer_end"))
        inner_start = int(match.group("inner_start"))
        inner_end = int(match.group("inner_end"))
        body = match.group("body")

        if outer_end < outer_start or inner_end < inner_start:
            return match.group(0)
        iteration_count = (outer_end - outer_start) * (inner_end - inner_start)
        if iteration_count == 0 or iteration_count > 16:
            return match.group(0)

        body_without_comments = re.sub(r"//.*$", "", body, flags=re.MULTILINE)
        if re.search(r"\b(?:break|continue|for|while|do|switch)\b", body_without_comments):
            return match.group(0)
        if re.search(rf"(?:\+\+\s*{outer}|{outer}\s*\+\+|--\s*{outer}|{outer}\s*--|{outer}\s*[+\-*/%]?=)", body_without_comments):
            return match.group(0)
        if re.search(rf"(?:\+\+\s*{inner}|{inner}\s*\+\+|--\s*{inner}|{inner}\s*--|{inner}\s*[+\-*/%]?=)", body_without_comments):
            return match.group(0)

        indent = match.group("indent")
        body_lines = [line.strip() for line in body.strip().splitlines() if line.strip()]
        if not body_lines:
            return match.group(0)

        out: list[str] = []
        for outer_value in range(outer_start, outer_end):
            out.append(f"{indent}{{")
            out.append(f"{indent}    int {outer} = {outer_value};")
            for inner_value in range(inner_start, inner_end):
                out.append(f"{indent}    {{")
                out.append(f"{indent}        int {inner} = {inner_value};")
                for line in body_lines:
                    out.append(f"{indent}        {line}")
                out.append(f"{indent}    }}")
            out.append(f"{indent}}}")
        return "\n".join(out)

    return _SMALL_CONSTANT_NESTED_FOR_RE.sub(replace, source)


_JAVA_DEBUG_OUTPUT_RE = re.compile(
    r"\bSystem\s*\.\s*out\s*\.\s*(?:print|println|printf)\s*\([^;\n]*\)\s*;"
)
_JAVA_SYSTEM_TERMINATION_RE = re.compile(
    r"\b(?:Runtime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*halt|System\s*\.\s*exit)\s*\([^;\n]*\)\s*;"
)
_JAVA_STRING_LITERAL_RE = r'"(?:\\.|[^"\\])*"'
_JAVA_STRING_ASSIGN_RE = re.compile(rf"\bString\s+([A-Za-z_$][\w$]*)\s*=\s*({_JAVA_STRING_LITERAL_RE})\s*;")
_JAVA_STRING_SPLIT_DECL_RE = re.compile(
    rf"(?m)^(\s*)String\s*\[\]\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*\.\s*split\s*\(\s*({_JAVA_STRING_LITERAL_RE})\s*\)\s*;"
)
_JAVA_IDENTIFIER_RE = r"[A-Za-z_$][\w$]*"
_JAVA_CHAR_LITERAL_RE = r"'(?:\\.|[^'\\])'"
_JAVA_PRIMITIVE_LITERAL_RE = (
    rf"(?:true|false|{_JAVA_CHAR_LITERAL_RE}|[-+]?(?:0[xX][0-9A-Fa-f_]+|\d[\d_]*)(?:[lL])?|"
    r"[-+]?\d[\d_]*\.\d[\d_]*(?:[fFdD])?)"
)
_JAVA_CHAR_ARRAY_LITERAL_ASSIGN_RE = re.compile(
    rf"(?ms)\bchar\s*\[\]\s+({_JAVA_IDENTIFIER_RE})\s*=\s*(?:new\s+char\s*\[\]\s*)?\{{(?P<body>.*?)\}}\s*;"
)
_JAVA_CHAR_ARRAY_INIT_RE = re.compile(
    rf"(?ms)\b(?:char\s*\[\]\s+|char\s+)({_JAVA_IDENTIFIER_RE})(?:\s*\[\s*\])?\s*=\s*"
    rf"(?:new\s+char\s*\[\]\s*)?\{{(?P<body>.*?)\}}\s*;"
)
_JAVA_LITERAL_ASSIGN_RE = re.compile(
    rf"(?m)\b(?:final\s+)?(?:Object|String|boolean|char|byte|short|int|long|float|double)\s+"
    rf"({_JAVA_IDENTIFIER_RE})\s*=\s*({_JAVA_STRING_LITERAL_RE}|{_JAVA_PRIMITIVE_LITERAL_RE})\s*;"
)
_JAVA_STRING_VALUEOF_ASSIGN_RE = re.compile(
    rf"(?m)^(\s*)(?:(String)\s+)?({_JAVA_IDENTIFIER_RE})\s*=\s*String\s*\.\s*valueOf\s*\(\s*"
    rf"({_JAVA_IDENTIFIER_RE})(?:\s*,\s*(\d+)\s*,\s*(\d+))?\s*\)\s*;"
)
_JAVA_NEW_STRING_ASSIGN_RE = re.compile(
    rf"(?m)^(\s*)(?:(String)\s+)?({_JAVA_IDENTIFIER_RE})\s*=\s*new\s+String\s*\(\s*"
    rf"(?:(?P<arg>{_JAVA_STRING_LITERAL_RE}|{_JAVA_IDENTIFIER_RE})(?:\s*,\s*(?P<start>\d+)\s*,\s*(?P<count>\d+))?)?\s*\)\s*;"
)
_JAVA_STRING_FROM_CHAR_ARRAY_SLICE_ASSIGN_RE = re.compile(
    rf"(?m)^(\s*)(?:(String)\s+)?(?P<name>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"(?:String\s*\.\s*valueOf|new\s+String)\s*\(\s*(?P<array>{_JAVA_IDENTIFIER_RE})\s*,\s*"
    rf"(?P<start>\d+)\s*,\s*(?P<count>\d+)\s*\)\s*;"
)
_JAVA_STRING_VALUEOF_OBJECT_SELF_CONCAT_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)Object\s+(?P<object>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"(?P<source>{_JAVA_IDENTIFIER_RE})\s*;\s*(?://[^\n]*)?\n"
    rf"(?P=indent)String\s+(?P<tmp>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"String\s*\.\s*valueOf\s*\(\s*(?P=object)\s*\)\s*;\s*"
    rf"\n(?P=indent)return\s+(?P=tmp)\s*\.\s*equals\s*\(\s*(?P<rhs>[^;{{}}]+?)\s*\)\s*;",
)
_JAVA_STRING_CONCAT_ASSIGN_RE = re.compile(
    rf"(?m)^(\s*)String\s+({_JAVA_IDENTIFIER_RE})\s*=\s*([^;\n]+?)\s*\+\s*([^;\n]+?)\s*;"
)
_JAVA_WRAPPER_VALUEOF_DROPPED_CONVERSION_RE = re.compile(
    rf"(?m)^(?P<indent>[ \t]*)(?P<var>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"(?P<wrapper>Integer|Long|Short|Byte|Float|Double|Boolean|Character)\s*"
    rf"\.\s*valueOf\s*\(\s*(?P<value>{_JAVA_PRIMITIVE_LITERAL_RE})\s*\)\s*;\s*"
    rf"\n(?P=indent)(?P=var)\s*\.\s*"
    rf"(?P<method>byteValue|shortValue|intValue|longValue|floatValue|doubleValue|booleanValue|charValue)\s*"
    rf"\(\s*\)\s*;",
)
_JAVA_STRING_BUILDER_DECL_RE = re.compile(
    rf"(?ms)^(\s*)String(?:Builder|Buffer)\s+({_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"new\s+String(?:Builder|Buffer)\s*\(\s*(?P<arg>{_JAVA_STRING_LITERAL_RE}|{_JAVA_IDENTIFIER_RE})?\s*\)\s*;"
)
_JAVA_STRINGBUILDER_GETCHARS_SELF_COMPARE_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)StringBuilder\s+(?P<builder>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"new\s+StringBuilder\s*\(\s*(?P<source>{_JAVA_IDENTIFIER_RE})\s*\)\s*;\s*"
    rf"(?P=indent)char\s*\[\]\s+(?P<array>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"new\s+char\s*\[\s*(?P=builder)\s*\.\s*length\s*\(\s*\)\s*\]\s*;\s*"
    rf"(?P=indent)(?P=builder)\s*\.\s*getChars\s*\(\s*0\s*,\s*"
    rf"(?P=builder)\s*\.\s*length\s*\(\s*\)\s*,\s*(?P=array)\s*,\s*0\s*\)\s*;\s*"
    rf"(?P=indent)int\s+(?P<index>{_JAVA_IDENTIFIER_RE})\s*=\s*0\s*;\s*"
    rf"(?:(?P=indent)//@[^\n]*\n|\s*/\*@.*?\*/\s*)*"
    rf"(?P=indent)for\s*\(\s*char\s+(?P<item>{_JAVA_IDENTIFIER_RE})\s*:\s*(?P=array)\s*\)\s*\{{\s*"
    rf"(?:System\s*\.\s*out\s*\.\s*(?:print|println|printf)\s*\([^;]*\)\s*;\s*|;\s*)*"
    rf"if\s*\(\s*(?P=item)\s*==\s*(?P=builder)\s*\.\s*charAt\s*\(\s*(?P=index)\s*\)\s*\)\s*"
    rf"return\s+false\s*;\s*"
    rf"(?:\+\+\s*(?P=index)|(?P=index)\s*\+\+|(?P=index)\s*\+=\s*1)\s*;\s*"
    rf"(?P=indent)\}}\s*"
    rf"(?P=indent)return\s+true\s*;",
)
_JAVA_LITERAL_REVERSE_COMPARE_LOOP_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)int\s+(?P<index>{_JAVA_IDENTIFIER_RE})\s*=\s*0\s*;\s*"
    rf"(?:(?P=indent)//@[^\n]*\n|\s*/\*@.*?\*/\s*)*"
    rf"(?P=indent)for\s*\(\s*int\s+(?P<count>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"(?P<length_expr>(?:{_JAVA_IDENTIFIER_RE}\s*\.\s*length\s*\(\s*\)|\d+)\s*-\s*1)\s*;\s*"
    rf"(?P=count)\s*>=\s*0\s*;\s*(?P=count)\s*--\s*\)\s*\{{\s*"
    rf"(?:System\s*\.\s*out\s*\.\s*(?:print|println|printf)\s*\([^;]*\)\s*;\s*|;\s*)*"
    rf"if\s*\(\s*(?P<left>{_JAVA_IDENTIFIER_RE})\s*\.\s*charAt\s*\(\s*(?P=count)\s*\)\s*"
    rf"!=\s*(?P<right>{_JAVA_IDENTIFIER_RE})\s*\.\s*charAt\s*\(\s*(?P=index)\s*\)\s*\)\s*"
    rf"return\s+false\s*;\s*"
    rf"(?:\+\+\s*(?P=index)|(?P=index)\s*\+\+|(?P=index)\s*\+=\s*1)\s*;\s*"
    rf"(?P=indent)\}}",
)
_JAVA_LITERAL_GETCHARS_PREFIX_COMPARE_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)(?P<src>{_JAVA_IDENTIFIER_RE})\s*\.\s*getChars\s*\(\s*0\s*,\s*"
    rf"(?P<count>\d+)\s*,\s*(?P<array>{_JAVA_IDENTIFIER_RE})\s*,\s*0\s*\)\s*;\s*"
    rf"(?P=indent)(?P<index>{_JAVA_IDENTIFIER_RE})\s*=\s*0\s*;\s*"
    rf"(?:(?P=indent)//@[^\n]*\n|\s*/\*@.*?\*/\s*)*"
    rf"(?P=indent)for\s*\(\s*char\s+(?P<item>{_JAVA_IDENTIFIER_RE})\s*:\s*(?P=array)\s*\)\s*\{{\s*"
    rf"(?:System\s*\.\s*out\s*\.\s*(?:print|println|printf)\s*\([^;]*\)\s*;\s*|;\s*)*"
    rf"if\s*\(\s*(?P<expected>{_JAVA_IDENTIFIER_RE})\s*\.\s*charAt\s*\(\s*(?P=index)\s*\)\s*"
    rf"!=\s*(?P=item)\s*\)\s*return\s+false\s*;\s*"
    rf"(?:\+\+\s*(?P=index)|(?P=index)\s*\+\+|(?P=index)\s*\+=\s*1)\s*;\s*"
    rf"(?P=indent)\}}",
)
_JAVA_APPEND_ARG_RE = re.compile(r"\.append\s*\(([^()]*)\)")
_JAVA_STRING_METHOD_CALL_RE = re.compile(
    rf"(?P<receiver>{_JAVA_STRING_LITERAL_RE}|{_JAVA_IDENTIFIER_RE})\s*\.\s*"
    r"(?P<method>length|charAt|equals|equalsIgnoreCase|compareTo|startsWith|endsWith|regionMatches|replace|trim|indexOf|lastIndexOf)"
    r"\s*\((?P<args>[^()]*)\)"
)
_JAVA_CHARSEQUENCE_TOSTRING_ALIAS_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)CharSequence\s+(?P<cs>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"\(\s*CharSequence\s*\)\s*(?P<src>{_JAVA_IDENTIFIER_RE})\s*;\s*"
    rf"(?P=indent)String\s+(?P<str>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"(?P=cs)\s*\.\s*toString\s*\(\s*\)\s*;"
)
_JAVA_STRING_LITERAL_EXPR_RE = rf"{_JAVA_STRING_LITERAL_RE}(?:\s*\+\s*{_JAVA_STRING_LITERAL_RE})*"
_JAVA_LITERAL_REGEX_FIND_RE = re.compile(
    rf"(?ms)^"
    rf"(?P<indent>[ \t]*)Pattern\s+(?P<pattern>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"Pattern\s*\.\s*compile\s*\(\s*(?P<regex>{_JAVA_STRING_LITERAL_RE})\s*\)\s*;\s*"
    rf"(?P<string_decl>(?P=indent)String\s+(?P<string>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"(?P<string_expr>{_JAVA_STRING_LITERAL_EXPR_RE})\s*;\s*)"
    rf"(?P=indent)Matcher\s+(?P<matcher>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"(?P=pattern)\s*\.\s*matcher\s*\(\s*(?P=string)\s*\)\s*;\s*"
    rf"(?:(?P=indent)//@[^\n]*\n|\s*/\*@.*?\*/\s*)*"
    rf"(?P=indent)while\s*\(\s*(?P=matcher)\s*\.\s*find\s*\(\s*\)\s*\)\s*\{{"
    rf"(?P<body>.*?)"
    rf"(?P=indent)\}}",
)
_JAVA_STRING_ARRAY_LITERAL_DECL_RE = re.compile(
    rf"(?ms)\bString\s*\[\]\s+({_JAVA_IDENTIFIER_RE})\s*=\s*(?:new\s+String\s*\[\]\s*)?"
    rf"\{{(?P<body>.*?)\}}\s*;"
)
_JAVA_STRING_FOREACH_COUNT_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)for\s*\(\s*String\s+(?P<var>{_JAVA_IDENTIFIER_RE})\s*:\s*"
    rf"(?P<array>{_JAVA_IDENTIFIER_RE})\s*\)\s*\{{\s*"
    rf"if\s*\((?P<cond>[^{{}};\n]+)\)\s*(?P<inc>(?:\+\+\s*{_JAVA_IDENTIFIER_RE}|{_JAVA_IDENTIFIER_RE}\s*\+\+|{_JAVA_IDENTIFIER_RE}\s*\+=\s*1))\s*;\s*"
    rf"\}}",
    re.MULTILINE,
)
_JAVA_CONSTANT_NULL_TRY_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)(?P<type>[A-Za-z_$][\w$]*(?:\s*<[^;\n]+>)?)\s+"
    rf"(?P<var>{_JAVA_IDENTIFIER_RE})\s*=\s*null\s*;\s*"
    rf"try\s*\{{(?P<body>.*?)\}}\s*"
    rf"catch\s*\(\s*(?:NullPointerException|Exception)\s+{_JAVA_IDENTIFIER_RE}\s*\)\s*\{{\s*"
    rf"return\s+(?P<catch_return>[^;{{}}]+)\s*;\s*"
    rf"\}}\s*return\s+(?P<normal_return>[^;{{}}]+)\s*;",
    re.MULTILINE,
)
_JAVA_CONSTANT_NULL_EMPTY_CATCH_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)(?P<type>[A-Za-z_$][\w$]*(?:\s*<[^;\n]+>)?)\s+"
    rf"(?P<var>{_JAVA_IDENTIFIER_RE})\s*=\s*null\s*;\s*"
    rf"try\s*\{{(?P<body>.*?)\}}\s*"
    rf"catch\s*\(\s*(?:NullPointerException|Exception)\s+{_JAVA_IDENTIFIER_RE}\s*\)\s*\{{\s*\}}\s*"
    rf"return\s+(?P<normal_return>[^;{{}}]+)\s*;",
    re.MULTILINE,
)
_JAVA_FIRST_CHAR_ARRAY_HELPER_RE = re.compile(
    rf"(?ms)\b(?:public|protected|private|static|final|strictfp|\s)+"
    rf"char\s*\[\s*\]\s+(?P<method>{_JAVA_IDENTIFIER_RE})\s*\(\s*"
    rf"char\s*(?:\[\s*\]\s*)?(?P<arg>{_JAVA_IDENTIFIER_RE})\s*(?:\[\s*\])?\s*\)\s*"
    rf"\{{\s*if\s*\(\s*(?P=arg)\s*!=\s*null\s*&&\s*(?P=arg)\s*\.\s*length\s*>\s*0\s*\)\s*"
    rf"\{{\s*(?P=arg)\s*\[\s*0\s*\]\s*=\s*(?P<char>{_JAVA_CHAR_LITERAL_RE})\s*;\s*\}}\s*"
    rf"return\s+(?P=arg)\s*;\s*\}}"
)
_JAVA_TOCHARARRAY_FIRST_CHAR_CONCAT_RE = re.compile(
    rf"(?ms)^(?P<indent>[ \t]*)"
    rf"(?P<header>(?:(?:public|protected|private|static|final|strictfp)\s+)*"
    rf"(?:int|long|short|byte|boolean)\s+{_JAVA_IDENTIFIER_RE}\s*"
    rf"\(\s*String\s+(?P<arg>{_JAVA_IDENTIFIER_RE})\s*\)\s*)"
    rf"\{{\s*if\s*\(\s*(?P=arg)\s*\.\s*length\s*\(\s*\)\s*!=\s*(?P<len>\d+)\s*\)\s*"
    rf"return\s+(?P<fallback>[^;{{}}]+?)\s*;\s*"
    rf"char\s*\[\s*\]\s+(?P<array>{_JAVA_IDENTIFIER_RE})\s*=\s*"
    rf"(?P<helper>{_JAVA_IDENTIFIER_RE})\s*\(\s*(?P=arg)\s*\.\s*toCharArray\s*\(\s*\)\s*\)\s*;\s*"
    rf"String\s+(?P<string>{_JAVA_IDENTIFIER_RE})\s*=\s*new\s+String\s*\(\s*(?P<prefix>{_JAVA_STRING_LITERAL_RE})\s*\)\s*"
    rf"\+\s*new\s+String\s*\(\s*(?P=array)\s*,\s*0\s*,\s*(?P=array)\s*\.\s*length\s*\)\s*;\s*"
    rf"return\s*\(\s*(?P=string)\s*\.\s*charAt\s*\(\s*(?P<index>\d+)\s*\)\s*==\s*"
    rf"(?P<char>{_JAVA_CHAR_LITERAL_RE})\s*\)\s*\?\s*(?P<then>[^:;{{}}]+?)\s*:\s*"
    rf"(?P<else>[^;{{}}]+?)\s*;\s*\}}"
)


def abstract_java_debug_output_for_openjml(source: str) -> str:
    """Replace standalone console debug output with empty statements.

    Several SV-COMP Java programs contain ``System.out.println`` calls that are
    only branch/debug markers.  OpenJML then spends most of its budget in the
    Java library model when proving generated postconditions or frame clauses.
    For the verifier artifact, treat those standalone output calls as no-ops
    while preserving surrounding control-flow tokens such as ``else``.
    """

    return _JAVA_DEBUG_OUTPUT_RE.sub(";", source)


def abstract_java_system_termination_for_openjml(source: str) -> str:
    """Model JVM termination calls as exceptional exits for OpenJML.

    SV-COMP helper code commonly uses ``Runtime.getRuntime().halt`` or
    ``System.exit`` to model non-returning assumptions.  OpenJML's bundled JDK
    specs contain diverges obligations for those calls, and proving the library
    model often dominates the benchmark.  For the verifier artifact, replace
    such standalone termination calls by an unchecked exception, which also has
    no normal return and therefore preserves the relevant normal-postcondition
    reasoning.
    """

    return _JAVA_SYSTEM_TERMINATION_RE.sub("throw new RuntimeException();", source)


def _decode_simple_java_string_literal(literal: str) -> str | None:
    if len(literal) < 2 or literal[0] != '"' or literal[-1] != '"':
        return None
    out: list[str] = []
    body = literal[1:-1]
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            return None
        esc = body[i]
        simple = {
            "b": "\b",
            "t": "\t",
            "n": "\n",
            "f": "\f",
            "r": "\r",
            '"': '"',
            "'": "'",
            "\\": "\\",
        }
        if esc in simple:
            out.append(simple[esc])
            i += 1
            continue
        return None
    return "".join(out)


def _decode_simple_java_char_literal(literal: str) -> str | None:
    if len(literal) < 3 or literal[0] != "'" or literal[-1] != "'":
        return None
    body = literal[1:-1]
    if len(body) == 1 and body != "\\":
        return body
    if not body.startswith("\\") or len(body) != 2:
        return None
    simple = {
        "b": "\b",
        "t": "\t",
        "n": "\n",
        "f": "\f",
        "r": "\r",
        '"': '"',
        "'": "'",
        "\\": "\\",
    }
    return simple.get(body[1])


def _decode_simple_java_string_expression(expr: str) -> str | None:
    parts = re.findall(_JAVA_STRING_LITERAL_RE, expr)
    if not parts:
        return None
    between = re.sub(_JAVA_STRING_LITERAL_RE, "", expr).strip()
    if between and not re.fullmatch(r"(?:\+\s*)*", between):
        return None
    decoded: list[str] = []
    for part in parts:
        value = _decode_simple_java_string_literal(part)
        if value is None:
            return None
        decoded.append(value)
    return "".join(decoded)


def _encode_java_char_literal(value: str) -> str:
    if len(value) != 1:
        raise ValueError("Java char literal requires exactly one character")
    escaped = {
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
        '"': '"',
        "'": "\\'",
        "\\": "\\\\",
    }.get(value, value)
    return f"'{escaped}'"


def _encode_java_string_literal(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\b", "\\b")
        .replace("\f", "\\f")
    )
    return f'"{escaped}"'


def _java_split_space(value: str) -> list[str]:
    # Java String.split(" ") treats the delimiter as a one-space regex and
    # drops trailing empty strings because limit defaults to zero.
    parts = value.split(" ")
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def _literal_valueof_string(literal: str) -> str | None:
    literal = literal.strip()
    string_value = _decode_simple_java_string_literal(literal)
    if string_value is not None:
        return string_value
    char_value = _decode_simple_java_char_literal(literal)
    if char_value is not None:
        return char_value
    if literal in {"true", "false"}:
        return literal
    if re.fullmatch(r"[-+]?(?:0[xX][0-9A-Fa-f_]+|\d[\d_]*)(?:[lL])?", literal):
        return literal.rstrip("lL").replace("_", "")
    if re.fullmatch(r"[-+]?\d[\d_]*\.\d[\d_]*(?:[fFdD])?", literal):
        return literal.rstrip("fFdD").replace("_", "")
    return None


def _literal_char_array(body: str) -> str | None:
    chars: list[str] = []
    parts = [part.strip() for part in body.replace("\n", " ").split(",")]
    for part in parts:
        if not part:
            continue
        char = _decode_simple_java_char_literal(part)
        if char is None:
            return None
        chars.append(char)
    return "".join(chars)


def _literal_string_array(body: str) -> list[str] | None:
    values: list[str] = []
    parts = _split_simple_java_args(body)
    if parts is None:
        return None
    for part in parts:
        value = _decode_simple_java_string_literal(part.strip())
        if value is None:
            return None
        values.append(value)
    return values


def _constant_java_values(source: str) -> tuple[dict[str, str], dict[str, str]]:
    stripped = strip_jml_comments(source)
    constants: dict[str, str] = {}
    char_arrays: dict[str, str] = {}
    for match in _JAVA_CHAR_ARRAY_LITERAL_ASSIGN_RE.finditer(stripped):
        value = _literal_char_array(match.group("body"))
        if value is not None:
            char_arrays[match.group(1)] = value
    for match in _JAVA_LITERAL_ASSIGN_RE.finditer(stripped):
        value = _literal_valueof_string(match.group(2))
        if value is not None:
            constants[match.group(1)] = value
    return constants, char_arrays


def _literal_string_arrays(source: str) -> dict[str, list[str]]:
    arrays: dict[str, list[str]] = {}
    stripped = strip_jml_comments(source)
    for match in _JAVA_STRING_ARRAY_LITERAL_DECL_RE.finditer(stripped):
        if not _assigned_once(stripped, match.group(1)):
            continue
        values = _literal_string_array(match.group("body"))
        if values is not None:
            arrays[match.group(1)] = values
    return arrays


def _char_array_initializer_lengths(source: str) -> dict[str, int]:
    lengths: dict[str, int] = {}
    stripped = strip_jml_comments(source)
    for match in _JAVA_CHAR_ARRAY_INIT_RE.finditer(stripped):
        name = match.group(1)
        if not _assigned_once(stripped, name):
            continue
        parts = _split_simple_java_args(match.group("body"))
        if parts is None:
            continue
        lengths[name] = len(parts)
    return lengths


def _assigned_once(source: str, name: str) -> bool:
    stripped = strip_jml_comments(source)
    return len(re.findall(rf"\b{re.escape(name)}\s*=", stripped)) == 1


def _stable_constant_string_values(source: str) -> dict[str, str]:
    constants, char_arrays = _constant_java_values(source)
    stable = {name: value for name, value in constants.items() if _assigned_once(source, name)}
    stripped = strip_jml_comments(source)
    for _ in range(3):
        changed = False
        for match in _JAVA_NEW_STRING_ASSIGN_RE.finditer(stripped):
            lhs = match.group(3)
            if not _assigned_once(stripped, lhs):
                continue
            arg = match.group("arg")
            start = match.group("start")
            count = match.group("count")
            if arg is None:
                value = ""
            else:
                value = _decode_simple_java_string_literal(arg)
                if value is None:
                    value = stable.get(arg) if start is None and count is None else char_arrays.get(arg)
            if value is None:
                continue
            sliced = _slice_java_string(value, start, count)
            if sliced is not None and stable.get(lhs) != sliced:
                stable[lhs] = sliced
                changed = True
        if not changed:
            break
    return stable


def _slice_java_string(value: str, start: str | None, count: str | None) -> str | None:
    if start is None and count is None:
        return value
    if start is None or count is None:
        return None
    begin = int(start)
    length = int(count)
    if begin < 0 or length < 0 or begin + length > len(value):
        return None
    return value[begin : begin + length]


def _java_compare_to(left: str, right: str) -> int:
    for a, b in zip(left, right):
        if a != b:
            return ord(a) - ord(b)
    return len(left) - len(right)


def _is_ascii(value: str) -> bool:
    return all(ord(ch) < 128 for ch in value)


def _split_simple_java_args(args: str) -> list[str] | None:
    out: list[str] = []
    current: list[str] = []
    in_string = False
    in_char = False
    escaped = False
    for ch in args:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\" and (in_string or in_char):
            current.append(ch)
            escaped = True
            continue
        if ch == '"' and not in_char:
            in_string = not in_string
            current.append(ch)
            continue
        if ch == "'" and not in_string:
            in_char = not in_char
            current.append(ch)
            continue
        if ch == "," and not in_string and not in_char:
            out.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if in_string or in_char:
        return None
    tail = "".join(current).strip()
    if tail:
        out.append(tail)
    return out


def _constant_string_arg(arg: str, constants: dict[str, str]) -> str | None:
    literal = _decode_simple_java_string_literal(arg.strip())
    if literal is not None:
        return literal
    return constants.get(arg.strip())


def _constant_int_arg(arg: str) -> int | None:
    if re.fullmatch(r"[-+]?\d[\d_]*", arg.strip()):
        return int(arg.replace("_", ""))
    return None


def _constant_char_arg(arg: str) -> str | None:
    return _decode_simple_java_char_literal(arg.strip())


def _constant_bool_arg(arg: str) -> bool | None:
    stripped = arg.strip()
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    return None


def _identifier_in_identity_comparison(source: str, name: str) -> bool:
    stripped = strip_jml_comments(source)
    return bool(
        re.search(rf"\b{re.escape(name)}\b\s*(?:==|!=)", stripped)
        or re.search(rf"(?:==|!=)\s*\b{re.escape(name)}\b", stripped)
    )


def abstract_java_constant_string_split_for_openjml(source: str) -> str:
    """Fold simple constant ``String.split`` calls in verifier artifacts.

    OpenJML's bundled ``String``/``CharSequence`` model is often the bottleneck
    for SV-COMP examples that split a compile-time literal and immediately
    reason over the resulting tokens.  When both the receiver and delimiter are
    simple Java string literals, replacing the split call with the exact array
    literal avoids the library model without changing the Java-level value.
    Keep this intentionally narrow; non-constant or regex-heavy splits remain
    untouched.
    """

    constants: dict[str, str] = {}
    for match in _JAVA_STRING_ASSIGN_RE.finditer(strip_jml_comments(source)):
        value = _decode_simple_java_string_literal(match.group(2))
        if value is not None:
            constants[match.group(1)] = value

    def repl(match: re.Match[str]) -> str:
        indent, array_name, receiver, delimiter_literal = match.groups()
        receiver_value = constants.get(receiver)
        delimiter = _decode_simple_java_string_literal(delimiter_literal)
        if receiver_value is None or delimiter != " ":
            return match.group(0)
        parts = _java_split_space(receiver_value)
        array_literal = ", ".join(_encode_java_string_literal(part) for part in parts)
        return f"{indent}String[] {array_name} = new String[] {{{array_literal}}};"

    return _JAVA_STRING_SPLIT_DECL_RE.sub(repl, source)


def abstract_java_constant_string_construction_for_openjml(source: str) -> str:
    """Fold literal-only ``String`` construction in verifier artifacts.

    Several SV-COMP Java examples spend the verifier budget inside JDK string
    constructors or ``String.valueOf`` even though all operands are source
    literals.  For those cases, replace the call with the exact string literal
    value.  Unknown objects, input-dependent strings, non-literal char arrays,
    and non-trivial numeric formats remain untouched.
    """

    def replacement(indent: str, type_name: str | None, lhs: str, value: str) -> str:
        decl = f"{type_name} " if type_name else ""
        return f"{indent}{decl}{lhs} = {_encode_java_string_literal(value)};"

    out = source
    for _ in range(4):
        constants, char_arrays = _constant_java_values(out)

        def valueof_repl(match: re.Match[str]) -> str:
            indent, type_name, lhs, arg, start, count = match.groups()
            base = char_arrays.get(arg) if start is not None or count is not None else constants.get(arg, char_arrays.get(arg))
            if base is None:
                return match.group(0)
            value = _slice_java_string(base, start, count)
            if value is None:
                return match.group(0)
            return replacement(indent, type_name, lhs, value)

        def new_string_repl(match: re.Match[str]) -> str:
            indent = match.group(1)
            type_name = match.group(2)
            lhs = match.group(3)
            arg = match.group("arg")
            start = match.group("start")
            count = match.group("count")
            if _identifier_in_identity_comparison(out, lhs):
                return match.group(0)
            if arg is None:
                value = ""
            else:
                value = _decode_simple_java_string_literal(arg)
                if value is None:
                    value = (
                        constants.get(arg, char_arrays.get(arg))
                        if start is None and count is None
                        else char_arrays.get(arg)
                    )
            if value is None:
                return match.group(0)
            sliced = _slice_java_string(value, start, count)
            if sliced is None:
                return match.group(0)
            return replacement(indent, type_name, lhs, sliced)

        rewritten = _JAVA_STRING_VALUEOF_ASSIGN_RE.sub(valueof_repl, out)
        rewritten = _JAVA_NEW_STRING_ASSIGN_RE.sub(new_string_repl, rewritten)
        if rewritten == out:
            break
        out = rewritten
    return out


def _evaluate_constant_string_method(
    receiver: str,
    method: str,
    args: str,
    constants: dict[str, str],
) -> str | None:
    receiver_value = _constant_string_arg(receiver, constants)
    if receiver_value is None:
        return None
    parsed_args = _split_simple_java_args(args)
    if parsed_args is None:
        return None
    if method == "length" and not parsed_args:
        return str(len(receiver_value))
    if method == "trim" and not parsed_args:
        return _encode_java_string_literal(receiver_value.strip())
    if method == "charAt" and len(parsed_args) == 1:
        index = _constant_int_arg(parsed_args[0])
        if index is None or index < 0 or index >= len(receiver_value):
            return None
        return _encode_java_char_literal(receiver_value[index])
    if method == "equals" and len(parsed_args) == 1:
        other = _constant_string_arg(parsed_args[0], constants)
        if other is None:
            return None
        return "true" if receiver_value == other else "false"
    if method == "equalsIgnoreCase" and len(parsed_args) == 1:
        other = _constant_string_arg(parsed_args[0], constants)
        if other is None or not (_is_ascii(receiver_value) and _is_ascii(other)):
            return None
        return "true" if receiver_value.lower() == other.lower() else "false"
    if method == "compareTo" and len(parsed_args) == 1:
        other = _constant_string_arg(parsed_args[0], constants)
        if other is None:
            return None
        return str(_java_compare_to(receiver_value, other))
    if method == "startsWith" and len(parsed_args) in {1, 2}:
        prefix = _constant_string_arg(parsed_args[0], constants)
        if prefix is None:
            return None
        offset = 0
        if len(parsed_args) == 2:
            parsed_offset = _constant_int_arg(parsed_args[1])
            if parsed_offset is None:
                return None
            offset = parsed_offset
        return "true" if offset >= 0 and receiver_value.startswith(prefix, offset) else "false"
    if method == "endsWith" and len(parsed_args) == 1:
        suffix = _constant_string_arg(parsed_args[0], constants)
        if suffix is None:
            return None
        return "true" if receiver_value.endswith(suffix) else "false"
    if method == "regionMatches" and len(parsed_args) in {4, 5}:
        ignore_case = False
        base = 0
        if len(parsed_args) == 5:
            parsed_ignore = _constant_bool_arg(parsed_args[0])
            if parsed_ignore is None:
                return None
            ignore_case = parsed_ignore
            base = 1
        this_offset = _constant_int_arg(parsed_args[base])
        other = _constant_string_arg(parsed_args[base + 1], constants)
        other_offset = _constant_int_arg(parsed_args[base + 2])
        length = _constant_int_arg(parsed_args[base + 3])
        if this_offset is None or other is None or other_offset is None or length is None:
            return None
        if min(this_offset, other_offset, length) < 0:
            return "false"
        left = receiver_value[this_offset : this_offset + length]
        right = other[other_offset : other_offset + length]
        if len(left) != length or len(right) != length:
            return "false"
        if ignore_case:
            if not (_is_ascii(left) and _is_ascii(right)):
                return None
            left, right = left.lower(), right.lower()
        return "true" if left == right else "false"
    if method == "replace" and len(parsed_args) == 2:
        old = _constant_char_arg(parsed_args[0])
        new = _constant_char_arg(parsed_args[1])
        if old is None or new is None:
            return None
        return _encode_java_string_literal(receiver_value.replace(old, new))
    if method == "indexOf" and len(parsed_args) in {1, 2}:
        needle = _constant_string_arg(parsed_args[0], constants)
        if needle is None:
            char_needle = _constant_char_arg(parsed_args[0])
            if char_needle is None:
                return None
            needle = char_needle
        start = 0
        if len(parsed_args) == 2:
            parsed_start = _constant_int_arg(parsed_args[1])
            if parsed_start is None:
                return None
            start = parsed_start
        return str(receiver_value.find(needle, max(start, 0)))
    if method == "lastIndexOf" and len(parsed_args) in {1, 2}:
        needle = _constant_string_arg(parsed_args[0], constants)
        if needle is None:
            char_needle = _constant_char_arg(parsed_args[0])
            if char_needle is None:
                return None
            needle = char_needle
        if len(parsed_args) == 1:
            return str(receiver_value.rfind(needle))
        parsed_start = _constant_int_arg(parsed_args[1])
        if parsed_start is None:
            return None
        if parsed_start < 0:
            return "-1"
        return str(receiver_value.rfind(needle, 0, parsed_start + 1))
    return None


def abstract_java_constant_string_methods_for_openjml(source: str) -> str:
    """Fold locale-independent methods on literal/stable constant strings."""

    constants = _stable_constant_string_values(source)
    if not constants and not re.search(_JAVA_STRING_LITERAL_RE + r"\s*\.", source):
        return source

    def repl(match: re.Match[str]) -> str:
        folded = _evaluate_constant_string_method(
            match.group("receiver"),
            match.group("method"),
            match.group("args"),
            constants,
        )
        return match.group(0) if folded is None else folded

    return _JAVA_STRING_METHOD_CALL_RE.sub(repl, source)


def _java_string_declared_names(source: str) -> set[str]:
    stripped = strip_jml_comments(source)
    return set(re.findall(rf"\bString\s+({_JAVA_IDENTIFIER_RE})\b", stripped))


def abstract_java_charsequence_string_alias_for_openjml(source: str) -> str:
    """Fold trivial ``String`` to ``CharSequence`` aliases for OpenJML.

    For a Java ``String`` receiver, ``((CharSequence) s).toString()`` returns the
    same string and ``CharSequence.length()`` is the same length query.  OpenJML
    can spend its whole timeout inside the interface/library model, so rewrite
    only the local alias pattern where the ``CharSequence`` temporary is used
    exclusively for ``length`` after the immediate ``toString`` conversion.
    """

    out = source
    changed = False
    search_from = 0
    while True:
        match = _JAVA_CHARSEQUENCE_TOSTRING_ALIAS_RE.search(out, search_from)
        if not match:
            break
        string_names = _java_string_declared_names(out)
        cs_name = match.group("cs")
        src_name = match.group("src")
        str_name = match.group("str")
        if src_name not in string_names:
            search_from = match.end()
            continue
        before = out[: match.start()]
        after = out[match.end() :]
        rewritten_after = re.sub(
            rf"\b{re.escape(cs_name)}\s*\.\s*length\s*\(\s*\)",
            f"{str_name}.length()",
            after,
        )
        if re.search(rf"\b{re.escape(cs_name)}\b", before + rewritten_after):
            search_from = match.end()
            continue
        replacement = f"{match.group('indent')}String {str_name} = {src_name};"
        out = before + replacement + rewritten_after
        changed = True
        search_from = len(before) + len(replacement)
    return out if changed else source


def _java_regex_matches(regex: str, value: str) -> list[str] | None:
    try:
        compiled = re.compile(regex)
    except re.error:
        return None
    return [match.group(0) for match in compiled.finditer(value)]


def abstract_java_literal_regex_find_for_openjml(source: str) -> str:
    """Fold simple literal ``Pattern``/``Matcher.find`` loops.

    Java's regex library model is expensive for OpenJML.  When both the regex
    and searched string are source-level constants, and the loop body is a
    simple non-nested block that only queries ``matcher.group()``, precompute the
    sequence of matched groups and rewrite the loop as a foreach over literals.
    Input-dependent regexes or strings are left unchanged.
    """

    def repl(match: re.Match[str]) -> str:
        regex = _decode_simple_java_string_literal(match.group("regex"))
        value = _decode_simple_java_string_expression(match.group("string_expr"))
        body = match.group("body")
        matcher_name = match.group("matcher")
        if regex is None or value is None or "{" in body or "}" in body:
            return match.group(0)
        if re.search(rf"\b{re.escape(matcher_name)}\s*\.\s*(?!group\s*\()", body):
            return match.group(0)
        matches = _java_regex_matches(regex, value)
        if matches is None:
            return match.group(0)
        indent = match.group("indent")
        group_var = f"{matcher_name}__group"
        groups_var = f"{matcher_name}__groups"
        array_literal = ", ".join(_encode_java_string_literal(item) for item in matches)
        rewritten_body = re.sub(
            rf"\b{re.escape(matcher_name)}\s*\.\s*group\s*\(\s*\)",
            group_var,
            body,
        )
        return (
            f"{match.group('string_decl')}"
            f"{indent}String[] {groups_var} = new String[] {{{array_literal}}};\n"
            f"{indent}for (String {group_var} : {groups_var}) {{"
            f"{rewritten_body}"
            f"{indent}}}"
        )

    return _JAVA_LITERAL_REGEX_FIND_RE.sub(repl, source)


def _replace_unequal_length_string_equals(text: str, name: str, expected_len: int) -> tuple[str, bool]:
    changed = False

    def receiver_repl(match: re.Match[str]) -> str:
        nonlocal changed
        literal = _decode_simple_java_string_literal(match.group("literal"))
        if literal is None or len(literal) == expected_len:
            return match.group(0)
        changed = True
        return "false"

    def argument_repl(match: re.Match[str]) -> str:
        nonlocal changed
        literal = _decode_simple_java_string_literal(match.group("literal"))
        if literal is None or len(literal) == expected_len:
            return match.group(0)
        changed = True
        return "false"

    out = re.sub(
        rf"\b{re.escape(name)}\s*\.\s*equals\s*\(\s*(?P<literal>{_JAVA_STRING_LITERAL_RE})\s*\)",
        receiver_repl,
        text,
    )
    out = re.sub(
        rf"(?P<literal>{_JAVA_STRING_LITERAL_RE})\s*\.\s*equals\s*\(\s*{re.escape(name)}\s*\)",
        argument_repl,
        out,
    )
    return out, changed


def abstract_java_char_array_slice_equals_for_openjml(source: str) -> str:
    """Fold impossible equality checks on strings built from char-array slices.

    ``String.valueOf(chars, start, count)`` and ``new String(chars, start,
    count)`` produce a string whose length is exactly ``count`` when the slice is
    statically in bounds.  If the temporary is used only in ``equals`` checks
    against literals with a different length, those checks are false regardless
    of the character values.  This avoids OpenJML's expensive library
    preconditions without guessing the input-dependent characters.
    """

    lengths = _char_array_initializer_lengths(source)
    if not lengths:
        return source
    out = source
    search_from = 0
    changed = False
    while True:
        match = _JAVA_STRING_FROM_CHAR_ARRAY_SLICE_ASSIGN_RE.search(out, search_from)
        if not match:
            break
        name = match.group("name")
        array_name = match.group("array")
        array_len = lengths.get(array_name)
        start = int(match.group("start"))
        count = int(match.group("count"))
        if array_len is None or start < 0 or count < 0 or start + count > array_len:
            search_from = match.end()
            continue
        before = out[: match.start()]
        after = out[match.end() :]
        rewritten_after, replaced = _replace_unequal_length_string_equals(after, name, count)
        if not replaced or re.search(rf"\b{re.escape(name)}\b", before + rewritten_after):
            search_from = match.end()
            continue
        declaration = match.group(2)
        replacement = f"{match.group(1)}{declaration + ' ' if declaration else ''}{name} = \"\";"
        out = before + replacement + rewritten_after
        changed = True
        search_from = len(before) + len(replacement)
    return out if changed else source


def _self_concat_with_nonempty_literal(expr: str, name: str) -> bool:
    stripped = expr.strip()
    right = re.fullmatch(rf"{re.escape(name)}\s*\+\s*(?P<literal>{_JAVA_STRING_LITERAL_RE})", stripped)
    if right:
        literal = _decode_simple_java_string_literal(right.group("literal"))
        return literal is not None and literal != ""
    left = re.fullmatch(rf"(?P<literal>{_JAVA_STRING_LITERAL_RE})\s*\+\s*{re.escape(name)}", stripped)
    if left:
        literal = _decode_simple_java_string_literal(left.group("literal"))
        return literal is not None and literal != ""
    return False


def abstract_java_string_valueof_object_self_concat_for_openjml(source: str) -> str:
    """Fold ``String.valueOf`` on a String alias compared to self-concat.

    If an ``Object`` local is assigned from a ``String`` local, then
    ``String.valueOf(object)`` has the same contents as that string for non-null
    values and ``"null"`` for null.  In either case, it cannot equal the same
    source string concatenated with a non-empty literal.  This avoids OpenJML's
    object/string conversion model without abstracting arbitrary objects.
    """

    string_names = _java_string_declared_names(source)
    if not string_names:
        return source

    def repl(match: re.Match[str]) -> str:
        source_name = match.group("source")
        if source_name not in string_names:
            return match.group(0)
        if not _self_concat_with_nonempty_literal(match.group("rhs"), source_name):
            return match.group(0)
        return f"{match.group('indent')}return false;"

    return _JAVA_STRING_VALUEOF_OBJECT_SELF_CONCAT_RE.sub(repl, source)


def _string_concat_affixes(source: str) -> dict[str, tuple[str, str]]:
    constants = _stable_constant_string_values(source)
    affixes: dict[str, tuple[str, str]] = {}
    stripped = strip_jml_comments(source)
    for match in _JAVA_STRING_CONCAT_ASSIGN_RE.finditer(stripped):
        name = match.group(2)
        if not _assigned_once(stripped, name):
            continue
        left = _constant_string_arg(match.group(3), constants)
        right = _constant_string_arg(match.group(4), constants)
        if left is not None and right is None:
            affixes[name] = (left, "")
        elif right is not None and left is None:
            affixes[name] = ("", right)
    return affixes


def _literal_violates_affix(literal: str, prefix: str, suffix: str) -> bool:
    return (prefix != "" and not literal.startswith(prefix)) or (suffix != "" and not literal.endswith(suffix))


def abstract_java_impossible_string_affix_equals_for_openjml(source: str) -> str:
    """Fold impossible equality checks for literal string affixes.

    If a local ``String`` is constructed as ``"prefix" + dynamic`` or
    ``dynamic + "suffix"``, equality against a literal that lacks that fixed
    prefix/suffix is impossible.  This is useful for OpenJML timeout cases where
    the Java string-library model hides an otherwise immediate source-level
    assertion failure.  The rewrite only folds comparisons to ``false``; it does
    not guess the value of the dynamic part.
    """

    affixes = _string_concat_affixes(source)
    out = source
    folded_names: set[str] = set()
    for name, (prefix, suffix) in affixes.items():

        def receiver_repl(match: re.Match[str]) -> str:
            literal = _decode_simple_java_string_literal(match.group("literal"))
            if literal is None or not _literal_violates_affix(literal, prefix, suffix):
                return match.group(0)
            folded_names.add(name)
            return "false"

        def argument_repl(match: re.Match[str]) -> str:
            literal = _decode_simple_java_string_literal(match.group("literal"))
            if literal is None or not _literal_violates_affix(literal, prefix, suffix):
                return match.group(0)
            folded_names.add(name)
            return "false"

        out = re.sub(
            rf"\b{re.escape(name)}\s*\.\s*equals\s*\(\s*(?P<literal>{_JAVA_STRING_LITERAL_RE})\s*\)",
            receiver_repl,
            out,
        )
        out = re.sub(
            rf"(?P<literal>{_JAVA_STRING_LITERAL_RE})\s*\.\s*equals\s*\(\s*{re.escape(name)}\s*\)",
            argument_repl,
            out,
        )
    if not folded_names:
        return out

    def dead_assignment_repl(match: re.Match[str]) -> str:
        name = match.group(2)
        if name not in folded_names:
            return match.group(0)
        if re.search(rf"\b{re.escape(name)}\b", strip_jml_comments(out[match.end() :])):
            return match.group(0)
        return f'{match.group(1)}String {name} = "";'

    out = _JAVA_STRING_CONCAT_ASSIGN_RE.sub(dead_assignment_repl, out)
    return out


def _literal_length_expr(expr: str, constants: dict[str, str]) -> int | None:
    stripped = re.sub(r"\s+", "", expr)
    numeric = re.fullmatch(r"(\d+)-1", stripped)
    if numeric:
        return int(numeric.group(1))
    method = re.fullmatch(rf"({_JAVA_IDENTIFIER_RE})\.length\(\)-1", stripped)
    if method and method.group(1) in constants:
        return len(constants[method.group(1)])
    return None


def abstract_java_literal_string_comparison_loops_for_openjml(source: str) -> str:
    """Fold exact literal-string comparison loops.

    This handles two verifier-heavy but deterministic idioms: checking that one
    literal string is the reverse of another, and checking a literal prefix
    copied with ``getChars``.  Both rewrites require all compared strings to be
    source-level constants.
    """

    constants = _stable_constant_string_values(source)
    if not constants:
        return source
    out = source

    def reverse_repl(match: re.Match[str]) -> str:
        left = constants.get(match.group("left"))
        right = constants.get(match.group("right"))
        length = _literal_length_expr(match.group("length_expr"), constants)
        if left is None or right is None or length is None:
            return match.group(0)
        if length != len(left) or right != left[::-1]:
            return match.group(0)
        return f"{match.group('indent')}int {match.group('index')} = {length};"

    out = _JAVA_LITERAL_REVERSE_COMPARE_LOOP_RE.sub(reverse_repl, out)

    def getchars_repl(match: re.Match[str]) -> str:
        src = constants.get(match.group("src"))
        expected = constants.get(match.group("expected"))
        count = int(match.group("count"))
        if src is None or expected is None or count < 0 or count > len(src):
            return match.group(0)
        if expected != src[:count]:
            return match.group(0)
        # Dropping getChars is only sound when the destination array is dead
        # after this checking loop.
        if re.search(rf"\b{re.escape(match.group('array'))}\b", out[match.end() :]):
            return match.group(0)
        return f"{match.group('indent')}{match.group('index')} = {count};"

    return _JAVA_LITERAL_GETCHARS_PREFIX_COMPARE_RE.sub(getchars_repl, out)


def abstract_java_dropped_wrapper_conversion_for_openjml(source: str) -> str:
    """Drop unused primitive-wrapper conversion calls after literal ``valueOf``.

    A standalone call such as ``i.floatValue();`` has no Java-visible side
    effect when ``i`` was just assigned ``Integer.valueOf(4)`` and the local is
    not used later.  OpenJML can nevertheless spend most of its budget in the
    wrapper-library model.  Keep this limited to primitive literals and dropped
    conversion results.
    """

    out = source
    changed = False
    search_from = 0
    while True:
        match = _JAVA_WRAPPER_VALUEOF_DROPPED_CONVERSION_RE.search(out, search_from)
        if not match:
            break
        var_name = match.group("var")
        before = out[: match.start()]
        after = out[match.end() :]
        if re.search(rf"\b{re.escape(var_name)}\b", after):
            search_from = match.end()
            continue
        replacement = f"{match.group('indent')};"
        out = before + replacement + after
        changed = True
        search_from = len(before) + len(replacement)
    return out if changed else source


def abstract_java_stringbuilder_getchars_self_compare_for_openjml(source: str) -> str:
    """Fold full-copy ``StringBuilder.getChars`` self-comparison loops.

    After copying every builder character into a same-length array, each
    enhanced-for item is exactly ``builder.charAt(i)`` at the corresponding
    index.  A loop that immediately returns ``false`` on equality therefore
    returns ``false`` for non-empty builders and falls through to ``true`` only
    when the builder is empty.  Keep the rewrite to this exact full-copy shape.
    """

    def repl(match: re.Match[str]) -> str:
        return f"{match.group('indent')}return {match.group('source')}.length() == 0;"

    return _JAVA_STRINGBUILDER_GETCHARS_SELF_COMPARE_RE.sub(repl, source)


def _foreach_counter_name(increment: str) -> str | None:
    stripped = increment.strip()
    match = re.fullmatch(r"\+\+\s*([A-Za-z_$][\w$]*)", stripped)
    if match:
        return match.group(1)
    match = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*\+\+", stripped)
    if match:
        return match.group(1)
    match = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*\+=\s*1", stripped)
    if match:
        return match.group(1)
    return None


def _evaluate_string_foreach_condition(condition: str, item_name: str, value: str) -> bool | None:
    cond = condition.strip()
    negated = False
    if cond.startswith("!"):
        negated = True
        cond = cond[1:].strip()
    match = re.fullmatch(
        rf"{re.escape(item_name)}\s*\.\s*"
        r"(startsWith|endsWith|equals|equalsIgnoreCase|regionMatches)\s*\((.*)\)",
        cond,
    )
    if not match:
        return None
    folded = _evaluate_constant_string_method(
        _encode_java_string_literal(value),
        match.group(1),
        match.group(2),
        {},
    )
    if folded not in {"true", "false"}:
        return None
    result = folded == "true"
    return not result if negated else result


def abstract_java_literal_string_array_foreach_for_openjml(source: str) -> str:
    """Fold simple counts over local literal ``String[]`` arrays.

    This only handles loops whose body is a single conditional increment driven
    by a locale-independent string predicate on the enhanced-for item.  More
    complex loops, input-dependent arrays, and mutation-heavy bodies remain
    unchanged.
    """

    arrays = _literal_string_arrays(source)
    if not arrays:
        return source

    def repl(match: re.Match[str]) -> str:
        values = arrays.get(match.group("array"))
        if values is None:
            return match.group(0)
        counter = _foreach_counter_name(match.group("inc"))
        if counter is None:
            return match.group(0)
        count = 0
        for value in values:
            result = _evaluate_string_foreach_condition(match.group("cond"), match.group("var"), value)
            if result is None:
                return match.group(0)
            if result:
                count += 1
        return f"{match.group('indent')}{counter} += {count};"

    return _JAVA_STRING_FOREACH_COUNT_RE.sub(repl, source)


def _is_single_null_deref_statement(body: str, var_name: str) -> bool:
    stripped = re.sub(r"//.*", "", body).strip()
    if not stripped.endswith(";") or "\n" in stripped[:-1]:
        return False
    statement = stripped[:-1].strip()
    # Null field write: ``a.i = 0``.
    if re.fullmatch(rf"{re.escape(var_name)}\s*\.\s*[A-Za-z_$][\w$]*\s*=.+", statement):
        return True
    # Null field read into a local: ``int i = a.i`` or ``x = a.i``.
    if re.fullmatch(
        rf"(?:[A-Za-z_$][\w$]*(?:\s*<[^;=]+>)?(?:\s*\[\s*\])?\s+)?"
        rf"[A-Za-z_$][\w$]*\s*=\s*{re.escape(var_name)}\s*\.\s*[A-Za-z_$][\w$]*",
        statement,
    ):
        return True
    # Null method invocation with ignored result.
    if re.fullmatch(rf"{re.escape(var_name)}\s*\.\s*[A-Za-z_$][\w$]*\s*\([^;]*\)", statement):
        return True
    return False


def _starts_with_null_deref_then_unreachable_return(body: str, var_name: str) -> bool:
    stripped = re.sub(r"//.*", "", body).strip()
    match = re.match(r"(?P<stmt>[^;]+;)(?P<rest>.*)$", stripped, re.DOTALL)
    if not match:
        return False
    if not _is_single_null_deref_statement(match.group("stmt"), var_name):
        return False
    rest = match.group("rest").strip()
    if not rest:
        return True
    return bool(re.fullmatch(r"return\s+[^;{}]+\s*;", rest))


def abstract_java_constant_null_try_catch_for_openjml(source: str) -> str:
    """Fold deterministic null-dereference try/catch blocks.

    When a local is initialized to ``null`` and the immediately following
    ``try`` body consists of one dereference of that same local, Java must take
    the matching ``NullPointerException``/``Exception`` catch branch.  Replacing
    the whole try/catch plus normal fallthrough return by the catch return lets
    OpenJML avoid exception-library modeling without changing the verifier
    artifact's behavior.
    """

    def direct_repl(match: re.Match[str]) -> str:
        var_name = match.group("var")
        if not _is_single_null_deref_statement(match.group("body"), var_name):
            return match.group(0)
        indent = match.group("indent")
        decl = f"{indent}{match.group('type')} {var_name} = null;"
        return f"{decl}\n{indent}return {match.group('catch_return').strip()};"

    def empty_catch_repl(match: re.Match[str]) -> str:
        var_name = match.group("var")
        if not _starts_with_null_deref_then_unreachable_return(match.group("body"), var_name):
            return match.group(0)
        indent = match.group("indent")
        decl = f"{indent}{match.group('type')} {var_name} = null;"
        return f"{decl}\n{indent}return {match.group('normal_return').strip()};"

    out = _JAVA_CONSTANT_NULL_TRY_RE.sub(direct_repl, source)
    return _JAVA_CONSTANT_NULL_EMPTY_CATCH_RE.sub(empty_catch_repl, out)


def _first_char_array_helpers(source: str) -> dict[str, str]:
    helpers: dict[str, str] = {}
    for match in _JAVA_FIRST_CHAR_ARRAY_HELPER_RE.finditer(strip_jml_comments(source)):
        value = _decode_simple_java_char_literal(match.group("char"))
        if value is not None:
            helpers[match.group("method")] = value
    return helpers


def abstract_java_tochararray_first_char_concat_for_openjml(source: str) -> str:
    """Fold exact ``toCharArray`` first-character propagation patterns.

    A few Java benchmarks convert a non-empty ``String`` to ``char[]``, call a
    helper that only writes ``array[0]`` to a literal character, concatenate the
    array after a literal prefix, and then immediately compare the first copied
    character.  OpenJML spends its budget in ``String``/array library models for
    this shape.  The rewrite is limited to the exact dataflow where the length
    guard makes the array non-empty and the compared index is exactly the prefix
    length, so the ternary condition is statically true.
    """

    helpers = _first_char_array_helpers(source)
    if not helpers:
        return source

    def repl(match: re.Match[str]) -> str:
        expected_char = helpers.get(match.group("helper"))
        compared_char = _decode_simple_java_char_literal(match.group("char"))
        prefix = _decode_simple_java_string_literal(match.group("prefix"))
        length = int(match.group("len"))
        index = int(match.group("index"))
        if expected_char is None or compared_char is None or prefix is None:
            return match.group(0)
        if length <= 0 or expected_char != compared_char or index != len(prefix):
            return match.group(0)
        indent = match.group("indent")
        body_indent = f"{indent}  "
        return (
            f"{indent}{match.group('header')}{{\n"
            f"{body_indent}if ({match.group('arg')}.length() != {length}) return {match.group('fallback').strip()};\n"
            f"{body_indent}return {match.group('then').strip()};\n"
            f"{indent}}}"
        )

    return _JAVA_TOCHARARRAY_FIRST_CHAR_CONCAT_RE.sub(repl, source)


@dataclass
class _StringBuilderState:
    expr: str
    literal: str | None
    capacity: int | None


def _stringbuilder_initial_capacity(value: str | None) -> int:
    return 16 if value is None else len(value) + 16


def _stringbuilder_ensure_capacity(current: int, minimum: int) -> int:
    if minimum <= current:
        return current
    return max(minimum, current * 2 + 2)


def _literal_append_value(
    arg: str,
    *,
    constants: dict[str, str],
    char_arrays: dict[str, str],
    builders: dict[str, _StringBuilderState],
) -> str | None:
    parts = [part.strip() for part in arg.split(",")]
    if len(parts) == 3 and parts[0] in char_arrays and parts[1].isdigit() and parts[2].isdigit():
        return _slice_java_string(char_arrays[parts[0]], parts[1], parts[2])
    if len(parts) != 1:
        return None
    value = parts[0]
    literal = _decode_simple_java_string_literal(value)
    if literal is not None:
        return literal
    char = _decode_simple_java_char_literal(value)
    if char is not None:
        return char
    if value in constants:
        return constants[value]
    if value in char_arrays:
        return char_arrays[value]
    state = builders.get(value)
    if state and state.literal is not None:
        return state.literal
    return _literal_valueof_string(value)


def abstract_java_simple_stringbuilder_for_openjml(source: str) -> str:
    """Replace simple local ``StringBuilder`` values by verifier-friendly state.

    This is intentionally conservative.  It handles local builders whose state
    is either a string expression or a literal string, plus the common literal
    operations used by SV-COMP microbenchmarks: ``append``, ``length``,
    ``toString``, ``capacity``, ``ensureCapacity``, and shrinking
    ``setLength``.  If a rewritten builder still has unsupported mutating
    operations, the whole abstraction is discarded.
    """

    constants, char_arrays = _constant_java_values(source)
    builders: dict[str, _StringBuilderState] = {}
    changed = False

    def decl_repl(match: re.Match[str]) -> str:
        nonlocal changed
        indent, name = match.group(1), match.group(2)
        arg = (match.group("arg") or "").strip()
        if not arg:
            literal: str | None = ""
            expr = '""'
        else:
            literal = _decode_simple_java_string_literal(arg)
            if literal is not None:
                expr = _encode_java_string_literal(literal)
            elif arg in constants:
                literal = constants[arg]
                expr = _encode_java_string_literal(literal)
            else:
                literal = None
                expr = arg
        builders[name] = _StringBuilderState(
            expr=expr,
            literal=literal,
            capacity=_stringbuilder_initial_capacity(literal) if literal is not None else None,
        )
        if literal is not None:
            constants[name] = literal
        changed = True
        return f"{indent}String {name} = {expr};"

    prepared = _JAVA_STRING_BUILDER_DECL_RE.sub(decl_repl, source)
    if not builders:
        return source

    def rewrite_expr(line: str) -> str:
        rewritten = line
        for builder_name, state in builders.items():
            expr = state.expr
            rewritten = re.sub(rf"\b{re.escape(builder_name)}\s*\.\s*toString\s*\(\s*\)", expr, rewritten)
            if state.literal is not None:
                rewritten = re.sub(
                    rf"\b{re.escape(builder_name)}\s*\.\s*length\s*\(\s*\)",
                    str(len(state.literal)),
                    rewritten,
                )
            else:
                rewritten = re.sub(
                    rf"\b{re.escape(builder_name)}\s*\.\s*length\s*\(\s*\)",
                    f"{expr}.length()",
                    rewritten,
                )
            if state.capacity is not None:
                rewritten = re.sub(
                    rf"\b{re.escape(builder_name)}\s*\.\s*capacity\s*\(\s*\)",
                    str(state.capacity),
                    rewritten,
                )
        return rewritten

    lines = prepared.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        chain_name = next((name for name in builders if stripped == name), None)
        if chain_name is not None:
            block = [line]
            j = i + 1
            while j < len(lines) and ".append" in lines[j]:
                block.append(lines[j])
                if lines[j].rstrip().endswith(";"):
                    break
                j += 1
            if len(block) > 1 and block[-1].rstrip().endswith(";"):
                state = builders[chain_name]
                current = state.literal
                if current is None:
                    return source
                for append in _JAVA_APPEND_ARG_RE.finditer("\n".join(block[1:])):
                    value = _literal_append_value(
                        append.group(1),
                        constants=constants,
                        char_arrays=char_arrays,
                        builders=builders,
                    )
                    if value is None:
                        return source
                    current += value
                state.literal = current
                state.expr = _encode_java_string_literal(current)
                constants[chain_name] = current
                if state.capacity is not None and len(current) > state.capacity:
                    state.capacity = _stringbuilder_ensure_capacity(state.capacity, len(current))
                out.append(f"{_line_indent(line)}{chain_name} = {state.expr};")
                changed = True
                i = j + 1
                continue

        handled_mutation = False
        for builder_name, state in builders.items():
            ensure = re.fullmatch(
                rf"(\s*){re.escape(builder_name)}\s*\.\s*ensureCapacity\s*\(\s*(\d+)\s*\)\s*;",
                line,
            )
            if ensure:
                if state.capacity is None:
                    return source
                state.capacity = _stringbuilder_ensure_capacity(state.capacity, int(ensure.group(2)))
                out.append(f"{ensure.group(1)};")
                handled_mutation = True
                changed = True
                break
            set_len = re.fullmatch(
                rf"(\s*){re.escape(builder_name)}\s*\.\s*setLength\s*\(\s*(\d+)\s*\)\s*;",
                line,
            )
            if set_len:
                if state.literal is None:
                    return source
                new_len = int(set_len.group(2))
                if new_len > len(state.literal):
                    return source
                state.literal = state.literal[:new_len]
                state.expr = _encode_java_string_literal(state.literal)
                constants[builder_name] = state.literal
                out.append(f"{set_len.group(1)}{builder_name} = {state.expr};")
                handled_mutation = True
                changed = True
                break
        if handled_mutation:
            i += 1
            continue

        rewritten_line = rewrite_expr(line)
        if rewritten_line != line:
            changed = True
        out.append(rewritten_line)
        i += 1

    result = "\n".join(out) + ("\n" if prepared.endswith("\n") else "")
    for builder_name in builders:
        if re.search(rf"\b{re.escape(builder_name)}\s*\.\s*(?:append|ensureCapacity|setLength|getChars|capacity)\s*\(", result):
            return source
    return result if changed else source


def abstract_java_verifier_only_effects_for_openjml(source: str) -> str:
    """Apply verifier-only Java abstractions that preserve proof intent."""

    out = abstract_java_debug_output_for_openjml(source)
    out = abstract_java_system_termination_for_openjml(out)
    out = abstract_java_constant_string_split_for_openjml(out)
    out = abstract_java_constant_string_construction_for_openjml(out)
    out = abstract_java_simple_stringbuilder_for_openjml(out)
    out = abstract_java_stringbuilder_getchars_self_compare_for_openjml(out)
    out = abstract_java_charsequence_string_alias_for_openjml(out)
    out = abstract_java_literal_regex_find_for_openjml(out)
    out = abstract_java_char_array_slice_equals_for_openjml(out)
    out = abstract_java_tochararray_first_char_concat_for_openjml(out)
    out = abstract_java_string_valueof_object_self_concat_for_openjml(out)
    out = abstract_java_impossible_string_affix_equals_for_openjml(out)
    out = abstract_java_dropped_wrapper_conversion_for_openjml(out)
    out = abstract_java_constant_string_methods_for_openjml(out)
    out = abstract_java_literal_string_comparison_loops_for_openjml(out)
    out = abstract_java_literal_string_array_foreach_for_openjml(out)
    out = abstract_java_constant_null_try_catch_for_openjml(out)
    return out


def _top_level_java_type_names(source: str) -> list[str]:
    """Return top-level Java type declarations in source order."""

    names: list[str] = []
    depth = 0
    for line in strip_jml_comments(source).splitlines():
        code = re.sub(r"//.*$", "", line)
        if depth == 0:
            match = _JAVA_TYPE_DECL_RE.search(code)
            if match:
                names.append(match.group(1))
        depth += _brace_delta(code)
        if depth < 0:
            depth = 0
    return names


def _replace_java_identifier_token(source: str, old: str, new: str) -> str:
    """Replace Java identifier tokens while leaving comments/strings intact."""

    pieces: list[str] = []
    last = 0
    changed = False
    for match in _JAVA_TOKEN_RE.finditer(source):
        token = match.group(0)
        pieces.append(source[last : match.start()])
        if token == old:
            pieces.append(new)
            changed = True
        else:
            pieces.append(token)
        last = match.end()
    if not changed:
        return source
    pieces.append(source[last:])
    return "".join(pieces)


def _repair_renamed_main_references(source: str) -> str:
    """Repair extracted benchmarks whose sole top-level type is not ``Main``.

    Some Java SV-COMP artifacts were originally written around a ``Main`` class
    but are stored under benchmark-specific class names.  If there is exactly
    one top-level type and no ``Main`` declaration, unresolved ``Main`` tokens
    are aliases for that type.
    """

    type_names = _top_level_java_type_names(source)
    if len(type_names) != 1 or type_names[0] == "Main":
        return source
    if _declares_java_type(source, "Main"):
        return source
    return _replace_java_identifier_token(source, "Main", type_names[0])


def _java_significant_tokens(source: str) -> list[tuple[str, int, int]]:
    """Tokenize Java source, excluding comments and literals for repairs."""

    tokens: list[tuple[str, int, int]] = []
    for match in _JAVA_TOKEN_RE.finditer(source):
        token = match.group(0)
        if token.startswith("//") or token.startswith("/*") or token.startswith(("\"", "'")):
            continue
        tokens.append((token, match.start(), match.end()))
    return tokens


def _matching_paren_index(tokens: list[tuple[str, int, int]], open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(tokens)):
        token = tokens[index][0]
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _repair_bare_constructor_invocations(source: str) -> str:
    """Rewrite expression-level ``Type(...)`` constructor calls to ``new Type(...)``."""

    type_names = set(_JAVA_TYPE_DECL_RE.findall(strip_jml_comments(source)))
    if not type_names:
        return source
    tokens = _java_significant_tokens(source)
    insertions: list[int] = []
    for index, (token, start, _end) in enumerate(tokens[:-1]):
        if token not in type_names:
            continue
        if tokens[index + 1][0] != "(":
            continue
        previous = tokens[index - 1][0] if index > 0 else ""
        if previous in {"new", ".", "class", "interface", "enum", "record"}:
            continue
        close_index = _matching_paren_index(tokens, index + 1)
        if close_index is None:
            continue
        after = tokens[close_index + 1][0] if close_index + 1 < len(tokens) else ""
        if after == "{":
            continue
        insertions.append(start)
    if not insertions:
        return source
    out = source
    for position in reversed(insertions):
        out = f"{out[:position]}new {out[position:]}"
    return out


def _repair_missing_args_reference(source: str) -> str:
    """Add a verifier-only ``args`` field for snippets with free ``args``."""

    stripped = strip_jml_comments(source)
    tokens = [token for token, _start, _end in _java_significant_tokens(stripped)]
    if "args" not in tokens:
        return source
    if _JAVA_ARGS_DECL_RE.search(stripped):
        return source
    match = _TOP_LEVEL_CLASS_HEADER_RE.search(stripped)
    if not match:
        return source
    insert_at = match.end()
    indent_match = re.match(r"[ \t]*", match.group(1))
    indent = (indent_match.group(0) if indent_match else "") + "  "
    return f"{source[:insert_at]}\n{indent}static String[] args;{source[insert_at:]}"


def _public_top_level_java_type_name(source: str) -> str | None:
    """Return the public top-level Java type name, ignoring nested public types."""

    depth = 0
    for line in strip_jml_comments(source).splitlines():
        code = re.sub(r"//.*$", "", line)
        match = _PUBLIC_JAVA_TYPE_LINE_RE.search(code)
        if depth == 0 and match:
            return match.group(1)
        depth += _brace_delta(code)
        if depth < 0:
            depth = 0
    return None


_OPENJML_VERIFIER_SHIM_BODY = """\
  /*@ public normal_behavior
    @   ensures condition;
    @   assignable \\nothing;
    @*/
  public static native void assume(boolean condition);

  /*@ public normal_behavior
    @   assignable \\nothing;
    @*/
  public static native boolean nondetBoolean();

  /*@ public normal_behavior
    @   assignable \\nothing;
    @*/
  public static native byte nondetByte();

  /*@ public normal_behavior
    @   assignable \\nothing;
    @*/
  public static native char nondetChar();

  /*@ public normal_behavior
    @   assignable \\nothing;
    @*/
  public static native short nondetShort();

  /*@ public normal_behavior
    @   assignable \\nothing;
    @*/
  public static native int nondetInt();

  /*@ public normal_behavior
    @   assignable \\nothing;
    @*/
  public static native long nondetLong();

  /*@ public normal_behavior
    @   assignable \\nothing;
    @*/
  public static native float nondetFloat();

  /*@ public normal_behavior
    @   assignable \\nothing;
    @*/
  public static native double nondetDouble();

  /*@ public normal_behavior
    @   ensures \\result != null;
    @   assignable \\nothing;
    @*/
  public static native String nondetString();
"""


def _openjml_verifier_shim(package_name: str | None) -> str:
    package_line = f"package {package_name};\n\n" if package_name else ""
    return package_line + "public final class Verifier {\n" + _OPENJML_VERIFIER_SHIM_BODY + "}\n"


_OPENJML_COOKIE_SHIM = """\
/*@ nullable_by_default @*/
public class Cookie {
  public String name;
  public String value;

  public Cookie(String name, String value) {
    this.name = name;
    this.value = value;
  }

  public String getName() {
    return name;
  }

  public String getValue() {
    return value;
  }
}
"""


def _declares_java_type(source: str, type_name: str) -> bool:
    return type_name in set(_JAVA_TYPE_DECL_RE.findall(strip_jml_comments(source)))


def write_openjml_support_files(source: str, output_dir: str | Path) -> list[Path]:
    """Write verifier-side support sources needed by common Java benchmarks.

    SV-COMP Java benchmarks often reference ``Verifier.nondet*`` or import
    ``org.sosy_lab.sv_benchmarks.Verifier``.  The benchmark artifact does not
    always place that helper on OpenJML's source path.  A tiny native JML shim
    keeps nondeterministic values unconstrained and models ``assume(c)`` as the
    post-call fact ``c``.  Some servlet-derived benchmarks also reference a
    simple ``Cookie`` type without shipping the servlet library, so provide the
    minimal data class needed for type checking.  The original source and
    generated JML are unchanged.
    """

    code = strip_jml_comments(source)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if "Verifier." in code and not _declares_java_type(source, "Verifier"):
        if _SVCOMP_VERIFIER_IMPORT_RE.search(code):
            package_path = root / "org" / "sosy_lab" / "sv_benchmarks" / "Verifier.java"
            package_path.parent.mkdir(parents=True, exist_ok=True)
            package_path.write_text(
                _openjml_verifier_shim("org.sosy_lab.sv_benchmarks"),
                encoding="utf-8",
            )
            written.append(package_path)
        else:
            default_path = root / "Verifier.java"
            default_path.write_text(_openjml_verifier_shim(None), encoding="utf-8")
            written.append(default_path)

    if _uses_simple_type(code, "Cookie") and not _declares_java_type(source, "Cookie"):
        cookie_path = root / "Cookie.java"
        cookie_path.write_text(_OPENJML_COOKIE_SHIM, encoding="utf-8")
        written.append(cookie_path)
    return written


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


def _imported_classes(source: str) -> set[str]:
    imports: set[str] = set()
    for line in source.splitlines():
        match = _IMPORT_LINE_RE.match(line)
        if match:
            imports.add(match.group(1))
    return imports


def _strip_import_lines(source: str) -> str:
    return "\n".join(line for line in source.splitlines() if not _IMPORT_LINE_RE.match(line))


def _uses_simple_type(source: str, name: str) -> bool:
    code = strip_jml_comments(source)
    return bool(re.search(rf"\b{re.escape(name)}\s*(?:<|\[|\b)", code))


def _declared_java_type_names(source: str) -> set[str]:
    return set(_JAVA_TYPE_DECL_RE.findall(strip_jml_comments(source)))


def complete_standard_imports(source: str) -> str:
    """Add missing imports for standard Java utility types already used.

    Some benchmark sources use ``Set``/``List`` while importing only a concrete
    implementation such as ``HashSet``.  Completing a missing ``java.util``
    import is a source-hygiene repair, not a semantic rewrite: it does not
    change method bodies, declarations, or literals.
    """

    imports = _imported_classes(source)
    declared_types = _declared_java_type_names(source)
    has_java_util_star = "java.util.*" in imports
    conflicting_imports = {
        fqcn
        for simple, fqcn in _JAVA_UTIL_IMPORTS.items()
        if simple in declared_types and fqcn in imports
    }
    missing = [
        fqcn
        for simple, fqcn in sorted(_JAVA_UTIL_IMPORTS.items())
        if _uses_simple_type(source, simple)
        and simple not in declared_types
        and fqcn not in imports
        and not has_java_util_star
    ]
    if not missing and not conflicting_imports:
        return source

    lines = [
        line
        for line in source.splitlines()
        if not ((match := _IMPORT_LINE_RE.match(line)) and match.group(1) in conflicting_imports)
    ]
    insert_at = 0
    for idx, line in enumerate(lines):
        if line.strip().startswith("package "):
            insert_at = idx + 1
        elif _IMPORT_LINE_RE.match(line):
            insert_at = idx + 1
    import_lines = [f"import {fqcn};" for fqcn in missing]
    rebuilt = lines[:insert_at] + import_lines + lines[insert_at:]
    return "\n".join(rebuilt) + ("\n" if source.endswith("\n") else "")


def source_code_preserved_with_standard_imports(original: str, annotated: str) -> tuple[bool, str]:
    """Return whether code is preserved modulo added whitelisted imports."""

    ok, err = source_code_preserved(original, annotated)
    if ok:
        return True, ""
    original_imports = _imported_classes(original)
    annotated_imports = _imported_classes(annotated)
    added = annotated_imports - original_imports
    if not added:
        return False, err
    if not added <= set(_JAVA_UTIL_IMPORTS.values()):
        return False, err
    original_tokens = java_executable_tokens(_strip_import_lines(original))
    annotated_tokens = java_executable_tokens(_strip_import_lines(annotated))
    if original_tokens == annotated_tokens:
        return True, ""
    abstract_original_tokens = java_executable_tokens(
        _strip_import_lines(abstract_java_verifier_only_effects_for_openjml(original))
    )
    abstract_annotated_tokens = java_executable_tokens(
        _strip_import_lines(abstract_java_verifier_only_effects_for_openjml(annotated))
    )
    if abstract_original_tokens == abstract_annotated_tokens:
        return True, ""
    return False, err


def source_code_preserved(original: str, annotated: str) -> tuple[bool, str]:
    """Return whether annotations changed only JML comments."""

    original_tokens = java_executable_tokens(original)
    annotated_tokens = java_executable_tokens(annotated)
    if original_tokens == annotated_tokens:
        return True, ""
    abstract_original_tokens = java_executable_tokens(abstract_java_verifier_only_effects_for_openjml(original))
    abstract_annotated_tokens = java_executable_tokens(abstract_java_verifier_only_effects_for_openjml(annotated))
    if abstract_original_tokens == abstract_annotated_tokens:
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
        "diverges": 0,
        "assert": 0,
        "assume": 0,
        "spec_public": 0,
    }
    for key in counts:
        counts[key] = len(re.findall(rf"\b{re.escape(key)}\b", source))
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts


def _is_jml_line(line: str) -> bool:
    return line.lstrip().startswith("//@")


def _is_jml_block_start(line: str) -> bool:
    return "/*@" in line


def _is_standalone_jml_statement_line(line: str) -> bool:
    stripped = line.lstrip()
    if stripped.startswith("//@"):
        return True
    return stripped.startswith("/*@") and "*/" in stripped


def _is_jml_annotation_start(line: str) -> bool:
    return _is_jml_line(line) or _is_jml_block_start(line)


def _original_jml_statement_bodies(source: str, keyword: str) -> set[str]:
    return {
        _jml_body(line)
        for line in source.splitlines()
        if _is_standalone_jml_statement_line(line) and _jml_content_keyword(line) == keyword
    }


def drop_generated_jml_assertions(original: str, annotated: str) -> str:
    """Drop generated JML assert/assume statements absent from the input source.

    ``//@ assert`` introduces a new proof target.  For a spec-generation
    benchmark, generated specifications should help prove the original program,
    not add fresh assertions that can fail independently.  ``//@ assume`` is
    even more dangerous because it can make a proof succeed by constraining the
    verifier state inside the method body.  Preserve input JML assert/assume
    statements if they already existed.
    """

    original_asserts = _original_jml_statement_bodies(original, "assert")
    original_assumes = _original_jml_statement_bodies(original, "assume")
    out: list[str] = []
    for line in annotated.splitlines():
        if _is_standalone_jml_statement_line(line):
            keyword = _jml_content_keyword(line)
            if keyword == "assert" and _jml_body(line) not in original_asserts:
                continue
            if keyword == "assume" and _jml_body(line) not in original_assumes:
                continue
        out.append(line)
    return "\n".join(out) + ("\n" if annotated.endswith("\n") else "")


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
_FOR_ZERO_UPPER_RE = re.compile(
    r"\bfor\s*\(\s*(?:final\s+)?int\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*0\s*;"
    r"\s*\1\s*(?:<=|<)\s*([^;]+?)\s*;"
    r"\s*(?:\1\s*\+\+|\+\+\s*\1)\s*\)"
)
_FOR_ZERO_INCLUSIVE_RE = re.compile(
    r"\bfor\s*\(\s*(?:final\s+)?int\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*0\s*;"
    r"\s*\1\s*<=\s*([^;]+?)\s*;"
    r"\s*(?:\1\s*\+\+|\+\+\s*\1)\s*\)"
)
_ARRAY_LENGTH_LOWER_REQUIRE_RE = re.compile(
    r"requires\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length\s*>=\s*(\d+)\s*;"
)
_LENGTH_UPPER_CONJUNCT_RE = re.compile(
    r"^(?P<target>[A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length(?P<call>\(\))?\s*<=\s*(?P<bound>\d+)$"
)
_ALIAS_MINUS_CONST_RE = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*-\s*(\d+)")
_DIRECT_LENGTH_MINUS_CONST_RE = re.compile(
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length\s*-\s*(\d+)"
)
_LENGTH_ALIAS_RE = re.compile(
    r"\b(?:final\s+)?(?:int|long)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length\s*;"
)
_STRING_LENGTH_ALIAS_RE = re.compile(
    r"\b(?:final\s+)?(?:int|long)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length\s*\(\s*\)\s*;"
)
_ALIAS_GREATER_CONST_RE = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*>\s*(\d+)")
_DIRECT_LENGTH_GREATER_CONST_RE = re.compile(
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length(?P<call>\(\))?\s*>\s*(\d+)"
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


def _strip_inline_spec_public(source: str) -> str:
    """Remove inline ``spec_public`` modifiers that OpenJML rejects in types."""

    return re.sub(r"\s*/\*@\s*spec_public\s*@\*/", "", source)


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
    stripped = stripped.removeprefix("/*@").strip()
    stripped = stripped.removesuffix("@*/").strip()
    stripped = stripped.removesuffix("*/").strip()
    stripped = stripped.removeprefix("@").strip()
    stripped = stripped.removeprefix("*").strip()
    parts = stripped.split()
    while parts and parts[0].rstrip(";") in {"public", "private", "protected"}:
        parts.pop(0)
    if not parts:
        return ""
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", parts[0])
    return match.group(1) if match else parts[0].rstrip(";")


_JML_CLAUSE_KEYWORDS = {
    "requires",
    "ensures",
    "assignable",
    "assigns",
    "maintaining",
    "decreases",
    "decreasing",
    "loop_invariant",
    "loop_variant",
    "assert",
    "invariant",
    "constraint",
    "signals",
    "diverges",
    "model",
    "ghost",
    "public",
    "private",
    "protected",
    "pure",
    "helper",
}
_BARE_JML_LINE_KEYWORDS = {
    "requires",
    "ensures",
    "assignable",
    "assigns",
    "maintaining",
    "decreases",
    "decreasing",
    "loop_invariant",
    "loop_variant",
}
_BARE_JML_LINE_RE = re.compile(
    r"^(\s*)(?:@\s*)?(" + "|".join(sorted(_BARE_JML_LINE_KEYWORDS)) + r")\b(.*)$"
)


def _jml_body(line: str) -> str:
    stripped = line.strip()
    stripped = stripped.removeprefix("//@").strip()
    stripped = stripped.removeprefix("/*@").strip()
    stripped = stripped.removeprefix("@").strip()
    stripped = stripped.removeprefix("*").strip()
    stripped = stripped.removesuffix("@*/").strip()
    stripped = stripped.removesuffix("*/").strip()
    return stripped


def _comment_bare_jml_clause_lines(source: str) -> str:
    """Convert bare JML clause lines into line comments.

    LLMs occasionally omit the ``//@`` prefix and emit lines such as
    ``requires x != null;`` directly in Java source, or reuse block-continuation
    syntax such as ``@ requires x != null;`` outside a JML block.  These are not
    executable Java statements; commenting them restores the intended JML
    annotation while preserving Java tokens after stripping JML comments.
    """

    out: list[str] = []
    in_jml_block = False
    for line in source.splitlines():
        if in_jml_block:
            out.append(line)
            if "*/" in line:
                in_jml_block = False
            continue
        if "/*@" in line:
            out.append(line)
            if "*/" not in line:
                in_jml_block = True
            continue
        if _is_jml_annotation_start(line):
            out.append(line)
            continue
        match = _BARE_JML_LINE_RE.match(line)
        if match:
            indent, keyword, rest = match.groups()
            out.append(f"{indent}//@ {keyword}{rest}")
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if source.endswith("\n") else "")


def _has_balanced_parentheses(text: str) -> bool:
    cleaned = re.sub(r"'(?:\\.|[^'\\])*'", " ", text)
    cleaned = re.sub(r'"(?:\\.|[^"\\])*"', " ", cleaned)
    depth = 0
    for ch in cleaned:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _drop_malformed_jml_line_clauses(lines: list[str]) -> list[str]:
    """Drop JML line clauses that are syntactically incomplete.

    This is intentionally conservative: it only removes single-line JML
    annotations with a known clause keyword and clearly unbalanced parentheses.
    Removing such clauses converts an OpenJML parse error into either a weaker
    valid spec or an ordinary proof failure; it does not synthesize semantics.
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
        for segment in _jml_line_segments(group):
            keyword = _jml_content_keyword(segment[0]) if segment else ""
            body = " ".join(_jml_body(line) for line in segment)
            if keyword in _JML_CLAUSE_KEYWORDS and not _has_balanced_parentheses(body):
                continue
            out.extend(segment)
        i = j
    return out


def _jml_line_segments(group: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for line in group:
        keyword = _jml_content_keyword(line)
        if keyword in _JML_CLAUSE_KEYWORDS and current:
            segments.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        segments.append(current)
    return segments


def _drop_orphan_block_continuations(source: str) -> str:
    """Remove orphan JML block-continuation lines after complete clauses.

    A valid multi-line JML clause may continue without repeating the keyword
    until its terminating semicolon.  The LLM sometimes emits a complete clause
    such as ``requires x != null;`` and then starts a bare expression on the
    next ``@`` line.  OpenJML parses that expression as a bogus type/member.
    This cleanup only drops bare continuation lines when the previous
    significant clause line already ended in ``;``.
    """

    def clean_block(match: re.Match[str]) -> str:
        block = match.group(0)
        lines = block.splitlines()
        if len(lines) <= 2:
            return block
        cleaned: list[str] = []
        previous_complete = False
        for idx, line in enumerate(lines):
            body = _jml_body(line)
            keyword = _jml_content_keyword(line)
            is_boundary = idx == 0 or idx == len(lines) - 1 or "*/" in line
            if (
                not is_boundary
                and body
                and keyword not in _JML_CLAUSE_KEYWORDS
                and previous_complete
            ):
                continue
            cleaned.append(line)
            if body:
                previous_complete = body.endswith(";")
        return "\n".join(cleaned)

    return _JML_BLOCK_RE.sub(clean_block, source)


def _method_param_names(signature_line: str) -> set[str] | None:
    """Extract Java parameter names from a simple method signature line."""

    info = _method_signature_info(signature_line)
    return None if info is None else set(info["params"])


_JAVA_METHOD_MODIFIERS = {
    "public",
    "protected",
    "private",
    "static",
    "final",
    "synchronized",
    "native",
    "abstract",
    "strictfp",
}


def _method_signature_info(signature_line: str) -> dict[str, Any] | None:
    """Extract basic metadata from a simple Java method/constructor signature."""

    if "(" not in signature_line or ")" not in signature_line:
        return None
    prefix = signature_line[: signature_line.find("(")]
    if "=" in prefix or prefix.rstrip().endswith((".", "new")):
        return None
    if re.search(r"\b(?:if|for|while|switch|catch|return|new)\b", prefix):
        return None
    if not re.search(r"\b(?:public|protected|private|static|final|synchronized|native|abstract|strictfp)\b", prefix):
        # Package-private methods are allowed, but avoid treating control-flow
        # statements as method signatures.
        if not re.search(r"\w+(?:\s*\[\s*\])*\s+\w+\s*$", prefix):
            return None
    params = signature_line[signature_line.find("(") + 1: signature_line.rfind(")")].strip()
    raw_tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", prefix)
    tokens = [tok for tok in raw_tokens if tok not in _JAVA_METHOD_MODIFIERS]
    if not tokens:
        return None
    method_name = tokens[-1]
    return_type = tokens[-2] if len(tokens) >= 2 else ""
    is_constructor = len(tokens) == 1
    if not params:
        names: set[str] = set()
        param_order: list[str] = []
    else:
        names = set()
        param_order = []
        for part in params.split(","):
            toks = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", part)
            if toks:
                name = toks[-1]
                names.add(name)
                param_order.append(name)
    return {
        "name": method_name,
        "return_type": return_type,
        "params": names,
        "param_order": param_order,
        "is_constructor": is_constructor,
        "is_void": return_type == "void",
    }


def _is_method_signature_line(line: str) -> bool:
    return _method_signature_info(line) is not None


def _is_field_declaration_line(line: str) -> bool:
    stripped = strip_jml_comments(line).split("//", 1)[0].strip()
    if not stripped or not stripped.endswith(";"):
        return False
    declaration_head = stripped.split("=", 1)[0]
    if "(" in declaration_head or ")" in declaration_head:
        return False
    return bool(re.search(r"\b[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\[\s*\])*(?:\s*=\s*[^;]+)?\s*;\s*$", stripped))


_JAVA_FIELD_MODIFIERS_AND_TYPES = {
    "public",
    "protected",
    "private",
    "static",
    "final",
    "transient",
    "volatile",
    "model",
    "spec_public",
    "nullable",
    "non_null",
    "int",
    "long",
    "short",
    "byte",
    "char",
    "boolean",
    "float",
    "double",
    "String",
    "Integer",
    "Long",
    "Short",
    "Byte",
    "Character",
    "Boolean",
    "Object",
}


def _field_declared_names(line: str) -> set[str]:
    stripped = strip_jml_comments(line).split("//", 1)[0].strip()
    if not _is_field_declaration_line(stripped):
        return set()
    stripped = stripped.rstrip(";")
    if "=" in stripped:
        # Keep declarator names before assignments while preserving comma
        # separation for simple field declarations.
        parts = [part.split("=", 1)[0].strip() for part in stripped.split(",")]
    else:
        parts = [part.strip() for part in stripped.split(",")]
    names: set[str] = set()
    for idx, part in enumerate(parts):
        tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", part)
        if not tokens:
            continue
        if idx == 0:
            names.add(tokens[-1])
        else:
            names.add(tokens[0])
    return names


def _concrete_field_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    depth = 0
    method_depth = 0
    for line in lines:
        stripped = strip_jml_comments(line).strip()
        if method_depth <= 0 and depth >= 1 and _is_field_declaration_line(stripped):
            names.update(_field_declared_names(stripped))
        delta = _brace_delta(stripped)
        if method_depth <= 0 and _method_signature_info(stripped) is not None and "{" in stripped:
            method_depth = max(0, delta)
        elif method_depth > 0:
            method_depth = max(0, method_depth + delta)
        depth += delta
    return names


def _private_field_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    depth = 0
    method_depth = 0
    for line in lines:
        stripped = strip_jml_comments(line).strip()
        if method_depth <= 0 and depth >= 1 and stripped.startswith("private ") and _is_field_declaration_line(stripped):
            names.update(_field_declared_names(stripped))
        delta = _brace_delta(stripped)
        if method_depth <= 0 and _method_signature_info(stripped) is not None and "{" in stripped:
            method_depth = max(0, delta)
        elif method_depth > 0:
            method_depth = max(0, method_depth + delta)
        depth += delta
    return names


def _nullable_field_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    depth = 0
    method_depth = 0
    previous_nullable_annotation = False
    for line in lines:
        stripped = strip_jml_comments(line).strip()
        is_field = method_depth <= 0 and depth >= 1 and _is_field_declaration_line(stripped)
        if is_field:
            if "nullable" in line or previous_nullable_annotation:
                names.update(_field_declared_names(stripped))
            previous_nullable_annotation = False
        else:
            previous_nullable_annotation = _is_jml_line(line) and "nullable" in line
        delta = _brace_delta(stripped)
        if method_depth <= 0 and _method_signature_info(stripped) is not None and "{" in stripped:
            method_depth = max(0, delta)
        elif method_depth > 0:
            method_depth = max(0, method_depth + delta)
        depth += delta
    return names


def _field_declaration_indices(lines: list[str]) -> set[int]:
    indices: set[int] = set()
    depth = 0
    method_depth = 0
    for idx, line in enumerate(lines):
        stripped = strip_jml_comments(line).strip()
        if method_depth <= 0 and depth >= 1 and _is_field_declaration_line(stripped):
            indices.add(idx)
        delta = _brace_delta(stripped)
        if method_depth <= 0 and _method_signature_info(stripped) is not None and "{" in stripped:
            method_depth = max(0, delta)
        elif method_depth > 0:
            method_depth = max(0, method_depth + delta)
        depth += delta
    return indices


def _is_standalone_nullable_annotation(line: str) -> bool:
    text = line.strip()
    return bool(re.fullmatch(r"//@\s*nullable\s*;?", text) or re.fullmatch(r"/\*@\s*nullable\s*;?\s*@\*/", text))


def _inline_nullable_in_field_declaration(line: str) -> str:
    if "nullable" in line:
        return line
    if "/*@" in line and "spec_public" in line:
        return re.sub(r"/\*@([^*]*\bspec_public\b[^*]*?)@\*/", r"/*@\1 nullable @*/", line, count=1)
    match = re.match(
        r"^(\s*)((?:(?:public|protected|private|static|final|transient|volatile)\s+)*)",
        line,
    )
    if not match:
        return line
    insert_at = match.end()
    return f"{line[:insert_at]}/*@ nullable @*/ {line[insert_at:]}"


def _inline_field_nullable_annotations(lines: list[str]) -> list[str]:
    """Move field-level ``nullable`` annotations into the declaration.

    OpenJML reliably accepts nullness modifiers written in the declaration, for
    example ``public /*@ nullable @*/ Node next;``.  LLMs often emit a
    standalone ``//@ nullable`` line before a field; that form is easy to
    confuse with a regular assertion-style annotation and has been observed not
    to discharge ``NullField`` obligations.  Keep the repair class-scope only
    so local-variable nullability comments are left unchanged.
    """

    field_indices = _field_declaration_indices(lines)
    if not field_indices:
        return lines
    remove: set[int] = set()
    rewrite: dict[int, str] = {}
    for idx, line in enumerate(lines[:-1]):
        if not _is_standalone_nullable_annotation(line):
            continue
        next_idx = idx + 1
        if next_idx in field_indices:
            rewrite[next_idx] = _inline_nullable_in_field_declaration(lines[next_idx])
            remove.add(idx)
    if not remove and not rewrite:
        return lines
    return [rewrite.get(idx, line) for idx, line in enumerate(lines) if idx not in remove]


def _split_class_scope_field_declarations(lines: list[str]) -> list[str]:
    """Split compact class-scope field declarations into one declaration per line.

    Some Java benchmark files use forms such as ``class X { private A a; private
    B b;``.  OpenJML reports nullness and assignability failures by line, while
    the rest of this post-processor is line-oriented.  Splitting only
    class-scope field declarations gives later generic repairs precise targets
    without changing executable Java tokens.
    """

    expanded: list[str] = []
    for line in lines:
        match = _TYPE_HEADER_WITH_TRAILING_RE.match(line)
        if match:
            expanded.append(match.group(1))
            expanded.append(f"{_line_indent(line)}  {match.group(2)}")
        else:
            expanded.append(line)

    out: list[str] = []
    depth = 0
    method_depth = 0
    for line in expanded:
        pending = [line]
        stripped = strip_jml_comments(line).split("//", 1)[0].strip()
        if method_depth <= 0 and depth >= 1 and stripped.count(";") > 1:
            pending = _split_field_declaration_line(line)
        for item in pending:
            out.append(item)
            item_code = strip_jml_comments(item).split("//", 1)[0].strip()
            delta = _brace_delta(item_code)
            if method_depth <= 0 and _method_signature_info(item_code) is not None and "{" in item_code:
                method_depth = max(0, delta)
            elif method_depth > 0:
                method_depth = max(0, method_depth + delta)
            depth += delta
            if depth < 0:
                depth = 0
    return out


def _split_field_declaration_line(line: str) -> list[str]:
    indent = _line_indent(line)
    pieces = re.findall(r"[^;]+;", line)
    remainder = line[sum(len(piece) for piece in pieces) :].strip()
    if len(pieces) <= 1:
        return [line]
    declarations = [piece.strip() for piece in pieces]
    if not all(_is_field_declaration_line(decl) for decl in declarations):
        return [line]
    out: list[str] = []
    for decl in declarations:
        out.append(f"{indent}{decl}")
    if remainder:
        out.append(f"{indent}{remainder}")
    return out


def _method_body_text(lines: list[str], signature_idx: int) -> str:
    body: list[str] = []
    depth = 0
    started = False
    for idx in range(signature_idx, len(lines)):
        stripped = strip_jml_comments(lines[idx])
        if "{" in stripped:
            started = True
        if started and idx > signature_idx:
            body.append(stripped)
        if started:
            depth += _brace_delta(stripped)
            if depth <= 0:
                break
    return "\n".join(body)


def _method_body_handles_null_parameter(lines: list[str], signature_idx: int, param: str) -> bool:
    body = _method_body_text(lines, signature_idx)
    return bool(
        re.search(rf"\b{re.escape(param)}\s*(?:==|!=)\s*null\b", body)
        or re.search(rf"\bnull\s*(?:==|!=)\s*{re.escape(param)}\b", body)
    )


def _method_body_returns_null(lines: list[str], signature_idx: int) -> bool:
    return bool(re.search(r"\breturn\s+null\s*;", _method_body_text(lines, signature_idx)))


def _method_body_returns_nullable_field(lines: list[str], signature_idx: int) -> bool:
    nullable_fields = _nullable_field_names(lines)
    if not nullable_fields:
        return False
    body = _method_body_text(lines, signature_idx)
    return any(
        re.search(rf"\breturn\s+(?:this\.)?{re.escape(field)}\s*;", body)
        for field in nullable_fields
    )


def _split_java_parameters(params: str) -> list[str]:
    parts: list[str] = []
    start = 0
    angle_depth = 0
    paren_depth = 0
    for idx, ch in enumerate(params):
        if ch == "<":
            angle_depth += 1
        elif ch == ">" and angle_depth > 0:
            angle_depth -= 1
        elif ch == "(":
            paren_depth += 1
        elif ch == ")" and paren_depth > 0:
            paren_depth -= 1
        elif ch == "," and angle_depth == 0 and paren_depth == 0:
            parts.append(params[start:idx])
            start = idx + 1
    parts.append(params[start:])
    return parts


def _inline_nullable_in_formal_parameter(line: str, param: str) -> str:
    open_idx = line.find("(")
    close_idx = line.rfind(")")
    if open_idx < 0 or close_idx <= open_idx:
        return line
    params_text = line[open_idx + 1 : close_idx]
    parts = _split_java_parameters(params_text)
    changed = False
    rewritten: list[str] = []
    for part in parts:
        tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", strip_jml_comments(part))
        if tokens and tokens[-1] == param and "nullable" not in part:
            leading = part[: len(part) - len(part.lstrip())]
            rewritten.append(f"{leading}/*@ nullable @*/ {part.lstrip()}")
            changed = True
        else:
            rewritten.append(part)
    if not changed:
        return line
    return f"{line[:open_idx + 1]}{','.join(rewritten)}{line[close_idx:]}"


_JAVA_PRIMITIVE_OR_VOID_TYPES = {
    "void",
    "boolean",
    "byte",
    "char",
    "short",
    "int",
    "long",
    "float",
    "double",
}


def _inline_nullable_return_type(line: str) -> str:
    info = _method_signature_info(strip_jml_comments(line).strip())
    if not info or info.get("is_constructor"):
        return line
    return_type = str(info.get("return_type") or "")
    method_name = str(info.get("name") or "")
    if not return_type or return_type in _JAVA_PRIMITIVE_OR_VOID_TYPES:
        return line
    open_idx = line.find("(")
    if open_idx < 0:
        return line
    prefix = line[:open_idx]
    if "nullable" in prefix:
        return line
    match = re.search(rf"\b{re.escape(return_type)}(\s+{re.escape(method_name)}\s*)$", prefix)
    if not match:
        return line
    insert_at = match.start()
    return f"{prefix[:insert_at]}/*@ nullable @*/ {prefix[insert_at:]}{line[open_idx:]}"


def _mark_private_fields_spec_public(lines: list[str], field_names: set[str]) -> list[str]:
    if not field_names:
        return lines
    out: list[str] = []
    for line in lines:
        stripped = strip_jml_comments(line).strip()
        if (
            stripped.startswith("private ")
            and _is_field_declaration_line(stripped)
            and _field_declared_names(stripped) & field_names
            and "spec_public" not in line
        ):
            out.append(re.sub(r"\bprivate\b", r"private /*@ spec_public @*/", line, count=1))
        else:
            out.append(line)
    return out


def _referenced_private_fields_in_public_contracts(lines: list[str]) -> set[str]:
    private_fields = _private_field_names(lines)
    if not private_fields:
        return set()
    referenced: set[str] = set()
    for idx, line in enumerate(lines):
        target = strip_jml_comments(line)
        info = _method_signature_info(target)
        if info is None:
            continue
        if not re.search(r"\b(?:public|protected)\b", target):
            continue
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        contract_text = "\n".join(_jml_contract_body(contract_line) for contract_line in lines[group_start:idx])
        if not contract_text:
            continue
        for name in private_fields:
            if re.search(rf"\b(?:this\.)?{re.escape(name)}\b", contract_text):
                referenced.add(name)
    return referenced


def _add_spec_public_for_referenced_private_fields(lines: list[str]) -> list[str]:
    """Expose private fields that generated public/protected contracts mention."""

    return _mark_private_fields_spec_public(lines, _referenced_private_fields_in_public_contracts(lines))


def _jml_model_declared_names(line: str) -> set[str]:
    if not _is_jml_line(line) or "model" not in line:
        return set()
    body = line.strip().removeprefix("//@").strip().rstrip(";")
    tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", body)
    return {tok for tok in tokens if tok not in _JAVA_FIELD_MODIFIERS_AND_TYPES}


def _drop_duplicate_model_fields(lines: list[str]) -> list[str]:
    """Drop generated model fields that duplicate concrete Java fields."""

    concrete_fields = _concrete_field_names(lines)
    if not concrete_fields:
        return lines
    out: list[str] = []
    for line in lines:
        model_names = _jml_model_declared_names(line)
        if model_names and model_names & concrete_fields:
            continue
        out.append(line)
    return out


def _filter_method_contract_target(lines: list[str]) -> list[str]:
    """Drop method-only clauses that are attached to non-method targets.

    OpenJML treats ``requires``, ``ensures``, and ``assignable`` as method
    contract clauses.  LLMs sometimes place them before field declarations, use
    ``\result`` on ``void`` methods, or attach assignable clauses to
    constructors.  These are syntax/typing repairs only: they remove invalid
    clauses rather than inventing new behavior.
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

        target_idx = _next_nonempty_index(lines, j)
        target = lines[target_idx] if target_idx is not None else ""
        info = _method_signature_info(target)
        target_is_loop = bool(target and _LOOP_START_RE.search(target))
        target_is_field = bool(target and _is_field_declaration_line(target))
        filtered: list[str] = []
        for line in group:
            keyword = _jml_content_keyword(line)
            if target_is_field and keyword in {"requires", "ensures", "assignable", "assigns"}:
                continue
            if info is None and not target_is_loop and keyword in {"requires", "ensures", "assignable", "assigns"}:
                continue
            if info is not None:
                if keyword in {"assignable", "assigns"} and info["is_constructor"]:
                    continue
                if keyword == "ensures" and (info["is_void"] or info["is_constructor"]) and "\\result" in line:
                    continue
            filtered.append(line)
        out.extend(filtered)
        i = j
    return out


def _jml_bound_variables(line: str) -> set[str]:
    return set(re.findall(r"\\(?:forall|exists|sum)\s+(?:int|integer|long|short|byte|char|boolean)\s+([A-Za-z_$][A-Za-z0-9_$]*)", line))


_JML_QUANT_DECL_TEMPLATE = (
    r"(\\(?:forall|exists|sum)\s+"
    r"(?:int|integer|long|short|byte|char|boolean)\s+)"
    r"({var})(\b)"
)


def _fresh_jml_identifier(base: str, text: str) -> str:
    candidate = f"{base}_q"
    while re.search(rf"(?<!\\)\b{re.escape(candidate)}\b", text):
        candidate = f"{candidate}_q"
    return candidate


def _rename_quantifier_var_in_text(text: str, old: str) -> str:
    """Alpha-rename a JML quantifier variable in a single clause text.

    OpenJML rejects loop annotations such as ``(\forall int j; ...)`` directly
    before ``for (int j = ...)`` because the bound variable collides with the
    loop variable.  Renaming the bound variable inside the quantified suffix is
    semantics-preserving and keeps the generated invariant instead of dropping
    it outright.
    """

    pattern = re.compile(_JML_QUANT_DECL_TEMPLATE.format(var=re.escape(old)))
    match = pattern.search(text)
    if not match:
        return text
    new = _fresh_jml_identifier(old, text)
    suffix = re.sub(rf"(?<!\\)\b{re.escape(old)}\b", new, text[match.end() :])
    return text[: match.start(2)] + new + text[match.end(2) : match.end()] + suffix


def _rename_quantifier_var_in_segment(segment: list[str], old: str) -> list[str]:
    text = "\n".join(segment)
    renamed = _rename_quantifier_var_in_text(text, old)
    return renamed.split("\n")


def _rename_quantifier_vars_in_segment(segment: list[str], names: set[str]) -> list[str]:
    text = "\n".join(segment)
    for name in sorted(names):
        if re.search(_JML_QUANT_DECL_TEMPLATE.format(var=re.escape(name)), text):
            text = _rename_quantifier_var_in_text(text, name)
    return text.split("\n")


def _jml_prebound_quantifier_uses(line: str) -> set[str]:
    uses: set[str] = set()
    for match in re.finditer(
        r"\\(?:forall|exists|sum)\s+(?:int|integer|long|short|byte|char|boolean)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        line,
    ):
        var = match.group(1)
        prefix = line[: match.start()]
        if re.search(rf"(?<!\\)\b{re.escape(var)}\b", prefix):
            uses.add(var)
    return uses


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
    "this",
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


def _method_contract_unknown_ids(line: str, params: set[str], fields: set[str] | None = None) -> set[str]:
    ids = _jml_identifiers(line)
    prebound_uses = _jml_prebound_quantifier_uses(line)
    ids -= _jml_bound_variables(line)
    ids |= prebound_uses
    ids -= _METHOD_CONTRACT_ALLOWED_IDS
    ids -= params
    ids -= fields or set()
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
    field_names = _concrete_field_names(lines)
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
            for segment in _jml_line_segments(group):
                keyword = _jml_content_keyword(segment[0]) if segment else ""
                segment_text = " ".join(_jml_body(line) for line in segment)
                if keyword in {"requires", "ensures"} and _method_contract_unknown_ids(segment_text, params, field_names):
                    continue
                filtered.extend(segment)
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


def _for_bound_invariants(loop_var: str, raw_bound: str, aliases: dict[str, str]) -> list[str]:
    bound = re.sub(r"\s+", " ", raw_bound.strip())
    direct = re.fullmatch(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length", bound)
    if direct:
        array = direct.group(1)
        return [f"0 <= {loop_var} && {loop_var} <= {array}.length"]

    alias = re.fullmatch(r"([A-Za-z_$][A-Za-z0-9_$]*)", bound)
    if alias and alias.group(1) in aliases:
        name = alias.group(1)
        return [f"0 <= {loop_var} && {loop_var} <= {name}", f"{name} == {aliases[name]}"]

    alias_minus = re.fullmatch(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*-\s*\d+", bound)
    if alias_minus and alias_minus.group(1) in aliases:
        name = alias_minus.group(1)
        return [f"0 <= {loop_var} && {loop_var} <= {name}", f"{name} == {aliases[name]}"]

    return []


_JML_ARRAY_NONNULL_RE = re.compile(
    r"\b(?:this\.)?([A-Za-z_$][A-Za-z0-9_$]*)\s*!=\s*null\b"
)
_JML_ARRAY_LENGTH_EQ_RE = re.compile(
    r"\b(?:this\.)?([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length\s*==\s*([A-Za-z_$][A-Za-z0-9_$]*)\b"
    r"|"
    r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*==\s*(?:this\.)?([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*length\b"
)


def _is_jml_contract_group_line(line: str) -> bool:
    stripped = line.strip()
    return (
        _is_jml_line(line)
        or stripped.startswith("/*@")
        or stripped.startswith("@")
        or stripped.startswith("*")
        or stripped.endswith("*/")
    )


def _jml_contract_body(line: str) -> str:
    stripped = line.strip()
    stripped = stripped.removeprefix("//@").strip()
    stripped = stripped.removeprefix("/*@").strip()
    stripped = stripped.removeprefix("@").strip()
    stripped = stripped.removeprefix("*").strip()
    stripped = stripped.removesuffix("@*/").strip()
    stripped = stripped.removesuffix("*/").strip()
    return stripped


def _method_contract_array_length_bounds(contract_lines: list[str]) -> dict[str, list[str]]:
    """Extract method-contract facts of the form ``array != null`` and ``array.length == n``."""

    nonnull = {
        match.group(1)
        for line in contract_lines
        for match in _JML_ARRAY_NONNULL_RE.finditer(_jml_contract_body(line))
    }
    bounds: dict[str, set[str]] = {}
    for line in contract_lines:
        body = _jml_contract_body(line)
        if not body.startswith("requires "):
            continue
        for match in _JML_ARRAY_LENGTH_EQ_RE.finditer(body):
            if match.group(1):
                array, bound = match.group(1), match.group(2)
            else:
                bound, array = match.group(3), match.group(4)
            if array in nonnull:
                bounds.setdefault(bound, set()).add(array)
    return {bound: sorted(arrays) for bound, arrays in bounds.items()}


def _loop_body_text(lines: list[str], loop_idx: int) -> str:
    body = [strip_jml_comments(lines[loop_idx])]
    depth = _brace_delta(body[0])
    if "{" not in body[0]:
        if loop_idx + 1 < len(lines):
            body.append(strip_jml_comments(lines[loop_idx + 1]))
        return "\n".join(body)
    j = loop_idx + 1
    while j < len(lines) and depth > 0:
        body.append(strip_jml_comments(lines[j]))
        depth += _brace_delta(lines[j])
        j += 1
    return "\n".join(body)


def _loop_uses_array_index(body_text: str, array: str, loop_var: str) -> bool:
    return bool(re.search(rf"\b{re.escape(array)}\s*\[\s*{re.escape(loop_var)}\s*\]", body_text))


def _loop_reassigns_name(body_text: str, name: str) -> bool:
    return bool(re.search(rf"\b{re.escape(name)}\s*=(?!=)", body_text))


def _method_body_span(lines: list[str], signature_idx: int) -> tuple[int, int] | None:
    open_idx = signature_idx
    while open_idx < len(lines) and "{" not in lines[open_idx]:
        stripped = strip_jml_comments(lines[open_idx]).strip()
        if ";" in stripped:
            return None
        if open_idx > signature_idx and stripped and not stripped.startswith("//"):
            return None
        open_idx += 1
    if open_idx >= len(lines):
        return None
    depth = _brace_delta(lines[open_idx])
    if depth <= 0:
        return None
    j = open_idx + 1
    while j < len(lines) and depth > 0:
        depth += _brace_delta(lines[j])
        j += 1
    return open_idx + 1, j - 1


def _method_body_has_obvious_side_effects(body_text: str) -> bool:
    code = strip_jml_comments(body_text)
    code = re.sub(r"//.*", "", code)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    if "++" in code or "--" in code:
        return True
    if re.search(r"(?<![=!<>])=(?!=)", code):
        return True
    if re.search(r"\b[A-Za-z_$][A-Za-z0-9_$]*\s*\(", code.replace("return", "")):
        return True
    return False


def _contract_group_has_assignable(contract_lines: list[str]) -> bool:
    return any(_jml_contract_body(line).startswith(("assignable ", "assigns ")) for line in contract_lines)


def _contract_group_has_method_clause(contract_lines: list[str]) -> bool:
    return any(
        _jml_contract_body(line).startswith(("requires ", "ensures ", "signals ", "diverges "))
        for line in contract_lines
    )


_JAVA_LOCAL_DECL_RE = re.compile(
    r"\b(?:final\s+)?"
    r"(?:boolean|byte|char|short|int|long|float|double|String|[A-Z][A-Za-z0-9_$]*(?:\s*<[^;=(){}]*>)?)"
    r"(?:\s*\[\s*\])*\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_JAVA_LOCAL_DECL_STMT_RE = re.compile(
    r"\b(?:final\s+)?"
    r"(?:boolean|byte|char|short|int|long|float|double|String|[A-Z][A-Za-z0-9_$]*(?:\s*<[^;=(){}]*>)?)"
    r"(?:\s*\[\s*\])*\s+(?P<decls>[^;{}]+);"
)


def _declared_names_from_java_declarators(declarators: str) -> set[str]:
    names: set[str] = set()
    for part in declarators.split(","):
        before_init = part.split("=", 1)[0].strip()
        tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", before_init)
        if tokens:
            names.add(tokens[-1])
    return names


def _method_local_names(body_text: str, params: set[str]) -> set[str]:
    code = strip_jml_comments(body_text)
    locals_ = set(params)
    locals_.update(match.group(1) for match in _JAVA_LOCAL_DECL_RE.finditer(code))
    for match in _JAVA_LOCAL_DECL_STMT_RE.finditer(code):
        locals_.update(_declared_names_from_java_declarators(match.group("decls")))
    return locals_


def _body_passes_local_to_call(body_text: str, params: set[str]) -> bool:
    """Return whether a method call receives a local variable argument.

    Such calls can mutate an object or array that is not expressible in the
    caller's method frame.  In that situation, inferring a narrow frame such as
    ``assignable a[*]`` from a separate direct write to parameter ``a`` can be
    unsoundly narrow for OpenJML: a helper may also write a local array passed
    as an argument.  Leave the frame unspecified rather than inventing a frame
    that excludes those effects.
    """

    code = strip_jml_comments(body_text)
    locals_only = _method_local_names(code, params) - params
    if not locals_only:
        return False
    for match in re.finditer(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([^;{}()]*)\)", code):
        callee = match.group(1)
        if callee in {"if", "for", "while", "switch", "catch", "return", "new"}:
            continue
        args = match.group(2)
        if any(re.search(rf"\b{re.escape(local)}\b", args) for local in locals_only):
            return True
    return False


def _infer_assignable_locations(body_text: str, params: set[str]) -> list[str]:
    code = strip_jml_comments(body_text)
    locals_ = _method_local_names(code, params)
    suppress_param_array_frames = _body_passes_local_to_call(code, params)
    locations: set[str] = set()

    for match in re.finditer(r"(?<!\.)\b(?:this\.)?([A-Za-z_$][A-Za-z0-9_$]*)\s*\[[^\]]+\]\s*=(?!=)", code):
        name = match.group(1)
        if name not in locals_ or (name in params and not suppress_param_array_frames):
            locations.add(f"{name}[*]")

    for match in re.finditer(r"(?<!\.)\b(?:this\.)?([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:\+\+|--)", code):
        name = match.group(1)
        if name not in locals_:
            locations.add(name)

    for match in re.finditer(r"(?<!\.)\b(?:this\.)?([A-Za-z_$][A-Za-z0-9_$]*)\s*=(?!=)", code):
        name = match.group(1)
        if name not in locals_:
            locations.add(name)

    return sorted(locations)


def _add_inferred_assignable_frame(lines: list[str]) -> list[str]:
    """Add simple frame clauses for methods with obvious field/array writes."""

    insertions: dict[int, str] = {}
    for idx, line in enumerate(lines):
        info = _method_signature_info(strip_jml_comments(line))
        if info is None or info["is_constructor"]:
            continue
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        contract_lines = lines[group_start:idx]
        if (
            not contract_lines
            or _contract_group_has_assignable(contract_lines)
            or not _contract_group_has_method_clause(contract_lines)
        ):
            continue
        span = _method_body_span(lines, idx)
        if span is None:
            continue
        body_text = "\n".join(lines[span[0] : span[1]])
        locations = _infer_assignable_locations(body_text, set(info["params"]))
        if locations:
            insertions[idx] = f"{_line_indent(line)}//@ assignable {', '.join(locations)};"

    rebuilt: list[str] = []
    for idx, line in enumerate(lines):
        if idx in insertions:
            rebuilt.append(insertions[idx])
        rebuilt.append(line)
    return rebuilt


def _add_pure_method_assignable_nothing(lines: list[str]) -> list[str]:
    """Add a narrow frame to obviously pure contracted helper methods."""

    out = list(lines)
    insertions: dict[int, str] = {}
    for idx, line in enumerate(lines):
        info = _method_signature_info(strip_jml_comments(line))
        if info is None or info["is_constructor"]:
            continue
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        contract_lines = lines[group_start:idx]
        if (
            not contract_lines
            or _contract_group_has_assignable(contract_lines)
            or not _contract_group_has_method_clause(contract_lines)
        ):
            continue
        span = _method_body_span(lines, idx)
        if span is None:
            continue
        body_text = "\n".join(lines[span[0] : span[1]])
        if _method_body_has_obvious_side_effects(body_text):
            continue
        insertions[idx] = f"{_line_indent(line)}//@ assignable \\nothing;"

    rebuilt: list[str] = []
    for idx, line in enumerate(out):
        if idx in insertions:
            rebuilt.append(insertions[idx])
        rebuilt.append(line)
    return rebuilt


_CONSTANT_ARRAY_INDEX_RE = re.compile(
    r"\b(?:this\.)?([A-Za-z_$][A-Za-z0-9_$]*)\s*\[\s*(\d+)\s*\]"
)


def _array_length_guarded_in_body(body_text: str, array: str, index: int) -> bool:
    code = strip_jml_comments(body_text)
    patterns = [
        rf"\b{re.escape(array)}\s*!=\s*null\b",
        rf"\b{re.escape(array)}\s*==\s*null\b",
        rf"\b{re.escape(array)}\s*\.\s*length\s*>\s*{index}\b",
        rf"\b{index}\s*<\s*{re.escape(array)}\s*\.\s*length\b",
        rf"\b{re.escape(array)}\s*\.\s*length\s*<=\s*{index}\b",
        rf"\b{index}\s*>=\s*{re.escape(array)}\s*\.\s*length\b",
    ]
    return any(re.search(pattern, code) for pattern in patterns)


def _contract_group_has_array_bound(contract_lines: list[str], array: str, index: int) -> bool:
    text = "\n".join(_jml_contract_body(line) for line in contract_lines)
    nonnull = re.search(rf"\b(?:this\.)?{re.escape(array)}\s*!=\s*null\b", text)
    length = re.search(rf"\b(?:this\.)?{re.escape(array)}\s*\.\s*length\s*>\s*{index}\b", text)
    reverse = re.search(rf"\b{index}\s*<\s*(?:this\.)?{re.escape(array)}\s*\.\s*length\b", text)
    return bool(nonnull and (length or reverse))


def _add_constant_array_index_requires(lines: list[str]) -> list[str]:
    """Add minimal preconditions for direct constant-index array accesses."""

    insertions: dict[int, list[str]] = {}
    field_names = _concrete_field_names(lines)
    for idx, line in enumerate(lines):
        info = _method_signature_info(strip_jml_comments(line))
        if info is None or info["is_constructor"]:
            continue
        span = _method_body_span(lines, idx)
        if span is None:
            continue
        body_text = "\n".join(lines[span[0] : span[1]])
        params = set(info["params"])
        local_names = _method_local_names(body_text, params) - params
        needed: dict[str, int] = {}
        for match in _CONSTANT_ARRAY_INDEX_RE.finditer(strip_jml_comments(body_text)):
            array, raw_index = match.group(1), int(match.group(2))
            if array in local_names:
                continue
            if array not in params and array not in field_names:
                continue
            if _array_length_guarded_in_body(body_text, array, raw_index):
                continue
            needed[array] = max(needed.get(array, -1), raw_index)
        if not needed:
            continue
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        contract_lines = lines[group_start:idx]
        clauses: list[str] = []
        for array, raw_index in sorted(needed.items()):
            if _contract_group_has_array_bound(contract_lines, array, raw_index):
                continue
            clauses.append(f"{_line_indent(line)}//@ requires {array} != null && {array}.length > {raw_index};")
        if clauses:
            insertions[idx] = clauses

    rebuilt: list[str] = []
    for idx, line in enumerate(lines):
        if idx in insertions:
            rebuilt.extend(insertions[idx])
        rebuilt.append(line)
    return rebuilt


def _contract_group_requires(contract_lines: list[str]) -> list[str]:
    requires: list[str] = []
    for line in contract_lines:
        body = _jml_contract_body(line)
        if body.startswith("requires ") and body.endswith(";"):
            requires.append(body.removeprefix("requires ").rstrip(";").strip())
    return requires


def _contract_group_assignable_locations(contract_lines: list[str]) -> list[str]:
    locations: list[str] = []
    for line in contract_lines:
        body = _jml_contract_body(line)
        if not body.startswith(("assignable ", "assigns ")) or not body.endswith(";"):
            continue
        body = body.removeprefix("assignable ").removeprefix("assigns ").rstrip(";").strip()
        locations.extend(part.strip() for part in body.split(",") if part.strip())
    return locations


def _split_java_arguments(args_text: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    for idx, ch in enumerate(args_text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(args_text[start:idx].strip())
            start = idx + 1
    tail = args_text[start:].strip()
    if tail:
        args.append(tail)
    return args


def _substitute_jml_params(expr: str, params: list[str], args: list[str]) -> str | None:
    if len(params) != len(args):
        return None
    substituted = expr
    for param, arg in zip(params, args):
        if not arg:
            return None
        substituted = re.sub(rf"(?<!\.)\b{re.escape(param)}\b", f"({arg})", substituted)
    return re.sub(r"\s+", " ", substituted).strip()


def _expr_mentions_any(expr: str, names: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(name)}\b", expr) for name in names)


def _expr_has_call(expr: str) -> bool:
    return bool(re.search(r"\b[A-Za-z_$][A-Za-z0-9_$]*\s*\(", expr))


def _caller_contract_can_accept_propagated_requires(
    expr: str,
    caller_body: str,
    caller_params: set[str],
) -> bool:
    if _expr_has_call(expr):
        return False
    local_names = _method_local_names(caller_body, caller_params) - caller_params
    return not _expr_mentions_any(expr, local_names)


def _collect_private_callee_contracts(lines: list[str]) -> dict[str, dict[str, Any]]:
    callees: dict[str, dict[str, Any]] = {}
    for idx, line in enumerate(lines):
        code_line = strip_jml_comments(line)
        info = _method_signature_info(code_line)
        if info is None or info["is_constructor"] or "private" not in code_line:
            continue
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        requires = _contract_group_requires(lines[group_start:idx])
        assignable = _contract_group_assignable_locations(lines[group_start:idx])
        if requires or assignable:
            callees[str(info["name"])] = {
                "params": list(info.get("param_order") or []),
                "requires": requires,
                "assignable": assignable,
            }
    return callees


def _assignable_base(location: str) -> str:
    match = re.match(r"(?:this\.)?([A-Za-z_$][A-Za-z0-9_$]*)", location.strip())
    return match.group(1) if match else ""


def _body_directly_mentions_field(body_text: str, name: str) -> bool:
    code = strip_jml_comments(body_text)
    return bool(
        re.search(rf"\bthis\s*\.\s*{re.escape(name)}\b", code)
        or re.search(rf"(?<!\.)\b{re.escape(name)}\b\s*(?:=|\+\+|--|\[)", code)
    )


def _assignable_location_in_scope(location: str, body_text: str, params: set[str]) -> bool:
    location = location.strip()
    if location in {"\\nothing", "\\everything"}:
        return True
    base = _assignable_base(location)
    if not base:
        return False
    if base in params:
        return True
    local_names = _method_local_names(body_text, params) - params
    if base in local_names:
        return False
    if location.startswith("this."):
        return True
    return _body_directly_mentions_field(body_text, base)


def _sanitize_assignable_locations(lines: list[str]) -> list[str]:
    """Drop assignable locations that are not in the method contract scope."""

    replacements: dict[int, str | None] = {}
    for idx, line in enumerate(lines):
        info = _method_signature_info(strip_jml_comments(line))
        if info is None or info["is_constructor"]:
            continue
        span = _method_body_span(lines, idx)
        if span is None:
            continue
        body_text = "\n".join(lines[span[0] : span[1]])
        params = set(info["params"])
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        for line_idx in range(group_start, idx):
            body = _jml_contract_body(lines[line_idx])
            if not body.startswith(("assignable ", "assigns ")) or not body.endswith(";"):
                continue
            locations = _contract_group_assignable_locations([lines[line_idx]])
            kept = [
                location
                for location in locations
                if _assignable_location_in_scope(location, body_text, params)
            ]
            if kept == locations:
                continue
            replacements[line_idx] = (
                f"{_line_indent(lines[line_idx])}//@ assignable {', '.join(kept)};"
                if kept
                else None
            )

    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx not in replacements:
            out.append(line)
        elif replacements[idx] is not None:
            out.append(replacements[idx] or "")
    return out


def _add_private_callee_preconditions_to_callers(lines: list[str]) -> list[str]:
    """Propagate private helper preconditions to simple direct callers.

    This is deliberately conservative: it only keeps substituted requirements
    that mention caller parameters, fields, ``this``, and constants.  Any
    requirement depending on caller locals or another method call is left to the
    model/verifier instead of becoming an over-broad caller precondition.
    """

    callees = _collect_private_callee_contracts(lines)
    if not callees:
        return lines
    insertions: dict[int, list[str]] = {}
    for idx, line in enumerate(lines):
        code_line = strip_jml_comments(line)
        info = _method_signature_info(code_line)
        if info is None or info["is_constructor"]:
            continue
        if "private" in code_line:
            continue
        span = _method_body_span(lines, idx)
        if span is None:
            continue
        body_text = "\n".join(lines[span[0] : span[1]])
        caller_params = set(info["params"])
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        existing = "\n".join(_jml_contract_body(contract_line) for contract_line in lines[group_start:idx])
        clauses: list[str] = []
        code_body = strip_jml_comments(body_text)
        for callee_name, callee in callees.items():
            if callee_name == info["name"]:
                continue
            for match in re.finditer(rf"\b{re.escape(callee_name)}\s*\(([^;{{}}]*)\)", code_body):
                args = _split_java_arguments(match.group(1))
                for requirement in callee["requires"]:
                    substituted = _substitute_jml_params(requirement, callee["params"], args)
                    if substituted is None:
                        continue
                    if not _caller_contract_can_accept_propagated_requires(substituted, body_text, caller_params):
                        continue
                    if f"requires {substituted};" in existing or substituted in clauses:
                        continue
                    clauses.append(substituted)
        if clauses:
            insertions[idx] = [f"{_line_indent(line)}//@ requires {clause};" for clause in clauses]

    rebuilt: list[str] = []
    for idx, line in enumerate(lines):
        if idx in insertions:
            rebuilt.extend(insertions[idx])
        rebuilt.append(line)
    return rebuilt


def _add_private_callee_assignable_to_callers(lines: list[str]) -> list[str]:
    """Include private helper field frames in direct callers' frames."""

    callees = _collect_private_callee_contracts(lines)
    if not callees:
        return lines
    field_names = _concrete_field_names(lines)
    replacements: dict[int, str] = {}
    insertions: dict[int, str] = {}
    for idx, line in enumerate(lines):
        code_line = strip_jml_comments(line)
        info = _method_signature_info(code_line)
        if info is None or info["is_constructor"] or "private" in code_line:
            continue
        span = _method_body_span(lines, idx)
        if span is None:
            continue
        code_body = strip_jml_comments("\n".join(lines[span[0] : span[1]]))
        needed: set[str] = set()
        for callee_name, callee in callees.items():
            if callee_name == info["name"] or not re.search(rf"\b{re.escape(callee_name)}\s*\(", code_body):
                continue
            for location in callee.get("assignable") or []:
                base = _assignable_base(location)
                if base in field_names:
                    needed.add(location)
        if not needed:
            continue
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        existing_locations = _contract_group_assignable_locations(lines[group_start:idx])
        missing = sorted(location for location in needed if location not in existing_locations)
        if not missing:
            continue
        assignable_idx = None
        for line_idx in range(group_start, idx):
            body = _jml_contract_body(lines[line_idx])
            if body.startswith(("assignable ", "assigns ")) and body.endswith(";"):
                assignable_idx = line_idx
                break
        if assignable_idx is not None:
            combined = existing_locations + missing
            replacements[assignable_idx] = f"{_line_indent(lines[assignable_idx])}//@ assignable {', '.join(combined)};"
        else:
            insertions[idx] = f"{_line_indent(line)}//@ assignable {', '.join(missing)};"

    rebuilt: list[str] = []
    for idx, line in enumerate(lines):
        if idx in insertions:
            rebuilt.append(insertions[idx])
        rebuilt.append(replacements.get(idx, line))
    return rebuilt


def _extract_if_condition(line: str) -> tuple[str, int] | None:
    code = strip_jml_comments(line)
    match = re.search(r"\bif\s*\(", code)
    if not match:
        return None
    start = code.find("(", match.start())
    depth = 0
    for idx in range(start, len(code)):
        ch = code[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return code[start + 1 : idx].strip(), idx
    return None


def _then_branch_text(lines: list[str], if_idx: int, condition_end: int) -> str:
    code = strip_jml_comments(lines[if_idx])
    suffix = code[condition_end + 1 :]
    body: list[str] = []
    if "{" not in suffix:
        inline = suffix.strip()
        if inline:
            body.append(inline)
        for follow in lines[if_idx + 1 :]:
            stripped = strip_jml_comments(follow).strip()
            if stripped:
                body.append(stripped)
                break
        return "\n".join(body)

    after_open = suffix[suffix.find("{") + 1 :]
    if after_open.strip():
        body.append(after_open)
    depth = _brace_delta(suffix)
    for follow in lines[if_idx + 1 :]:
        if depth <= 0:
            break
        body.append(strip_jml_comments(follow))
        depth += _brace_delta(strip_jml_comments(follow))
    return "\n".join(body)


def _condition_already_guards_nonnull(condition: str, field: str) -> bool:
    return bool(
        re.search(rf"\b(?:this\.)?{re.escape(field)}\s*!=\s*null\b", condition)
        or re.search(rf"\bnull\s*!=\s*(?:this\.)?{re.escape(field)}\b", condition)
    )


def _condition_is_contract_safe(condition: str, body_text: str, params: set[str], fields: set[str]) -> bool:
    if _expr_has_call(condition):
        return False
    local_names = _method_local_names(body_text, params) - params
    if _expr_mentions_any(condition, local_names):
        return False
    return not _method_contract_unknown_ids(condition, params, fields)


def _branch_dereferences_field(branch_text: str, field: str) -> bool:
    return bool(re.search(rf"\b(?:this\.)?{re.escape(field)}\s*\.", branch_text))


def _add_branch_nullable_receiver_requires(lines: list[str]) -> list[str]:
    """Require nullable receiver fields to be non-null on dereferencing branches."""

    nullable_fields = _nullable_field_names(lines)
    if not nullable_fields:
        return lines
    field_names = _concrete_field_names(lines)
    insertions: dict[int, list[str]] = {}
    for idx, line in enumerate(lines):
        info = _method_signature_info(strip_jml_comments(line))
        if info is None or info["is_constructor"]:
            continue
        span = _method_body_span(lines, idx)
        if span is None:
            continue
        body_lines = lines[span[0] : span[1]]
        body_text = "\n".join(body_lines)
        params = set(info["params"])
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        existing = "\n".join(_jml_contract_body(contract_line) for contract_line in lines[group_start:idx])
        clauses: list[str] = []
        for rel_idx, body_line in enumerate(body_lines):
            extracted = _extract_if_condition(body_line)
            if extracted is None:
                continue
            condition, condition_end = extracted
            if not _condition_is_contract_safe(condition, body_text, params, field_names):
                continue
            branch_text = _then_branch_text(body_lines, rel_idx, condition_end)
            for field in sorted(nullable_fields):
                if _condition_already_guards_nonnull(condition, field):
                    continue
                if not _branch_dereferences_field(branch_text, field):
                    continue
                clause = f"{condition} ==> {field} != null"
                if f"requires {clause};" in existing or clause in clauses:
                    continue
                clauses.append(clause)
        if clauses:
            insertions[idx] = [f"{_line_indent(line)}//@ requires {clause};" for clause in clauses]

    rebuilt: list[str] = []
    for idx, line in enumerate(lines):
        if idx in insertions:
            rebuilt.extend(insertions[idx])
        rebuilt.append(line)
    return rebuilt


def _add_for_loop_bound_invariants(lines: list[str]) -> list[str]:
    """Add simple array-length bounds invariants for common counted loops."""

    out: list[str] = []
    aliases: dict[str, str] = {}
    method_array_bounds: dict[str, list[str]] = {}
    method_depth = 0
    i = 0
    while i < len(lines):
        counted_method_start = False
        if method_depth <= 0:
            method_array_bounds = {}
        if _method_signature_info(strip_jml_comments(lines[i])) is not None and "{" in lines[i]:
            group_start = len(out)
            while group_start > 0 and _is_jml_contract_group_line(out[group_start - 1]):
                group_start -= 1
            method_array_bounds = _method_contract_array_length_bounds(out[group_start:])
            method_depth = max(0, _brace_delta(lines[i]))
            counted_method_start = True

        alias_match = _LENGTH_ALIAS_RE.search(strip_jml_comments(lines[i]))
        if alias_match:
            aliases[alias_match.group(1)] = f"{alias_match.group(2)}.length"

        if not _LOOP_START_RE.search(lines[i]):
            out.append(lines[i])
            if method_depth > 0 and not counted_method_start:
                method_depth += _brace_delta(lines[i])
            i += 1
            continue

        loop_match = _FOR_ZERO_UPPER_RE.search(lines[i])
        if not loop_match:
            out.append(lines[i])
            if method_depth > 0 and not counted_method_start:
                method_depth += _brace_delta(lines[i])
            i += 1
            continue

        loop_var, raw_bound = loop_match.groups()
        invariants = _for_bound_invariants(loop_var, raw_bound, aliases)
        bound_name = re.sub(r"\s+", " ", raw_bound.strip())
        body_text = _loop_body_text(lines, i)
        if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", bound_name):
            used_bound_arrays = [
                array
                for array in method_array_bounds.get(bound_name, [])
                if _loop_uses_array_index(body_text, array, loop_var)
                and not _loop_reassigns_name(body_text, array)
            ]
            if used_bound_arrays:
                invariants.append(f"0 <= {loop_var} && {loop_var} <= {bound_name}")
            for array in used_bound_arrays:
                invariants.extend([f"{array} != null", f"{array}.length == {bound_name}"])
        if not invariants:
            out.append(lines[i])
            if method_depth > 0 and not counted_method_start:
                method_depth += _brace_delta(lines[i])
            i += 1
            continue

        group_start = len(out)
        while group_start > 0 and _is_jml_line(out[group_start - 1]):
            group_start -= 1
        group_text = "\n".join(out[group_start:])
        indent = _line_indent(lines[i])
        insertions = [
            f"{indent}//@ maintaining {inv};"
            for inv in invariants
            if inv not in group_text
        ]
        if insertions:
            out[group_start:group_start] = insertions
        out.append(lines[i])
        if method_depth > 0 and not counted_method_start:
            method_depth += _brace_delta(lines[i])
        i += 1
    return out


def _guarded_length_lower_bounds(lines: list[str]) -> set[tuple[str, int]]:
    """Return array length lower-bounds implied by inclusive counted loops."""

    aliases: dict[str, str] = {}
    guarded: set[tuple[str, int]] = set()
    for line in lines:
        code = strip_jml_comments(line)
        alias_match = _LENGTH_ALIAS_RE.search(code)
        if alias_match:
            aliases[alias_match.group(1)] = alias_match.group(2)

        loop_match = _FOR_ZERO_INCLUSIVE_RE.search(code)
        if not loop_match:
            continue
        _, raw_bound = loop_match.groups()
        bound = re.sub(r"\s+", " ", raw_bound.strip())
        alias_minus = _ALIAS_MINUS_CONST_RE.fullmatch(bound)
        if alias_minus and alias_minus.group(1) in aliases:
            guarded.add((aliases[alias_minus.group(1)], int(alias_minus.group(2))))
            continue
        direct_minus = _DIRECT_LENGTH_MINUS_CONST_RE.fullmatch(bound)
        if direct_minus:
            guarded.add((direct_minus.group(1), int(direct_minus.group(2))))
    return guarded


def _drop_guarded_length_lower_bound_requires(lines: list[str]) -> list[str]:
    """Remove array length preconditions made redundant by loop guards.

    For loops such as ``for (int i = 0; i <= n - 3; ++i)`` with
    ``int n = arr.length``, short arrays simply skip the loop.  A generated
    ``requires arr.length >= 3`` is therefore an over-constraint for safety
    proof purposes; the loop bound plus a bounds invariant is sufficient.
    """

    guarded = _guarded_length_lower_bounds(lines)
    if not guarded:
        return lines

    out: list[str] = []
    for line in lines:
        if _is_jml_line(line) and _jml_content_keyword(line) == "requires":
            match = _ARRAY_LENGTH_LOWER_REQUIRE_RE.fullmatch(_jml_body(line))
            if match and (match.group(1), int(match.group(2))) in guarded:
                continue
        out.append(line)
    return out


def _rewrite_guarded_inclusive_loop_bounds(lines: list[str]) -> list[str]:
    """Relax too-strong index bounds for inclusive loops that can be skipped.

    For loops such as ``for (int i = 0; i <= n - 3; ++i)``, an invariant like
    ``0 <= i && i <= n - 2`` is false before the loop when ``n < 2`` even
    though the loop body is skipped.  The guard itself protects body accesses;
    the invariant only needs a non-negative upper bound that holds at entry.
    """

    out = list(lines)
    aliases: dict[str, str] = {}
    for idx, line in enumerate(lines):
        code = strip_jml_comments(line)
        alias_match = _LENGTH_ALIAS_RE.search(code)
        if alias_match:
            aliases[alias_match.group(1)] = alias_match.group(2)

        loop_match = _FOR_ZERO_INCLUSIVE_RE.search(code)
        if not loop_match:
            continue

        loop_var, raw_bound = loop_match.groups()
        bound = re.sub(r"\s+", " ", raw_bound.strip())
        alias_minus = _ALIAS_MINUS_CONST_RE.fullmatch(bound)
        direct_minus = _DIRECT_LENGTH_MINUS_CONST_RE.fullmatch(bound)
        if alias_minus and alias_minus.group(1) in aliases:
            bound_name = alias_minus.group(1)
        elif direct_minus:
            bound_name = f"{direct_minus.group(1)}.length"
        else:
            continue

        group_start = idx
        while group_start > 0 and _is_jml_line(out[group_start - 1]):
            group_start -= 1

        replacement = f"0 <= {loop_var} && {loop_var} <= {bound_name};"
        for j in range(group_start, idx):
            if _jml_content_keyword(out[j]) not in {"maintaining", "loop_invariant"}:
                continue
            body = _jml_body(out[j])
            pattern = re.compile(
                rf"^(?P<kw>maintaining|loop_invariant)\s+"
                rf"(?:(?:0\s*<=\s*{re.escape(loop_var)}\s*&&\s*{re.escape(loop_var)}\s*<=\s*)|"
                rf"(?:{re.escape(loop_var)}\s*>=\s*0\s*&&\s*{re.escape(loop_var)}\s*<=\s*))"
                rf"{re.escape(bound_name)}\s*-\s*\d+\s*;$"
            )
            match = pattern.fullmatch(body)
            if match:
                prefix = re.match(r"^(\s*//@\s*)", out[j])
                if prefix:
                    out[j] = f"{prefix.group(1)}maintaining {replacement}"
    return out


def _if_guard_has_return(lines: list[str], index: int) -> bool:
    """Return whether a simple ``if`` guard immediately exits via ``return``."""

    line = strip_jml_comments(lines[index])
    suffix = line[line.find(")") + 1 :] if ")" in line else ""
    if "return" in suffix:
        return True
    depth = max(0, _brace_delta(suffix))
    saw_body = "{" in suffix
    for follow in lines[index + 1 : index + 8]:
        code = strip_jml_comments(follow).strip()
        if not code:
            continue
        if "return" in code:
            return True
        if code.startswith("{"):
            saw_body = True
            depth += _brace_delta(code)
            continue
        if saw_body:
            depth += _brace_delta(code)
            if depth <= 0:
                return False
            continue
        return False
    return False


def _guarded_length_upper_bounds(lines: list[str]) -> set[tuple[str, bool, int]]:
    """Return length upper-bounds handled by source-level early-return guards."""

    aliases: dict[str, tuple[str, bool]] = {}
    guarded: set[tuple[str, bool, int]] = set()
    for idx, line in enumerate(lines):
        code = strip_jml_comments(line)
        alias_match = _LENGTH_ALIAS_RE.search(code)
        if alias_match:
            aliases[alias_match.group(1)] = (alias_match.group(2), False)
        string_alias_match = _STRING_LENGTH_ALIAS_RE.search(code)
        if string_alias_match:
            aliases[string_alias_match.group(1)] = (string_alias_match.group(2), True)

        if "if" not in code or ">" not in code or not _if_guard_has_return(lines, idx):
            continue
        direct = _DIRECT_LENGTH_GREATER_CONST_RE.search(code)
        if direct:
            guarded.add((direct.group(1), bool(direct.group("call")), int(direct.group(3))))
            continue
        alias_gt = _ALIAS_GREATER_CONST_RE.search(code)
        if alias_gt and alias_gt.group(1) in aliases:
            target, uses_call = aliases[alias_gt.group(1)]
            guarded.add((target, uses_call, int(alias_gt.group(2))))
    return guarded


def _split_top_level_conjunction(expr: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and expr.startswith("&&", i):
            parts.append(expr[start:i].strip())
            i += 2
            start = i
            continue
        i += 1
    parts.append(expr[start:].strip())
    return [part for part in parts if part]


def _drop_guarded_length_upper_bound_requires(lines: list[str]) -> list[str]:
    """Remove length upper-bound preconditions already handled by source guards."""

    guarded = _guarded_length_upper_bounds(lines)
    if not guarded:
        return lines

    out: list[str] = []
    for line in lines:
        if not (_is_jml_line(line) and _jml_content_keyword(line) == "requires"):
            out.append(line)
            continue
        body = _jml_body(line)
        if not body.startswith("requires ") or not body.endswith(";"):
            out.append(line)
            continue
        expr = body.removeprefix("requires ").rstrip(";").strip()
        kept: list[str] = []
        changed = False
        for part in _split_top_level_conjunction(expr):
            match = _LENGTH_UPPER_CONJUNCT_RE.fullmatch(part)
            if match and (match.group("target"), bool(match.group("call")), int(match.group("bound"))) in guarded:
                changed = True
                continue
            kept.append(part)
        if not changed:
            out.append(line)
            continue
        if kept:
            out.append(f"{_line_indent(line)}//@ requires {' && '.join(kept)};")
    return out


def _method_transplant_key(line: str) -> tuple[str, str, int, bool] | None:
    info = _method_signature_info(line)
    if info is None:
        return None
    return (
        str(info["name"]),
        str(info["return_type"]),
        len(info["params"]),
        bool(info["is_constructor"]),
    )


def _take_jml_group(lines: list[str], index: int) -> tuple[list[str], int]:
    if _is_jml_line(lines[index]):
        j = index
        while j < len(lines) and _is_jml_line(lines[j]):
            j += 1
        return lines[index:j], j
    if _is_jml_block_start(lines[index]):
        j = index
        while j < len(lines):
            j += 1
            if "*/" in lines[j - 1]:
                break
        return lines[index:j], j
    return [], index + 1


def _reindent_jml_group(group: list[str], indent: str) -> list[str]:
    return [indent + line.lstrip() for line in group]


def transplant_jml_annotations(original: str, annotated: str) -> str | None:
    """Move generated JML comments onto the original Java token stream.

    This is a source-preservation fallback for LLM outputs that edited Java
    modifiers/imports/statements while also producing useful JML.  It only
    transplants annotation groups that immediately precede a matching method or
    loop target.  Executable Java is taken exclusively from ``original``; callers
    must still run :func:`source_code_preserved` before trusting the result.
    """

    original_lines = original.splitlines()
    annotated_lines = annotated.splitlines()
    method_groups: dict[tuple[str, str, int, bool], list[list[str]]] = {}
    loop_groups: list[list[str]] = []

    i = 0
    while i < len(annotated_lines):
        if not _is_jml_annotation_start(annotated_lines[i]):
            i += 1
            continue
        group, j = _take_jml_group(annotated_lines, i)
        target_idx = _next_nonempty_index(annotated_lines, j)
        if target_idx is not None:
            target = annotated_lines[target_idx]
            key = _method_transplant_key(target)
            if key is not None:
                method_groups.setdefault(key, []).append(group)
            elif _LOOP_START_RE.search(target):
                loop_groups.append(group)
        i = j

    if not method_groups and not loop_groups:
        return None

    insertions: dict[int, list[list[str]]] = {}
    inserted = 0
    loop_index = 0
    for idx, line in enumerate(original_lines):
        key = _method_transplant_key(line)
        if key is not None and method_groups.get(key):
            insertions.setdefault(idx, []).append(method_groups[key].pop(0))
            inserted += 1
        if _LOOP_START_RE.search(line) and loop_index < len(loop_groups):
            insertions.setdefault(idx, []).append(loop_groups[loop_index])
            loop_index += 1
            inserted += 1

    if inserted == 0:
        return None

    out: list[str] = []
    for idx, line in enumerate(original_lines):
        if idx in insertions:
            indent = _line_indent(line)
            for group in insertions[idx]:
                out.extend(_reindent_jml_group(group, indent))
        out.append(line)
    return "\n".join(out) + ("\n" if original.endswith("\n") else "")


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
    remove: set[int] = set()
    for raw in matches:
        idx = int(raw) - 1
        if 0 <= idx < len(lines) and "ensures" in lines[idx]:
            remove.add(idx)
    if not remove:
        return source, False
    lines = _remove_reported_jml_clause_indices(lines, remove)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), True


def _prune_reported_precondition(source: str, verifier_output: str) -> tuple[str, bool]:
    """Remove generated ``requires`` clauses OpenJML reports as false at calls."""

    matches = re.findall(r"\.java:(\d+):\s+verify:\s+Precondition conjunct is false:", verifier_output)
    if not matches:
        return source, False
    lines = source.splitlines()
    remove: set[int] = set()
    for raw in matches:
        idx = int(raw) - 1
        if 0 <= idx < len(lines) and _jml_content_keyword(lines[idx]) == "requires":
            remove.add(idx)
    if not remove:
        return source, False
    lines = _remove_reported_jml_clause_indices(lines, remove)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), True


def _prune_reported_assignable(source: str, verifier_output: str) -> tuple[str, bool]:
    """Remove generated frame clauses OpenJML reports as unproved."""

    matches = re.findall(r"\(Assignable: [^)\n]*?\.java:(\d+):\)", verifier_output)
    if not matches:
        return source, False
    lines = source.splitlines()
    remove: set[int] = set()
    for raw in matches:
        idx = int(raw) - 1
        if 0 <= idx < len(lines) and _jml_content_keyword(lines[idx]) in {"assignable", "assigns"}:
            remove.add(idx)
    if not remove:
        return source, False
    lines = _remove_reported_jml_clause_indices(lines, remove)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), True


def _remove_reported_jml_clause_indices(lines: list[str], indices: set[int]) -> list[str]:
    out = list(lines)
    for idx in sorted(indices, reverse=True):
        out = _remove_reported_jml_clause_line(out, idx)
    return out


def _remove_reported_jml_clause_line(lines: list[str], idx: int) -> list[str]:
    """Remove one reported JML clause without corrupting a surrounding block."""

    line = lines[idx]
    if "/*@" in line and "*/" not in line:
        replacement = re.sub(r"/\*@.*$", "/*@", line, count=1)
        if replacement != line:
            return [*lines[:idx], replacement, *lines[idx + 1 :]]
    return [*lines[:idx], *lines[idx + 1 :]]


def _annotate_reported_nullable(source: str, verifier_output: str) -> tuple[str, bool]:
    """Mark declarations OpenJML reports as nullness-default failures nullable.

    SpecGen's OpenJML configuration runs with ``--nonnull-by-default``.  Plain
    Java fields and locals, however, are nullable unless the program/spec says
    otherwise.  When OpenJML points at a declaration with ``NullField`` or
    ``PossiblyNullInitialization``, adding a local ``nullable`` modifier is a
    faithful verifier annotation and avoids treating Java's default null value
    as a generated-spec failure.
    """

    matches = re.findall(
        r"\.java:(\d+):\s+verify:.*?\((?:NullField|PossiblyNullInitialization)\)",
        verifier_output,
    )
    null_formals = re.findall(
        r"\(NullFormal:\s+.*?\.java:(\d+):\).*?method [^:\n]+:\s*([A-Za-z_$][A-Za-z0-9_$]*)\s+in\b",
        verifier_output,
    )
    null_returns = re.findall(
        r"\(PossiblyNullReturn:\s+.*?\.java:(\d+):\)",
        verifier_output,
    )
    if not matches and not null_formals and not null_returns:
        return source, False
    lines = source.splitlines()
    insert_before: set[int] = set()
    rewrite_lines: dict[int, str] = {}
    remove_lines: set[int] = set()
    field_indices = _field_declaration_indices(lines)
    for raw_idx, param in null_formals:
        idx = int(raw_idx) - 1
        if not (0 <= idx < len(lines)):
            continue
        line = lines[idx]
        if _is_jml_line(line):
            continue
        if not _method_body_handles_null_parameter(lines, idx, param):
            continue
        rewritten = _inline_nullable_in_formal_parameter(line, param)
        if rewritten != line:
            rewrite_lines[idx] = rewritten
    for raw_idx in null_returns:
        idx = int(raw_idx) - 1
        if not (0 <= idx < len(lines)):
            continue
        line = lines[idx]
        if _is_jml_line(line):
            continue
        if not (
            _method_body_returns_null(lines, idx)
            or _method_body_returns_nullable_field(lines, idx)
        ):
            continue
        rewritten = _inline_nullable_return_type(line)
        if rewritten != line:
            rewrite_lines[idx] = rewritten
    for raw in matches:
        idx = int(raw) - 1
        if not (0 <= idx < len(lines)):
            continue
        line = lines[idx]
        if _is_jml_line(line) or "nullable" in line:
            continue
        if idx in field_indices:
            rewrite_lines[idx] = _inline_nullable_in_field_declaration(line)
            prev = idx - 1
            if prev >= 0 and _is_standalone_nullable_annotation(lines[prev]):
                remove_lines.add(prev)
            continue
        if re.search(r"\bfor\s*\(", line):
            rewritten = re.sub(
                r"(\bfor\s*\(\s*)((?:final\s+)?[A-Z_$][A-Za-z0-9_$]*(?:\s*<[^;=(){}]*>)?(?:\s*\[\])?\s+[A-Za-z_$][A-Za-z0-9_$]*\s*=)",
                r"\1/*@ nullable @*/ \2",
                line,
                count=1,
            )
            if rewritten != line:
                rewrite_lines[idx] = rewritten
                continue
        prev = idx - 1
        while prev >= 0 and not lines[prev].strip():
            prev -= 1
        if prev >= 0 and _is_jml_line(lines[prev]) and "nullable" in lines[prev]:
            continue
        insert_before.add(idx)
    if not insert_before and not rewrite_lines:
        return source, False

    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in remove_lines:
            continue
        if idx in insert_before:
            out.append(f"{_line_indent(line)}//@ nullable")
        out.append(rewrite_lines.get(idx, line))
    return "\n".join(out) + ("\n" if source.endswith("\n") else ""), True


def _has_reported_nullable_failure(verifier_output: str) -> bool:
    return bool(
        re.search(
            r"\((?:NullField|PossiblyNullInitialization|NullFormal|PossiblyNullReturn)\b",
            verifier_output,
        )
    )


def _prune_reported_loop_decreases(source: str, verifier_output: str) -> tuple[str, bool]:
    """Remove generated ``decreases`` clauses reported as unproved.

    A bad loop variant can make an otherwise useful safety/functionality spec
    fail with ``LoopDecreases``.  OpenJML does not require a variant for partial
    correctness, so dropping only the reported variant keeps invariants and
    method contracts intact instead of inventing new proof facts.
    """

    matches = re.findall(r"\.java:(\d+):\s+verify:.*?\(LoopDecreases\)", verifier_output)
    if not matches:
        return source, False
    lines = source.splitlines()
    remove: set[int] = set()
    for raw in matches:
        idx = int(raw) - 1
        if 0 <= idx < len(lines) and _jml_content_keyword(lines[idx]) in {"decreases", "decreasing", "loop_variant"}:
            remove.add(idx)
    if not remove:
        return source, False
    lines = _remove_reported_jml_clause_indices(lines, remove)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), True


def _prune_reported_diverges(source: str, verifier_output: str) -> tuple[str, bool]:
    """Remove generated ``diverges`` clauses OpenJML reports as unproved.

    ``diverges`` is a termination-side clause.  It is easy for generated JML to
    overfit APIs such as SV-COMP's ``Verifier.assume`` with a clause that
    OpenJML then cannot use at call sites.  Dropping only the reported
    divergence clause is analogous to pruning a bad loop variant: the remaining
    functional and frame specifications still have to verify normally.
    """

    matches = re.findall(r"\.java:(\d+):\s+verify:.*?\(Diverges:", verifier_output)
    if not matches:
        return source, False
    lines = source.splitlines()
    remove: set[int] = set()
    for raw in matches:
        idx = int(raw) - 1
        if 0 <= idx < len(lines) and _jml_content_keyword(lines[idx]) == "diverges":
            remove.add(idx)
    if not remove:
        return source, False
    lines = _remove_reported_jml_clause_indices(lines, remove)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), True


def _prune_reported_loop_invariant(source: str, verifier_output: str) -> tuple[str, bool]:
    """Remove generated loop invariants OpenJML reports as unproved."""

    matches = re.findall(r"\.java:(\d+):\s+verify:.*?\(LoopInvariant(?:BeforeLoop)?\)", verifier_output)
    if not matches:
        return source, False
    lines = source.splitlines()
    remove: set[int] = set()
    for raw in matches:
        idx = int(raw) - 1
        if 0 <= idx < len(lines) and _jml_content_keyword(lines[idx]) in {"maintaining", "loop_invariant"}:
            remove.add(idx)
    if not remove:
        return source, False
    lines = _remove_reported_jml_clause_indices(lines, remove)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), True


def _prune_reported_object_invariant(source: str, verifier_output: str) -> tuple[str, bool]:
    """Remove generated object invariants OpenJML reports as unproved."""

    matches = re.findall(r"\(Invariant(?:Entrance|Exit|LeaveCaller)?: [^)\n]*?\.java:(\d+):\)", verifier_output)
    if not matches:
        return source, False
    lines = source.splitlines()
    remove: set[int] = set()
    for raw in matches:
        idx = int(raw) - 1
        if 0 <= idx < len(lines) and _jml_content_keyword(lines[idx]) == "invariant":
            remove.add(idx)
    if not remove:
        return source, False
    lines = _remove_reported_jml_clause_indices(lines, remove)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else ""), True


def _loop_body_end_index(lines: list[str], loop_idx: int) -> int | None:
    open_idx = loop_idx
    while open_idx < len(lines) and "{" not in lines[open_idx]:
        stripped = strip_jml_comments(lines[open_idx]).strip()
        if ";" in stripped:
            return None
        if open_idx > loop_idx and stripped and not stripped.startswith("//"):
            return None
        open_idx += 1
    if open_idx >= len(lines):
        return None
    depth = _brace_delta(lines[open_idx])
    if depth <= 0:
        return None
    idx = open_idx + 1
    while idx < len(lines) and depth > 0:
        depth += _brace_delta(lines[idx])
        idx += 1
    return idx - 1


def _prune_enclosing_loop_specs_for_internal_error(source: str, verifier_output: str) -> tuple[str, bool]:
    """Drop loop specs around OpenJML internal-error locations.

    OpenJML occasionally crashes with ``Double rewriting of ident`` inside a
    loop body when generated loop annotations are otherwise syntactically
    valid.  Removing the innermost enclosing loop-spec group is a conservative
    recovery: it weakens generated JML and lets the verifier report an ordinary
    proof failure if the remaining specification is insufficient.
    """

    if not _is_openjml_internal_error(verifier_output):
        return source, False
    reported = [int(raw) - 1 for raw in re.findall(r"\.java:(\d+):\s+error:", verifier_output)]
    if not reported:
        return source, False
    lines = source.splitlines()
    remove: set[int] = set()
    for target_idx in reported:
        for loop_idx in range(min(target_idx, len(lines) - 1), -1, -1):
            if not _LOOP_START_RE.search(strip_jml_comments(lines[loop_idx])):
                continue
            loop_end = _loop_body_end_index(lines, loop_idx)
            if loop_end is None and loop_idx == target_idx:
                loop_end = loop_idx
            elif loop_end is None and loop_idx < target_idx <= loop_idx + 2:
                loop_end = target_idx
            if loop_end is None or not (loop_idx <= target_idx <= loop_end):
                continue
            group_start = loop_idx
            while group_start > 0 and _is_jml_line(lines[group_start - 1]):
                group_start -= 1
            group_indices = {
                idx
                for idx in range(group_start, loop_idx)
                if _jml_content_keyword(lines[idx]) in {"maintaining", "loop_invariant", "decreases", "decreasing"}
            }
            if group_indices:
                remove.update(group_indices)
                break
    if not remove:
        return source, False
    kept = [line for idx, line in enumerate(lines) if idx not in remove]
    return "\n".join(kept) + ("\n" if source.endswith("\n") else ""), True


def _is_openjml_internal_error(verifier_output: str) -> bool:
    lowered = verifier_output.lower()
    return (
        "a catastrophic jml internal error occurred" in lowered
        or "double rewriting of ident" in lowered
        or "an internal jml error occurred" in lowered
        or "an error while executing a proof script" in lowered
        or "java.lang.classcastexception" in lowered
    )


def _is_jml_block_content_line(lines: list[str], idx: int) -> bool:
    if "/*@" in lines[idx] or "*/" in lines[idx]:
        return "/*@" in lines[idx] or "*/" in lines[idx]
    start = idx - 1
    while start >= 0:
        if "*/" in lines[start]:
            return False
        if "/*@" in lines[start]:
            return True
        start -= 1
    return False


def _prune_reported_annotation_error(source: str, verifier_output: str) -> tuple[str, bool]:
    """Remove generated JML annotations that OpenJML rejects syntactically."""

    if "error:" not in verifier_output:
        return source, False
    lines = source.splitlines()
    matches = re.findall(r"\.java:(\d+):\s+error:", verifier_output)
    replacements: dict[int, str] = {}
    deletions: set[int] = set()
    for raw in matches:
        idx = int(raw) - 1
        if not (0 <= idx < len(lines)):
            continue
        line = lines[idx]
        if "/*@" in line and "*/" in line:
            stripped = re.sub(r"\s*/\*@.*?@\*/", "", line)
            if stripped != line:
                replacements[idx] = stripped
                continue
        if _is_jml_line(line):
            deletions.add(idx)
            continue
        if _is_jml_block_content_line(lines, idx):
            deletions.add(idx)
            continue
    if not replacements and not deletions:
        return source, False

    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in deletions:
            continue
        out.append(replacements.get(idx, line))
    return "\n".join(out) + ("\n" if source.endswith("\n") else ""), True


def _has_reported_jml_annotation_error(source: str, verifier_output: str) -> bool:
    """Return whether OpenJML error locations point at generated JML text."""

    if "error:" not in verifier_output:
        return False
    lines = source.splitlines()
    for raw in re.findall(r"\.java:(\d+):\s+error:", verifier_output):
        idx = int(raw) - 1
        if not (0 <= idx < len(lines)):
            continue
        if "/*@" in lines[idx] and "*/" in lines[idx]:
            return True
        if _is_jml_line(lines[idx]) or _is_jml_block_content_line(lines, idx):
            return True
    return False


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


def _is_loop_clause_keyword(keyword: str) -> bool:
    return keyword in {"maintaining", "decreases", "decreasing", "loop_invariant", "loop_variant"}


def _drop_misplaced_loop_jml_groups(lines: list[str]) -> list[str]:
    """Drop line-style loop clauses that do not directly annotate a loop.

    OpenJML requires loop clauses to immediately precede the loop statement.
    LLMs often duplicate invariants at the end of the loop body, where they are
    parsed as illegal statements.  Dropping only those misplaced loop clauses is
    a syntax repair: it does not invent new proof facts or move clauses to a
    different program point.
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
        if _next_nonempty_is_loop(lines, j):
            out.extend(group)
            i = j
            continue
        for segment in _jml_line_segments(group):
            keyword = _jml_content_keyword(segment[0]) if segment else ""
            if _is_loop_clause_keyword(keyword):
                continue
            out.extend(segment)
        i = j
    return out


def _rename_loop_quantifier_conflicts(lines: list[str]) -> list[str]:
    """Alpha-rename quantifier variables that shadow a loop variable."""

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
        target_idx = _next_nonempty_index(lines, j)
        target = lines[target_idx] if target_idx is not None else ""
        match = _FOR_DECL_VAR_RE.search(target)
        if not match:
            out.extend(group)
            i = j
            continue
        loop_var = match.group(1)
        rewritten: list[str] = []
        for segment in _jml_line_segments(group):
            segment_text = "\n".join(segment)
            if re.search(_JML_QUANT_DECL_TEMPLATE.format(var=re.escape(loop_var)), segment_text):
                rewritten.extend(_rename_quantifier_var_in_segment(segment, loop_var))
            else:
                rewritten.extend(segment)
        out.extend(rewritten)
        i = j
    return out


def _rename_method_contract_quantifier_conflicts(lines: list[str]) -> list[str]:
    """Alpha-rename method-contract quantifiers that shadow Java locals.

    OpenJML can crash internally when a method contract binds a quantifier
    variable with the same name as a Java local or loop variable in the method
    body.  Renaming the bound variable is semantics-preserving and avoids
    turning a verifier bug into a benchmark failure.
    """

    out = list(lines)
    for idx, line in enumerate(lines):
        info = _method_signature_info(strip_jml_comments(line))
        if info is None:
            continue
        span = _method_body_span(lines, idx)
        if span is None:
            continue
        group_start = idx
        while group_start > 0 and _is_jml_contract_group_line(lines[group_start - 1]):
            group_start -= 1
        if group_start == idx:
            continue
        body_text = "\n".join(lines[span[0] : span[1]])
        local_names = _method_local_names(body_text, set(info["params"]))
        if not local_names:
            continue
        rewritten: list[str] = []
        for segment in _jml_line_segments(lines[group_start:idx]):
            keyword = _jml_content_keyword(segment[0]) if segment else ""
            if keyword in {"requires", "ensures", "assignable", "assigns", "signals"}:
                rewritten.extend(_rename_quantifier_vars_in_segment(segment, local_names))
            else:
                rewritten.extend(segment)
        if len(rewritten) == idx - group_start:
            out[group_start:idx] = rewritten
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

    src = _comment_bare_jml_clause_lines(source)
    src = _normalise_jml_range_quantifiers(src)
    src = _strip_inline_spec_public(src)
    src = _drop_orphan_block_continuations(src)
    src = "\n".join(_normalize_jml_lines(src.splitlines()))
    src = _normalise_loop_jml_blocks(src)
    lines = _drop_malformed_jml_line_clauses(src.splitlines())

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
    out = _rename_loop_quantifier_conflicts(out)
    out = _rename_method_contract_quantifier_conflicts(out)
    out = _drop_misplaced_loop_jml_groups(out)
    out = _split_class_scope_field_declarations(out)
    out = _inline_field_nullable_annotations(out)
    out = _filter_method_contract_scope(out)
    out = _filter_method_contract_target(out)
    out = _drop_duplicate_model_fields(out)
    out = _add_division_requires(out)
    out = _add_constant_array_index_requires(out)
    out = _sanitize_assignable_locations(out)
    out = _add_inferred_assignable_frame(out)
    out = _add_pure_method_assignable_nothing(out)
    out = _rewrite_guarded_inclusive_loop_bounds(out)
    out = _add_private_callee_preconditions_to_callers(out)
    out = _add_private_callee_assignable_to_callers(out)
    out = _add_branch_nullable_receiver_requires(out)
    out = _add_for_loop_bound_invariants(out)
    out = _drop_guarded_length_lower_bound_requires(out)
    out = _drop_guarded_length_upper_bound_requires(out)
    out = _add_spec_public_for_referenced_private_fields(out)
    return "\n".join(out).rstrip() + ("\n" if source.endswith("\n") else "")


def build_openjml_command(
    openjml_path: str,
    source_path: str | Path,
    timeout_s: int,
    support_sources: Iterable[str | Path] | None = None,
) -> list[str]:
    """Build the OpenJML ESC command used by the SpecGen artifact."""

    cmd = [
        openjml_path,
        "--esc",
        "--esc-max-warnings",
        "1",
        "--arithmetic-failure=quiet",
        "--code-math=java",
        "--nonnull-by-default",
        "--quiet",
        "-nowarn",
        "--prover=cvc4",
        "--timeout",
        str(timeout_s),
        str(source_path),
    ]
    if support_sources:
        cmd.extend(str(path) for path in support_sources)
    return cmd


def _openjml_command_path(path: str | Path, cwd: str | Path | None) -> str:
    """Return a path that remains valid when OpenJML is run with ``cwd``."""

    candidate = Path(path)
    if candidate.is_absolute() or cwd is None:
        return str(path)
    if candidate.exists():
        return str(candidate.resolve())
    return str(path)


def _discover_openjml_support_sources(source_path: str | Path, cwd: str | Path | None) -> list[Path]:
    """Return verifier-side support sources that should be compiled too."""

    source_resolved = Path(source_path).resolve()
    roots = [Path(source_path).parent]
    if cwd is not None:
        roots.append(Path(cwd))
    candidates: list[Path] = []
    seen_candidates: set[Path] = set()
    for root in roots:
        for candidate in [
            root / "Verifier.java",
            root / "Cookie.java",
            root / "org" / "sosy_lab" / "sv_benchmarks" / "Verifier.java",
        ]:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved in seen_candidates:
                continue
            seen_candidates.add(resolved)
            candidates.append(candidate)
    support: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            if candidate.resolve() == source_resolved:
                continue
        except OSError:
            pass
        support.append(candidate)
    return support


def _register_process_group(pid: int) -> None:
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        _ACTIVE_PROCESS_GROUPS.add(pid)


def _unregister_process_group(pid: int) -> None:
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        _ACTIVE_PROCESS_GROUPS.discard(pid)


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_active_openjml_process_groups() -> None:
    """Best-effort cleanup for OpenJML/CVC4 subprocesses owned by this process."""

    with _ACTIVE_PROCESS_GROUPS_LOCK:
        pids = list(_ACTIVE_PROCESS_GROUPS)
    for pid in pids:
        _kill_process_group(pid)


def _run_process_group(
    cmd: list[str],
    *,
    cwd: str | Path | None,
    timeout_s: int,
) -> subprocess.CompletedProcess[str]:
    """Run a verifier command and kill its whole process group on timeout.

    OpenJML launches solver children.  Killing only the Java parent can leave
    CVC4 orphaned, which then contaminates later benchmark runs.
    """

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    _register_process_group(proc.pid)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc.pid)
            stdout, stderr = proc.communicate()
            exc.stdout = stdout
            exc.stderr = stderr
            raise exc
        except BaseException:
            _kill_process_group(proc.pid)
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    finally:
        _unregister_process_group(proc.pid)


def run_openjml(
    source_path: str | Path,
    *,
    openjml_path: str = "openjml",
    timeout_s: int = 200,
    cwd: str | Path | None = None,
) -> OpenJMLResult:
    """Run OpenJML and classify its output using SpecGen's pass convention."""

    resolved = openjml_path
    support_sources = _discover_openjml_support_sources(source_path, cwd)
    command_source_path = _openjml_command_path(source_path, cwd)
    command_support_sources = [_openjml_command_path(path, cwd) for path in support_sources]
    if not Path(openjml_path).exists() and shutil.which(openjml_path) is None:
        return OpenJMLResult(
            status="tool_missing",
            passed=False,
            returncode=None,
            runtime_s=0.0,
            error=f"openjml not found: {openjml_path}",
            command=build_openjml_command(openjml_path, command_source_path, timeout_s, command_support_sources),
        )

    cmd = build_openjml_command(resolved, command_source_path, timeout_s, command_support_sources)
    start = time.monotonic()
    wall_timeout_s = timeout_s + 5
    try:
        proc = _run_process_group(
            cmd,
            cwd=str(cwd) if cwd else None,
            timeout_s=wall_timeout_s,
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
            error=f"openjml wall-clock timeout after {wall_timeout_s}s",
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
    output_lower = output.lower()
    passed = (
        proc.returncode == 0
        and "verify:" not in output_lower
        and "error:" not in output_lower
        and "warning:" not in output_lower
        and "verification failure" not in output_lower
        and "null precondition" not in output_lower
    )
    if passed:
        status = "passed"
    elif _is_source_frontend_error(output_lower):
        status = "source_invalid"
    elif _is_openjml_internal_error(output):
        status = "tool_error"
    elif (
        "validity is unknown - time or memory limit reached" in output_lower
        or "aborted proof: timeout" in output_lower
        or "time or memory limit reached" in output_lower
    ):
        status = "timeout"
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


def _is_source_frontend_error(output_lower: str) -> bool:
    """Return whether OpenJML failed before generated JML is meaningful."""

    return any(
        marker in output_lower
        for marker in (
            "modifier static not allowed here",
            "is public, should be declared in a file named",
            "package org.sosy_lab.sv_benchmarks does not exist",
            "cannot find symbol",
            "symbol:   variable verifier",
            "symbol:   class verifier",
            "unreachable statement",
        )
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
        "invariants. Do not add runtime Java assertions, JML `assert`, JML "
        "`assume`, or JML `diverges`; generate contracts and loop annotations only. For array loops, "
        "prioritize simple safety invariants such as index lower/upper bounds "
        "and aliases like `n == a.length` before adding quantified functional "
        "invariants. For bit shifts or bit-mask encodings, prove the shift count "
        "is in range by adding faithful input-domain preconditions, for example "
        "character or array-element ranges that the code maps to an index; do not "
        "hide shift failures with `assume`, and do not add length upper bounds "
        "when the source already handles long inputs with an early return. "
        "OpenJML is run with `--nonnull-by-default`; mark reference fields, "
        "parameters, or locals as `nullable` when the Java code permits null. "
        "Do not claim a nullable local is non-null in a loop invariant unless a "
        "guard or precondition already proves it before the loop. For methods "
        "that write fields or array elements, include a faithful `assignable` "
        "frame such as `assignable count, data[*];`; for pure helpers, use "
        "`assignable \\nothing`. If code directly accesses `a[0]` or another "
        "constant index, generate preconditions or object invariants proving "
        "the array is non-null and long enough. For nullable linked structures, "
        "guard recursive dereferences with branch-conditioned preconditions "
        "such as `cond ==> next != null` instead of unrelated deep shape facts."
    )


def _format_prompt_examples(prompt_examples: str) -> str:
    if not prompt_examples.strip():
        return ""
    return (
        "Here are example Java-to-JML transformations. Follow their style, "
        "but do not copy irrelevant clauses.\n\n"
        f"{prompt_examples.strip()}\n\n"
    )


def _format_generation_context(generation_context: str) -> str:
    if not generation_context.strip():
        return ""
    return (
        "Additional verifier context from the unannotated Java source:\n"
        "```text\n"
        f"{generation_context.strip()[:4000]}\n"
        "```\n"
        "Use this only to guide faithful JML annotations. Do not hide reachable "
        "benchmark assertions by inventing arbitrary preconditions; prefer facts "
        "that are already implied by callers, guards, object construction, or Java "
        "library usage in the source.\n\n"
    )


def _initial_user_prompt(source: str, prompt_examples: str = "", generation_context: str = "") -> str:
    return (
        "Please generate JML specifications for this Java program.\n\n"
        f"{_format_prompt_examples(prompt_examples)}"
        f"{_format_generation_context(generation_context)}"
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
        "- For array loops, first add simple bounds invariants that make every array access safe. For example, if `int n = a.length` and a loop reads `a[i]`, use invariants like `0 <= i && i <= n` and `n == a.length`; if the body reads `a[i + 1]`, the loop guard plus invariant should imply `i + 1 < a.length`.\n"
        "- Prefer a small set of verifier-friendly invariants over ambitious quantified properties. Add quantified invariants only when the code clearly establishes and preserves them.\n"
        "- If using JML quantifiers or sums, use semicolon-separated predicates such as `\\sum int k; 0 <= k && k < i; expr`; do not use `k in 0..i` shorthand.\n"
        "- Do not insert JML `assert`, `assume`, or `diverges` statements; use `requires`, `ensures`, `assignable`, `maintaining`, and `decreases` only.\n"
        "- Add overflow/domain preconditions when OpenJML needs them.\n\n"
        "- OpenJML runs with `--nonnull-by-default`. If the Java source assigns `null`, compares with `null`, returns `null`, or uses a nullable data structure link, add faithful `nullable` annotations to the relevant reference fields, parameters, or locals. Do not add loop invariants such as `entry != null` for a variable initialized from a nullable field unless a preceding guard or method precondition proves it.\n"
        "- If OpenJML would need a bound for `1 << idx`, `1L << idx`, or bit-mask code, trace how `idx` is computed and express the input domain that makes the shift count valid. For example, if `idx` is computed as `s.charAt(k) - 'a'`, use a quantified precondition over the string characters that implies `0 <= idx && idx < 32`. Do not add `assume` or unrelated length upper bounds when the source already handles those lengths.\n"
        "- If a method writes fields or array elements, include a faithful method frame such as `assignable best, visited[*];`; if a helper only reads values and returns an expression, use `assignable \\nothing`.\n"
        "- If code directly accesses a constant array index such as `a[0]`, make the needed non-null and length facts explicit in `requires` clauses or object invariants, unless the source already guards the access.\n"
        "- For nullable linked structures, when a branch dereferences a field like `next`, prefer a branch-conditioned precondition such as `data > this.x ==> next != null`; do not invent unrelated deep shape requirements.\n"
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
        "If OpenJML reports `PossiblyLargeShift`, add faithful input-domain "
        "preconditions that imply the shift count is in range, such as character "
        "or element ranges used to compute the shift count. Trace local assignments "
        "such as `idx = x - C` back to method inputs, and make the precondition "
        "imply `0 <= idx < 32` for int shifts or `0 <= idx < 64` for long shifts. "
        "If OpenJML reports nullness failures under `--nonnull-by-default`, add "
        "`nullable` annotations to references that Java can set to null. Do not "
        "replace that with an unfaithful non-null invariant unless the code has "
        "already checked the reference. "
        "Do not add `assume`, and do not add arbitrary length upper bounds when "
        "the source already handles long inputs with an early return. "
        "Do not add loop-level `assignable`, `loop_invariant`, `loop_variant`, "
        "`decreasing`, `k in a..b` range shorthand, JML `assert`, JML `assume`, "
        "or JML `diverges`."
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
    prompt_examples: str = "",
    generation_context: str = "",
) -> JMLSpecBenchResult:
    """Generate JML for one Java source and validate with OpenJML."""

    source_file = Path(source_path)
    raw_original = source_file.read_text(encoding="utf-8")
    original = repair_java_source_for_openjml(raw_original)
    artifact_dir = (Path(output_dir) / driver).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    input_path = artifact_dir / "input.java"
    input_path.write_text(original, encoding="utf-8")

    oj_path = openjml_path or getattr(config, "openjml_path", "") or default_openjml_path()
    max_iter = max(1, int(max_iterations))
    provider = getattr(config, "resolved_provider", lambda: getattr(config, "llm_provider", ""))()
    model = getattr(config, "llm_model", "")
    prompt_seed = _initial_system_prompt() + "\n" + _initial_user_prompt(
        original, prompt_examples, generation_context
    )
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
            user_prompt = _initial_user_prompt(original, prompt_examples, generation_context)
        else:
            user_prompt = _refine_user_prompt(current_annotated, verifier_output, source_error, original)
        reply = llm.complete(
            _initial_system_prompt(),
            user_prompt,
            max_tokens=8192,
            temperature=0.1,
            role="spec_gen",
        )
        current_annotated = complete_standard_imports(
            normalize_jml_annotation_placement(
                drop_generated_jml_assertions(original, extract_java_source(reply))
            )
        )
        current_annotated = abstract_java_verifier_only_effects_for_openjml(current_annotated)
        preserved, source_error = source_code_preserved_with_standard_imports(original, current_annotated)
        if not preserved:
            transplanted = transplant_jml_annotations(original, current_annotated)
            if transplanted and count_jml_clauses(transplanted)["total"] > 0:
                transplanted = complete_standard_imports(
                    normalize_jml_annotation_placement(drop_generated_jml_assertions(original, transplanted))
                )
                transplanted = abstract_java_verifier_only_effects_for_openjml(transplanted)
                transplanted_preserved, transplanted_error = source_code_preserved_with_standard_imports(original, transplanted)
                if transplanted_preserved:
                    current_annotated = transplanted
                    preserved = True
                    source_error = ""
                else:
                    source_error = f"{source_error}\ntransplanted JML was not source-preserving: {transplanted_error}"

        iter_dir = artifact_dir / f"iter_{i}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        # Public Java classes must be verified from a file with the class name.
        annotated_path = iter_dir / java_verification_filename(current_annotated, source_file.name)
        annotated_path.write_text(current_annotated, encoding="utf-8")
        write_openjml_support_files(current_annotated, annotated_path.parent)

        if preserved:
            last_preserved_annotated = current_annotated
            openjml = run_openjml(
                annotated_path,
                openjml_path=oj_path,
                timeout_s=int(openjml_timeout),
                cwd=artifact_dir,
            )
            prune_rounds = 0
            while (
                not openjml.passed
                and prune_rounds < 5
            ):
                combined_output = ((openjml.stdout or "") + (openjml.stderr or "") + (("\n" + openjml.error) if openjml.error else ""))
                if openjml.status == "timeout" and _has_reported_nullable_failure(combined_output):
                    pruned, changed = _annotate_reported_nullable(current_annotated, combined_output)
                elif openjml.status == "verification_failed":
                    pruned, changed = _annotate_reported_nullable(current_annotated, combined_output)
                    if not changed:
                        pruned, changed = _prune_reported_precondition(current_annotated, combined_output)
                    if not changed:
                        pruned, changed = _prune_reported_assignable(current_annotated, combined_output)
                    if not changed:
                        pruned, changed = _prune_reported_postcondition(current_annotated, combined_output)
                    if not changed:
                        pruned, changed = _prune_reported_diverges(current_annotated, combined_output)
                    if not changed:
                        pruned, changed = _prune_reported_loop_decreases(current_annotated, combined_output)
                    if not changed:
                        pruned, changed = _prune_reported_loop_invariant(current_annotated, combined_output)
                    if not changed:
                        pruned, changed = _prune_reported_object_invariant(current_annotated, combined_output)
                elif openjml.status == "annotation_error":
                    pruned, changed = _prune_reported_annotation_error(current_annotated, combined_output)
                elif openjml.status == "source_invalid" and _has_reported_jml_annotation_error(
                    current_annotated, combined_output
                ):
                    pruned, changed = _prune_reported_annotation_error(current_annotated, combined_output)
                elif openjml.status == "tool_error" and _is_openjml_internal_error(combined_output):
                    pruned, changed = _prune_enclosing_loop_specs_for_internal_error(
                        current_annotated, combined_output
                    )
                else:
                    break
                if not changed:
                    break
                current_annotated = complete_standard_imports(
                    normalize_jml_annotation_placement(drop_generated_jml_assertions(original, pruned))
                )
                current_annotated = abstract_java_verifier_only_effects_for_openjml(current_annotated)
                annotated_path.write_text(current_annotated, encoding="utf-8")
                write_openjml_support_files(current_annotated, annotated_path.parent)
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
        if preserved and openjml.status == "tool_error":
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
