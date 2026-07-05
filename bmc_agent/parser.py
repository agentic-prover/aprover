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
    is_static: bool = False             # True if declared with `static` storage class


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
    # Full function-definition text (return type + attributes + declarator + body)
    # captured directly from tree-sitter.  Used by the harness generator to
    # excise complete function defs from the source when emitting type-decl
    # context, so multi-line return types and attribute lines don't leak through
    # as orphan declarations.
    function_definitions: dict[str, str] = field(default_factory=dict)
    # Struct definitions encountered at translation-unit scope. Keyed by
    # the struct's tag name (the part after ``struct``, or the typedef'd
    # alias when bound via ``typedef struct { ... } Name;``). The value is
    # a list of ``(field_type, field_name)`` pairs preserving declaration
    # order. Used by the harness emitter to populate struct-pointer params
    # with per-field initialisation (pointer fields → fresh backing
    # buffers; length/index fields → ``>= 0`` constraint) so opaque-struct
    # arguments don't produce 100+ spurious CBMC field-access findings.
    struct_definitions: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # When the file was preprocessed before parsing, the expanded source is
    # stored here so harness generators can use it instead of re-reading the
    # original (unexpanded) file.
    preprocessed_source: Optional[str] = None
    # cpp ``# N "filename"`` line directives let us tell which header (or
    # the original .c) each function body came from. Populated only when the
    # parser sees those directives in the input (i.e. a preprocessed ``.i``
    # or ``.c`` file dumped from ``make foo.i``). Empty otherwise.
    # Keyed by function name (matches ``functions``); value is the originating
    # source path as written in the cpp directive (e.g.
    # ``drivers/usb/serial/ch341.c`` or ``./include/linux/usb.h``).
    function_source_files: dict[str, str] = field(default_factory=dict)
    # Primary source the TU came from — taken from the first ``# N "..."``
    # directive at line 1. For a preprocessed kernel driver, this is the
    # original .c file. None for non-preprocessed input. Used by
    # ``restrict_to_primary_source`` to drop header-inlined functions
    # (kernel preprocessing inlines several thousand ``static inline``
    # helpers from ``linux/*.h``; without filtering, the pipeline tries
    # to spec all of them).
    primary_source: Optional[str] = None
    # Functions whose ADDRESS is taken inside another function's body
    # (passed as a callback argument, stored in a struct field, etc.).
    # Used by Phase 3's caller-feasibility check to handle vtable-
    # dispatched functions: when the FUT has no direct callers (because
    # it's only invoked via a function pointer), the address-takers
    # serve as evidence of indirect reachability. Without this, Phase 3
    # marks every libarchive format-reader / vtable-callback function
    # as "unresolved", suppressing real bugs in those code paths.
    #
    # Keyed by function name; value is a set of caller-function names
    # whose body contains the function's identifier in a non-call
    # position (no immediately-following ``(``). Conservative — picks
    # up some non-callback references too, but those are harmless for
    # Phase 3's "is the function reachable from any in-tree usage"
    # question.
    address_taken_in: dict[str, set[str]] = field(default_factory=dict)

    def restrict_to_primary_source(self) -> int:
        """Drop functions whose body did NOT originate in
        ``self.primary_source``. No-op if ``primary_source`` is None or
        ``function_source_files`` is empty (i.e. the input wasn't
        preprocessed and we have no provenance info).

        Returns the number of functions dropped, so the caller can log
        the filtering action.
        """
        if not self.primary_source or not self.function_source_files:
            return 0
        primary_base = self.primary_source.rsplit("/", 1)[-1]
        keep: set[str] = set()
        for name, origin in self.function_source_files.items():
            if not origin:
                continue
            if origin == self.primary_source:
                keep.add(name)
                continue
            # Match on basename too — cpp may show the same file with
            # different prefixes (``./drivers/...`` vs ``drivers/...``)
            # depending on how the build was invoked.
            if origin.rsplit("/", 1)[-1] == primary_base:
                keep.add(name)
        dropped = [n for n in list(self.functions) if n not in keep]
        for n in dropped:
            self.functions.pop(n, None)
            self.function_bodies.pop(n, None)
            self.function_definitions.pop(n, None)
            self.call_graph.pop(n, None)
            self.function_source_files.pop(n, None)
        # Don't prune call-graph edges. Each primary-file function's
        # callee set was populated from its own body and lists every
        # call site verbatim, including kernel-header inlines like
        # ``phy_write``. Dropping those edges leaves the harness
        # generator unable to recognise the callee and emit a proper
        # stub, which is fatal when the dropped-but-called inline
        # exercises CBMC-unsupported features (anonymous-tag struct
        # inclusion, statement-expression macros). Keeping the edge
        # treats header inlines uniformly with truly external symbols.
        return len(dropped)

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


