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
    # When the function was extracted from `impl FOO { fn name() {...} }`,
    # this is the verbatim impl type text ("FOO" or "FOO<T>"). For free
    # functions and inline-mod functions, empty. Used by the cargo-mode
    # harness generator to emit `FOO::name(args)` call-site syntax so the
    # function resolves in the parent module's namespace.
    impl_type: str = ""


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

    def _ingest_function(fn_node, impl_type: str = "") -> None:
        sig = _extract_signature(fn_node, src_bytes)
        if sig is None:
            return
        body_node = fn_node.child_by_field_name("body")
        if body_node is None:
            return  # trait fn declaration without body
        # Skip test functions. `#[test]` and `#[tokio::test]` are wrappers
        # that only compile under `#[cfg(test)]`. Under `--cfg kani` the
        # function may not exist (or, when wrapped in `#[cfg(test)] mod`,
        # is completely absent from the kani build). Generating a harness
        # for a test function always produces 'harness not discovered'.
        if _function_is_test(fn_node, src_bytes):
            return
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
        # Track the impl type so the cargo-mode harness gen can emit
        # `<impl_type>::<method>(args)` instead of bare `method(args)`.
        # For free fns and inline-mod fns this stays empty.
        sig.impl_type = impl_type
        # If we picked this up from inside an impl block, namespace it so
        # name collisions across impls (e.g. multiple `pub fn new` definitions)
        # don't clobber each other.
        name = sig.name
        if name in functions:
            return  # first wins; avoid silent overwrite
        functions[name] = sig
        function_bodies[name] = body_text
        call_graph[name] = callees

    def _impl_type_text(impl_node) -> str:
        """Extract the verbatim type text for an `impl FOO {...}` node.

        Tree-sitter exposes the type as the `type` field of impl_item.
        For ``impl Foo<T> { ... }`` returns ``"Foo<T>"``; for ``impl Trait
        for Foo`` returns ``"Foo"`` (we want the implementing type, not
        the trait).
        """
        # impl_item structure: `impl [generics] [TRAIT for] TYPE { body }`.
        # tree-sitter-rust exposes `type` (the implementing type) and
        # optionally `trait` (the trait being implemented).
        type_node = impl_node.child_by_field_name("type")
        if type_node is None:
            return ""
        return _slice(src_bytes, type_node).strip()

    def _node_has_cfg_gate(node) -> bool:
        """Return True if *node* is preceded by `#[cfg(...)]` attribute
        siblings that aren't part of the default build (i.e. anything but
        the unconditional default). Conservative: ANY cfg gate counts,
        because we can't reliably know which features are enabled when
        cargo-kani builds the crate. False positives (skipping cfg-gated
        items that ARE in the default build) are fine -- a missed harness
        is better than one that fails to compile and pollutes the bug
        report with 'cannot find type X' noise.
        """
        cursor = node.prev_sibling
        while cursor is not None:
            if cursor.type == "attribute_item":
                txt = _slice(src_bytes, cursor)
                if "cfg(" in txt or "cfg_attr(" in txt:
                    return True
                cursor = cursor.prev_sibling
            elif cursor.type in ("line_comment", "block_comment", "inner_attribute_item"):
                cursor = cursor.prev_sibling
            else:
                break
        return False

    for top in tree.root_node.children:
        if top.type == "function_item":
            _ingest_function(top)
        elif top.type == "impl_item":
            # Walk the impl's declaration_list (or `body`) for method items.
            body = top.child_by_field_name("body")
            if body is None:
                continue
            # Skip cfg-gated impl blocks: their types may not exist in the
            # default cargo-kani build. Example: lz4_flex's `PtrSink` is
            # behind `#[cfg(not(all(feature = "safe-encode", feature =
            # "safe-decode")))]` and the default features enable both,
            # so the impl's methods would generate harnesses that fail
            # at rustc with E0412 "cannot find type PtrSink".
            if _node_has_cfg_gate(top):
                continue
            impl_ty = _impl_type_text(top)
            for member in body.named_children:
                if member.type == "function_item":
                    _ingest_function(member, impl_type=impl_ty)
        elif top.type == "mod_item":
            # Inline modules: `mod foo { fn bar() {} }`. Walk one level
            # deeper. Nested modules will be reached on subsequent iterations
            # via recursion in the same loop if we recursed -- but to keep
            # behaviour close to the previous parser, stop at one level.
            #
            # Skip `#[cfg(test)] mod tests { ... }` and similar test-only
            # modules: their functions only exist under `--cfg test`, not
            # under `--cfg kani`. Generating a harness for any function
            # inside such a module always produces 'harness not discovered'.
            # tree-sitter attaches `#[..]` attribute_items as PRECEDING
            # SIBLINGS, not children — walk prev_sibling to find them.
            _is_test_mod = False
            _cursor = top.prev_sibling
            while _cursor is not None:
                if _cursor.type == "attribute_item":
                    _txt = _slice(src_bytes, _cursor)
                    if "cfg(test)" in _txt or "cfg(any(test" in _txt or "cfg(all(test" in _txt:
                        _is_test_mod = True
                        break
                    _cursor = _cursor.prev_sibling
                elif _cursor.type in ("line_comment", "block_comment", "inner_attribute_item"):
                    _cursor = _cursor.prev_sibling
                else:
                    break
            if _is_test_mod:
                continue
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
                    impl_ty = _impl_type_text(member)
                    for impl_member in impl_body.named_children:
                        if impl_member.type == "function_item":
                            _ingest_function(impl_member, impl_type=impl_ty)

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


def _function_is_test(fn_node, src: bytes) -> bool:
    """Return True if the function carries a `#[test]`, `#[tokio::test]`,
    `#[cfg(test)]`, etc. attribute. Test functions live in the test
    compilation only and aren't reachable under `--cfg kani`, so
    generating a harness for them always produces 'harness not
    discovered'.

    Tree-sitter exposes attributes as `attribute_item` SIBLINGS that
    precede the `function_item` (not children). Walk back through
    prev_sibling to collect all directly-preceding attribute_items.
    Stop at the first non-attribute / line_comment node.
    """
    cursor = fn_node.prev_sibling
    while cursor is not None:
        if cursor.type == "attribute_item":
            text = _slice(src, cursor)
            if "#[test]" in text or "#[tokio::test]" in text or "test_case" in text:
                return True
            if "cfg(test)" in text or "cfg(any(test" in text or "cfg(all(test" in text:
                return True
            cursor = cursor.prev_sibling
        elif cursor.type in ("line_comment", "block_comment", "inner_attribute_item"):
            cursor = cursor.prev_sibling
        else:
            # Some other node (a previous fn, struct, etc.) -- no more
            # attributes on THIS fn. Stop.
            break
    return False


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
