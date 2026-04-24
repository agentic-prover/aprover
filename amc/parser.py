"""
Tree-sitter C parser for call graph extraction.

Tries to use tree-sitter + tree-sitter-c; falls back to regex-based
extraction if the grammar is not available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FunctionSignature:
    """Parsed signature of a C function."""

    name: str
    return_type: str
    parameters: list[tuple[str, str]]  # [(type, name), ...]


@dataclass
class FunctionInfo:
    """All information about a single C function."""

    name: str
    signature: FunctionSignature
    body: str                   # source text of function body
    callees: set[str]           # functions this function calls
    source_file: str


@dataclass
class ParsedCFile:
    """Result of parsing a C source file."""

    path: str
    functions: dict[str, FunctionSignature]  # name -> signature
    call_graph: dict[str, set[str]]          # caller -> set of callee names
    function_bodies: dict[str, str]          # name -> raw body text

    def get_function_info(self, name: str) -> Optional["FunctionInfo"]:
        """Return a FunctionInfo for the named function, or None if not found."""
        if name not in self.functions:
            return None
        return FunctionInfo(
            name=name,
            signature=self.functions[name],
            body=self.function_bodies.get(name, ""),
            callees=self.call_graph.get(name, set()),
            source_file=self.path,
        )

    def all_function_infos(self) -> list["FunctionInfo"]:
        """Return FunctionInfo for every parsed function."""
        return [self.get_function_info(n) for n in self.functions]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tree-sitter setup (optional)
# ---------------------------------------------------------------------------

_TS_AVAILABLE = False
_TS_LANGUAGE = None


def _try_load_tree_sitter() -> None:
    """Attempt to load the tree-sitter C grammar; set _TS_AVAILABLE on success."""
    global _TS_AVAILABLE, _TS_LANGUAGE
    if _TS_AVAILABLE:
        return

    try:
        import tree_sitter_c as tsc
        from tree_sitter import Language

        # tree-sitter >= 0.22 exposes language() as a capsule
        if hasattr(tsc, "language"):
            _TS_LANGUAGE = Language(tsc.language())
        else:
            # Older binding: Language(path, name)
            _TS_LANGUAGE = Language(tsc.__file__, "c")
        _TS_AVAILABLE = True
    except Exception:
        _TS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_c_file(path: str | Path) -> ParsedCFile:
    """
    Parse a C source file and return function signatures + call graph.

    Uses tree-sitter if available; otherwise falls back to regex.
    """
    path = Path(path)
    source_bytes = path.read_bytes()
    source_text = source_bytes.decode("utf-8", errors="replace")

    _try_load_tree_sitter()

    if _TS_AVAILABLE:
        try:
            return _parse_with_tree_sitter(source_bytes, source_text, str(path))
        except Exception:
            pass  # fall through to regex

    return _parse_with_regex(source_text, str(path))


# ---------------------------------------------------------------------------
# Tree-sitter implementation
# ---------------------------------------------------------------------------


def _parse_with_tree_sitter(src_bytes: bytes, source: str, path: str) -> ParsedCFile:
    """Parse using tree-sitter. Uses byte offsets for all node slicing."""
    from tree_sitter import Parser

    parser = Parser(_TS_LANGUAGE)
    tree = parser.parse(src_bytes)
    root = tree.root_node

    functions: dict[str, FunctionSignature] = {}
    call_graph: dict[str, set[str]] = {}
    function_bodies: dict[str, str] = {}

    # Walk top-level function definitions
    for node in root.children:
        if node.type == "function_definition":
            sig = _extract_sig_ts(node, src_bytes)
            if sig:
                functions[sig.name] = sig
                call_graph[sig.name] = set()
                # Body is the compound_statement child
                body_node = node.child_by_field_name("body")
                if body_node:
                    body_text = src_bytes[body_node.start_byte:body_node.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    function_bodies[sig.name] = body_text
                    # Collect call expressions within the body
                    _collect_calls_ts(body_node, call_graph[sig.name], src_bytes)

    return ParsedCFile(
        path=path,
        functions=functions,
        call_graph=call_graph,
        function_bodies=function_bodies,
    )


def _slice_bytes(src_bytes: bytes, node) -> str:
    """Decode a node's byte range from src_bytes."""
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _extract_sig_ts(node, src_bytes: bytes) -> Optional[FunctionSignature]:
    """Extract FunctionSignature from a tree-sitter function_definition node."""
    declarator = node.child_by_field_name("declarator")
    if declarator is None:
        return None

    # Unwrap pointer_declarator(s), counting stars so void *malloc -> "void *"
    pointer_stars = ""
    while declarator.type == "pointer_declarator":
        pointer_stars += "*"
        declarator = declarator.child_by_field_name("declarator") or declarator

    fn_name = ""
    params: list[tuple[str, str]] = []

    if declarator.type == "function_declarator":
        name_node = declarator.child_by_field_name("declarator")
        if name_node:
            fn_name = _slice_bytes(src_bytes, name_node).strip()

        param_list = declarator.child_by_field_name("parameters")
        if param_list:
            for child in param_list.named_children:
                if child.type == "parameter_declaration":
                    p_type, p_name = _extract_param_ts(child, src_bytes)
                    params.append((p_type, p_name))
                elif child.type == "variadic_parameter":
                    params.append(("...", ""))

    if not fn_name:
        return None

    # Return type: base type node + any pointer stars from the declarator
    type_node = node.child_by_field_name("type")
    ret_type = _slice_bytes(src_bytes, type_node).strip() if type_node else "unknown"
    if pointer_stars:
        ret_type = ret_type + " " + pointer_stars

    return FunctionSignature(name=fn_name, return_type=ret_type, parameters=params)