def parse_c_file(
    path: str | Path,
    source_text: Optional[str] = None,
) -> ParsedCFile:
    """
    Parse a C source file and return function signatures + call graph.

    Parameters
    ----------
    path:
        Path to the original ``.c`` file (used for artifact naming).
    source_text:
        If provided, parse this string instead of reading *path* from disk.
        Use this to pass preprocessed / expanded source.

    Uses tree-sitter if available; otherwise falls back to regex.
    """
    path = Path(path)
    provided_source = source_text is not None
    if source_text is None:
        source_bytes = path.read_bytes()
        source_text = source_bytes.decode("utf-8", errors="replace")
    else:
        source_bytes = source_text.encode("utf-8", errors="replace")

    _try_load_tree_sitter()

    if _TS_AVAILABLE:
        try:
            result = _parse_with_tree_sitter(source_bytes, source_text, str(path))
            if provided_source or result.primary_source:
                # ``primary_source`` is populated from cpp ``# N "..."``
                # line directives, which only appear when the input was
                # preprocessed. Treating "directives present" as a
                # synonym for "preprocessed" lets the harness emitter
                # skip its libc-header prepend (which conflicts with the
                # inlined glibc/kernel types).
                result.preprocessed_source = source_text
            return result
        except Exception:
            pass  # fall through to regex

    result = _parse_with_regex(source_text, str(path))
    if provided_source or result.primary_source:
        result.preprocessed_source = source_text
    return result


# ---------------------------------------------------------------------------
# Tree-sitter implementation
# ---------------------------------------------------------------------------


def _build_line_to_source_map(source: str) -> tuple[list[str], Optional[str]]:
    """Walk cpp ``# N "filename" [flags]`` line directives and return
    ``(line_to_source, primary_source)``:

    * ``line_to_source[i]`` is the originating source filename for the
      0-indexed line ``i`` (empty string for lines that fall outside any
      directive).
    * ``primary_source`` is the first non-cpp-synthetic filename seen
      (i.e. the original ``.c`` the TU was built from), or ``None`` if
      the input has no cpp line directives.

    The map covers each non-directive line. Directive lines themselves
    get the file they introduce — they're tagged with the same source as
    the lines below them.
    """
    lines = source.split("\n")
    line_to_source: list[str] = [""] * len(lines)
    primary: Optional[str] = None
    current: str = ""
    # Match ``# 12 "file.c"`` (with optional trailing flag digits)
    pat = re.compile(r'^#\s+\d+\s+"([^"]+)"')
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            current = m.group(1)
            # First "real" source seen — synthetic ones look like
            # ``<built-in>``, ``<command-line>``, ``<stdin>``.
            if primary is None and not (current.startswith("<") and current.endswith(">")):
                primary = current
        line_to_source[i] = current
    return line_to_source, primary


