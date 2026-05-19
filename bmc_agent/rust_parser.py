"""
Tree-sitter Rust parser for AProver's Phase 1 / Phase 2 pipeline.

Mirrors :mod:`bmc_agent.parser` (the C-side tree-sitter parser) but works
against ``tree_sitter_rust``.  The output dataclasses are *structurally*
compatible with the C ones — same field names (``name``, ``return_type``,
``parameters``, ``body``, ``callees``, ``source_file``) — so downstream
consumers like :class:`bmc_agent.backends.kani_backend.KaniBackend` work
on either without modification.

Scope (M1):
  * Top-level ``fn`` items only.  ``impl`` and ``trait`` methods are
    skipped — handling ``self`` parameters and qualified names (``Type::m``)
    is part of M2.
  * Functions with bodies only; ``fn foo();`` trait declarations are
    skipped.
  * Callees are collected as text — either a bare identifier
    (``helper()``), a scoped path (``std::cmp::max``), a field-expression
    receiver (``x.clone`` for method calls), or a macro name
    (``println``).  Phase 1 prompts use these as hints, so over-collection
    is acceptable.

What is intentionally *not* attempted here:
  * Type inference, generic-bound checking, lifetime elaboration.
  * Cross-module resolution.  ``ParsedRustFile`` describes one source
    file.  The pipeline composes per-file results higher up.
  * Macro expansion.  Macro invocations are recorded by name only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Public dataclasses (structurally compatible with bmc_agent.parser)
# ---------------------------------------------------------------------------


@dataclass
class RustFunctionSignature:
    """Parsed signature of a top-level Rust function.

    ``parameters`` is a list of ``(type_text, name_text)`` pairs matching
    the C parser's convention (type-first).  ``return_type`` is the
    verbatim type text or ``"()"`` for the implicit unit return.
    ``modifiers`` is the list of leading qualifiers (``unsafe``, ``async``,
    ``const``, ``extern "C"`` -> ``extern``) in source order.
    """

    name: str
    return_type: str
    parameters: list[tuple[str, str]]
    is_pub: bool = False
    modifiers: list[str] = field(default_factory=list)
    type_parameters: str = ""
    where_clause: str = ""
    # is_static is a C storage-class concept with no Rust counterpart; kept
    # at False to preserve structural compatibility with the C-side
    # FunctionSignature so duck-typed consumers don't need to branch on
    # language.
    is_static: bool = False


@dataclass
class RustFunctionInfo:
    """All information about a single Rust function.

    Field names match :class:`bmc_agent.parser.FunctionInfo` so duck-typed
    consumers do not need to branch on language.
    """

    name: str
    signature: RustFunctionSignature
    body: str
    callees: set[str]
    source_file: str


@dataclass
class ParsedRustFile:
    """Result of parsing a single ``.rs`` source file."""

    path: str
    functions: dict[str, RustFunctionSignature]
    call_graph: dict[str, set[str]]
    function_bodies: dict[str, str]
    preprocessed_source: Optional[str] = None

    def get_function_info(self, name: str) -> Optional[RustFunctionInfo]:
        if name not in self.functions:
            return None
        return RustFunctionInfo(
            name=name,
            signature=self.functions[name],
            body=self.function_bodies.get(name, ""),
            callees=self.call_graph.get(name, set()),
            source_file=self.path,
        )

    def all_function_infos(self) -> list[RustFunctionInfo]:
        return [self.get_function_info(n) for n in self.functions]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tree-sitter setup
# ---------------------------------------------------------------------------


_TS_LANGUAGE = None


def _load_language():
    """Load the tree-sitter-rust grammar lazily (first call constructs it)."""
    global _TS_LANGUAGE
    if _TS_LANGUAGE is not None:
        return _TS_LANGUAGE
    import tree_sitter_rust as tsr
    from tree_sitter import Language

    _TS_LANGUAGE = Language(tsr.language())
    return _TS_LANGUAGE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_rust_file(
    path: str | Path,
    source_text: Optional[str] = None,
) -> ParsedRustFile:
    """Parse a Rust source file and return its function table + call graph.

    Parameters
    ----------
    path:
        Path to the ``.rs`` file.  Used for ``source_file`` attribution
        even when ``source_text`` is supplied.
    source_text:
        Optional in-memory source.  If omitted, the file is read from
        disk as UTF-8 (errors='replace').
    """
    path = Path(path)
    if source_text is None:
        src_bytes = path.read_bytes()
        source_text = src_bytes.decode("utf-8", errors="replace")
    else:
        src_bytes = source_text.encode("utf-8", errors="replace")

    from tree_sitter import Parser

    parser = Parser(_load_language())
    tree = parser.parse(src_bytes)

    functions: dict[str, RustFunctionSignature] = {}
    call_graph: dict[str, set[str]] = {}
    function_bodies: dict[str, str] = {}

    def _ingest_function(fn_node) -> None:
        sig = _extract_signature(fn_node, src_bytes)
        if sig is None:
            return
        body_node = fn_node.child_by_field_name("body")
        if body_node is None:
            return  # trait fn declaration without body
        # Skip methods that take a self receiver: their bodies reference
        # `self.foo` which we can't harness without constructing an instance
        # of the impl type, and the existing kani harness generator only
        # knows how to materialise free-function parameters. Static methods
        # in impl blocks (`impl Foo { fn bar(x: i32) {...} }`) work fine
        # and are the high-value unlock.
        if _function_has_self_param(fn_node):
            return
        body_text = _slice(src_bytes, body_node)
        callees: set[str] = set()
        _collect_callees(body_node, callees, src_bytes)
        # If we picked this up from inside an impl block, namespace it so
        # name collisions across impls (e.g. multiple `pub fn new` definitions)
        # don't clobber each other.
        name = sig.name
        if name in functions:
            return  # first wins; avoid silent overwrite
        functions[name] = sig
        function_bodies[name] = body_text
        call_graph[name] = callees

    for top in tree.root_node.children:
        if top.type == "function_item":
            _ingest_function(top)
        elif top.type == "impl_item":
            # Walk the impl's declaration_list (or `body`) for method items.
            body = top.child_by_field_name("body")
            if body is None:
                continue
            for member in body.named_children:
                if member.type == "function_item":
                    _ingest_function(member)
        elif top.type == "mod_item":
            # Inline modules: `mod foo { fn bar() {} }`. Walk one level
            # deeper. Nested modules will be reached on subsequent iterations
            # via recursion in the same loop if we recursed -- but to keep
            # behaviour close to the previous parser, stop at one level.
            body = top.child_by_field_name("body")
            if body is None:
                continue
            for member in body.named_children:
                if member.type == "function_item":
                    _ingest_function(member)
                elif member.type == "impl_item":
                    impl_body = member.child_by_field_name("body")
                    if impl_body is None:
                        continue
                    for impl_member in impl_body.named_children:
                        if impl_member.type == "function_item":
                            _ingest_function(impl_member)

    return ParsedRustFile(
        path=str(path),
        functions=functions,
        call_graph=call_graph,
        function_bodies=function_bodies,
        preprocessed_source=source_text if source_text is not None else None,
    )


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _slice(src: bytes, node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _extract_signature(node, src: bytes) -> Optional[RustFunctionSignature]:
    """Extract a RustFunctionSignature from a ``function_item`` node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _slice(src, name_node).strip()

    # Parameters: walk the `parameters` child and pull out each `parameter`.
    params_node = node.child_by_field_name("parameters")
    parameters: list[tuple[str, str]] = []
    if params_node is not None:
        for child in params_node.named_children:
            if child.type != "parameter":
                # `self_parameter` lives here in impl methods; we skip it,
                # which is correct for the free-fn-only M1 scope and the
                # least surprising behaviour for accidentally-included
                # methods (the body still parses, but `self` is lost).
                continue
            pattern_node = child.child_by_field_name("pattern")
            type_node = child.child_by_field_name("type")
            pname = _slice(src, pattern_node).strip() if pattern_node else ""
            ptype = _slice(src, type_node).strip() if type_node else ""
            parameters.append((ptype, pname))

    # Return type: explicit `return_type` field, or implicit unit `()`.
    rt_node = node.child_by_field_name("return_type")
    return_type = _slice(src, rt_node).strip() if rt_node is not None else "()"

    # Modifiers: unsafe / async / const / extern — collected from the
    # `function_modifiers` child if present.  Each leaf is a keyword token.
    modifiers: list[str] = []
    is_pub = False
    type_parameters = ""
    where_clause = ""
    for child in node.children:
        if child.type == "visibility_modifier":
            is_pub = True
        elif child.type == "function_modifiers":
            for kw in child.children:
                text = _slice(src, kw).strip()
                if text:
                    modifiers.append(text)
        elif child.type == "type_parameters":
            type_parameters = _slice(src, child).strip()
        elif child.type == "where_clause":
            where_clause = _slice(src, child).strip()

    return RustFunctionSignature(
        name=name,
        return_type=return_type,
        parameters=parameters,
        is_pub=is_pub,
        modifiers=modifiers,
        type_parameters=type_parameters,
        where_clause=where_clause,
    )