def _extract_param_ts(param_node, src_bytes: bytes) -> tuple[str, str]:
    """Return (type_str, name_str) from a parameter_declaration node."""
    full_text = _slice_bytes(src_bytes, param_node).strip()
    # Last whitespace-separated token is the name (possibly prefixed with *)
    parts = full_text.rsplit(None, 1)
    if len(parts) == 2:
        last = parts[1]           # e.g. "*rb", "**pp", "len"
        name = last.lstrip("*")   # strip leading stars from the name
        stars = "*" * (len(last) - len(name))  # stars belong to the type
        type_str = parts[0].strip() + stars
        return type_str, name
    return full_text, ""


def _collect_calls_ts(node, callees: set[str], src_bytes: bytes) -> None:
    """Recursively collect function call names from a tree-sitter subtree."""
    if node.type == "call_expression":
        fn_node = node.child_by_field_name("function")
        if fn_node:
            name = _slice_bytes(src_bytes, fn_node).strip()
            callees.add(name)
    for child in node.children:
        _collect_calls_ts(child, callees, src_bytes)


# ---------------------------------------------------------------------------
# Regex fallback
# ---------------------------------------------------------------------------

# Matches C function definitions (handles pointers, multi-word return types)
_FUNC_DEF_RE = re.compile(
    r"""
    (?:^|\n)                          # start of line
    (?P<ret>[\w\s\*]+?)               # return type (non-greedy)
    \s+
    (?P<name>[A-Za-z_]\w*)            # function name
    \s*\(                             # opening paren
    (?P<params>[^)]*)                 # parameter list
    \)\s*\{                           # closing paren + opening brace
    """,
    re.VERBOSE | re.MULTILINE,
)

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

_KEYWORDS = frozenset(
    [
        "if", "else", "while", "for", "do", "switch", "case", "return",
        "sizeof", "typeof", "alignof", "alignas", "static", "extern",
        "inline", "const", "volatile", "struct", "union", "enum",
        "typedef", "void", "int", "long", "short", "char", "float",
        "double", "unsigned", "signed",
    ]
)


def _parse_with_regex(source: str, path: str) -> ParsedCFile:
    """Fallback regex-based C parser."""
    functions: dict[str, FunctionSignature] = {}
    call_graph: dict[str, set[str]] = {}
    function_bodies: dict[str, str] = {}

    matches = list(_FUNC_DEF_RE.finditer(source))

    for i, m in enumerate(matches):
        fn_name = m.group("name")
        if fn_name in _KEYWORDS:
            continue

        ret_type = m.group("ret").strip()
        raw_params = m.group("params").strip()
        params = _parse_params_regex(raw_params)

        # Extract body: from { to matching }
        body_start = m.end() - 1  # points at the '{'
        body_text = _extract_body(source, body_start)

        functions[fn_name] = FunctionSignature(
            name=fn_name,
            return_type=ret_type,
            parameters=params,
        )
        function_bodies[fn_name] = body_text

        # Collect calls within the body
        callees: set[str] = set()
        for cm in _CALL_RE.finditer(body_text):
            callee = cm.group(1)
            if callee not in _KEYWORDS:
                callees.add(callee)
        call_graph[fn_name] = callees

    return ParsedCFile(
        path=path,
        functions=functions,
        call_graph=call_graph,
        function_bodies=function_bodies,
    )


def _parse_params_regex(raw: str) -> list[tuple[str, str]]:
    """Parse a raw parameter string into [(type, name), ...] pairs."""
    if not raw or raw.strip() in ("", "void"):
        return []
    params: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        tokens = part.split()
        if len(tokens) >= 2:
            last = tokens[-1]
            name = last.lstrip("*")
            stars = "*" * (len(last) - len(name))
            typ = " ".join(tokens[:-1]) + stars
            params.append((typ, name))
        elif tokens:
            params.append((tokens[0], ""))
    return params


def _extract_body(source: str, open_brace: int) -> str:
    """Extract the text from the opening brace to its matching closing brace."""
    depth = 0
    i = open_brace
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace: i + 1]
        i += 1
    return source[open_brace:]