def _parse_with_tree_sitter(src_bytes: bytes, source: str, path: str) -> ParsedCFile:
    """Parse using tree-sitter. Uses byte offsets for all node slicing."""
    from tree_sitter import Parser

    parser = Parser(_TS_LANGUAGE)
    tree = parser.parse(src_bytes)
    root = tree.root_node

    functions: dict[str, FunctionSignature] = {}
    call_graph: dict[str, set[str]] = {}
    function_bodies: dict[str, str] = {}
    function_definitions: dict[str, str] = {}
    function_source_files: dict[str, str] = {}
    line_to_source, primary_source = _build_line_to_source_map(source)

    # Preprocessor wrapper node types whose children must also be walked.
    # Without recursing into these, functions guarded by ``#ifndef
    # CURL_DISABLE_PARSEDATE`` (curl/parsedate.c) or ``#ifdef __linux__``
    # are invisible to the parser even though they're in the build by
    # default.
    #
    # ``compound_statement`` and ``ERROR`` appear at the top level only
    # under tree-sitter's parse-error recovery: when a macro-heavy kernel
    # body (FIELD_PREP nests, _Static_assert inside struct{} type-exprs)
    # confuses the C grammar, tree-sitter wraps a span of trailing
    # function_definitions into a synthetic ``compound_statement`` (or
    # ``ERROR``) child of the translation_unit instead of failing the
    # whole parse. The functions inside are still valid; we just need to
    # walk through the wrapper. Without this, ~15% of the functions in
    # ``drivers/net/ethernet/airoha/airoha_eth.i`` go invisible and the
    # harness emitter's body-excision misses their definitions, leaving
    # 73KB of orphaned ``FIELD_PREP`` expansions in the type-decls.
    _PREPROC_CONTAINER_TYPES = {
        "preproc_if", "preproc_ifdef", "preproc_ifndef",
        "preproc_else", "preproc_elif", "preproc_elifdef", "preproc_elifndef",
        "linkage_specification",  # extern "C" { ... }
        "compound_statement",     # parse-error recovery wrapper (kernel TUs)
        "ERROR",                  # parse-error recovery wrapper (kernel TUs)
    }

    def _collect_function_defs(node):
        """Yield every function_definition node, recursing through
        preprocessor / linkage wrappers and parse-error-recovery
        compound_statement wrappers.

        At a ``function_definition`` we yield the node *and* recurse
        into its compound_statement body. The recursion is needed
        because tree-sitter's error recovery on macro-heavy kernel TUs
        often nests trailing functions inside an earlier function's
        body (parent chain: ``function_definition → compound_statement
        → function_definition``). Without this, ``hid-pidff.c``'s
        ``pidff_rescale`` and ~10 siblings vanish. Real GCC nested
        function defs are extremely rare in kernel/driver code, so the
        spurious recurse cost is negligible.
        """
        if node.type == "function_definition":
            yield node
            for child in node.children:
                if child.type == "compound_statement":
                    for sub in child.children:
                        yield from _collect_function_defs(sub)
            return
        if node.type in _PREPROC_CONTAINER_TYPES or node.type == "translation_unit":
            for child in node.children:
                yield from _collect_function_defs(child)

    for node in _collect_function_defs(root):
        sig = _extract_sig_ts(node, src_bytes)
        if sig:
            # Tree-sitter's parse-error tolerance occasionally reports a
            # function_definition end_byte that lands on a `}` belonging
            # to an inner GCC statement-expression ``({ ... })`` rather
            # than the actual function close. This leaves orphan body
            # statements after end_byte that the harness emitter's body
            # excision misses, leading to "syntax error before 'if'"
            # in CBMC. Detect this by brace-counting the captured text;
            # if the count is positive (more ``{`` than ``}``), walk
            # forward from end_byte until balanced.
            true_end = _brace_balanced_end_byte(
                src_bytes, node.start_byte, node.end_byte
            )

            new_def_text = src_bytes[
                node.start_byte:true_end
            ].decode("utf-8", errors="replace")
            # Body is the compound_statement child
            body_node = node.child_by_field_name("body")
            new_body_text = ""
            new_callees: set[str] = set()
            if body_node:
                body_end = _brace_balanced_end_byte(
                    src_bytes, body_node.start_byte, body_node.end_byte
                )
                new_body_text = src_bytes[
                    body_node.start_byte:body_end
                ].decode("utf-8", errors="replace")
                _collect_calls_ts(body_node, new_callees, src_bytes)

            # Two-definition disambiguation: when a function name appears
            # twice in the TU (typical ``#ifdef CURL_DISABLE_X ... #else
            # static stub ... #endif`` shape — tree-sitter parses both
            # branches because it doesn't process preprocessor), prefer
            # the entry with the LONGER body. Stubs are short
            # (``{ (void)x; return 0; }``); real implementations are
            # multi-statement. Without this, the empty-body stub
            # overwrites the real one, the call graph for the real
            # function vanishes, and any function the real one called
            # gets mis-classified as a system-entry point.
            # Regression: curl/parsedate.c run 2026-05-19 flagged
            # ``datenum`` and ``time2epoch`` as caller-less entry points
            # (false confirmed_system_entry verdicts) because the
            # ``#else`` stub of ``parsedate`` overwrote the real body.
            prev_body = function_bodies.get(sig.name)
            if prev_body is not None and len(new_body_text) <= len(prev_body):
                continue

            functions[sig.name] = sig
            call_graph[sig.name] = new_callees
            function_definitions[sig.name] = new_def_text
            row = node.start_point[0]
            if 0 <= row < len(line_to_source):
                function_source_files[sig.name] = line_to_source[row]
            if new_body_text:
                function_bodies[sig.name] = new_body_text

    struct_definitions = _collect_struct_defs(root, src_bytes)

    address_taken_in = _compute_address_takers(functions, function_bodies)

    return ParsedCFile(
        path=path,
        functions=functions,
        call_graph=call_graph,
        function_bodies=function_bodies,
        function_definitions=function_definitions,
        struct_definitions=struct_definitions,
        function_source_files=function_source_files,
        primary_source=primary_source,
        address_taken_in=address_taken_in,
    )