def _function_has_self_param(fn_node) -> bool:
    """Return True if the function takes a ``self``/``&self``/``&mut self`` receiver.

    Tree-sitter exposes the self receiver as a separate ``self_parameter``
    node under the ``parameters`` list. Static methods inside ``impl`` blocks
    have no ``self_parameter`` and are safe to harness as free functions.
    """
    params_node = fn_node.child_by_field_name("parameters")
    if params_node is None:
        return False
    for child in params_node.named_children:
        if child.type == "self_parameter":
            return True
    return False


def _collect_callees(node, out: set[str], src: bytes) -> None:
    """Walk a subtree and gather call/macro names into *out*.

    For ``call_expression`` we record the text of the ``function`` field —
    which may be a bare identifier, a scoped path (``std::cmp::max``), or
    a field expression (``x.clone`` for method calls).  For
    ``macro_invocation`` we record the macro identifier.  This is coarse
    by design: Phase 1 prompts use the set as hints, not as a precise
    resolution, and over-collection is preferable to losing references.
    """
    t = node.type
    if t == "call_expression":
        fn_node = node.child_by_field_name("function")
        if fn_node is not None:
            out.add(_slice(src, fn_node).strip())
    elif t == "macro_invocation":
        mac = node.child_by_field_name("macro")
        if mac is not None:
            out.add(_slice(src, mac).strip())
    for child in node.children:
        _collect_callees(child, out, src)
