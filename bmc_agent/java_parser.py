"""Lightweight Java source parser for JBMC entry discovery.

This parser intentionally does not try to build a Java AST.  BMC-Agent's
existing C/Rust parsers are used for compositional harness synthesis; Java
support initially delegates actual verification to JBMC over compiled bytecode.
The parser exists so CLI/front-end code can detect classes and methods, produce
clear errors, and normalize entries such as ``main`` to ``Class.main``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class JavaMethodSignature:
    """Parsed signature of a Java method or constructor."""

    name: str
    return_type: str
    parameters: list[tuple[str, str]]
    class_name: str = ""
    modifiers: list[str] | None = None
    is_static: bool = False


@dataclass
class JavaMethodInfo:
    """All lightweight information about a Java method."""

    name: str
    signature: JavaMethodSignature
    body: str
    callees: set[str]
    source_file: str


@dataclass
class ParsedJavaFile:
    """Result of parsing a single Java source file."""

    path: str
    functions: dict[str, JavaMethodSignature]
    call_graph: dict[str, set[str]]
    function_bodies: dict[str, str]
    primary_class: str
    preprocessed_source: Optional[str] = None

    def get_function_info(self, name: str) -> Optional[JavaMethodInfo]:
        key = self._resolve_key(name)
        if key is None:
            return None
        return JavaMethodInfo(
            name=key,
            signature=self.functions[key],
            body=self.function_bodies.get(key, ""),
            callees=self.call_graph.get(key, set()),
            source_file=self.path,
        )

    def all_function_infos(self) -> list[JavaMethodInfo]:
        return [self.get_function_info(n) for n in self.functions]  # type: ignore[misc]

    def _resolve_key(self, name: str) -> Optional[str]:
        if name in self.functions:
            return name
        matches = [k for k in self.functions if k.rsplit(".", 1)[-1] == name]
        if len(matches) == 1:
            return matches[0]
        return None


_CLASS_RX = re.compile(r"\b(?:public\s+)?(?:final\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)\b")
_METHOD_RX = re.compile(
    r"(?P<prefix>(?:(?:public|protected|private|static|final|synchronized|native|strictfp)\s+)*)"
    r"(?P<ret>[A-Za-z_$][\w$<>\[\].?,\s]*?)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"\((?P<params>[^)]*)\)\s*"
    r"(?:throws\s+[^{;]+)?\{",
    re.MULTILINE,
)
_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "throw",
    "new",
    "assert",
    "super",
    "this",
}


def parse_java_file(path: str | Path, source_text: Optional[str] = None) -> ParsedJavaFile:
    """Parse a Java source file enough to discover classes and methods."""

    p = Path(path)
    source = source_text if source_text is not None else p.read_text(encoding="utf-8")
    classes = _find_classes(source)
    primary_class = _primary_class_name(source, p)
    functions: dict[str, JavaMethodSignature] = {}
    call_graph: dict[str, set[str]] = {}
    function_bodies: dict[str, str] = {}

    for class_name, class_start, class_end in classes:
        class_body = source[class_start:class_end]
        class_offset = class_start
        for match in _METHOD_RX.finditer(class_body):
            name = match.group("name")
            if name in _KEYWORDS:
                continue
            # Constructors have no return type; the regex can misread a
            # constructor modifier as the return type, so skip constructors for
            # now. JBMC entries are methods.
            if name == class_name:
                continue
            body_open = class_offset + match.end() - 1
            body_close = _find_matching_brace(source, body_open)
            if body_close is None:
                continue
            prefix = (match.group("prefix") or "").strip()
            modifiers = prefix.split() if prefix else []
            return_type = " ".join((match.group("ret") or "").split())
            params = _parse_params(match.group("params") or "")
            key = f"{class_name}.{name}"
            body = source[body_open : body_close + 1]
            functions[key] = JavaMethodSignature(
                name=name,
                return_type=return_type,
                parameters=params,
                class_name=class_name,
                modifiers=modifiers,
                is_static="static" in modifiers,
            )
            function_bodies[key] = body
            call_graph[key] = _extract_callees(body, name)

    return ParsedJavaFile(
        path=str(p),
        functions=functions,
        call_graph=call_graph,
        function_bodies=function_bodies,
        primary_class=primary_class,
        preprocessed_source=source_text,
    )


def _primary_class_name(source: str, path: Path) -> str:
    m = re.search(r"\bpublic\s+class\s+([A-Za-z_$][\w$]*)\b", source)
    if m:
        return m.group(1)
    for class_name, start, end in _find_classes(source):
        if class_name and re.search(r"\bstatic\s+void\s+main\s*\(", source[start:end]):
            return class_name
    m = _CLASS_RX.search(source)
    if m:
        return m.group(1)
    return path.stem


def _find_classes(source: str) -> list[tuple[str, int, int]]:
    classes: list[tuple[str, int, int]] = []
    for m in _CLASS_RX.finditer(source):
        brace = source.find("{", m.end())
        if brace < 0:
            continue
        end = _find_matching_brace(source, brace)
        if end is None:
            continue
        classes.append((m.group(1), brace + 1, end))
    if classes:
        return classes
    return [("", 0, len(source))]


def _find_matching_brace(source: str, open_index: int) -> Optional[int]:
    depth = 0
    in_string: str | None = None
    escaped = False
    i = open_index
    while i < len(source):
        ch = source[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
        else:
            if ch in ("'", '"'):
                in_string = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _parse_params(raw: str) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        tokens = part.replace("...", "[]").split()
        if not tokens:
            continue
        name = tokens[-1]
        type_text = " ".join(tokens[:-1]) or "Object"
        params.append((type_text, name))
    return params


def _extract_callees(body: str, own_name: str) -> set[str]:
    callees: set[str] = set()
    for m in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*\(", body):
        name = m.group(1)
        if name in _KEYWORDS or name == own_name:
            continue
        callees.add(name)
    return callees