def _brace_balanced_end_byte(src_bytes: bytes, start: int, ts_end: int) -> int:
    """Return the byte offset of the function's true closing ``}``.

    Tree-sitter occasionally truncates a function_definition's
    ``end_byte`` on macro-heavy kernel bodies (FIELD_PREP +
    _Static_assert inside ``struct{}`` inside GCC statement-expressions
    ``({ ... })``). The grammar mistakes a ``}`` of an inner expression
    for the body close.

    Count ``{`` / ``}`` over the captured slice, skipping over string
    literals, character literals, and ``/* */`` / ``//`` comments. If
    the count is positive (more ``{`` than ``}``), walk forward from
    ``ts_end`` byte-by-byte (using the same skip rules) until the count
    reaches zero. Return that offset (inclusive of the final ``}``).

    Conservative fallback: if walking forward never balances within a
    safety cap, return the original ``ts_end`` unchanged. Better to
    leave a faulty bound than chew the rest of the TU.
    """
    # Phase 1: scan captured slice and compute imbalance + end-position
    # of the scanner inside the slice. We then continue the scanner
    # past ``ts_end`` if needed.
    depth = 0
    i = start
    end = ts_end
    n = len(src_bytes)
    # Safety cap: don't walk more than 200KB beyond ts_end. The largest
    # kernel function we've encountered is ~225KB; 200KB beyond is
    # enough headroom for the recovery while preventing runaway scans
    # on truly broken input.
    cap = min(n, ts_end + 200_000)

    def _skip_string_or_char(j: int, quote: int) -> int:
        j += 1
        while j < n and src_bytes[j] != quote:
            if src_bytes[j] == 0x5C:  # backslash
                j += 2
            else:
                j += 1
        return j + 1

    def _skip_block_comment(j: int) -> int:
        k = src_bytes.find(b"*/", j + 2)
        return k + 2 if k != -1 else n

    def _skip_line_comment(j: int) -> int:
        k = src_bytes.find(b"\n", j + 2)
        return k + 1 if k != -1 else n

    # Walk through [start, end) first to compute depth at ts_end.
    while i < end:
        b = src_bytes[i]
        if b == 0x22:  # "
            i = _skip_string_or_char(i, 0x22)
            continue
        if b == 0x27:  # '
            i = _skip_string_or_char(i, 0x27)
            continue
        if b == 0x2F and i + 1 < n:
            nxt = src_bytes[i + 1]
            if nxt == 0x2A:
                i = _skip_block_comment(i)
                continue
            if nxt == 0x2F:
                i = _skip_line_comment(i)
                continue
        if b == 0x7B:  # {
            depth += 1
        elif b == 0x7D:  # }
            depth -= 1
            if depth == 0:
                # tree-sitter's end is correct; nothing to do.
                return end
        i += 1

    # Tree-sitter's end_byte was reached but the captured slice has
    # depth != 0. If depth < 0, we already over-shot inside the slice
    # — leave as-is (rare; means tree-sitter included a stray ``}``).
    if depth <= 0:
        return end

    # Phase 2: continue scanning past ts_end until balanced.
    i = end
    while i < cap:
        b = src_bytes[i]
        if b == 0x22:
            i = _skip_string_or_char(i, 0x22)
            continue
        if b == 0x27:
            i = _skip_string_or_char(i, 0x27)
            continue
        if b == 0x2F and i + 1 < n:
            nxt = src_bytes[i + 1]
            if nxt == 0x2A:
                i = _skip_block_comment(i)
                continue
            if nxt == 0x2F:
                i = _skip_line_comment(i)
                continue
        if b == 0x7B:
            depth += 1
        elif b == 0x7D:
            depth -= 1
            if depth == 0:
                return i + 1  # inclusive of closing ``}``
        i += 1

    # Couldn't balance within cap — give up and keep ts_end.
    return ts_end


def _collect_struct_defs(root, src_bytes: bytes) -> dict[str, list[tuple[str, str]]]:
    """Walk the translation unit and collect struct definitions, keyed by
    tag name (or typedef'd alias for anonymous structs).

    Each value is a list of ``(field_type, field_name)`` pairs in
    declaration order. Forward declarations (``struct opaque;``) are
    skipped because they have no field_declaration_list.
    """
    _PREPROC_CONTAINER_TYPES = {
        "preproc_if", "preproc_ifdef", "preproc_ifndef",
        "preproc_else", "preproc_elif", "preproc_elifdef", "preproc_elifndef",
        "linkage_specification",
        # Tree-sitter parse-recovery wrappers. On large preprocessed kernel
        # TUs (e.g. rtltool.i / r8125_rss.i) a recovery error earlier in
        # the file causes tree-sitter to put subsequent top-level
        # declarations — including ``struct rtl8125_private`` — inside a
        # phantom ``function_definition > compound_statement`` block.
        # Recurse into both so the collector still finds the struct
        # (rtl8125 OOT batch, 2026-05-18). The same fix was applied to
        # _collect_function_defs in 2026-05-18 for buried nested
        # function bodies.
        "function_definition", "compound_statement",
        "ERROR",
        # Bare top-level struct definitions ``struct X { ... };`` (no typedef,
        # as in aws-c-common) are parsed by tree-sitter as a `declaration`
        # wrapping the struct_specifier. Without recursing into `declaration`
        # the collector NEVER reaches them -> 0 structs extracted -> every
        # struct param treated opaque (verified on aws .i: 0/N structs). Recurse
        # into declarations too; forward-decls (no field list) are skipped by
        # _record_struct, so this only adds genuine definitions.
        "declaration",
    }
    structs: dict[str, list[tuple[str, str]]] = {}
    # Aliases declared via a separate ``typedef struct Tag Alias;``
    # statement, where the struct body lives in another translation-unit
    # node. We can't resolve these until the full walk completes.
    pending_aliases: list[tuple[str, str]] = []

    def walk(node):
        if node.type == "struct_specifier":
            _record_struct(node, src_bytes, structs, alias=None)
            return
        if node.type == "type_definition":
            # Two cases:
            #   (1) ``typedef struct [Tag] { ... } Alias;`` — body present, record under tag+alias.
            #   (2) ``typedef struct Tag Alias;`` — separate-typedef form,
            #       body lives in a sibling struct_specifier elsewhere. We
            #       record the alias→tag mapping here and rebind it after
            #       the walk completes (alias may point to a struct whose
            #       body hasn't been visited yet).
            inner_struct = None
            inner_struct_has_body = False
            inner_struct_tag = None
            alias = None
            for c in node.children:
                if c.type == "struct_specifier":
                    inner_struct = c
                    for cc in c.children:
                        if cc.type == "type_identifier":
                            inner_struct_tag = src_bytes[cc.start_byte:cc.end_byte].decode(
                                "utf-8", errors="replace"
                            )
                        elif cc.type == "field_declaration_list":
                            inner_struct_has_body = True
                elif c.type == "type_identifier":
                    alias = src_bytes[c.start_byte:c.end_byte].decode(
                        "utf-8", errors="replace"
                    )
            if inner_struct is not None and inner_struct_has_body:
                _record_struct(inner_struct, src_bytes, structs, alias=alias)
            elif alias and inner_struct_tag:
                # Pending alias — resolve after the full walk so we pick up
                # the struct body that appears later in the file.
                pending_aliases.append((alias, inner_struct_tag))
            return
        if node.type in _PREPROC_CONTAINER_TYPES or node.type == "translation_unit":
            for c in node.children:
                walk(c)

    walk(root)
    # Resolve pending typedef aliases: ``typedef struct Tag Alias;`` is
    # common in libxml2 / libcurl / OpenSSL headers, where the struct
    # body and the typedef are separate statements. Without this, harness
    # generation falls back to a flat ``Type x;`` nondet rather than the
    # per-field init path, and self-referential pointer fields stay
    # symbolic, producing linked-list traversal false positives.
    for alias, tag in pending_aliases:
        if tag in structs and alias not in structs:
            structs[alias] = structs[tag]
    # libxml2 / glib idiom: struct tag with leading underscore, typedef
    # alias without (typedef and struct usually live in a public header
    # we don't parse). Rather than fight headers in real-libc mode, infer
    # the alias from the convention: ``struct _xmlPattern`` → ``xmlPattern``.
    for tag in list(structs.keys()):
        if tag.startswith("_"):
            alias = tag[1:]
            if alias and alias not in structs:
                structs[alias] = structs[tag]
    return structs


def _record_struct(
    struct_node,
    src_bytes: bytes,
    structs: dict[str, list[tuple[str, str]]],
    alias: Optional[str],
) -> None:
    """Pull (type, name) field pairs out of a struct_specifier and store
    them under the tag name and/or typedef alias."""
    tag_name: Optional[str] = None
    fdecl_list = None
    for c in struct_node.children:
        if c.type == "type_identifier":
            tag_name = src_bytes[c.start_byte:c.end_byte].decode(
                "utf-8", errors="replace"
            )
        elif c.type == "field_declaration_list":
            fdecl_list = c
    if fdecl_list is None:
        # Forward declaration / opaque struct — no fields to extract.
        return

    fields: list[tuple[str, str]] = []
    for fdecl in fdecl_list.children:
        if fdecl.type != "field_declaration":
            continue
        # Gather the field type prefix (everything before the declarator).
        type_parts: list[str] = []
        declarator_node = None
        for c in fdecl.children:
            if c.type in {
                "type_qualifier", "primitive_type", "type_identifier",
                "sized_type_specifier", "struct_specifier",
                "union_specifier", "enum_specifier",
            }:
                # For nested struct/union specifiers, prefer the type
                # identifier ("struct Curl_str") rather than the full body.
                if c.type == "struct_specifier":
                    tag = None
                    for cc in c.children:
                        if cc.type == "type_identifier":
                            tag = src_bytes[cc.start_byte:cc.end_byte].decode(
                                "utf-8", errors="replace"
                            )
                    type_parts.append(f"struct {tag}" if tag else "struct")
                else:
                    type_parts.append(
                        src_bytes[c.start_byte:c.end_byte].decode(
                            "utf-8", errors="replace"
                        )
                    )
            elif c.type in {
                "field_identifier", "pointer_declarator", "array_declarator",
            }:
                declarator_node = c

        if declarator_node is None:
            continue

        # Walk the declarator to recover the field name and any pointer
        # stars / array brackets that belong to the type prefix.
        name, type_suffix = _flatten_declarator(declarator_node, src_bytes)
        if not name:
            continue
        ftype = " ".join(type_parts) + type_suffix
        fields.append((ftype.strip(), name))

    if not fields:
        return
    if tag_name:
        structs[tag_name] = fields
    if alias and alias not in structs:
        structs[alias] = fields


def _flatten_declarator(decl_node, src_bytes: bytes) -> tuple[str, str]:
    """Return (field_name, type_suffix) for a field declarator.

    ``type_suffix`` accumulates ``*`` stars (pointer_declarator) and
    ``[N]`` array dimensions (array_declarator) so callers can append
    them to the type prefix.
    """
    suffix = ""
    node = decl_node
    while True:
        if node.type == "pointer_declarator":
            suffix = "*" + suffix
            inner = node.child_by_field_name("declarator")
            if inner is None:
                return ("", suffix)
            node = inner
        elif node.type == "array_declarator":
            # Capture the [N] portion verbatim.
            text = src_bytes[node.start_byte:node.end_byte].decode(
                "utf-8", errors="replace"
            )
            # The trailing bracket-segment is everything after the inner
            # declarator's name; recover the name by recursing into the
            # inner child.
            inner = node.child_by_field_name("declarator")
            if inner is None:
                return ("", suffix)
            # The bracket portion of this array declarator goes to suffix.
            bracket_idx = text.find("[")
            if bracket_idx >= 0:
                suffix = suffix + text[bracket_idx:]
            node = inner
        elif node.type == "field_identifier":
            name = src_bytes[node.start_byte:node.end_byte].decode(
                "utf-8", errors="replace"
            )
            return (name, suffix)
        else:
            return ("", suffix)


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
            siblings = list(param_list.named_children)
            for idx, child in enumerate(siblings):
                if child.type == "parameter_declaration":
                    p_type, p_name = _extract_param_ts(child, src_bytes)
                    # Recover from tree-sitter's misparse of
                    # ``T * MACRO name`` where MACRO is an unknown
                    # qualifier (GGML_RESTRICT, __restrict__,
                    # __attribute__((nonnull)), GGML_NORETURN, etc.).
                    # tree-sitter consumes MACRO as the declarator
                    # identifier and emits the real param name as a
                    # sibling ERROR node. When (a) the parsed name is
                    # ALL-CAPS / underscored (looks like a macro), and
                    # (b) the next sibling is an ERROR containing one
                    # identifier, fold the macro into the type and use
                    # the ERROR identifier as the name.
                    if (
                        p_name
                        and _looks_like_macro(p_name)
                        and idx + 1 < len(siblings)
                        and siblings[idx + 1].type == "ERROR"
                    ):
                        err_node = siblings[idx + 1]
                        err_text = _slice_bytes(src_bytes, err_node).strip()
                        if err_text and err_text.isidentifier():
                            p_type = f"{p_type} {p_name}".strip()
                            p_name = err_text
                    params.append((p_type, p_name))
                elif child.type == "variadic_parameter":
                    params.append(("...", ""))

    if not fn_name:
        return None

    # Return type: base type node + any pointer stars from the declarator
    type_node = node.child_by_field_name("type")
    ret_type = _slice_bytes(src_bytes, type_node).strip() if type_node else "unknown"

    # Recover from tree-sitter's misparse of ``MACRO RealType fn(...)``
    # where MACRO is an unknown storage-class macro (curl's ``UNITTEST``
    # expands to ``static`` or empty; OpenSSL's ``OSSL_API`` adds
    # visibility; Linux's ``__init`` / ``__cold`` are linker-section
    # annotations). tree-sitter consumes MACRO as the ``type`` field
    # and stashes ``RealType`` in a sibling ERROR node. When (a) the
    # parsed type looks like a macro (ALL_CAPS-with-underscore or
    # leading ``__``) and (b) one of the function_definition's
    # children is an ERROR containing a single identifier, fold the
    # macro into the type prefix and use the ERROR identifier as
    # the actual return type. Regression: curl/urlapi.c run 2026-05-19
    # produced ``UNITTEST result = parse_port(...)`` harness lines
    # that CBMC rejected with "expected constant expression".
    if type_node is not None and _looks_like_macro(ret_type):
        for c in node.children:
            if c.type == "ERROR":
                err_text = _slice_bytes(src_bytes, c).strip()
                if err_text and err_text.isidentifier():
                    ret_type = f"{ret_type} {err_text}"
                    break

    # Recover from tree-sitter's misparse of ``MACRO struct T * fn(...)``.
    # When an unknown macro prefixes the signature (GGML_API, EXPORT,
    # __attribute__((...)), …), tree-sitter consumes ``MACRO struct`` as
    # a stray declaration and the function_definition's ``type`` field
    # picks up only the ``T`` half — yielding ``T *`` instead of
    # ``struct T *``. CBMC then rejects the harness because the bare
    # tag is not a valid type without a typedef.
    #
    # Look at the source bytes immediately before type_node.start_byte:
    # skip whitespace, then check whether the preceding token is one of
    # ``struct``, ``union``, ``enum``. If so, prepend it. We stop at the
    # nearest statement separator (``;``, ``{``, ``}``) so a struct/
    # union keyword from an UNRELATED prior declaration is not picked up.
    if type_node is not None:
        prepend = _recover_struct_keyword(src_bytes, type_node.start_byte)
        if prepend and not ret_type.split()[:1] == [prepend]:
            ret_type = f"{prepend} {ret_type}"

    if pointer_stars:
        ret_type = ret_type + " " + pointer_stars

    is_static = "static" in ret_type.split()
    # Tree-sitter parses the ``static`` storage-class specifier as a separate
    # node and strips it from the ``type`` field, so the ret_type heuristic
    # above misses it (signature.is_static was silently False for every
    # tree-sitter-parsed static function). Recover it from the source span
    # between the function-definition start and the return-type node — that
    # span holds exactly the storage-class / qualifier prefix.
    if not is_static and type_node is not None:
        try:
            _prefix = src_bytes[node.start_byte:type_node.start_byte].decode("utf-8", "replace")
            if re.search(r"\bstatic\b", _prefix):
                is_static = True
        except Exception:
            pass
    return FunctionSignature(name=fn_name, return_type=ret_type, parameters=params, is_static=is_static)


_STRUCT_TAG_KEYWORDS = (b"struct", b"union", b"enum")


def _looks_like_macro(name: str) -> bool:
    """Heuristic: identifier is a parameter-qualifier or storage-class
    macro (not a real parameter name or type).

    True when the identifier is:
      * all-uppercase with at least one underscore (``GGML_RESTRICT``,
        ``OSSL_API``), OR
      * a single all-uppercase word of >= 4 chars (``UNITTEST``,
        ``EXPORT``, ``INLINE``), OR
      * begins with a double underscore (``__restrict__``,
        ``__nonnull__``, ``__attribute__``).

    Used to detect tree-sitter's misparse of ``MACRO RealType name``
    (return type) or ``T * MACRO name`` (param) so the qualifier is
    folded into the type and the real identifier is recovered from a
    sibling ERROR node. Conservative — single-letter caps (``T``
    template-style) and 2-3 letter words (``OK``, ``NO``) do not match.
    """
    if not name:
        return False
    if name.startswith("__"):
        return True
    if "_" in name and name.isupper():
        return True
    if len(name) >= 4 and name.isupper() and name.isalpha():
        return True
    return False


def _recover_struct_keyword(src_bytes: bytes, type_start: int) -> str:
    """Return ``struct`` / ``union`` / ``enum`` if it precedes the type
    in the source text (separated only by whitespace and statement-
    local tokens), else ``""``.

    Used to repair the tree-sitter misparse where a macro prefix
    (GGML_API, EXPORT) causes the parser to consume the struct keyword
    as part of a stray declaration. We scan backwards from ``type_start``
    over whitespace, then check whether the next preceding token is
    one of struct/union/enum. We stop at ``;``, ``{``, or ``}`` so
    keywords from an unrelated earlier declaration are not picked up.
    """
    i = type_start - 1
    # Skip whitespace
    while i >= 0 and src_bytes[i:i + 1] in (b" ", b"\t", b"\n", b"\r"):
        i -= 1
    if i < 0:
        return ""
    # Stop at statement boundaries — don't claim a struct keyword from
    # an unrelated earlier declaration.
    if src_bytes[i:i + 1] in (b";", b"{", b"}", b")", b"("):
        return ""
    # Walk back to the start of the preceding identifier-like token.
    end = i + 1
    while i >= 0 and (
        src_bytes[i:i + 1].isalpha() or src_bytes[i:i + 1].isdigit() or src_bytes[i:i + 1] == b"_"
    ):
        i -= 1
    start = i + 1
    token = src_bytes[start:end]
    if token in _STRUCT_TAG_KEYWORDS:
        return token.decode("ascii")
    return ""


def _extract_param_ts(param_node, src_bytes: bytes) -> tuple[str, str]:
    """Return (type_str, name_str) from a parameter_declaration node.

    Handles three declarator shapes that put non-identifier punctuation
    onto the "name" half of a whitespace split:

      * pointer prefix:   ``T *p``      → type=``T*``     name=``p``
      * double pointer:   ``T **pp``    → type=``T**``    name=``pp``
      * array decay:      ``T buf[N]``  → type=``T*``     name=``buf``
        (the array size is lost — that's correct for C, where array
        parameters decay to pointers at the call site; a downstream
        harness that emits ``buf[N]`` from this string would be passing
        an element, not the array. ch341/pl2303 sweep regression.)
    """
    full_text = _slice_bytes(src_bytes, param_node).strip()
    parts = full_text.rsplit(None, 1)
    # Handle ``void*param`` / ``int**pp`` / etc. — no whitespace between
    # the pointer stars and the identifier. ``rsplit(None, 1)`` then
    # returns a single element and the name is lost. Detect and split.
    if len(parts) == 1:
        m = re.match(
            r"^(.+?)\s*(\*+)([A-Za-z_]\w*)\s*$",
            full_text,
        )
        if m:
            parts = [m.group(1).strip() + m.group(2), m.group(3)]
    if len(parts) == 2:
        last = parts[1]
        # Strip leading pointer stars from the name; they belong on the
        # type. ``**pp`` → name=``pp`` + 2 trailing stars on type.
        name = last.lstrip("*")
        stars = "*" * (len(last) - len(name))
        type_str = parts[0].strip() + stars
        # Array-decay: ``buf[N]`` (or ``buf[]``) on the name half means
        # the parameter is logically a pointer. Strip ``[...]`` from
        # the name and add one ``*`` to the type. Multi-dimensional
        # arrays (``buf[N][M]``) also decay — first dim only becomes a
        # pointer; later dims stay as part of the type.
        if "[" in name:
            bracket = name.index("[")
            tail = name[bracket:]
            name = name[:bracket]
            # First ``[...]`` decays to ``*``; any remaining brackets
            # stay on the type. ``buf[N][M]`` → name=``buf``,
            # type=``T (*)[M]`` (we approximate as ``T*[M]`` because the
            # downstream harness gen doesn't currently use the inner
            # dimension and the value-arg form is what matters).
            first_close = tail.find("]")
            remainder = tail[first_close + 1:] if first_close >= 0 else ""
            type_str = type_str + "*" + remainder
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
    function_definitions: dict[str, str] = {}

    matches = list(_FUNC_DEF_RE.finditer(source))

    for i, m in enumerate(matches):
        fn_name = m.group("name")
        if fn_name in _KEYWORDS:
            continue

        ret_type = m.group("ret").strip()
        raw_params = m.group("params").strip()
        params = _parse_params_regex(raw_params)
        is_static = "static" in ret_type.split()

        # Extract body: from { to matching }
        body_start = m.end() - 1  # points at the '{'
        body_text = _extract_body(source, body_start)
        body_end = body_start + len(body_text)

        functions[fn_name] = FunctionSignature(
            name=fn_name,
            return_type=ret_type,
            parameters=params,
            is_static=is_static,
        )
        function_bodies[fn_name] = body_text
        function_definitions[fn_name] = source[m.start():body_end]

        # Collect calls within the body
        callees: set[str] = set()
        for cm in _CALL_RE.finditer(body_text):
            callee = cm.group(1)
            if callee not in _KEYWORDS:
                callees.add(callee)
        call_graph[fn_name] = callees

    address_taken_in = _compute_address_takers(functions, function_bodies)

    return ParsedCFile(
        path=path,
        functions=functions,
        call_graph=call_graph,
        function_bodies=function_bodies,
        function_definitions=function_definitions,
        address_taken_in=address_taken_in,
    )


def _compute_address_takers(
    functions: "dict[str, FunctionSignature]",
    function_bodies: "dict[str, str]",
) -> dict[str, set[str]]:
    """For each function in *functions*, find which OTHER functions' bodies
    contain its identifier in a non-call position.

    Returns a dict ``address_taken_in[name] = {caller1, caller2, ...}``
    where each entry means "caller's body mentions name without an
    immediately-following ``(``".

    Used by Phase 3's caller-feasibility check: a function whose
    address is taken (passed to a registration function, stored in a
    vtable struct field, used as a callback) is reachable through
    that indirection even though direct-call search returns nothing.

    Conservative — picks up some non-callback references too (e.g. a
    function name appearing in a comment, in a #define expansion, or
    cast to a pointer for diagnostic purposes). Those false-positives
    are harmless for Phase 3's "is the function reachable at all"
    question.

    Cheap to compute: one body-scan per function, with the name set as
    a precomputed regex alternation.
    """
    if not functions or not function_bodies:
        return {}
    fn_names = list(functions.keys())
    if not fn_names:
        return {}
    # Build a regex that matches any function name followed by a
    # non-call context (anything other than ``(``, possibly with
    # whitespace).
    name_alt = "|".join(re.escape(n) for n in fn_names)
    # ``\b(NAME)\b(?!\s*\()`` — match the bare identifier, NOT followed
    # by optional whitespace + ``(``.
    pat = re.compile(rf"\b({name_alt})\b(?!\s*\()")
    result: dict[str, set[str]] = {}
    for caller_name, body in function_bodies.items():
        if not body:
            continue
        for m in pat.finditer(body):
            target = m.group(1)
            if target == caller_name:
                # Self-reference (recursion mark, etc.) — not an
                # address-take we care about for cross-function flow.
                continue
            result.setdefault(target, set()).add(caller_name)
    return result


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
    """Extract the text from the opening brace to its matching closing brace.

    Skips braces that appear inside char literals (``'}'``), string literals
    (``"...{..."``), and comments (``/* } */``, ``// }``).  CIL/kernel sources
    (ldv) embed ``'{'``/``'}'`` char literals in function bodies; a naive brace
    counter miscounts those and returns a TRUNCATED body.  Downstream that
    truncation orphaned the remainder of the function as a bare ``{...}`` block
    (``_kernel_raw_decls`` def->prototype rewrite), yielding a CBMC
    ``syntax error before '__cil_tmpNNN'`` PARSING ERROR that masked the bug
    (p54usb).  Proper C lexing fixes it generally; AWS bodies are unaffected
    (their brace counts already balance)."""
    depth = 0
    i = open_brace
    n = len(source)
    while i < n:
        ch = source[i]
        # char literal: '..'  (handle escapes like '\\}' / '\\0')
        if ch == "'":
            i += 1
            while i < n and source[i] != "'":
                if source[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        # string literal: "..."
        if ch == '"':
            i += 1
            while i < n and source[i] != '"':
                if source[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        # comments
        if ch == "/" and i + 1 < n:
            nxt = source[i + 1]
            if nxt == "*":
                i += 2
                while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            if nxt == "/":
                i += 2
                while i < n and source[i] != "\n":
                    i += 1
                continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace: i + 1]
        i += 1
    return source[open_brace:]
