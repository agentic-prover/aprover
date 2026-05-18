"""
CBMC harness generator for BMC-Agent Phase 2.

For each function F, generates a self-contained C file that:
  1. Declares all structs/typedefs from the original source.
  2. Provides stubs for each callee of F.
  3. Creates nondeterministic inputs constrained by F's precondition.
  4. Calls F and asserts F's postcondition at all exit points.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Optional

from bmc_agent.config import Config
from bmc_agent.dsl_to_cbmc import postcond_to_assert, precond_to_assume
from bmc_agent.parser import FunctionInfo, FunctionSignature, ParsedCFile
from bmc_agent.spec import Spec


# ---------------------------------------------------------------------------
# Helpers: extract non-function declarations from a source file
# ---------------------------------------------------------------------------


def _extract_type_declarations(source_text: str, parsed_file: Optional["ParsedCFile"] = None) -> str:
    """
    Return only non-function-definition portions of a C source file.

    When *parsed_file* is supplied we use the already-extracted function bodies
    to locate (and excise) each function definition precisely.  This handles
    both K&R brace-on-same-line and ANSI brace-on-next-line styles.

    Without *parsed_file* we fall back to a conservative line-by-line scan.

    Always strips cpp ``# N "filename" [flags]`` line directives at the end —
    these only appear when the input was preprocessed (``cc -E`` / ``make foo.i``).
    They are diagnostic hints for compilers that *can* re-resolve the named
    headers; CBMC's frontend tries to re-read them from disk (relative paths)
    and either fails or pulls in conflicting kernel typedefs (``u_int8_t``
    in ``./include/linux/types.h``) on top of our libc ``<stdint.h>``.
    Stripping them is safe: the inlined content is still present, only the
    "this line came from header X" annotation goes away.
    """
    if parsed_file is not None and parsed_file.function_bodies:
        text = _extract_type_decls_using_bodies(source_text, parsed_file)
    else:
        text = _extract_type_decls_heuristic(source_text)
    return _strip_cpp_linemarkers(text)


# Match cpp ``# N "filename" [flags]`` line directives anchored at line start.
# The optional trailing digits are the cpp ``flags`` ( 1=enter file, 2=exit,
# 3=system, 4=extern). Whole line is removed.
_CPP_LINEMARKER_RE = re.compile(r'^\s*#\s+\d+\s+"[^"]*"(?:\s+[0-9 ]+)?\s*$', re.MULTILINE)


def _strip_cpp_linemarkers(text: str) -> str:
    """Remove cpp ``# N "filename" [flags]`` line directives left over from
    preprocessing. They carry no semantic content for CBMC; their original
    purpose was to let downstream compilers report errors against the
    untouched source. CBMC's frontend treats them either as hints to
    re-include (which fails when relative paths don't resolve from the
    harness's working dir) or processes the nested context as live header
    inclusions (which conflicts with the libc types the harness already
    pulled in via system ``#include``s).

    Replace each matched line with an empty line so downstream byte/line
    counts stay aligned with whatever was before the strip.
    """
    return _CPP_LINEMARKER_RE.sub("", text)


def _find_decl_preamble(source_text: str, def_start: int) -> int:
    """
    Walk backward from *def_start* over any contiguous attribute / annotation
    lines that belong to the same declaration but sit above the line tree-sitter
    treats as the function_definition node (e.g. ``UPB_NOINLINE`` on its own
    line, or ``__attribute__((...))`` annotations).

    Stops at the previous statement-terminator (``;``), block-terminator (``}``),
    comment-end (``*/``), preprocessor directive, or beginning of file.

    Returns the offset of the first character of the preamble (which may equal
    ``def_start`` if there is no preamble).
    """
    if def_start <= 0:
        return def_start

    # Walk backward line-by-line over candidate preamble lines.
    line_end = def_start  # exclusive
    while line_end > 0:
        # Find start of the previous line (exclusive of '\n').
        prev_nl = source_text.rfind("\n", 0, line_end - 1)
        line_start = prev_nl + 1 if prev_nl >= 0 else 0
        line = source_text[line_start:line_end - 1] if line_end > 0 else ""
        stripped = line.strip()

        if not stripped:
            # Blank line — preamble does not bridge blank lines.
            return line_end

        # Hard stops: previous decl/block terminator, comment, preprocessor.
        if stripped.endswith(";") or stripped.endswith("}") or stripped.endswith("*/"):
            return line_end
        if stripped.startswith("#"):
            return line_end

        # Candidate preamble line: identifier(s) or attribute macros, possibly
        # with parens (e.g. __attribute__((noinline))).  Heuristic: no braces,
        # no semicolons, doesn't end mid-statement.
        if "{" in stripped or "}" in stripped:
            return line_end

        # Include this line in the preamble.
        line_end = line_start
        if line_start == 0:
            return 0

    return line_end


def _extract_type_decls_using_bodies(source_text: str, parsed_file: "ParsedCFile") -> str:
    """
    Exclude function definitions from *source_text*.

    Preferred path: use ``parsed_file.function_definitions`` (full text from the
    tree-sitter ``function_definition`` node, including return type and body),
    extended backward over any attribute/annotation preamble.  Multi-line
    return types and attribute-on-own-line declarations are excised cleanly.

    Fallback path (for parsers that didn't populate ``function_definitions``):
    locate each function body and walk backward to pick up the return-type
    line.  Only reliably handles single-line signatures.
    """
    exclude: list[tuple[int, int]] = []  # (start, end) char offsets to drop
    function_defs = getattr(parsed_file, "function_definitions", None) or {}

    for func_name, body_text in parsed_file.function_bodies.items():
        if not body_text:
            continue

        full_def = function_defs.get(func_name)
        if full_def:
            def_start = source_text.find(full_def)
            if def_start != -1:
                preamble_start = _find_decl_preamble(source_text, def_start)
                exclude.append((preamble_start, def_start + len(full_def)))
                continue

        # Fallback: walk back from body to the signature's '(' line.
        body_start = source_text.find(body_text)
        if body_start == -1:
            continue
        body_end = body_start + len(body_text)

        # Walk backward from body_start over whitespace to reach the ')'
        j = body_start - 1
        while j >= 0 and source_text[j] in " \t\n\r":
            j -= 1

        # If we land on ')' find its matching '(' (end of parameter list)
        if j >= 0 and source_text[j] == ")":
            depth = 0
            while j >= 0:
                if source_text[j] == ")":
                    depth += 1
                elif source_text[j] == "(":
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1

        # Walk backward to the start of the line that begins the signature
        # (the return-type line).
        while j > 0 and source_text[j - 1] != "\n":
            j -= 1
        sig_start = j

        exclude.append((sig_start, body_end))

    if not exclude:
        return source_text.strip()

    # Merge overlapping spans, sort, then build output
    exclude.sort()
    merged: list[tuple[int, int]] = []
    for span in exclude:
        if merged and span[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
        else:
            merged.append(list(span))  # type: ignore[arg-type]

    parts: list[str] = []
    pos = 0
    for start, end in merged:
        if pos < start:
            parts.append(source_text[pos:start])
        pos = end
    if pos < len(source_text):
        parts.append(source_text[pos:])

    return "".join(parts).strip()


def _extract_type_decls_heuristic(source_text: str) -> str:
    """
    Fallback: collect non-function lines using a brace-depth state machine.
    Handles both same-line and next-line opening braces.
    """
    lines = source_text.splitlines(keepends=True)
    result_lines: list[str] = []
    brace_depth = 0
    in_function = False
    # Lines that might be a function signature (buffered until we know)
    sig_buffer: list[str] = []
    saw_parens = False  # saw '(' ... ')' at depth 0

    for line in lines:
        stripped = line.strip()
        opens = line.count("{")
        closes = line.count("}")

        if in_function:
            brace_depth += opens - closes
            if brace_depth <= 0:
                in_function = False
                brace_depth = 0
                sig_buffer = []
                saw_parens = False
            continue

        # At top level.
        if "(" in line and ")" in line and not stripped.endswith(";"):
            # Might be the start of a function signature
            if not re.match(r"^\s*(struct|union|enum|typedef)\b", line):
                saw_parens = True

        if opens > 0 and brace_depth == 0:
            new_depth = opens - closes
            is_struct = re.match(r"^\s*(struct|union|enum|typedef)\b", line)
            if saw_parens and not is_struct:
                # Opening brace of a function definition — drop sig_buffer + this line
                in_function = True
                brace_depth = new_depth
                sig_buffer = []
                saw_parens = False
                continue
            else:
                # Struct/union/enum/typedef — flush buffer and keep this line
                result_lines.extend(sig_buffer)
                sig_buffer = []
                saw_parens = False
                result_lines.append(line)
                brace_depth += new_depth
                continue

        if saw_parens:
            # Buffering potential signature lines
            sig_buffer.append(line)
        else:
            result_lines.extend(sig_buffer)
            sig_buffer = []
            result_lines.append(line)

    # Flush any remaining buffered lines
    result_lines.extend(sig_buffer)
    return "".join(result_lines).rstrip()


# ---------------------------------------------------------------------------
# Helpers: generate stub for a callee
# ---------------------------------------------------------------------------


def _c_default_value(ret_type: str) -> str:
    """Return a sensible C default / nondeterministic value for a return type."""
    rt = ret_type.strip().lower().rstrip("*").strip()
    if "*" in ret_type:
        return "NULL"
    if rt in ("void",):
        return ""
    if rt in ("int", "long", "short", "char", "signed", "unsigned",
              "size_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
              "int8_t", "int16_t", "int32_t", "int64_t", "ssize_t"):
        return "0"
    if rt in ("float", "double"):
        return "0.0"
    return "0"


def _params_str(params: list[tuple[str, str]]) -> str:
    """Build a C parameter list string, handling variadic '...' correctly."""
    if not params:
        return "void"
    parts = []
    for ptype, pname in params:
        if ptype == "...":
            parts.append("...")
        elif pname:
            parts.append(f"{ptype} {pname}")
        else:
            parts.append(ptype)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Selective callee inlining (FP reduction)
# ---------------------------------------------------------------------------

# Allocator-family names. Calls to these inside a candidate callee body
# disqualify it from inlining, because we want CBMC to use our built-in
# stub contracts (which model NULL-or-valid-pointer) rather than chase
# the real implementation's symbolic allocation behaviour.
_ALLOC_FAMILY_DISQUALIFY: frozenset[str] = frozenset({
    "malloc", "calloc", "realloc", "free", "strdup", "strndup",
    "xmlMalloc", "xmlMallocAtomic", "xmlRealloc", "xmlFree", "xmlMemStrdup",
    "Curl_cmalloc", "Curl_ccalloc", "Curl_crealloc", "Curl_cfree", "Curl_cstrdup",
    "OPENSSL_malloc", "OPENSSL_zalloc", "OPENSSL_free", "OPENSSL_realloc",
    "CRYPTO_malloc", "CRYPTO_free", "CRYPTO_realloc",
    "g_malloc", "g_free", "g_realloc",
})


def _strip_c_comments(src: str) -> str:
    """Return *src* with C line- and block-comments removed.

    Used only for callee-body shape analysis (LoC count + token scan),
    so we don't need a full lexer — a regex pass is sufficient and
    safer than mis-stripping a string literal containing /* … */.
    String / char literals are left intact (we only strip comments).
    """
    # Block comments — non-greedy across lines.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    # Line comments — to end of line.
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _should_inline_callee(
    callee_name: str,
    parsed_file: "ParsedCFile",
    max_loc: int = 30,
) -> tuple[bool, str]:
    """Decide whether *callee_name* should be inlined (real body
    embedded in the harness) instead of stubbed.

    Returns ``(eligible, reason)``. Conservative by design — when a
    rule rejects, the caller falls back to the existing stub path, so
    rejection cannot regress correctness.

    Eligibility (ALL must hold):
      - callee body is defined in the parsed file (not extern);
      - signature is ``static`` (file-local linkage);
      - body is at most ``max_loc`` non-empty, non-comment lines;
      - no loops in body (``for`` / ``while`` / ``do``);
      - body does not call any allocator-family function;
      - body is not directly recursive;
      - body does not dispatch through a function pointer
        (``(*foo)(...)`` patterns).

    Rationale: the disqualifiers exactly cover the cases where
    inlining (a) explodes CBMC state (loops, allocators, recursion),
    or (b) requires extra reasoning the rule set can't do safely
    (function-pointer dispatch). What's left is small pure helpers —
    predicates, getters, accessors — which is exactly where stub
    disconnect drives FPs (jv_get_kind, xmlIsBlank_ch, BUF_ERROR, …).
    """
    cfi = parsed_file.get_function_info(callee_name)
    if cfi is None:
        return False, "callee not defined in parsed file (extern)"
    # The parser's tree-sitter path doesn't include the storage class in
    # signature.return_type (it strips to the bare base type), so
    # signature.is_static is unreliable for tree-sitter-parsed sources.
    # Cross-check against the full function definition text, which captures
    # storage class specifiers verbatim. The regex fallback DOES set
    # is_static correctly, so we accept either signal.
    is_static = bool(getattr(cfi.signature, "is_static", False))
    if not is_static:
        fdef = (parsed_file.function_definitions or {}).get(callee_name, "")
        # ``static`` appearing before the function name in the definition
        # header is the C storage-class specifier we care about.
        header = fdef.split("{", 1)[0]
        if re.search(r"\bstatic\b", header):
            is_static = True
    if not is_static:
        return False, "callee is not file-local static"
    body = cfi.body or ""
    if not body.strip():
        return False, "callee body is empty"
    body_clean = _strip_c_comments(body)
    nonempty = [ln for ln in body_clean.splitlines() if ln.strip()]
    if len(nonempty) > max_loc:
        return False, f"body has {len(nonempty)} LoC (cap {max_loc})"
    # Loop detection. ``\b(for|while|do)\s*[({]`` covers C control-flow
    # keywords; ``do { … } while`` is caught by the ``do`` match.
    if re.search(r"\b(for|while|do)\s*[({]", body_clean):
        return False, "body contains a loop"
    # goto-based loops: a backward goto would also form a loop, but in
    # practice the linux-kernel-style ``goto out;`` pattern is forward.
    # We conservatively reject any goto at all, since CBMC unwind
    # analysis on goto-graphs is harder to reason about.
    if re.search(r"\bgoto\s+\w+", body_clean):
        return False, "body uses goto"
    # Allocator-family disqualifier.
    for alloc in _ALLOC_FAMILY_DISQUALIFY:
        if re.search(r"\b" + re.escape(alloc) + r"\s*\(", body_clean):
            return False, f"body calls allocator-family function {alloc}"
    # Direct recursion.
    if re.search(r"\b" + re.escape(callee_name) + r"\s*\(", body_clean):
        return False, "body is directly recursive"
    # Function-pointer dispatch — ``(*foo)(...)`` or ``(*foo->bar)(...)``.
    if re.search(r"\(\s*\*\s*[\w.\->]+\s*\)\s*\(", body_clean):
        return False, "body dispatches through function pointer"
    return True, "eligible"


def _generate_stub(
    callee_name: str,
    callee_spec: Optional[Spec],
    parsed_file: ParsedCFile,
    extern_sigs: Optional[dict] = None,
) -> str:
    """Generate a C stub function for a callee.

    *extern_sigs* is an optional dict mapping callee names to FunctionSignature
    objects sourced from other parsed files (multi-file mode).  When the callee
    is not in *parsed_file.functions* we check here before giving up.
    """
    sig = parsed_file.functions.get(callee_name)
    if sig is None and extern_sigs:
        sig = extern_sigs.get(callee_name)
    if sig is None:
        # Unknown external — emit a fully-generic havoc stub.
        # We don't know the signature, so we use a conservative
        # void-returning stub that at least prevents a compile error.
        return (
            f"/* Auto-stub for unknown external: {callee_name} */\n"
            f"void {callee_name}_stub(void) {{ /* unknown signature — void havoc */ }}"
        )

    ret_type = sig.return_type.strip()
    params = sig.parameters
    params_str = _params_str(params)

    stub_name = f"{callee_name}_stub"

    lines: list[str] = [
        f"/* Stub for callee: {callee_name} */",
        f"{ret_type} {stub_name}({params_str}) {{",
    ]

    # Assert callee precondition (to catch violations)
    if callee_spec and callee_spec.precondition.strip() not in ("true", "", "1"):
        param_names = [pname for _, pname in params]
        assume_stmts = precond_to_assume(callee_spec.precondition, param_names)
        if assume_stmts:
            lines.append("    /* Assert callee precondition */")
            for stmt in assume_stmts:
                lines.append(f"    {stmt}")

    if ret_type.strip() == "void":
        lines.append("    /* void return — nothing to havoc */")
    else:
        # Havoc the return value: declare it, constrain by postcondition
        lines.append(f"    {ret_type} result;")

        # Built-in stub contracts for well-known allocator-family externs.
        # Without these, CBMC stubs return arbitrary garbage pointers that
        # alias unrelated memory regions, producing a large class of
        # false-positive NULL-deref / OOB findings. The contracts model
        # the documented behavior: return NULL or a valid pointer to N bytes.
        builtin_contract = _builtin_stub_return_contract(
            callee_name, ret_type, params
        )
        if builtin_contract:
            lines.append("    /* Built-in stub contract (allocator-family) */")
            lines.extend(f"    {c}" for c in builtin_contract)
        else:
            # Sibling-derived return contract: when the callee has no body
            # in this translation unit (i.e. it's extern), look at other
            # functions with the same prefix and infer a return-value
            # contract from theirs (``vfs_*`` consistently returns 0/-1).
            # Kills the recurring kapi.c-style FP where a nondet stub
            # returns ``1`` and trips a caller assertion that real callers
            # never see. Conservative — emits nothing when siblings don't
            # agree.
            inferred = _infer_extern_return_contract(
                callee_name, ret_type, parsed_file
            )
            if inferred:
                lines.append(
                    f"    /* Inferred from {callee_name.split('_',1)[0]}_* sibling return values */"
                )
                lines.extend(f"    {c}" for c in inferred)
            elif callee_spec and callee_spec.postcondition.strip() not in ("true", "", "1"):
                param_names = [pname for _, pname in params]
                assert_stmts = postcond_to_assert(callee_spec.postcondition, param_names)
                if assert_stmts:
                    lines.append("    /* Havoc return value subject to postcondition */")
                    for stmt in assert_stmts:
                        # In stub context, use __CPROVER_assume instead of assert
                        stmt = stmt.replace("assert(", "__CPROVER_assume(")
                        lines.append(f"    {stmt}")
        lines.append("    return result;")

    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Library-init global function pointers — assumed non-NULL by harness
# ---------------------------------------------------------------------------

# Known library-init global function pointers. CBMC's nondet default for
# an unbound extern is NULL, which is wrong for these: real public APIs
# always go through library init that assigns them. The harness assumes
# `extern != NULL` up front so CBMC doesn't waste effort (or emit
# spurious CEs) on the impossible "uninitialized library" state.
#
# Grouped by library so we can detect them via either header-style or
# `extern ... = ` source pattern; the actual `__CPROVER_assume` is the
# same — we just need the symbol to exist in the linked image.
_LIBRARY_INIT_GLOBALS: tuple[str, ...] = (
    # libxml2
    "xmlMalloc", "xmlMallocAtomic", "xmlRealloc", "xmlFree", "xmlMemStrdup",
    "xmlGenericError", "xmlStructuredError",
    "xmlOutputBufferCreateFilenameValue",
    "xmlParserInputBufferCreateFilenameValue",
    # libcurl
    "Curl_cmalloc", "Curl_ccalloc", "Curl_crealloc", "Curl_cfree", "Curl_cstrdup",
    # OpenSSL / BoringSSL
    "CRYPTO_malloc", "CRYPTO_free", "CRYPTO_realloc",
    # glib
    "g_malloc", "g_free", "g_realloc",
)


def _emit_learned_clauses(
    config: "Config", func_name: str, scope: str
) -> list[str]:
    """Return `__CPROVER_assume(...)` statements for clauses learned
    from previous realism rejections (feedback loop arm (b)/(c)).

    ``scope`` is "project" or "function". Function clauses are bound
    to the named function; project clauses apply to every harness.
    Returns empty list when the feedback loop is disabled or the store
    is empty.
    """
    if not getattr(config, "enable_feedback_loop", False):
        return []
    try:
        from bmc_agent.feedback_loop import LearnedConstraintsStore
        store = LearnedConstraintsStore(config.artifact_dir)
        if scope == "project":
            clauses = store.project_clauses()
        else:
            clauses = store.function_clauses(func_name)
    except Exception as exc:
        # Don't ever fail harness generation because of feedback-loop I/O.
        # The constraints are an OPTIMIZATION, not a correctness invariant.
        # Logging at debug level since this isn't an error per se.
        from bmc_agent.logger import get_logger
        get_logger("harness").debug(
            "Feedback-loop clause read failed for '%s': %s", func_name, exc,
        )
        return []
    return [f"__CPROVER_assume({c});" for c in clauses if c.strip()]


def _emit_library_init_function_pointer_overrides(parsed_file: "ParsedCFile") -> tuple[list[str], list[str]]:
    """Return (decl_lines, init_lines) that:
      - declare a BMC-side stub function (allocator-shaped) and
      - in main(), set the library's global function pointer to that stub.

    Why we need this on top of ``_emit_library_init_assumptions``:
      ``__CPROVER_assume(xmlMalloc != NULL)`` only constrains the NULL
      check. CBMC's ``--pointer-check`` is stricter for function pointers:
      it verifies the pointer points to a valid function code object.
      An extern function pointer with no body satisfies "not NULL" but
      can still trip pointer-check at the call site
      ``ret = xmlMalloc(size);`` → reported as
      ``pointer_dereference.N (dereference failure: pointer NULL in xmlMalloc)``.

    Pointing the global at a real stub function in main() makes the call
    site dispatch to a concrete function body — pointer-check passes,
    AND the stub's return value is constrained the same way our
    ``_builtin_stub_return_contract`` constrains it (NULL or valid pointer
    to ``size`` writable bytes).
    """
    import re as _re

    bodies = getattr(parsed_file, "function_bodies", None) or {}
    text_blobs: list[str] = []
    for body in bodies.values():
        if body:
            text_blobs.append(body)
    preproc = getattr(parsed_file, "preprocessed_source", None)
    if preproc:
        text_blobs.append(preproc)
    combined = "\n".join(text_blobs)

    decls: list[str] = []
    inits: list[str] = []
    seen_stub_names: set[str] = set()

    # xmlMalloc / xmlMallocAtomic / OPENSSL_malloc / CRYPTO_malloc — single
    # size_t arg, return malloc-style pointer.
    #
    # Use `__CPROVER_allocate(size, 0)` which creates a CBMC-tracked
    # writable symbolic region of exactly `size` bytes. Downstream memset
    # / pointer-write checks then correctly see the allocation metadata.
    # The earlier `void *p; __CPROVER_assume(__CPROVER_w_ok(p, size));`
    # version didn't propagate writability through the return (schematron.c
    # ``memset(ret, 0, sizeof(xmlSchematron))`` was reported as
    # `precondition_instance.1` despite the stub's assume).
    for name in ("xmlMalloc", "xmlMallocAtomic", "OPENSSL_malloc",
                 "OPENSSL_zalloc", "CRYPTO_malloc"):
        if not _re.search(r"(?<![A-Za-z0-9_])" + _re.escape(name) + r"(?![A-Za-z0-9_])", combined):
            continue
        stub = f"_bmc_stub_{name}"
        if stub in seen_stub_names:
            continue
        seen_stub_names.add(stub)
        decls.append(
            f"static void *{stub}(size_t size) {{\n"
            f"    int nondet_null;\n"
            f"    if (nondet_null) return NULL;\n"
            f"    return __CPROVER_allocate(size, 0);\n"
            f"}}"
        )
        inits.append(f"{name} = {stub};")

    # xmlRealloc / OPENSSL_realloc — (void *, size_t) → void *
    for name in ("xmlRealloc", "OPENSSL_realloc", "CRYPTO_realloc"):
        if not _re.search(r"(?<![A-Za-z0-9_])" + _re.escape(name) + r"(?![A-Za-z0-9_])", combined):
            continue
        stub = f"_bmc_stub_{name}"
        if stub in seen_stub_names:
            continue
        seen_stub_names.add(stub)
        decls.append(
            f"static void *{stub}(void *old, size_t size) {{\n"
            f"    (void)old;\n"
            f"    int nondet_null;\n"
            f"    if (nondet_null) return NULL;\n"
            f"    return __CPROVER_allocate(size, 0);\n"
            f"}}"
        )
        inits.append(f"{name} = {stub};")

    # xmlFree / OPENSSL_free / CRYPTO_free — void(void *)
    for name in ("xmlFree", "OPENSSL_free", "CRYPTO_free"):
        if not _re.search(r"(?<![A-Za-z0-9_])" + _re.escape(name) + r"(?![A-Za-z0-9_])", combined):
            continue
        stub = f"_bmc_stub_{name}"
        if stub in seen_stub_names:
            continue
        seen_stub_names.add(stub)
        decls.append(
            f"static void {stub}(void *p) {{ (void)p; }}"
        )
        inits.append(f"{name} = {stub};")

    # xmlMemStrdup / OPENSSL_strdup — (const char *) → char *
    for name in ("xmlMemStrdup", "OPENSSL_strdup"):
        if not _re.search(r"(?<![A-Za-z0-9_])" + _re.escape(name) + r"(?![A-Za-z0-9_])", combined):
            continue
        stub = f"_bmc_stub_{name}"
        if stub in seen_stub_names:
            continue
        seen_stub_names.add(stub)
        decls.append(
            f"static char *{stub}(const char *s) {{\n"
            f"    (void)s;\n"
            f"    char *p;\n"
            f"    __CPROVER_assume(p == NULL || __CPROVER_r_ok(p, 1));\n"
            f"    return p;\n"
            f"}}"
        )
        inits.append(f"{name} = {stub};")

    return decls, inits


def _emit_library_init_assumptions(parsed_file: "ParsedCFile") -> list[str]:
    """Return `__CPROVER_assume(X != NULL);` statements for each
    library-init global referenced by code parsed_file knows about.

    Detection: a global is "referenced" if its name appears as a bare
    identifier (word boundary on both sides) anywhere in any function
    body of the parsed file. We can't safely emit the assume if the
    symbol isn't even mentioned because the linker / preprocessor
    might not expose it.
    """
    import re as _re
    referenced: set[str] = set()
    bodies = getattr(parsed_file, "function_bodies", None) or {}
    text_blobs: list[str] = []
    for body in bodies.values():
        if body:
            text_blobs.append(body)
    # Also scan the preprocessed source if available.
    preproc = getattr(parsed_file, "preprocessed_source", None)
    if preproc:
        text_blobs.append(preproc)
    if not text_blobs:
        return []
    combined = "\n".join(text_blobs)
    for name in _LIBRARY_INIT_GLOBALS:
        if _re.search(r"(?<![A-Za-z0-9_])" + _re.escape(name) + r"(?![A-Za-z0-9_])", combined):
            referenced.add(name)
    if not referenced:
        return []
    return [
        f"__CPROVER_assume({name} != NULL);"
        for name in sorted(referenced)
    ]


def _extract_source_precondition_asserts(
    func_body: str, param_names: list[str]
) -> list[str]:
    """Return ``__CPROVER_assume(expr);`` statements derived from
    ``assert(expr)`` calls at the top of ``func_body`` whose expression
    references only the function's own parameter names.

    Pattern from jq's jv_alloc.c (shipped 2026-05-13):

        void* jv_mem_calloc(size_t nemb, size_t sz) {{
            assert(nemb > 0 && sz > 0);
            ...
        }}

    The C ``assert()`` documents an internal-helper precondition. Real
    callers obey it; the bmc-agent harness, passing nondet params, does
    not — so the assertion fires and CBMC reports a "real_bug". The fix
    is to mirror the precondition into a ``__CPROVER_assume(expr)`` at
    Step 2 of the harness, exactly as a real caller would respect it.

    Conservative scope:
      * Only matches ``assert(...)`` invocations BEFORE the first
        side-effecting statement (assignment, call, return). Once the
        body modifies state, an assert is no longer a pure precondition.
      * Only accepts assertions whose expression's identifiers are all
        either parameter names or numeric literals / constants. This
        excludes asserts over global state we'd have no business
        assuming about.
      * Skips ``assert(0)`` and ``assert(false)`` — those are
        unreachability markers, not preconditions.
    """
    if not func_body or not param_names:
        return []
    import re as _re
    # Find the opening brace (function body start).
    brace = func_body.find("{")
    if brace < 0:
        return []
    body = func_body[brace + 1 :]
    # Strip line comments and block comments — keep it simple, scan the
    # first ~2 KB of body content which is more than enough for the
    # "precondition asserts at the top" idiom.
    body = body[:2000]
    body = _re.sub(r"//[^\n]*", "", body)
    body = _re.sub(r"/\*.*?\*/", "", body, flags=_re.DOTALL)

    out: list[str] = []
    seen: set[str] = set()
    param_set = set(param_names)
    pos = 0
    while pos < len(body):
        # Skip whitespace.
        while pos < len(body) and body[pos].isspace():
            pos += 1
        if pos >= len(body):
            break
        # If the next token is "assert" followed by "(", parse the call.
        m = _re.match(r"assert\s*\(", body[pos:])
        if m:
            # Find the matching close-paren.
            start = pos + m.end()
            depth = 1
            i = start
            while i < len(body) and depth > 0:
                if body[i] == "(":
                    depth += 1
                elif body[i] == ")":
                    depth -= 1
                i += 1
            if depth != 0:
                break  # malformed; bail
            expr = body[start : i - 1].strip()
            # Advance past the trailing ';' if present.
            j = i
            while j < len(body) and body[j].isspace():
                j += 1
            if j < len(body) and body[j] == ";":
                j += 1
            pos = j
            # Reject useless / wrong-shape asserts.
            if expr in ("0", "false", "1", "true", ""):
                continue
            # Only emit if every bare identifier in expr is a
            # parameter, a C keyword, NULL, or a numeric literal.
            # Strip struct member-access tails (`->field`, `.field`)
            # before extracting identifiers — `l->nlines` references
            # `l` (a parameter) plus the *member name* `nlines` which
            # is not a free identifier. This matches the jq
            # `locfile_line_length` idiom `assert(line < l->nlines)`.
            scrub = _re.sub(r"(?:->|\.)\s*[A-Za-z_][A-Za-z0-9_]*", "", expr)
            idents = set(_re.findall(r"[A-Za-z_][A-Za-z0-9_]*", scrub))
            non_param = idents - param_set - {
                "NULL", "true", "false",
                # benign C / stdint constants commonly appearing in
                # parameter-bound asserts:
                "SIZE_MAX", "INT_MAX", "INT_MIN", "UINT_MAX",
                "LONG_MAX", "LONG_MIN", "ULONG_MAX",
                "sizeof",
            }
            if non_param:
                continue
            if expr in seen:
                continue
            seen.add(expr)
            out.append(f"__CPROVER_assume({expr});")
            continue
        # Not an assert: detect "side-effecting statement starts".
        # Crude heuristic — if we hit any '=' or ';' or '(' that isn't
        # inside an assert(), we've left the prelude. We treat the
        # first such char as the boundary.
        if body[pos] in ("=", ";", "{", "}"):
            break
        # Tokens like "if", "while", "for", "return" also end the
        # prelude.
        if _re.match(r"(if|while|for|switch|return|do|goto)\b", body[pos:]):
            break
        # Otherwise advance one char (skip declarations like
        # `int x;` — but the ';' check above will catch those too).
        pos += 1
    return out


# Map jq jv-accessor APIs to the JV_KIND constant their parameter must
# carry. When a static helper opens by unconditionally calling one of
# these accessors on its parameter, real callers in jq always satisfy
# the corresponding kind precondition; the harness should mirror that
# (otherwise CBMC explores impossible nondet jv states and reports
# spurious NULL-deref via the stubbed accessor returning NULL).
_JV_KIND_ACCESSORS: dict[str, str] = {
    "jv_string_value": "JV_KIND_STRING",
    "jv_string_length_bytes": "JV_KIND_STRING",
    "jv_string_length_codepoints": "JV_KIND_STRING",
    "jv_string_hash": "JV_KIND_STRING",
    "jv_number_value": "JV_KIND_NUMBER",
    "jv_array_length": "JV_KIND_ARRAY",
    "jv_array_get": "JV_KIND_ARRAY",
    "jv_object_length": "JV_KIND_OBJECT",
    "jv_object_get": "JV_KIND_OBJECT",
    "jv_object_iter": "JV_KIND_OBJECT",
}


def _extract_jv_kind_preconditions(
    func, param_names: list[str]
) -> list[str]:
    """Return ``__CPROVER_assume(jv_get_kind(p) == JV_KIND_X);`` for each
    parameter ``p`` of a STATIC helper whose body opens by unconditionally
    invoking a jq jv-accessor of kind X on ``p`` (e.g.
    ``jv_string_value(name)`` at the top of ``validate_relpath``).

    Rationale (jq linker.c, 2026-05-13): static helpers in jq commonly
    skip kind-checking because their internal callers always check; the
    harness, however, passes nondet ``jv`` and the stubbed accessor
    returns NULL, producing spurious NULL-deref findings.

    Conservative: only fires when
      (1) the function is declared ``static``,
      (2) the parameter is a ``jv`` (by type token), and
      (3) an accessor call on the parameter appears in the first ~10
          non-blank lines AND is NOT preceded by a kind check
          (``jv_get_kind(p) ==`` / ``jv_is_valid(p)``).
    """
    sig = getattr(func, "signature", None)
    body = getattr(func, "body", None) or ""
    if not sig or not body:
        return []
    if not getattr(sig, "is_static", False):
        return []
    import re as _re
    # Map param name → param type so we know which params are `jv`.
    jv_params: dict[str, str] = {}
    for ptype, pname in (sig.parameters or []):
        if pname and ptype and _re.search(r"\bjv\b", ptype) and "*" not in ptype:
            jv_params[pname] = ptype
    if not jv_params:
        return []

    # Scan first 800 chars of body for early accessor calls.
    brace = body.find("{")
    head = body[brace + 1 :][:800] if brace >= 0 else body[:800]
    head = _re.sub(r"//[^\n]*", "", head)
    head = _re.sub(r"/\*.*?\*/", "", head, flags=_re.DOTALL)

    out: list[str] = []
    seen_kinds: set[tuple[str, str]] = set()
    for accessor, kind in _JV_KIND_ACCESSORS.items():
        pattern = _re.compile(
            r"(?<![A-Za-z0-9_])" + _re.escape(accessor) + r"\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)"
        )
        for m in pattern.finditer(head):
            param = m.group(1)
            if param not in jv_params:
                continue
            # Reject if a kind-check on this param precedes the accessor
            # call in head.
            prior = head[: m.start()]
            # Any earlier reference to jv_get_kind(param) or
            # jv_is_valid(param) signals the function self-validates.
            guarded = bool(_re.search(
                r"jv_get_kind\s*\(\s*" + _re.escape(param) + r"\s*\)",
                prior,
            )) or bool(_re.search(
                r"jv_is_valid\s*\(\s*" + _re.escape(param) + r"\s*\)",
                prior,
            ))
            if guarded:
                continue
            key = (param, kind)
            if key in seen_kinds:
                continue
            seen_kinds.add(key)
            out.append(
                f"__CPROVER_assume(jv_get_kind({param}) == {kind});"
            )
    return out


# ---------------------------------------------------------------------------
# Built-in stub contracts for well-known C-runtime / libxml / curl externals
# ---------------------------------------------------------------------------


def _builtin_stub_return_contract(
    name: str,
    ret_type: str,
    params: list[tuple[str, str]],
) -> list[str]:
    """Return CBMC __CPROVER_assume statements that constrain ``result``
    for a known allocator-family / library function. Empty list when the
    function isn't in the contract table.

    Patterns covered:
      malloc(size) / xmlMalloc(size) / OPENSSL_malloc / ...
        → result == NULL || valid pointer with (size) writable bytes
      calloc(n, sz) / xmlMallocAtomic
        → result == NULL || valid pointer with (n*sz) writable bytes,
          AND the bytes are zero-initialized
      realloc(p, size) / xmlRealloc
        → result == NULL || valid pointer with (size) writable bytes
      strdup(s) / xmlStrdup / strndup
        → result == NULL || valid pointer to a NUL-terminated string
      getenv(name) / secure_getenv
        → result == NULL || valid pointer to a NUL-terminated string
    """
    if "*" not in ret_type:
        return []  # only pointer-returning allocators interesting here

    n = name
    # Find size-like argument name (first parameter whose type is integral).
    size_arg = ""
    second_arg = ""
    for i, (ptype, pname) in enumerate(params):
        if not pname:
            continue
        ptype_l = (ptype or "").lower()
        looks_integral = any(
            t in ptype_l for t in
            ("size_t", "ssize_t", "unsigned", "signed", "int", "long",
             "uint", "u8", "u16", "u32", "u64")
        ) and "*" not in ptype
        if looks_integral and not size_arg:
            size_arg = pname
        elif looks_integral and not second_arg:
            second_arg = pname

    malloc_like = {
        "malloc", "xmlMalloc", "xmlMallocAtomic", "OPENSSL_malloc",
        "OPENSSL_zalloc", "g_malloc", "g_malloc0",
        "CRYPTO_malloc", "CRYPTO_zalloc",
    }
    calloc_like = {"calloc", "g_malloc_n", "g_malloc0_n"}
    realloc_like = {
        "realloc", "xmlRealloc", "OPENSSL_realloc",
        "g_realloc", "CRYPTO_realloc",
    }
    strdup_like = {
        "strdup", "xmlStrdup", "xmlCharStrdup",
        "g_strdup", "OPENSSL_strdup",
    }
    strndup_like = {
        "strndup", "xmlStrndup", "xmlCharStrndup",
        "g_strndup",
    }
    getenv_like = {"getenv", "secure_getenv"}

    if n in malloc_like and size_arg:
        return [
            f"__CPROVER_assume(result == NULL || "
            f"__CPROVER_w_ok(result, {size_arg}));"
        ]
    if n in calloc_like and size_arg and second_arg:
        return [
            f"__CPROVER_assume(result == NULL || "
            f"__CPROVER_w_ok(result, ((size_t){size_arg}) * ((size_t){second_arg})));",
        ]
    if n in realloc_like and size_arg:
        return [
            f"__CPROVER_assume(result == NULL || "
            f"__CPROVER_w_ok(result, {size_arg}));"
        ]
    if n in strdup_like:
        return [
            "/* strdup-like: NULL or pointer to NUL-terminated string */",
            "__CPROVER_assume(result == NULL || __CPROVER_r_ok(result, 1));",
        ]
    if n in strndup_like and size_arg:
        return [
            f"__CPROVER_assume(result == NULL || "
            f"__CPROVER_w_ok(result, ((size_t){size_arg}) + 1));",
        ]
    if n in getenv_like:
        return [
            "__CPROVER_assume(result == NULL || __CPROVER_r_ok(result, 1));",
        ]

    # libc string-length-bounded functions: strspn, strcspn, strlen,
    # strnlen. CBMC's default stub returns unconstrained size_t. Real
    # libc returns ≤ strlen of the input string. Without this contract,
    # callers like jq's jq_set_colors get spurious "pointer advances
    # past end" findings (LLM TODO from jv_print.c sweep).
    if "*" in ret_type:
        # not pointer-returning, handled above
        pass
    elif n in ("strlen", "strnlen"):
        # First arg is the string; size_t return ≤ strlen(s).
        # We can't easily express ≤ strlen(s) on a symbolic s; settle
        # for "≤ very-large" instead of "any size_t including >2^60".
        # Cap to a generous but finite value (1 MB) — any real string
        # passed through bmc-agent's bounded harness is far smaller.
        return [f"__CPROVER_assume(result <= 1048576);"]
    elif n in ("strspn", "strcspn"):
        return [f"__CPROVER_assume(result <= 1048576);"]
    elif n in ("strcmp", "strncmp", "memcmp"):
        # Return -1, 0, or +1 typically — but compilers may return any
        # int with sign of (a-b). Cap to plausible range.
        return [f"__CPROVER_assume(result >= -1048576 && result <= 1048576);"]
    return []


# ---------------------------------------------------------------------------
# Helpers: infer stub return contracts from sibling functions
# ---------------------------------------------------------------------------


def _infer_extern_return_contract(
    callee_name: str,
    ret_type: str,
    parsed_file: "ParsedCFile",
) -> list[str]:
    """Return CBMC __CPROVER_assume statements that constrain ``result``
    based on sibling functions in the project sharing the same prefix.

    Motivation: arm-(a) feedback from kapi.c sweep. When an extern
    callee has no body (e.g. ``vfs_rename``), CBMC defaults to
    unconstrained nondet return, allowing positive ints like ``1``
    that no real implementation would produce. The realism LLM
    correctly diagnoses this every time — but absorbing it as a
    function-spec clause for every caller is brittle. Inferring the
    contract from sibling functions in the SAME naming namespace
    (``vfs_*`` already-defined functions consistently use 0/-1) is a
    much cleaner FP filter.

    Returns empty list when:
      - ``ret_type`` is not a plain integer (we only handle int returns)
      - No prefix can be derived
      - No defined siblings exist
      - Sibling return values aren't consistent enough to derive a contract

    Conservative by design: if anything looks unusual, no contract is
    emitted (the existing stub path runs unchanged).
    """
    # Only handle plain int returns for now — pointer / struct returns
    # have their own contract paths.
    rt = (ret_type or "").strip().rstrip("*").strip()
    if rt not in ("int", "signed int", "unsigned int", "long", "signed long",
                  "unsigned long", "short", "signed short", "unsigned short",
                  "char", "signed char", "unsigned char", "int32_t", "int64_t",
                  "uint32_t", "uint64_t", "int16_t", "uint16_t", "int8_t",
                  "uint8_t", "ssize_t"):
        return []

    # Derive prefix: split at the first underscore. ``vfs_rename`` → ``vfs_``.
    if "_" not in callee_name:
        return []
    prefix = callee_name.split("_", 1)[0] + "_"
    if len(prefix) < 4:  # too generic (e.g. ``a_`` matches everything)
        return []

    fdefs = getattr(parsed_file, "function_definitions", None) or {}
    fbodies = getattr(parsed_file, "function_bodies", None) or {}
    fsigs = getattr(parsed_file, "functions", None) or {}
    # Find SIBLINGS — same prefix, NOT the callee itself, body is available,
    # AND same return type. Without the return-type filter we mix int-
    # returning vfs_set_cwd with pointer-returning vfs_lookup and get
    # garbage signal.
    target_rt = rt
    siblings: list[str] = []
    for fname in fdefs.keys():
        if fname == callee_name:
            continue
        if not fname.startswith(prefix):
            continue
        if not fbodies.get(fname):
            continue
        sib_sig = fsigs.get(fname)
        if sib_sig is None:
            continue
        sib_rt = (getattr(sib_sig, "return_type", "") or "").strip().rstrip("*").strip()
        if sib_rt != target_rt:
            continue
        siblings.append(fname)
    if len(siblings) < 2:
        # Need at least 2 siblings sharing the prefix + return type to argue
        # this is a project convention rather than a one-off.
        return []

    # Collect literal return values across siblings.  We classify each
    # ``return EXPR;`` into:
    #   - literal int (0, 1, -1, 0x10, ...)
    #   - negative macro (``-EINVAL`` — we know it's negative)
    #   - non-literal (function call, variable, expression — IGNORED)
    # Earlier versions bailed on any non-literal return.  That was too
    # conservative: a sibling like vfs_delete returns ``fat32_delete(p)``
    # alongside several literal ``-1``s.  We now collect what we can and
    # only commit to a contract when we have enough literal evidence
    # AND no sibling exclusively returns non-literals (which would
    # suggest the namespace doesn't actually use a literal convention).
    constants: set[int] = set()
    saw_negative_macro = False
    siblings_with_literals = 0
    siblings_with_only_nonliterals = 0
    # ``mixed`` siblings have BOTH a literal return AND a non-literal
    # return (e.g., ``return -1;`` alongside ``return ttULONG(...);``).
    # This is the offset/index pattern: -1 sentinel for error, otherwise
    # a computed non-negative value. Used to infer ``result >= -1``.
    siblings_with_mixed_returns = 0
    for sib in siblings:
        body = fbodies.get(sib, "")
        sib_constants: set[int] = set()
        sib_neg_macro = False
        sib_nonliteral = False
        for m in re.finditer(r"\breturn\s+([^;]+?)\s*;", body):
            e = m.group(1).strip()
            while e.startswith("(") and e.endswith(")"):
                e = e[1:-1].strip()
            try:
                sib_constants.add(int(e, 0))
                continue
            except ValueError:
                pass
            if e.startswith("-"):
                try:
                    sib_constants.add(int(e, 0))
                    continue
                except ValueError:
                    pass
                if re.match(r"^-\s*[A-Z_][A-Z0-9_]*$", e):
                    sib_neg_macro = True
                    continue
            sib_nonliteral = True
        # ``return;`` with the callee returning int is a type mismatch
        # in the sibling — skip it.
        if re.search(r"\breturn\s*;", body):
            continue
        constants |= sib_constants
        saw_negative_macro = saw_negative_macro or sib_neg_macro
        if sib_constants or sib_neg_macro:
            siblings_with_literals += 1
            if sib_nonliteral:
                # Mixed: sibling has both a literal return (e.g. -1) AND
                # a non-literal return (e.g. an offset expression).
                siblings_with_mixed_returns += 1
        elif sib_nonliteral:
            siblings_with_only_nonliterals += 1

    # Need at least 2 siblings producing literal returns AND fewer
    # all-nonliteral siblings than literal ones — otherwise the
    # "convention" signal is too weak.
    if siblings_with_literals < 2:
        return []
    if siblings_with_only_nonliterals >= siblings_with_literals:
        return []
    if not constants and not saw_negative_macro:
        return []

    # Derive the contract.
    has_positive = any(c > 0 for c in constants)
    has_zero = 0 in constants
    has_negative = any(c < 0 for c in constants) or saw_negative_macro

    # Case "offset/index family": siblings consistently return -1 (error
    # sentinel) alongside a computed non-literal value, with no positive
    # literal observed. Pattern is ``return -1; ... return computed_offset;``
    # — canonical example: stb_truetype's stbtt_GetFontOffsetForIndex,
    # which returns -1 for "no more fonts" or a non-negative byte offset.
    # Without this contract, the stub returns unconstrained int and CBMC
    # picks impossible values like -8193. Conservative trigger:
    #   - the only literal observed is -1
    #   - no negative macros (which would imply a wider error code range)
    #   - at least one sibling produced mixed literal+non-literal returns
    #     (so we're sure the namespace includes non-literal "success" paths)
    if (
        not has_positive
        and constants == {-1}
        and not saw_negative_macro
        and siblings_with_mixed_returns >= 1
    ):
        return ["__CPROVER_assume(result >= -1);"]

    # Case "non-positive, possibly with macros": all observed literals
    # are 0 or negative → result <= 0
    if not has_positive:
        if has_zero and has_negative:
            return ["__CPROVER_assume(result <= 0);"]
        if has_zero and not has_negative:
            return ["__CPROVER_assume(result == 0);"]
        if has_negative and not has_zero:
            return ["__CPROVER_assume(result < 0);"]

    # Case "small fixed set including positives": e.g. {-1, 0, 1} comparators.
    if has_positive and len(constants) <= 4 and not saw_negative_macro:
        clauses = " || ".join(f"result == {c}" for c in sorted(constants))
        return [f"__CPROVER_assume({clauses});"]

    return []


# ---------------------------------------------------------------------------
# Helpers: substitute callee calls with stubs in a function body
# ---------------------------------------------------------------------------


def _substitute_callee_calls(body: str, callees: set[str]) -> str:
    """
    Replace callee calls in *body* with stub calls.

    For each callee name C that is known to the parser, replace ``C(`` with
    ``C_stub(``.  We use a word-boundary regex to avoid partial matches.
    """
    result = body
    for callee in sorted(callees):  # deterministic order
        # Match the function name as a whole word followed by '('
        result = re.sub(
            r"\b" + re.escape(callee) + r"\s*\(",
            f"{callee}_stub(",
            result,
        )
    return result


# ---------------------------------------------------------------------------
# Helpers: generate nondeterministic variable declarations
# ---------------------------------------------------------------------------


def _infer_nonnull_params(
    func: FunctionInfo,
    all_funcs: Optional[dict],
    parsed_file: Optional[ParsedCFile],
) -> set:
    """
    Return the set of pointer parameter names that are NEVER passed NULL at any
    call site found in the current file.  Only includes params for which at least
    one call site was found (so params with no call sites are left unconstrained).
    """
    ptr_params: list[str] = []
    for ptype, pname in func.signature.parameters:
        if not pname:
            continue
        ptype_stripped = ptype.strip()
        if (ptype_stripped.endswith("*") or "*" in pname):
            base = ptype_stripped.rstrip("*").strip()
            if base.lower() not in ("void", "const void") and ptype_stripped.count("*") == 1:
                ptr_params.append(pname)

    if not ptr_params:
        return set()

    call_pattern = re.compile(
        r"(?<![a-zA-Z0-9_])" + re.escape(func.name) + r"\s*\(([^;)]{0,400})\)",
        re.DOTALL,
    )
    null_re = re.compile(r"^(NULL|0|0x0+|\(\s*\w[\w\s*]*\s*\)\s*0)$")

    found_site: dict[str, bool] = {p: False for p in ptr_params}
    null_seen: dict[str, bool] = {p: False for p in ptr_params}

    all_bodies: list[str] = []
    for fname, finfo in (all_funcs or {}).items():
        if fname != func.name and finfo.body:
            all_bodies.append(finfo.body)
    if parsed_file:
        for fname in list((parsed_file.functions or {}).keys()):
            if fname != func.name:
                fi = parsed_file.get_function_info(fname)
                if fi and fi.body:
                    all_bodies.append(fi.body)

    for body in all_bodies:
        for m in call_pattern.finditer(body):
            raw_args = m.group(1)
            args = [a.strip() for a in re.split(r",(?![^(]*\))", raw_args)]
            for i, arg in enumerate(args):
                if i < len(ptr_params):
                    pname = ptr_params[i]
                    found_site[pname] = True
                    if null_re.match(arg):
                        null_seen[pname] = True

    return {p for p in ptr_params if found_site[p] and not null_seen[p]}


def _detect_paired_pointers(
    precondition: str,
    pointer_params: set[str],
) -> list[list[str]]:
    """Return groups of pointer parameter names that should share a backing buffer.

    Parser-style C APIs commonly take pairs (or triples) of pointers
    that must point into the *same* allocation — typically a ``start`` /
    ``end`` window over an input buffer.  The precondition encodes this
    relationship via comparisons (``a <= b``) and pointer arithmetic
    (``valid_range(a, 0, b - a)``).  When the harness allocates each
    pointer parameter as an independent stack object, those constraints
    become inter-object pointer comparisons — undefined behavior in C,
    which CBMC's ``--pointer-check`` correctly flags as ``main.pointer``.
    The result is a spurious "memory_safety" finding that is purely a
    harness artifact, not a bug in the function under test.

    This helper extracts the pairing structure from the precondition so
    the harness generator can allocate one backing buffer per paired
    group and place the pointers at nondeterministic offsets within it.

    Recognised patterns (a, b are pointer parameter names):
      * ``a <= b``, ``a < b``, ``b >= a``, ``b > a``  — direct ordering
      * ``valid_range(a, lo, b - a)``                  — buffer slice
      * ``valid_range(a, lo, b - a + k)``              — inclusive slice

    Returns
    -------
    A list of groups, where each group is a list of 2+ parameter names.
    Pointer parameters that don't appear in any pairing are not
    returned (the caller falls back to per-pointer independent
    allocation for them).
    """
    if not precondition or len(pointer_params) < 2:
        return []

    parent = {p: p for p in pointer_params}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Pointer-pointer ordering: a <= b, a < b, a >= b, a > b.
    cmp_re = re.compile(r"\b(\w+)\s*(?:<=|<|>=|>)\s*(\w+)\b")
    for m in cmp_re.finditer(precondition):
        a, b = m.group(1), m.group(2)
        if a in pointer_params and b in pointer_params:
            union(a, b)

    # valid_range(a, _, b - a [+ k]): pairs (a, b).
    range_re = re.compile(
        r"\bvalid_range\(\s*(\w+)\s*,\s*[^,]+,\s*(\w+)\s*-\s*\1\b"
    )
    for m in range_re.finditer(precondition):
        a, b = m.group(1), m.group(2)
        if a in pointer_params and b in pointer_params:
            union(a, b)

    # valid_range(a, _, _ + b - a): same pairing flipped form.
    range_re2 = re.compile(
        r"\bvalid_range\(\s*(\w+)\s*,\s*[^,]+,\s*[^,)]*\+\s*(\w+)\s*-\s*\1\b"
    )
    for m in range_re2.finditer(precondition):
        a, b = m.group(1), m.group(2)
        if a in pointer_params and b in pointer_params:
            union(a, b)

    groups: dict[str, list[str]] = {}
    for p in pointer_params:
        groups.setdefault(find(p), []).append(p)
    return sorted(
        (sorted(g) for g in groups.values() if len(g) >= 2),
        key=lambda g: g[0],
    )


def _resolve_struct_name(base_type: str, struct_definitions: Optional[dict]) -> Optional[str]:
    """Given a parameter base type like ``struct Curl_str`` or ``CURLU``,
    return the matching key in ``struct_definitions`` or None.

    Handles three forms:
      - ``struct Tag`` → look up ``Tag``
      - ``Alias`` (typedef'd) → look up ``Alias``
      - ``const struct Tag`` / ``const Alias`` → strip const, retry
    """
    if not struct_definitions:
        return None
    t = re.sub(r"\bconst\b", "", base_type).strip()
    if t.startswith("struct "):
        name = t[len("struct "):].strip()
    else:
        name = t
    return name if name in struct_definitions else None


# Substrings that suggest an integer field carries a length / count /
# size, which should be constrained ``>= 0 && <= cbmc_unwind`` so CBMC
# explores realistic states rather than wildly negative or astronomical
# values that no real caller would set.
_LENGTH_FIELD_HINTS = (
    "len", "length", "size", "count", "n_", "num", "nbytes",
    "bufsize", "buflen", "mlen", "inlen", "amount", "off", "offset",
    "pos", "position", "remaining",
)


def _is_likely_length_field(field_name: str) -> bool:
    fname = field_name.lower()
    # Exact-match short names.
    if fname in ("n", "num", "len", "size", "count", "off", "pos"):
        return True
    # Substring match (e.g. ``buf_len``, ``num_items``).
    return any(hint in fname for hint in _LENGTH_FIELD_HINTS)


def _matches_struct_tag(pointee_base: str, struct_tag: str) -> bool:
    """True when *pointee_base* refers to the same struct type as
    *struct_tag*. Both forms are normalized to strip ``struct``, leading
    underscores, and the typedef alias from ``typedef struct {...} Foo;``
    so ``xmlPattern`` matches a ``struct _xmlPattern *next;`` field.
    """
    def _norm(s: str) -> str:
        s = re.sub(r"\bstruct\b", "", s).strip()
        s = s.lstrip("_")
        return s

    return _norm(pointee_base) == _norm(struct_tag)


def _emit_struct_field_init(
    obj_name: str, ftype: str, fname: str, cbmc_unwind: int,
    enclosing_struct_tag: Optional[str] = None,
) -> list[str]:
    """Emit harness statements that initialise a single struct field.

    Heuristics (conservative — never violate field types):
      - ``char *`` or ``const char *`` field: malloc a small backing
        buffer + NUL terminator + assign. Models the common
        "this struct owns a NUL-terminated string" pattern.
      - ``unsigned char *`` / ``uint8_t *`` field: raw byte buffer
        (no NUL), suitable for binary buffer fields.
      - self-referential pointer fields (linked-list ``next`` /
        ``prev`` / ``children`` style, where the field's pointee
        matches *enclosing_struct_tag*): force NULL to terminate the
        chain. Otherwise CBMC's nondet treats the field as "valid
        non-NULL pointer to garbage" and reports a spurious deref on
        the next loop iteration (libxml2 xmlPatternStreamable, see
        findings/bounty/FP_REFLECTIONS.md).
      - integer field whose name looks like a length / count / size
        (``len``, ``length``, ``size``, ``count``, ``n``, ``num``,
        ``off``, ``pos``): ``__CPROVER_assume`` it's in ``[0,
        cbmc_unwind]`` so CBMC explores reasonable inputs.
      - everything else: leave the field nondet (default tree-sitter
        behaviour); intentionally don't fight CBMC's natural symbolic
        exploration.
    """
    out: list[str] = []
    t = ftype.strip()
    # Pointer fields
    if t.endswith("*"):
        stars = t.count("*")
        # Only single-pointer fields get auto-backing buffers; double
        # pointers and beyond stay nondet (too speculative to model).
        if stars != 1:
            return out
        base = re.sub(r"\bconst\b", "", t[:-1]).strip()
        # Self-referential pointer? (linked-list-style field whose pointee
        # type matches the enclosing struct's tag.) Force NULL so the
        # harness exits any traversal loop after one iteration instead of
        # treating ->next as "valid pointer to garbage".
        if enclosing_struct_tag and _matches_struct_tag(base, enclosing_struct_tag):
            out.append(
                f"    {obj_name}.{fname} = NULL;  "
                f"/* terminate self-ref chain ({enclosing_struct_tag}.{fname}) */"
            )
            return out
        buf_size = cbmc_unwind + 1
        backing = f"_{obj_name}_{fname}_buf"
        if base == "char":
            # NUL-terminated string backing for char *.
            len_var = f"_{obj_name}_{fname}_len"
            out.append(f"    char {backing}[{buf_size}];")
            out.append(f"    unsigned int {len_var};")
            out.append(
                f"    __CPROVER_assume({len_var} <= (unsigned int){cbmc_unwind});"
            )
            out.append(f"    {backing}[{len_var}] = '\\0';")
            out.append(f"    {obj_name}.{fname} = {backing};")
        elif base in ("unsigned char", "uint8_t", "int8_t"):
            # Raw byte buffer for binary fields.
            btype = "unsigned char" if base != "int8_t" else "signed char"
            out.append(f"    {btype} {backing}[{buf_size}];")
            out.append(f"    {obj_name}.{fname} = ({t}){backing};")
        # Other pointer types (void *, struct pointers, function pointers,
        # arrays of pointers) stay nondet — modelling them would risk
        # over-constraining or compile errors on incomplete types.
        return out

    # Integer / size fields with length-suggesting names.
    if _is_likely_length_field(fname):
        out.append(
            f"    __CPROVER_assume({obj_name}.{fname} >= 0 && "
            f"{obj_name}.{fname} <= (long)({cbmc_unwind}));"
        )
    return out


def _generate_nd_decls(
    func: FunctionInfo,
    cbmc_unwind: int = 4,
    nonnull_params: Optional[set] = None,
    precondition: Optional[str] = None,
    raw_bytes: bool = False,
    struct_definitions: Optional[dict] = None,
) -> list[str]:
    """
    Generate nondeterministic variable declarations for each parameter.

    For pointer parameters, we allocate a local struct/array on the stack and
    point to it. char* and const char* parameters receive a bounded
    null-terminated string (max length = cbmc_unwind) so that string-traversal
    loops always terminate within the unwinding bound.

    Parameters that are never passed NULL at any call site in the codebase
    (as determined by _infer_nonnull_params) receive an explicit
    __CPROVER_assume(param != NULL) guard so CBMC explores only realistic paths.

    When *precondition* is supplied, _detect_paired_pointers extracts
    pointer parameter groups that should share a backing buffer (parser
    start/end pairs, etc.).  Each group is emitted as a single backing
    array with per-pointer offsets, avoiding the inter-object pointer
    comparison UB that would otherwise produce spurious memory-safety
    findings on every paired-pointer parser API.
    """
    if nonnull_params is None:
        nonnull_params = set()

    # Collect pointer parameter names for paired-buffer analysis.
    pointer_pnames: set[str] = set()
    for ptype, pname in func.signature.parameters:
        if not pname:
            continue
        if ptype.strip().endswith("*") or "*" in pname:
            pointer_pnames.add(pname)
    paired_groups = (
        _detect_paired_pointers(precondition, pointer_pnames)
        if precondition
        else []
    )
    paired_emitted: set[str] = set()

    lines: list[str] = []

    # Emit shared backing buffer per paired group first.
    ptype_by_name = {pn: pt.strip() for pt, pn in func.signature.parameters if pn}
    for group_idx, group in enumerate(paired_groups):
        # Use the first parameter's type as the canonical pointer type
        # for the group; in practice paired pointers in C parser APIs
        # always have the same element type (const char*, uint8_t*, etc.).
        ref_type = ptype_by_name[group[0]]
        base_type = ref_type.rstrip("*").strip()
        clean_base = re.sub(r"\bconst\b", "", base_type).strip() or base_type
        buf_size = cbmc_unwind + 1
        buf_name = f"_shared_buf_{group_idx}"
        lines.append(
            f"    /* Shared backing buffer for paired pointer params: "
            f"{', '.join(group)} (avoids inter-object pointer-compare UB) */"
        )
        lines.append(f"    {clean_base} {buf_name}[{buf_size}];")
        for pname in group:
            off = f"_{pname}_off"
            ptype = ptype_by_name[pname]
            lines.append(f"    unsigned int {off};")
            lines.append(
                f"    __CPROVER_assume({off} <= (unsigned int){cbmc_unwind});"
            )
            lines.append(f"    {ptype} {pname} = {buf_name} + {off};")
            paired_emitted.add(pname)
            if pname in nonnull_params:
                lines.append(
                    f"    /* {pname} is non-null by construction (shared buffer) */"
                )

    # Detect double-indirection (in-out cursor) parameters: T** + optional
    # size sibling param. Pattern is endemic to C parser APIs
    # (OpenSSL ASN.1 `(const unsigned char **pp, long max)`,
    # libxml2 `(xmlChar **str, int *len)`, nghttp2 wire decoders, ...).
    # The naive "T** -> local of T + addr-of" treatment allocates a single
    # byte and lets the function dereference into garbage; we instead
    # allocate a backing buffer of cbmc_unwind+1 elements, point the
    # cursor at it, and pass &cursor.  A sibling integer param whose
    # name suggests it's the available byte count is clamped to the
    # backing-buffer size so CBMC explores only consistent states.
    _SIZE_PARAM_NAMES = {
        "max", "omax", "len", "length", "size", "n", "nbytes",
        "bufsize", "buflen", "mlen", "inlen", "limit", "remaining",
    }
    cursor_size_assumes: dict[str, str] = {}  # size_param_name -> assume stmt
    cursor_pnames: set[str] = set()
    for ptype_outer, pname_outer in func.signature.parameters:
        if not pname_outer or pname_outer in paired_emitted:
            continue
        if ptype_outer.strip().count("*") == 2 and "void" not in ptype_outer.lower():
            cursor_pnames.add(pname_outer)
    if cursor_pnames:
        for ptype_inner, pname_inner in func.signature.parameters:
            if (pname_inner
                    and "*" not in ptype_inner
                    and pname_inner.lower() in _SIZE_PARAM_NAMES
                    and pname_inner not in cursor_size_assumes):
                cursor_size_assumes[pname_inner] = (
                    f"    __CPROVER_assume({pname_inner} >= 0 && "
                    f"{pname_inner} <= (long){cbmc_unwind});"
                )
                break  # one size param per cursor group is enough

    # Fall through to the original per-parameter logic for everything that
    # wasn't already emitted as part of a paired group.
    for ptype, pname in func.signature.parameters:
        if not pname:
            continue
        if pname in paired_emitted:
            continue
        ptype_stripped = ptype.strip()

        if ptype_stripped.endswith("*") or "*" in pname:
            # Pointer parameter
            base_type = ptype_stripped.rstrip("*").strip()
            local_name = f"_{pname}_val"
            clean_base = re.sub(r"\bconst\b", "", base_type).strip()

            # Count pointer depth: char* is depth 1, char** is depth 2
            star_count = ptype_stripped.count("*")

            if base_type.lower() in ("void", "const void"):
                lines.append(f"    /* {ptype_stripped} {pname} — void* param, left as NULL */")
                lines.append(f"    {ptype_stripped} {pname} = NULL;")
            elif star_count == 2 and pname in cursor_pnames:
                # T** in-out cursor — allocate backing buffer + cursor + addr-of.
                #
                # ptype_stripped is e.g. "const unsigned char **" or
                # "unsigned char **".  Strip exactly the two trailing stars
                # to get the element type ("const unsigned char" or
                # "unsigned char").  Const promotes on assignment so the
                # backing buffer can drop const.
                #
                # Termination mirrors the single char* policy:
                #  - raw_bytes=True: no NUL — wire-format readers (protobuf
                #    varint, ASN.1 DER) bound reads by an explicit size param
                #    rather than NUL.
                #  - raw_bytes=False (default): emit a NUL terminator at a
                #    nondet position within the buffer so string-style
                #    traversal loops (`while (ISBLANK(*p)) p++`) terminate
                #    within the unwinding bound. This is the right policy
                #    for curl's strparse.c, jq utf8 helpers, etc.
                #
                # ``char`` is heuristic for "string-shaped" — same trigger
                # as the single-char* path.
                inner_type = ptype_stripped.rstrip("*").rstrip().rstrip("*").rstrip()
                backing_base = re.sub(r"\bconst\b", "", inner_type).strip() or inner_type
                buf_size = cbmc_unwind + 1
                backing_name = f"_{pname}_backing"
                cursor_name = f"_{pname}_cursor"
                # C convention: ``char *`` means text (NUL-terminated);
                # ``unsigned char *`` / ``uint8_t *`` means raw bytes.
                # Restrict the NUL emission to plain ``char`` so wire-format
                # parsers (ASN.1, protobuf upb, etc.) using ``unsigned char *``
                # don't get artificially NUL-bounded reads.
                is_text_string = backing_base.strip() == "char"
                emit_nul = (not raw_bytes) and is_text_string
                lines.append(
                    f"    /* in-out cursor for '{pname}': "
                    f"backing buffer + advanceable cursor + addr-of-cursor"
                    f"{' (NUL-terminated)' if emit_nul else ''} */"
                )
                lines.append(f"    {backing_base} {backing_name}[{buf_size}];")
                if emit_nul:
                    nul_name = f"_{pname}_nul_at"
                    lines.append(f"    unsigned int {nul_name};")
                    lines.append(
                        f"    __CPROVER_assume({nul_name} <= (unsigned int){cbmc_unwind});"
                    )
                    lines.append(f"    {backing_name}[{nul_name}] = '\\0';")
                lines.append(f"    {inner_type} *{cursor_name} = {backing_name};")
                lines.append(f"    {ptype_stripped} {pname} = &{cursor_name};")
                if pname in nonnull_params:
                    lines.append(
                        f"    /* {pname} is non-null by construction (addr of cursor) */"
                    )
            elif clean_base in ("char", "unsigned char", "uint8_t", "int8_t") and star_count == 1:
                # Single-indirection byte-shaped pointer.  Three strategies:
                #
                #  - Default for ``char`` (raw_bytes=False): bounded
                #    null-terminated string, so strlen-style traversal
                #    loops terminate within the CBMC unwinding bound.
                #    Right for textual APIs (printf, strcpy).
                #
                #  - Default for ``unsigned char``/``uint8_t``/``int8_t``:
                #    raw byte buffer (no NUL).  These types are the C
                #    convention for binary data; NUL termination would
                #    artificially over-constrain wire-format inputs and
                #    miss bugs that only fire when the buffer contains
                #    embedded zeros.
                #
                #  - raw_bytes=True: raw byte buffer for ALL byte-shaped
                #    pointers, including ``char *``.  Right for
                #    wire-format parsers (protobuf upb varints,
                #    length-prefixed blobs) that read N raw bytes from
                #    ptr[0..N) regardless of NULs.
                #
                # char** (e.g. argv) uses the default treatment in either mode.
                buf_name = f"_{pname}_buf"
                is_textual = (clean_base == "char")
                emit_raw = raw_bytes or not is_textual
                if emit_raw:
                    lines.append(
                        f"    /* raw byte buffer for '{pname}' "
                        f"({cbmc_unwind + 1} bytes, no NUL termination) */"
                    )
                    # Use unsigned char as the backing type since we're
                    # treating these as raw bytes; const promotes on assign.
                    backing_t = "unsigned char" if clean_base != "int8_t" else "signed char"
                    lines.append(f"    {backing_t} {buf_name}[{cbmc_unwind + 1}];")
                    lines.append(f"    {ptype_stripped} {pname} = ({ptype_stripped}){buf_name};")
                else:
                    len_name = f"_{pname}_len"
                    lines.append(f"    /* bounded null-terminated string for '{pname}' (max {cbmc_unwind} chars) */")
                    lines.append(f"    char {buf_name}[{cbmc_unwind + 1}];")
                    lines.append(f"    unsigned int {len_name};")
                    lines.append(f"    __CPROVER_assume({len_name} <= (unsigned int){cbmc_unwind});")
                    lines.append(f"    {buf_name}[{len_name}] = '\\0';")
                    lines.append(f"    {ptype_stripped} {pname} = {buf_name};")
                if pname in nonnull_params:
                    lines.append(f"    __CPROVER_assume({pname} != NULL);  /* call-site: never passed NULL */")
            elif star_count == 1 and _resolve_struct_name(base_type, struct_definitions):
                # Single-pointer to a known struct — emit per-field
                # initialisation instead of just leaving the struct
                # nondet. Empirical pattern: opaque-struct pointer args
                # (`Curl_URL *u`, `nghttp2_bufs *bufs`, `ASN1_STRING *s`)
                # produced 100+ spurious CEs each because every field
                # access was unconstrained.
                struct_tag = _resolve_struct_name(base_type, struct_definitions)
                fields = struct_definitions[struct_tag]
                obj_name = f"_{pname}_obj"
                lines.append(
                    f"    /* struct-pointer init for '{pname}' ({base_type}, "
                    f"{len(fields)} field{'s' if len(fields) != 1 else ''}) */"
                )
                # Emit a stack-allocated instance (CBMC fills it with
                # nondet); we then constrain individual fields below.
                lines.append(f"    {base_type} {obj_name};")
                lines.append(f"    {ptype_stripped} {pname} = &{obj_name};")
                for ftype, fname in fields:
                    lines.extend(
                        _emit_struct_field_init(
                            obj_name, ftype, fname, cbmc_unwind,
                            enclosing_struct_tag=struct_tag,
                        )
                    )
                if pname in nonnull_params:
                    lines.append(
                        f"    /* {pname} is non-null by construction (addr of {obj_name}) */"
                    )
            elif "const" in ptype_stripped.lower():
                lines.append(f"    {clean_base} {local_name};")
                lines.append(f"    {ptype_stripped} {pname} = &{local_name};")
            else:
                lines.append(f"    {base_type} {local_name};")
                lines.append(f"    {ptype_stripped} {pname} = &{local_name};")
                if pname in nonnull_params:
                    lines.append(f"    __CPROVER_assume({pname} != NULL);  /* call-site: never passed NULL */")
        else:
            # Value parameter
            lines.append(f"    {ptype_stripped} {pname};")
            if pname in cursor_size_assumes:
                lines.append(cursor_size_assumes[pname])
    return lines


# ---------------------------------------------------------------------------
# Main harness generator
# ---------------------------------------------------------------------------


class HarnessGenerator:
    """Generates CBMC harnesses for individual C functions."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def generate_reachability_harness(
        self,
        caller: "FunctionInfo",
        callee_name: str,
        counterexample: "Counterexample",
        caller_spec: Spec,
        parsed_file: ParsedCFile,
        all_specs: Optional[dict] = None,
        callee_sig: Optional["FunctionSignature"] = None,
    ) -> str:
        """
        Generate a CBMC harness to check whether ``caller`` can produce the
        state described by ``counterexample`` at its call site to ``callee_name``.

        Strategy:
        - Run the caller body with all callees stubbed.
        - The stub for ``callee_name`` uses ``__CPROVER_assume`` to constrain
          its arguments to match the counterexample variable assignments.
        - After calling the caller, emit ``assert(0)`` — CBMC will always find
          a "counterexample" path, but only if the __CPROVER_assume constraints
          inside the stub are consistent. If CBMC returns a CEX → reachable.

        Returns the harness as a C source string.
        """
        from bmc_agent.cbmc import Counterexample  # local import to avoid circular

        fn_name = caller.name
        sig = caller.signature

        # --- 1. Collect type declarations from the source ---
        source_text = (
            parsed_file.preprocessed_source
            if parsed_file.preprocessed_source is not None
            else _read_source(caller.source_file)
        )
        type_decls = _extract_type_declarations(source_text, parsed_file)

        # --- 2. Identify callees that are defined in the parsed file ---
        defined_callees = caller.callees & set(parsed_file.functions.keys())

        # --- 3. Generate stubs for each callee ---
        # The reachability stub for ``callee_name`` constrains state via __CPROVER_assume.
        # All other callees get normal stubs.
        # callee_name always gets the reachability stub — even when it is defined in
        # a *different* file (cross-file case).  In that case we fall back to the
        # caller-provided ``callee_sig``.
        stub_sections: list[str] = []
        stubs_to_substitute: set[str] = set()
        for cname in sorted(defined_callees):
            if cname == callee_name:
                stub_src = self._generate_reachability_stub(
                    cname, counterexample, parsed_file
                )
                stubs_to_substitute.add(cname)
            else:
                callee_spec = (all_specs or {}).get(cname)
                stub_src = _generate_stub(cname, callee_spec, parsed_file)
                stubs_to_substitute.add(cname)
            stub_sections.append(stub_src)

        # If callee_name is external (not defined in parsed_file), still emit the
        # reachability stub so that assert(0) inside it can be reached by CBMC.
        if callee_name not in defined_callees and callee_name in caller.callees:
            reach_sig = callee_sig or parsed_file.functions.get(callee_name)
            stub_src = self._generate_reachability_stub(
                callee_name, counterexample, parsed_file, override_sig=reach_sig
            )
            stub_sections.append(stub_src)
            stubs_to_substitute.add(callee_name)

        # --- 4. Build the function body with callee calls substituted ---
        body_with_stubs = _substitute_callee_calls(caller.body, stubs_to_substitute)

        # Reconstruct the full function definition
        params_str = _params_str(sig.parameters)
        func_def = f"{sig.return_type} {fn_name}({params_str})\n{body_with_stubs}"

        # --- 5. Generate nondeterministic input declarations ---
        nd_decls = _generate_nd_decls(
            caller,
            raw_bytes=getattr(self.config, "raw_bytes", False),
            struct_definitions=getattr(parsed_file, "struct_definitions", None),
        )

        # --- 6. Precondition assumptions ---
        param_names = [pname for _, pname in sig.parameters if pname]
        assume_stmts = precond_to_assume(caller_spec.precondition, param_names)

        # --- 7. Call arguments ---
        # Filter lone "void" params — `f(void)` means no params in C.
        real_sig_params = [
            (pt, pn) for pt, pn in sig.parameters
            if not (pt.strip() == "void" and not pn.strip())
        ]
        call_args = ", ".join(
            (pname if pname else "_") for _, pname in real_sig_params
        )
        ret_type = sig.return_type.strip()

        # --- 8. Assemble the harness ---
        sections: list[str] = []

        sections.append(
            f"/* Reachability harness: can '{fn_name}' produce state\n"
            f"   {counterexample.variable_assignments}\n"
            f"   at call to '{callee_name}'? */\n"
            f"/* Generated by AMC Phase 3                            */"
        )

        _stdlib_fns2 = {"malloc", "free", "calloc", "realloc", "abort", "exit"}
        _stdio_fns2  = {"printf", "fprintf", "sprintf", "snprintf", "puts", "putchar"}
        _string_fns2 = {"memcpy", "memset", "memmove", "memcmp", "strlen", "strcpy", "strcmp"}
        _def2 = set(parsed_file.functions.keys())
        inc2 = ["#include <assert.h>"]
        if not (_def2 & _stdlib_fns2):
            inc2.append("#include <stdlib.h>")
        if not (_def2 & _stdio_fns2):
            inc2.append("#include <stdio.h>")
        if not (_def2 & _string_fns2):
            inc2.append("#include <string.h>")
        inc2 += ["#include <stddef.h>", "#include <stdint.h>"]
        sections.append("\n".join(inc2))

        if type_decls.strip():
            sections.append(
                "/* --- Type declarations from source file --- */\n"
                + type_decls
            )

        if stub_sections:
            sections.append("/* --- Callee stubs (with reachability stub for target) --- */")
            sections.extend(stub_sections)

        sections.append(
            f"/* --- Caller function under analysis: {fn_name} --- */\n"
            + func_def
        )

        # Harness main
        harness_body_lines: list[str] = []
        harness_body_lines.append("    /* Step 1: nondeterministic inputs for caller */")
        harness_body_lines.extend(nd_decls)
        harness_body_lines.append("")
        harness_body_lines.append("    /* Step 2: assume caller's precondition */")
        for stmt in assume_stmts:
            for sub_line in stmt.splitlines():
                harness_body_lines.append(f"    {sub_line}")
        harness_body_lines.append("")
        harness_body_lines.append(
            f"    /* Step 3: call {fn_name} — reachability stub constrains state */"
        )
        if ret_type == "void":
            harness_body_lines.append(f"    {fn_name}({call_args});")
        else:
            harness_body_lines.append(f"    {ret_type} _caller_result = {fn_name}({call_args});")
            harness_body_lines.append(f"    (void)_caller_result;")
        harness_body_lines.append("")
        harness_body_lines.append(
            "    /* Step 4: reachability verdict is determined by assert(0) inside\n"
            "       the reachability stub — if CBMC finds a CEx there, the callee\n"
            "       state is reachable from this caller. */"
        )

        harness_main = (
            "void main(void) {\n"
            + "\n".join(harness_body_lines)
            + "\n}"
        )
        sections.append(
            f"/* --- Reachability harness entry point --- */\n"
            + harness_main
        )

        return "\n\n".join(sections) + "\n"

    def _generate_reachability_stub(
        self,
        callee_name: str,
        counterexample: "Counterexample",
        parsed_file: ParsedCFile,
        override_sig: Optional["FunctionSignature"] = None,
    ) -> str:
        """
        Generate a stub for ``callee_name`` that uses ``__CPROVER_assume`` to
        constrain its arguments to match the counterexample state.

        ``override_sig`` is used when the callee is defined in a different file
        (cross-file case) and its signature is not in ``parsed_file``.
        """
        sig = parsed_file.functions.get(callee_name) or override_sig
        if sig is None:
            # Last resort: emit a minimal int-returning stub with assert(0).
            return (
                f"/* Reachability stub for external callee: {callee_name} */\n"
                f"int {callee_name}_stub(void) {{\n"
                f"    assert(0); /* reachability witness */\n"
                f"    return 0;\n"
                f"}}"
            )

        ret_type = sig.return_type.strip()
        params = sig.parameters
        params_str = _params_str(params)

        stub_name = f"{callee_name}_stub"

        lines: list[str] = [
            f"/* Reachability stub for: {callee_name} */",
            f"/* Constrains arguments to match counterexample state */",
            f"{ret_type} {stub_name}({params_str}) {{",
        ]

        # Emit __CPROVER_assume for each relevant variable in the counterexample.
        # Only emit assumes for variables that are actual C identifiers accessible
        # in the stub context (i.e. parameters or their struct fields).
        # Skip CBMC-internal variables (__CPROVER_*, _name*, name$N).
        lines.append("    /* Counterexample state constraints */")
        param_names = {pname for _, pname in params if pname}
        for var_name, var_value in counterexample.variable_assignments.items():
            clean_var = var_name.strip()
            # CBMC sometimes reports values as Python bools (for boolean
            # variables, overflow flags, etc.); coerce so .strip() doesn't
            # crash the whole reachability harness.
            clean_val = str(var_value).strip() if var_value is not None else ""

            # --- Filter out CBMC-internal variable names ---
            # 1. CBMC builtins: __CPROVER_*
            if clean_var.startswith("__CPROVER_"):
                lines.append(f"    /* cex (cbmc-internal): {clean_var} = {clean_val} */")
                continue
            # 2. CBMC-internal allocation names: _varname (underscore + varname)
            if clean_var.startswith("_"):
                lines.append(f"    /* cex (cbmc-internal): {clean_var} = {clean_val} */")
                continue
            # 3. CBMC SSA / object-validity variables: contain '$'
            if "$" in clean_var:
                lines.append(f"    /* cex (cbmc-internal): {clean_var} = {clean_val} */")
                continue
            # 4. Must reference a known parameter (directly or via -> / .)
            base_name = clean_var.split("->")[0].split(".")[0]
            if base_name not in param_names:
                lines.append(f"    /* cex (not a param): {clean_var} = {clean_val} */")
                continue

            # Only emit assumes for simple numeric/NULL values
            if _is_simple_value(clean_val):
                lines.append(
                    f"    __CPROVER_assume({clean_var} == {clean_val}); "
                    f"/* cex: {clean_var} = {clean_val} */"
                )
            else:
                lines.append(
                    f"    /* cex: {clean_var} = {clean_val} (complex — skipped) */"
                )

        # assert(0) fires iff the __CPROVER_assume constraints above were
        # satisfiable and this stub was actually called.  CBMC reports a CEx
        # iff such a path exists, confirming the callee state is reachable.
        lines.append("    assert(0); /* reachability witness */")

        if ret_type == "void":
            lines.append("    /* void return */")
        else:
            lines.append(f"    {ret_type} result;")
            lines.append("    return result;")

        lines.append("}")
        return "\n".join(lines)

    def generate_feasibility_harness(
        self,
        func: "FunctionInfo",
        spec: Spec,
        counterexample: "Counterexample",
        parsed_file: ParsedCFile,
        all_specs: Optional[dict] = None,
    ) -> str:
        """
        Generate a CBMC harness for CEx feasibility checking (Phase 3 Stage 2).

        Strategy (tiered):
        1. Fix scalar inputs to CEx witness values — eliminates input-space search,
           making inlining tractable.
        2. Inline local callee bodies (available in parsed_file) — real joint
           execution, no stub approximation for in-source callees.
        3. Use postcondition-constrained stubs for external/hardware callees.

        If CBMC finds a violation: CEx is feasible under real callee bodies.
        If CBMC verifies: CEx relied on callee behaviour not achievable in real code.
        """
        fn_name = func.name
        sig = func.signature

        source_text = (
            parsed_file.preprocessed_source
            if parsed_file.preprocessed_source is not None
            else _read_source(func.source_file)
        )
        type_decls = _extract_type_declarations(source_text, parsed_file)

        # --- 1. Transitive local call closure ---
        # All functions reachable from func that are defined in parsed_file.
        local_closure = self._local_call_closure(fn_name, func, parsed_file)

        # --- 2. External callees (need stubs) ---
        all_local = local_closure | {fn_name}
        external_callees: set[str] = set()
        for name in all_local:
            for callee in parsed_file.call_graph.get(name, set()):
                if callee not in all_local:
                    external_callees.add(callee)

        # --- 3. Build stubs for external callees ---
        stub_sections: list[str] = []
        for cname in sorted(external_callees):
            callee_spec = (all_specs or {}).get(cname)
            stub_src = _generate_stub(cname, callee_spec, parsed_file)
            stub_sections.append(stub_src)

        # --- 4. Build real function definitions for local closure ---
        # Substitute only external callee calls (local callees are real).
        local_func_defs: list[str] = []
        for cname in sorted(local_closure):
            cfi = parsed_file.get_function_info(cname)
            if cfi is None:
                continue
            cbody = _substitute_callee_calls(cfi.body, external_callees)
            cparams = _params_str(cfi.signature.parameters)
            local_func_defs.append(
                f"/* inlined local callee: {cname} */\n"
                f"{cfi.signature.return_type} {cname}({cparams})\n{cbody}"
            )

        # --- 5. Build func definition (substitute only external callee calls) ---
        func_body = _substitute_callee_calls(func.body, external_callees)
        params_str = _params_str(sig.parameters)
        func_def = f"{sig.return_type} {fn_name}({params_str})\n{func_body}"

        # --- 6. Build fixed-input declarations from CEx witness values ---
        fixed_decls: list[str] = []
        nondet_decls: list[str] = []
        real_params = [
            (pt, pn) for pt, pn in sig.parameters
            if not (pt.strip() == "void" and not pn.strip())
        ]
        for ptype, pname in real_params:
            if not pname:
                continue
            ptype_s = ptype.strip()
            is_pointer = "*" in ptype_s
            witness = counterexample.variable_assignments.get(pname, "")
            if not is_pointer and witness and _is_simple_value(witness):
                fixed_decls.append(f"    {ptype_s} {pname} = {witness};  /* CEx witness */")
            elif is_pointer:
                # Pointer: allocate a local value on the stack (conservative)
                base_type = ptype_s.rstrip("*").strip()
                if base_type.lower() in ("void", "const void"):
                    nondet_decls.append(f"    {ptype_s} {pname} = NULL;")
                else:
                    local_name = f"_{pname}_val"
                    nondet_decls.append(f"    {base_type} {local_name};")
                    nondet_decls.append(f"    {ptype_s} {pname} = &{local_name};")
            else:
                # Scalar without witness: uninitialized → nondet in CBMC
                nondet_decls.append(f"    {ptype_s} {pname};")

        # --- 7. Postcondition assertions ---
        param_names = [pn for _, pn in real_params if pn]
        assert_stmts = postcond_to_assert(spec.postcondition, param_names)
        ret_type = sig.return_type.strip()
        call_args = ", ".join(pn for _, pn in real_params if pn)

        # --- 8. Assemble ---
        sections: list[str] = []
        sections.append(
            f"/* Feasibility harness for '{fn_name}' — real callee bodies, fixed inputs */\n"
            f"/* Generated by AMC Phase 3 Stage 2 */"
        )

        _stdlib_fns = {"malloc", "free", "calloc", "realloc", "abort", "exit"}
        _stdio_fns  = {"printf", "fprintf", "sprintf", "snprintf", "puts", "putchar"}
        _string_fns = {"memcpy", "memset", "memmove", "memcmp", "strlen", "strcpy", "strcmp"}
        _def = set(parsed_file.functions.keys())
        inc_lines = ["#include <assert.h>"]
        if not (_def & _stdlib_fns):
            inc_lines.append("#include <stdlib.h>")
        if not (_def & _stdio_fns):
            inc_lines.append("#include <stdio.h>")
        if not (_def & _string_fns):
            inc_lines.append("#include <string.h>")
        inc_lines += ["#include <stddef.h>", "#include <stdint.h>"]
        sections.append("\n".join(inc_lines))

        if type_decls.strip():
            sections.append("/* --- Type declarations --- */\n" + type_decls)

        if stub_sections:
            sections.append("/* --- Stubs for external/hardware callees --- */")
            sections.extend(stub_sections)

        if local_func_defs:
            sections.append("/* --- Inlined local callees (real implementations) --- */")
            sections.extend(local_func_defs)

        sections.append(f"/* --- Function under test: {fn_name} --- */\n" + func_def)

        harness_lines: list[str] = []
        harness_lines.append("    /* Fixed inputs from CEx witness */")
        harness_lines.extend(fixed_decls)
        if nondet_decls:
            harness_lines.append("    /* Remaining inputs (no witness value) */")
            harness_lines.extend(nondet_decls)
        harness_lines.append(f"    /* Call {fn_name} with real callee bodies */")
        if ret_type == "void":
            harness_lines.append(f"    {fn_name}({call_args});")
        else:
            harness_lines.append(f"    {ret_type} _result = {fn_name}({call_args});")
            harness_lines.append("    (void)_result;")
            if assert_stmts:
                harness_lines.append("    /* Postcondition — check violation still fires */")
                for stmt in assert_stmts:
                    harness_lines.append(f"    {stmt}")

        sections.append(
            "void main(void) {\n" + "\n".join(harness_lines) + "\n}"
        )

        return "\n\n".join(sections) + "\n"

    def generate_dynamic_harness(
        self,
        entry_func: "FunctionInfo",
        counterexample: "Counterexample",
        parsed_file: ParsedCFile,
        all_funcs: Optional[dict] = None,
        all_specs: Optional[dict] = None,
        with_globals: bool = True,
    ) -> str:
        """
        Generate a GCC-compilable dynamic validation harness (Phase 3 Stage 3).

        The harness includes the function's call closure, signal handlers that catch
        SIGSEGV/SIGABRT/SIGFPE/SIGILL, optionally sets global state from CEx witness
        values, and calls ``entry_func`` with concrete CEx witness inputs.

        Output conventions (stdout):
          ``DYNAMIC:CONFIRMED signal=<NAME>``  — fault caught
          ``DYNAMIC:NOT_TRIGGERED``             — no fault within timeout
        """
        fn_name = entry_func.name
        sig = entry_func.signature

        source_text = (
            parsed_file.preprocessed_source
            if parsed_file.preprocessed_source is not None
            else _read_source(entry_func.source_file)
        )
        type_decls = _extract_type_declarations(source_text, parsed_file)

        # --- 1. Transitive local call closure ---
        local_closure = self._local_call_closure(fn_name, entry_func, parsed_file)

        # --- 2. External callees (need stubs) ---
        all_local = local_closure | {fn_name}
        external_callees: set[str] = set()
        for name in all_local:
            for callee in parsed_file.call_graph.get(name, set()):
                if callee not in all_local:
                    external_callees.add(callee)

        # --- 3. Build runtime-safe stubs for external callees ---
        stub_sections: list[str] = []
        for cname in sorted(external_callees):
            stub_sections.append(_generate_dynamic_stub(cname, parsed_file))

        # --- 4. Build real function definitions for local closure ---
        local_func_defs: list[str] = []
        for cname in sorted(local_closure):
            cfi = parsed_file.get_function_info(cname)
            if cfi is None:
                continue
            cbody = _substitute_callee_calls(cfi.body, external_callees)
            cparams = _params_str(cfi.signature.parameters)
            # Strip 'static' so the function is accessible from main()
            ret_local = re.sub(r'\bstatic\b\s*', '', cfi.signature.return_type).strip()
            local_func_defs.append(
                f"/* local callee: {cname} */\n"
                f"{ret_local} {cname}({cparams})\n{cbody}"
            )

        # --- 5. Build entry function definition ---
        func_body = _substitute_callee_calls(entry_func.body, external_callees)
        params_str = _params_str(sig.parameters)
        ret_entry = re.sub(r'\bstatic\b\s*', '', sig.return_type).strip()
        func_def = f"{ret_entry} {fn_name}({params_str})\n{func_body}"

        # --- 6. Identify global variable assignments from CEx witness ---
        entry_param_names: set[str] = {pname for _, pname in sig.parameters if pname}
        global_assigns: list[str] = []
        if with_globals:
            for var_name, var_value in counterexample.variable_assignments.items():
                clean_var = var_name.strip()
                clean_val = var_value.strip()
                if (clean_var.startswith("__CPROVER_") or clean_var.startswith("_")
                        or "$" in clean_var
                        or not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', clean_var)
                        or clean_var in entry_param_names):
                    continue
                if _is_simple_value(clean_val):
                    global_assigns.append(
                        f"    {clean_var} = {clean_val};  /* witness */"
                    )

        # --- 7. Build entry function call argument setup ---
        call_arg_lines: list[str] = []
        call_args_list: list[str] = []
        real_params = [
            (pt, pn) for pt, pn in sig.parameters
            if not (pt.strip() == "void" and not pn.strip())
        ]
        for ptype, pname in real_params:
            if not pname:
                continue
            ptype_s = ptype.strip()
            is_pointer = "*" in ptype_s
            witness = counterexample.variable_assignments.get(pname, "")
            arg_var = f"_amc_arg_{pname}"

            if not is_pointer and witness and _is_simple_value(witness):
                call_arg_lines.append(
                    f"    {ptype_s} {arg_var} = {witness};  /* witness */"
                )
            elif is_pointer:
                if witness.strip() in ("NULL", "0", ""):
                    call_arg_lines.append(
                        f"    {ptype_s} {arg_var} = NULL;  /* witness */"
                    )
                else:
                    base_type = re.sub(r'\bconst\b', '', ptype_s.rstrip("*")).strip()
                    if base_type.lower() in ("void",):
                        call_arg_lines.append(f"    {ptype_s} {arg_var} = NULL;")
                    else:
                        buf_var = f"_amc_buf_{pname}"
                        call_arg_lines.append(f"    {base_type} {buf_var};")
                        call_arg_lines.append(
                            f"    memset(&{buf_var}, 0, sizeof({buf_var}));"
                        )
                        call_arg_lines.append(f"    {ptype_s} {arg_var} = &{buf_var};")
            else:
                call_arg_lines.append(f"    {ptype_s} {arg_var} = 0;")

            call_args_list.append(arg_var)

        call_expr_args = ", ".join(call_args_list)

        # --- 8. Strip inline ASM and bare-metal header stubs ---
        # Bare-metal sources (e.g. VibeOS) contain ARM64 asm blocks and expanded
        # libc stubs (signal(), setjmp(), ...) that won't compile on x86.
        type_decls = _strip_stdlib_decls(
            _strip_glibc_internal_typedefs(
                _strip_static_inline_defs(
                    _strip_inline_asm(_strip_gcc_addr_space_quals(type_decls))
                )
            )
        )
        func_def   = _strip_inline_asm(func_def)
        local_func_defs = [_strip_inline_asm(d) for d in local_func_defs]

        # --- 9. Assemble the harness ---
        sections: list[str] = []

        # System headers come first so their types take priority.
        # We drop setjmp.h: signal handling uses _Exit() instead of siglongjmp
        # so there is no jmp_buf type conflict with bare-metal setjmp stubs.
        sections.append(
            "/* AMC Dynamic Validation Harness — Phase 3 Stage 3 */\n"
            "#include <signal.h>\n"
            "#include <stdio.h>\n"
            "#include <string.h>\n"
            "#include <stdlib.h>\n"
            "#include <stddef.h>\n"
            "#include <stdint.h>"
        )

        if type_decls.strip():
            sections.append(
                "/* --- Type declarations and globals from source --- */\n"
                + type_decls
            )

        if stub_sections:
            sections.append("/* --- Dynamic stubs for external callees --- */")
            sections.extend(stub_sections)

        if local_func_defs:
            sections.append("/* --- Local callee implementations --- */")
            sections.extend(local_func_defs)

        sections.append(f"/* --- Entry function: {fn_name} --- */\n" + func_def)

        # Signal handler: print confirmation and exit immediately.
        # Using _Exit() avoids atexit handlers and is async-signal-safe enough
        # for testing.  We also use numeric signal values (11/6/8/4) so the
        # handler compiles even when the preprocessed source has already
        # re-defined SIGSEGV etc. as different constants.
        sections.append(
            "/* AMC signal handler */\n"
            "static volatile const char *_amc_signal_name = \"UNKNOWN\";\n"
            "static void _amc_handler(int sig) {\n"
            "    if (sig == 11) _amc_signal_name = \"SIGSEGV\";\n"
            "    else if (sig == 6)  _amc_signal_name = \"SIGABRT\";\n"
            "    else if (sig == 8)  _amc_signal_name = \"SIGFPE\";\n"
            "    else if (sig == 4)  _amc_signal_name = \"SIGILL\";\n"
            "    printf(\"DYNAMIC:CONFIRMED signal=%s\\n\","
            " (const char *)_amc_signal_name);\n"
            "    fflush(stdout);\n"
            "    _Exit(1);\n"
            "}"
        )

        # Global state setup
        if global_assigns:
            sections.append(
                "static void _amc_setup_state(void) {\n"
                + "\n".join(global_assigns) + "\n"
                "}"
            )
        else:
            sections.append(
                "static void _amc_setup_state(void) { /* no global state to set */ }"
            )

        # main() — register handlers (best-effort; may be no-ops in bare-metal
        # environments), call the function, report result.
        # If the function crashes and signal() is a no-op, the process is killed
        # by the OS signal and dynamic_validator._run() detects the negative
        # exit code.
        main_lines: list[str] = [
            "    signal(11, _amc_handler);  /* SIGSEGV */",
            "    signal(6,  _amc_handler);  /* SIGABRT */",
            "    signal(8,  _amc_handler);  /* SIGFPE  */",
            "    signal(4,  _amc_handler);  /* SIGILL  */",
            "    _amc_setup_state();",
        ]
        main_lines.extend(call_arg_lines)
        if ret_entry == "void":
            main_lines.append(f"    {fn_name}({call_expr_args});")
        else:
            main_lines.append(
                f"    {ret_entry} _amc_result = {fn_name}({call_expr_args});"
            )
            main_lines.append("    (void)_amc_result;")
        main_lines.append('    puts("DYNAMIC:NOT_TRIGGERED");')
        main_lines.append("    return 0;")

        main_func = "int main(void) {\n" + "\n".join(main_lines) + "\n}"
        sections.append("/* --- Dynamic harness entry point --- */\n" + main_func)

        return "\n\n".join(sections) + "\n"

    def _local_call_closure(
        self,
        fn_name: str,
        func: "FunctionInfo",
        parsed_file: ParsedCFile,
    ) -> set[str]:
        """Return all local function names transitively reachable from fn_name."""
        visited: set[str] = set()
        queue = [fn_name]
        while queue:
            name = queue.pop()
            if name in visited:
                continue
            visited.add(name)
            for callee in parsed_file.call_graph.get(name, set()):
                if callee in parsed_file.functions and callee not in visited:
                    queue.append(callee)
        visited.discard(fn_name)
        return visited

    def generate_harness(
        self,
        func: FunctionInfo,
        spec: Spec,
        parsed_file: ParsedCFile,
        extern_sigs: Optional[dict] = None,
        all_funcs: Optional[dict] = None,
    ) -> str:
        """
        Generate a CBMC harness for *func* against *spec*.

        Parameters
        ----------
        extern_sigs:
            Optional mapping of function-name → FunctionSignature for
            functions defined in *other* source files (multi-file mode).
            Used to generate proper stubs for cross-file callees.
        all_funcs:
            Optional mapping of function-name → FunctionInfo for all
            functions in the current file.  Used to infer which pointer
            parameters are never passed NULL at any call site so that
            __CPROVER_assume constraints can be added to the harness.

        Returns the harness as a C source string.
        """
        # Real-libc mode: emit a minimal harness that #includes the
        # source .c file directly so CBMC handles all preprocessing,
        # rather than the default expand-then-strip pipeline that
        # struggles with glibc/gcc internals.  See _generate_real_libc.
        if getattr(self.config, "cbmc_real_libc", False):
            return self._generate_real_libc(func, spec, parsed_file, all_funcs)

        fn_name = func.name
        sig = func.signature

        # --- 1. Collect type declarations from the source ---
        # Prefer preprocessed_source (all includes already expanded) over
        # reading the original file (which may have unresolved #include "...").
        source_text = (
            parsed_file.preprocessed_source
            if parsed_file.preprocessed_source is not None
            else _read_source(func.source_file)
        )
        type_decls = _extract_type_declarations(source_text, parsed_file)

        # When the preprocessed source pulls in glibc headers (e.g.
        # /usr/include/stdio.h via a real include path) it brings the
        # full glibc-internal type machinery — __fpos_t, __mbstate_t,
        # __off_t, struct _G_fpos_t, etc.  Our harness then `#include
        # <stdio.h>` again on top, and CBMC errors on the duplicate
        # struct/typedef definitions.  Strip the glibc-internal types
        # and POSIX duplicates before emitting (mirrors the dynamic
        # validation harness, which has done this since the VibeOS
        # work; the CBMC harness path was missed).
        #
        # Kernel TUs (preprocessed source present) don't get libc
        # headers prepended (see "Standard includes" section below),
        # so the kernel's ``typedef __kernel_size_t size_t;`` etc.
        # must survive — pass ``keep_system_typedefs=True`` to the
        # stripper. For non-preprocessed input the default is
        # preserved (strip system typedefs; libc headers fill them in).
        _preprocessed = parsed_file.preprocessed_source is not None
        # In kernel mode (preprocessed TU, no libc prepend) we skip the
        # static-inline strip. Its purpose was to remove VibeOS-style
        # inline libc stubs (``signal()``, ``setjmp()``, …) that conflict
        # with the prepended system headers. With no libc prepend, no
        # conflict — and kernel headers are full of complex
        # macro-expanded static inlines (``READ_ONCE``, ``GENMASK``,
        # ``__compiletime_assert``) whose brace structure trips the
        # naive scanner; stripping them leaves orphan body fragments
        # ("syntax error before ')'", "syntax error after enum end").
        _intermediate = _strip_inline_asm(
            _strip_static_assert(
                _rewrite_auto_type(_strip_gcc_addr_space_quals(type_decls))
            )
        )
        if not _preprocessed:
            _intermediate = _strip_static_inline_defs(_intermediate)
        type_decls = _strip_stdlib_decls(
            _strip_glibc_internal_typedefs(_intermediate, kernel_mode=_preprocessed)
        )

        # --- 2. Identify callees to stub ---
        # "local" callees: defined in this parsed file
        local_callees = func.callees & set(parsed_file.functions.keys())
        # "extern" callees: not in this file but known from other parsed files
        extern_callees = set()
        if extern_sigs:
            extern_callees = (func.callees - local_callees) & set(extern_sigs.keys())

        # --- 2a. Partition local callees: inline-eligible vs stub ---
        # Eligible callees are small, pure, file-local helpers (predicates /
        # getters / accessors); inlining their real bodies lets CBMC verify
        # against the truth rather than an LLM-generated contract, which
        # kills the "stub disconnect" FP class on jv_get_kind, xmlIsBlank_ch,
        # BUF_ERROR, …. Static eligibility — no LLM in this loop.
        inline_local_callees: set[str] = set()
        if getattr(self.config, "inline_pure_callees", True):
            max_loc = int(getattr(self.config, "inline_pure_callees_max_loc", 30))
            for cname in sorted(local_callees):
                ok, _reason = _should_inline_callee(cname, parsed_file, max_loc=max_loc)
                if ok:
                    inline_local_callees.add(cname)
        stubbed_local_callees = local_callees - inline_local_callees
        all_stub_callees = stubbed_local_callees | extern_callees

        # --- 3. Generate stubs for each callee that wasn't inlined ---
        stub_sections: list[str] = []
        for callee_name in sorted(all_stub_callees):
            callee_spec = spec.callee_specs.get(callee_name)
            stub_src = _generate_stub(callee_name, callee_spec, parsed_file, extern_sigs)
            stub_sections.append(stub_src)

        # --- 3a. Emit inlined-callee bodies verbatim ---
        # The inlined callee may itself call into ``all_stub_callees``; rewrite
        # those nested calls so the inlined body compiles in the harness.
        inline_func_defs: list[str] = []
        for cname in sorted(inline_local_callees):
            cfi = parsed_file.get_function_info(cname)
            if cfi is None:
                continue
            cbody = _substitute_callee_calls(cfi.body, all_stub_callees)
            cparams = _params_str(cfi.signature.parameters)
            inline_func_defs.append(
                f"/* Inlined real callee body: {cname} */\n"
                f"static {cfi.signature.return_type} {cname}({cparams})\n{cbody}"
            )

        defined_callees = all_stub_callees  # used below for substitution

        # --- 4. Build the function body with callee calls substituted ---
        # Inlined callees keep their original names — only stubbed ones get
        # rewritten to {name}_stub.
        body_with_stubs = _substitute_callee_calls(func.body, defined_callees)

        # Reconstruct the full function definition (with original signature)
        params_str = _params_str(sig.parameters)
        func_def = f"{sig.return_type} {fn_name}({params_str})\n{body_with_stubs}"

        # --- 5. Generate nondeterministic input declarations ---
        nonnull = _infer_nonnull_params(func, all_funcs, parsed_file)
        nd_decls = _generate_nd_decls(
            func,
            self.config.cbmc_unwind,
            nonnull_params=nonnull,
            precondition=spec.precondition,
            raw_bytes=getattr(self.config, "raw_bytes", False),
            struct_definitions=getattr(parsed_file, "struct_definitions", None),
        )

        # --- 6. Precondition assumptions ---
        param_names = [pname for _, pname in sig.parameters if pname]
        assume_stmts = precond_to_assume(spec.precondition, param_names)

        # --- 7. Function call and postcondition assertions ---
        # Filter out lone "void" params — `f(void)` means no params in C.
        real_params = [(pt, pn) for pt, pn in sig.parameters
                       if not (pt.strip() == "void" and not pn.strip())]
        call_args = ", ".join(
            (pname if pname else "_") for _, pname in real_params
        )
        ret_type = sig.return_type.strip()
        # Replace callee function names with their _stub variants so that the
        # postcondition assertion compiles (the original functions are not
        # defined in the harness, only their stubs are).
        postcond_for_assert = spec.postcondition
        for _callee in sorted(defined_callees):
            postcond_for_assert = re.sub(
                rf'\b{re.escape(_callee)}\s*\(',
                f'{_callee}_stub(',
                postcond_for_assert,
            )
        assert_stmts = postcond_to_assert(postcond_for_assert, param_names)

        # --- 8. Assemble the harness ---
        sections: list[str] = []

        # Header comment
        sections.append(
            f"/* Auto-generated CBMC harness for function: {fn_name} */\n"
            f"/* Generated by AMC Phase 2                            */"
        )

        # Standard includes — omit headers whose functions are redefined
        # in source, AND omit ALL system includes when the source has
        # already been preprocessed (preprocessed_source is set).  In
        # that case the expanded source carries glibc's full type
        # machinery (struct _IO_FILE, struct _G_fpos_t, …) and a second
        # `#include <stdio.h>` would clash with body redefinitions that
        # bare-struct stripping can't safely remove.  CBMC's
        # __CPROVER_assume/__CPROVER_assert intrinsics don't need any
        # of these headers themselves.
        preprocessed = parsed_file.preprocessed_source is not None
        if preprocessed:
            inc_lines: list[str] = []
        else:
            _stdlib_fns  = {"malloc", "free", "calloc", "realloc", "abort", "exit"}
            _stdio_fns   = {"printf", "fprintf", "sprintf", "snprintf", "puts", "putchar"}
            _string_fns  = {"memcpy", "memset", "memmove", "memcmp", "strlen", "strcpy", "strcmp"}
            _defined = set(parsed_file.functions.keys())
            inc_lines = ["#include <assert.h>"]
            if not (_defined & _stdlib_fns):
                inc_lines.append("#include <stdlib.h>")
            if not (_defined & _stdio_fns):
                inc_lines.append("#include <stdio.h>")
            if not (_defined & _string_fns):
                inc_lines.append("#include <string.h>")
            inc_lines += ["#include <stddef.h>", "#include <stdint.h>"]
        if inc_lines:
            sections.append("\n".join(inc_lines))

        # Type declarations extracted from source
        if type_decls.strip():
            sections.append(
                "/* --- Type declarations from source file --- */\n"
                + type_decls
            )

        # Callee stubs
        if stub_sections:
            sections.append("/* --- Callee stubs --- */")
            sections.extend(stub_sections)

        # Inlined real callee bodies (small file-local pure helpers).
        # Emitted before the function under test so the call sites resolve.
        if inline_func_defs:
            sections.append("/* --- Inlined real callee bodies --- */")
            sections.extend(inline_func_defs)

        # Original function (with stubs substituted for callee calls)
        sections.append(
            f"/* --- Function under test: {fn_name} --- */\n"
            + func_def
        )

        # Harness main
        harness_body_lines: list[str] = []

        # Step 1: declare nondeterministic inputs
        harness_body_lines.append("    /* Step 1: nondeterministic inputs */")
        harness_body_lines.extend(nd_decls)

        # Step 1.5: pre-init library globals (see _emit_library_init_assumptions docstring)
        lib_init_assumes = _emit_library_init_assumptions(parsed_file)
        if lib_init_assumes:
            harness_body_lines.append("")
            harness_body_lines.append("    /* Step 1.5: assume library init has run */")
            for s in lib_init_assumes:
                harness_body_lines.append(f"    {s}")

        # Step 1.6: project-wide invariants learned from prior realism
        # rejections (feedback loop arm (c)). Off unless enable_feedback_loop.
        proj_clauses = _emit_learned_clauses(self.config, fn_name, "project")
        if proj_clauses:
            harness_body_lines.append("")
            harness_body_lines.append("    /* Step 1.6: learned project invariants */")
            harness_body_lines.extend(f"    {s}" for s in proj_clauses)

        # Step 1.7: function-local invariants learned for THIS function
        # (feedback loop arm (b)).
        fn_clauses = _emit_learned_clauses(self.config, fn_name, "function")
        if fn_clauses:
            harness_body_lines.append("")
            harness_body_lines.append("    /* Step 1.7: learned function invariants */")
            harness_body_lines.extend(f"    {s}" for s in fn_clauses)

        # Step 1.8: precondition asserts mined from the source body.
        source_assume_stmts = _extract_source_precondition_asserts(
            func.body or "", param_names,
        )
        source_assume_stmts.extend(
            _extract_jv_kind_preconditions(func, param_names)
        )
        if source_assume_stmts:
            harness_body_lines.append("")
            harness_body_lines.append(
                "    /* Step 1.8: source-level assert() preconditions (auto-promoted) */"
            )
            for s in source_assume_stmts:
                harness_body_lines.append(f"    {s}")

        # Step 2: constrain by precondition
        harness_body_lines.append("")
        harness_body_lines.append(
            "    /* Step 2: assume precondition */"
        )
        for stmt in assume_stmts:
            for sub_line in stmt.splitlines():
                harness_body_lines.append(f"    {sub_line}")

        # Step 3: call the function under test
        harness_body_lines.append("")
        harness_body_lines.append("    /* Step 3: call the function under test */")
        if ret_type == "void":
            harness_body_lines.append(f"    {fn_name}({call_args});")
        else:
            harness_body_lines.append(f"    {ret_type} result = {fn_name}({call_args});")
            harness_body_lines.append(f"    (void)result;  /* suppress unused-variable warning */")

        # Step 4: assert postcondition
        harness_body_lines.append("")
        harness_body_lines.append(
            "    /* Step 4: assert postcondition */\n"
            "    /* (CBMC also checks OOB, null deref, overflow automatically) */"
        )
        for stmt in assert_stmts:
            for sub_line in stmt.splitlines():
                harness_body_lines.append(f"    {sub_line}")

        harness_main = (
            "void main(void) {\n"
            + "\n".join(harness_body_lines)
            + "\n}"
        )
        sections.append(
            f"/* --- Harness entry point --- */\n"
            + harness_main
        )

        return "\n\n".join(sections) + "\n"

    # ------------------------------------------------------------------
    # Real-libc harness mode
    # ------------------------------------------------------------------

    def _generate_real_libc(
        self,
        func: FunctionInfo,
        spec: Spec,
        parsed_file: ParsedCFile,
        all_funcs: Optional[dict],
    ) -> str:
        """Emit a minimal CBMC harness that defers all preprocessing.

        The harness ``#include``s the original source file directly, so
        every project header and system header it transitively pulls in
        gets parsed by CBMC's own preprocessor (with ``-I`` flags from
        ``config.include_dirs``).  This avoids the entire failure mode
        of "Python ``cc -E`` expands gcc/glibc internals → CBMC's
        frontend rejects ``__gnuc_va_list``, ``struct _IO_FILE``, …".

        Trade-offs vs. the default mode:
          * Callees are NOT stubbed — the real implementations from the
            included source are verified inline.  This is generally what
            you want for bounty / CVE work (no stub-induced false
            positives), but means BMC has to chew through more code.
          * The "function under test" body is not separately reconstructed;
            it comes from the ``#include``.
          * No glibc-internal stripping is needed because we never
            inline the preprocessor output.

        Required CLI:  ``--include-dir <project_src_dir>`` so CBMC can
        resolve project headers, plus ``--real-libc``.
        """
        fn_name = func.name
        sig = func.signature

        # Resolve the source path against any include_dirs the user passed
        # so the harness can write a clean `#include "<basename>"` and rely
        # on CBMC's -I.  Falls back to the absolute path if we can't find
        # the file under any -I root.
        src_path = Path(func.source_file)
        src_basename = src_path.name
        include_target = src_basename
        if not any(
            (Path(d) / src_basename).exists()
            for d in (getattr(self.config, "include_dirs", []) or [])
        ):
            # Fallback: use the absolute path so the include resolves even
            # without a matching -I dir.
            include_target = str(src_path)

        # Nondeterministic input declarations — reuse the existing helper.
        nonnull = _infer_nonnull_params(func, all_funcs, parsed_file)
        nd_decls = _generate_nd_decls(
            func,
            self.config.cbmc_unwind,
            nonnull_params=nonnull,
            precondition=spec.precondition,
            raw_bytes=getattr(self.config, "raw_bytes", False),
            struct_definitions=getattr(parsed_file, "struct_definitions", None),
        )

        # Precondition assume + postcondition assert via existing DSL.
        param_names = [pname for _, pname in sig.parameters if pname]
        assume_stmts = precond_to_assume(spec.precondition, param_names)
        assert_stmts = postcond_to_assert(spec.postcondition, param_names)

        # Filter out lone "void" params — `f(void)` means no params in C.
        real_params = [(pt, pn) for pt, pn in sig.parameters
                       if not (pt.strip() == "void" and not pn.strip())]
        call_args = ", ".join(
            (pname if pname else "_") for _, pname in real_params
        )
        ret_type = sig.return_type.strip()

        body_lines: list[str] = []
        body_lines.append("    /* Step 1: nondeterministic inputs */")
        body_lines.extend(nd_decls)

        # Step 1.5a: assume library-init globals are non-NULL.
        lib_init_assumes = _emit_library_init_assumptions(parsed_file)
        if lib_init_assumes:
            body_lines.append("    /* Step 1.5a: assume library init has run (NULL check) */")
            body_lines.extend(f"    {s}" for s in lib_init_assumes)

        # Step 1.5b: point library allocator globals at concrete stubs.
        # ``!= NULL`` alone doesn't satisfy CBMC's --pointer-check on
        # function-pointer calls; we need a real function body for the
        # solver to dispatch to. The stub itself constrains its return
        # the same way our built-in stub contracts do.
        fp_decls, fp_inits = _emit_library_init_function_pointer_overrides(parsed_file)
        if fp_inits:
            body_lines.append("    /* Step 1.5b: point library function-pointer globals at concrete stubs */")
            body_lines.extend(f"    {s}" for s in fp_inits)

        # Step 1.6: project-wide invariants learned from prior realism
        # rejections (feedback loop arm (c)). Off unless enable_feedback_loop.
        proj_clauses = _emit_learned_clauses(self.config, fn_name, "project")
        if proj_clauses:
            body_lines.append("    /* Step 1.6: learned project invariants */")
            body_lines.extend(f"    {s}" for s in proj_clauses)

        # Step 1.7: function-local invariants learned for THIS function
        # (feedback loop arm (b)).
        fn_clauses = _emit_learned_clauses(self.config, fn_name, "function")
        if fn_clauses:
            body_lines.append("    /* Step 1.7: learned function invariants */")
            body_lines.extend(f"    {s}" for s in fn_clauses)

        # Step 1.8: precondition asserts mined from the source body.
        # When the function starts with `assert(precondition)`, real
        # callers obey it; mirror that as __CPROVER_assume.
        source_assume_stmts = _extract_source_precondition_asserts(
            func.body or "", param_names,
        )
        source_assume_stmts.extend(
            _extract_jv_kind_preconditions(func, param_names)
        )
        if source_assume_stmts:
            body_lines.append(
                "    /* Step 1.8: source-level assert() preconditions (auto-promoted) */"
            )
            body_lines.extend(f"    {s}" for s in source_assume_stmts)

        if assume_stmts:
            body_lines.append("    /* Step 2: precondition assumptions */")
            body_lines.extend(f"    {s}" for s in assume_stmts)

        body_lines.append(f"    /* Step 3: call function under test */")
        if ret_type == "void":
            body_lines.append(f"    {fn_name}({call_args});")
            result_line_present = False
        else:
            body_lines.append(f"    {ret_type} result = {fn_name}({call_args});")
            result_line_present = True

        # Step 3.5: when the postcondition will dereference `result` (e.g.
        # ``result->doc == doc``) and the function returns a pointer, the
        # extern callees we don't have a stub contract for can return a
        # NON-NULL but invalid pointer. The deref then trips a spurious
        # `main.pointer_dereference.*` failure that no real caller would
        # ever hit because they NULL-check first. Mirror that real-caller
        # pattern: assume the returned pointer is either NULL or a
        # 1-byte-readable region. From feedback-loop arm-(a) TODO #1
        # on xmlXPtrNewContext (xpointer.c).
        if (
            result_line_present
            and "*" in ret_type
            and any("result->" in s for s in (assert_stmts or []))
        ):
            body_lines.append(
                "    /* Step 3.5: harness-safe NULL-check on returned pointer "
                "(prevents spurious main.pointer_dereference.* on extern returns) */"
            )
            body_lines.append(
                "    __CPROVER_assume(result == NULL || __CPROVER_r_ok(result, 1));"
            )

        if assert_stmts:
            body_lines.append(f"    /* Step 4: postcondition assertions */")
            body_lines.extend(f"    {s}" for s in assert_stmts)

        # Silence unused-variable warnings if the postcondition didn't
        # reference `result` (e.g. trivial postcondition).
        if result_line_present and not any(
            "result" in s for s in assert_stmts
        ):
            body_lines.append("    (void)result;")

        # Top-level stub-function declarations for library-init function
        # pointer overrides (Step 1.5b). Must precede main() and the
        # in-main assignments. Declared AFTER the #include so xmlMallocFunc
        # etc. typedefs are visible.
        stub_decl_section = ""
        if fp_decls:
            stub_decl_section = (
                "/* --- Library-init function-pointer stubs (Step 1.5b) --- */\n"
                + "\n".join(fp_decls)
                + "\n"
            )

        sections = [
            f"/* Auto-generated CBMC harness (real-libc mode) for: {fn_name} */",
            f"/* Source: {func.source_file} */",
            "",
            f'#include "{include_target}"',
            "",
            stub_decl_section,
            "int main(void) {",
            "\n".join(body_lines),
            "    return 0;",
            "}",
        ]
        return "\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# Helpers: dynamic stub (runtime-safe — no CBMC constructs)
# ---------------------------------------------------------------------------


def _generate_dynamic_stub(callee_name: str, parsed_file: "ParsedCFile") -> str:
    """Generate a runtime-safe stub for dynamic harnesses (no __CPROVER_assume)."""
    sig = parsed_file.functions.get(callee_name)
    if sig is None:
        return f"/* dynamic stub: {callee_name} — no signature, skipped */"

    ret_type = sig.return_type.strip()
    params_str = _params_str(sig.parameters)
    stub_name = f"{callee_name}_stub"

    lines = [
        f"/* Dynamic stub: {callee_name} */",
        f"{ret_type} {stub_name}({params_str}) {{",
    ]
    if ret_type == "void":
        lines.append("    /* void */")
    else:
        default = _c_default_value(ret_type)
        lines.append(f"    return ({ret_type}){default};")
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utility: read source file text
# ---------------------------------------------------------------------------


def _read_source(source_file: str) -> str:
    """Read a source file, returning empty string if not found."""
    try:
        return Path(source_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _strip_static_assert(text: str) -> str:
    """Strip ``_Static_assert(condition, "msg");`` calls.

    The Linux kernel embeds ``_Static_assert`` inside ``sizeof(struct{...})``
    constructs to perform bounds checks at compile time (e.g.
    ``GENMASK_INPUT_CHECK`` in linux/bits.h, ``BUILD_BUG_ON*`` family).
    These expressions reference function parameters (``size``, ``offset``,
    ``shift``) which are runtime values from CBMC's point of view —
    CBMC's parser then errors with ``expected constant expression, but
    got 'size + 18446744073709551615ul >= offset'``. Strip the assert
    entirely; for verification purposes the check would only catch
    misuse with literal constants anyway, which is orthogonal to model
    checking.

    Replace each match with a typed expression (``(int)0``) so the
    enclosing ``struct{...}`` member declaration remains syntactically
    valid (it expects an expression-statement in the field-decl list).
    """
    # Match ``_Static_assert(`` and consume to the matching ``)``;
    # then expect ``;`` after.  Track paren depth + comments/strings
    # so we don't trip on nested constructs.
    out: list[str] = []
    i = 0
    n = len(text)
    pat = re.compile(r'\b_Static_assert\s*\(')
    while i < n:
        m = pat.search(text, i)
        if m is None:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        j = m.end()  # position right after the opening '('
        depth = 1
        while j < n and depth > 0:
            ch = text[j]
            if ch == '/' and j + 1 < n and text[j + 1] == '*':
                end = text.find('*/', j + 2)
                j = n if end == -1 else end + 2
                continue
            if ch == '/' and j + 1 < n and text[j + 1] == '/':
                end = text.find('\n', j + 2)
                j = n if end == -1 else end
                continue
            if ch == '"':
                j += 1
                while j < n:
                    if text[j] == '\\' and j + 1 < n:
                        j += 2
                        continue
                    if text[j] == '"':
                        j += 1
                        break
                    j += 1
                continue
            if ch == "'":
                j += 1
                while j < n:
                    if text[j] == '\\' and j + 1 < n:
                        j += 2
                        continue
                    if text[j] == "'":
                        j += 1
                        break
                    j += 1
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        # Optionally consume the trailing ``;``
        while j < n and text[j] in ' \t':
            j += 1
        if j < n and text[j] == ';':
            j += 1
        # Replace with a trivially-valid ``_Static_assert(1, "")`` so
        # the construct is well-formed in all C11 contexts (TU-scope
        # declaration, function-body declaration, struct field-list).
        # Keeps the semantic shape but drops the runtime-dependent
        # condition CBMC was choking on.
        out.append('_Static_assert(1, "");')
        i = j
    return ''.join(out)


def _rewrite_auto_type(text: str) -> str:
    """Rewrite GCC's ``__auto_type`` declarations into ``typeof()``
    equivalents. CBMC's parser (5.95) doesn't recognise ``__auto_type``
    and errors out with ``syntax error before '__auto_type'``. The
    Linux kernel uses the keyword extensively in ``min``/``max``/
    ``clamp``-family macros (linux/minmax.h), so a kernel TU has
    hundreds of occurrences.

    The transform is purely textual:

        const __auto_type x = SOME_EXPR;
        → const typeof(SOME_EXPR) x = SOME_EXPR;

    The initializer is captured by scanning to the next ``;`` (or
    ``,`` — for multi-declarators) at paren/brace depth 0, skipping
    comments and string/char literals. The duplicated RHS evaluates
    twice in principle, but the kernel's ``__auto_type`` initializers
    are pure value-producing expressions (variables, simple casts,
    arithmetic), so the duplication is harmless for verification.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    pat = re.compile(r'\b__auto_type\b')
    while i < n:
        m = pat.search(text, i)
        if m is None:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        # After ``__auto_type``, expect optional whitespace + identifier
        # + optional whitespace + ``=`` + initializer + ``;`` (or ``,``).
        j = m.end()
        # Skip whitespace
        while j < n and text[j] in ' \t\n\r':
            j += 1
        # Identifier
        id_match = re.match(r'(\w+)', text[j:])
        if not id_match:
            # Not a recognisable declaration shape — leave as-is.
            out.append(text[m.start():j])
            i = j
            continue
        var_name = id_match.group(1)
        j += len(var_name)
        # Skip to ``=``
        eq_pos = text.find('=', j)
        # Sanity: ``=`` must come before the next ``;`` at depth 0
        if eq_pos == -1:
            out.append(text[m.start():j])
            i = j
            continue
        # Scan from after ``=`` to the next ``;`` or ``,`` at depth 0,
        # skipping comments and string/char literals.
        rhs_start = eq_pos + 1
        # Skip leading whitespace on RHS for cleanliness
        k = rhs_start
        while k < n and text[k] in ' \t':
            k += 1
        rhs_start = k
        depth_p = 0
        depth_b = 0
        while k < n:
            ch = text[k]
            # /* ... */ block comment
            if ch == '/' and k + 1 < n and text[k + 1] == '*':
                end = text.find('*/', k + 2)
                k = n if end == -1 else end + 2
                continue
            # // line comment
            if ch == '/' and k + 1 < n and text[k + 1] == '/':
                end = text.find('\n', k + 2)
                k = n if end == -1 else end
                continue
            if ch == '"':
                k += 1
                while k < n:
                    if text[k] == '\\' and k + 1 < n:
                        k += 2
                        continue
                    if text[k] == '"':
                        k += 1
                        break
                    k += 1
                continue
            if ch == "'":
                k += 1
                while k < n:
                    if text[k] == '\\' and k + 1 < n:
                        k += 2
                        continue
                    if text[k] == "'":
                        k += 1
                        break
                    k += 1
                continue
            if ch in '([':
                depth_p += 1
            elif ch in ')]':
                depth_p -= 1
            elif ch == '{':
                depth_b += 1
            elif ch == '}':
                depth_b -= 1
            elif depth_p == 0 and depth_b == 0 and ch in ';,':
                break
            k += 1
        rhs = text[rhs_start:k].strip()
        if not rhs:
            out.append(text[m.start():k])
            i = k
            continue
        # Emit ``typeof(RHS) VAR = RHS``, preserving the trailing
        # terminator (``;`` or ``,``) the caller's text had.
        out.append(f'typeof({rhs}) {var_name} = {rhs}')
        i = k
    return ''.join(out)


# GCC named address-space qualifiers used by the x86_64 Linux kernel to
# place data in the GS/FS segment (per-CPU areas). They're emitted as
# bare keywords surviving preprocessing (not macros), and CBMC's frontend
# doesn't understand them. They have no verification-relevant semantics
# in single-threaded model checking, so erase them entirely. Pattern is
# anchored on word boundaries so it doesn't accidentally match names
# like ``__seg_gs_var``.
_GCC_ADDR_SPACE_PAT = re.compile(r'\b__seg_(?:gs|fs)\b')


def _strip_gcc_addr_space_quals(text: str) -> str:
    """Erase GCC named-address-space qualifiers (``__seg_gs`` / ``__seg_fs``)
    surviving the cpp pass. They tag types with x86 segment registers
    used for per-CPU storage in the kernel; CBMC's frontend doesn't
    recognise them and emits ``syntax error before '__seg_gs'``. They
    have no verification value, so just remove the token."""
    return _GCC_ADDR_SPACE_PAT.sub("", text)


def _strip_inline_asm(text: str) -> str:
    """Remove asm/asm volatile/__asm__/etc. statements so the harness compiles on x86.

    Two shapes the kernel uses:

      * Statement form:   ``asm volatile ("nop");``  — the ``;`` is part of
        the asm statement itself and SHOULD be consumed.
      * Clause form:      ``register unsigned long sp asm("rsp");`` — the
        ``;`` belongs to the surrounding declaration (the asm() is a
        GCC asm-name clause on a register-storage variable). Consuming
        it leaves a declaration without a terminator and the next token
        (typically the following declaration) trips a syntax error.

    Distinguish by looking backward from the ``asm`` match: if we see a
    ``register`` token before reaching a statement boundary (``;`` / ``{``
    / ``}``) , we're in clause form and must NOT eat the trailing ``;``.
    """
    result: list[str] = []
    i = 0
    pat = re.compile(r'\b(asm|__asm__|__asm)\b')
    while i < len(text):
        m = pat.search(text, i)
        if m is None:
            result.append(text[i:])
            break
        result.append(text[i:m.start()])
        # Look backward for ``register`` in the current declaration so we
        # can decide whether the trailing ``;`` belongs to us. The
        # look-back is unbounded back to the previous statement
        # boundary — kernel macro expansions (e.g. ``__get_user`` family)
        # put the ``register`` keyword arbitrarily far from the ``asm``
        # clause, so an earlier 200-byte cap missed them and produced
        # malformed declarations.
        prev = text[:m.start()]
        last_term = max(prev.rfind(";"), prev.rfind("{"), prev.rfind("}"))
        in_decl_window = prev[last_term + 1:] if last_term >= 0 else prev
        is_register_clause = bool(re.search(r'\bregister\b', in_decl_window))
        j = m.end()
        # optional volatile qualifier
        vol = re.match(r'\s+(?:volatile|__volatile__)\b', text[j:])
        if vol:
            j += vol.end()
        # skip whitespace
        while j < len(text) and text[j] in ' \t\n\r':
            j += 1
        if j < len(text) and text[j] == '(':
            depth = 0
            while j < len(text):
                if text[j] == '(':
                    depth += 1
                elif text[j] == ')':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            while j < len(text) and text[j] in ' \t':
                j += 1
            if j < len(text) and text[j] == ';' and not is_register_clause:
                j += 1
            result.append('/* asm removed */')
            i = j
        else:
            result.append(text[m.start():j])
            i = j
    return ''.join(result)


def _strip_static_inline_defs(text: str) -> str:
    """
    Remove static inline function *definitions* from preprocessed type declarations.

    Bare-metal codebases (e.g. VibeOS) expand their own libc stubs (signal(),
    setjmp(), etc.) into the preprocessed source.  These conflict with the system
    headers we include in the dynamic harness.  Strip the definitions; forward
    declarations (ending in ';') are kept so callers still compile.
    """
    result: list[str] = []
    i = 0
    pat = re.compile(r'\bstatic\s+(?:inline|__inline__)\b')
    while i < len(text):
        m = pat.search(text, i)
        if m is None:
            result.append(text[i:])
            break
        j = m.end()
        # Scan forward for the first '{' or ';' at top brace depth.
        depth = 0
        found_brace = False
        while j < len(text):
            ch = text[j]
            if ch == '{':
                if depth == 0:
                    found_brace = True
                    break
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == ';' and depth == 0:
                break
            j += 1
        if found_brace:
            # Function definition — strip from 'static' to matching '}'
            result.append(text[i:m.start()])
            depth = 0
            while j < len(text):
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            result.append('/* static inline removed */')
            i = j
        else:
            # Declaration ending in ';' — keep it
            result.append(text[i:j + 1])
            i = j + 1
    return ''.join(result)


# Standard C / POSIX functions that kernel headers may re-declare with
# non-standard signatures (e.g. VibeOS printf.h uses int size for snprintf).
# Any forward declaration (no body) whose name is in this set is stripped from
# the preprocessed type_decls so our system #include directives win.
_SYSTEM_FUNCTION_NAMES: frozenset[str] = frozenset({
    # <stdio.h>
    "printf", "fprintf", "sprintf", "snprintf",
    "vprintf", "vfprintf", "vsprintf", "vsnprintf",
    "puts", "putchar", "putc", "getchar", "getc",
    "fopen", "fclose", "fread", "fwrite",
    "fgetc", "fputc", "fputs", "fgets",
    "fflush", "fseek", "ftell", "feof", "ferror",
    "rewind", "perror", "remove", "rename",
    "scanf", "fscanf", "sscanf",
    # <string.h>
    "memcpy", "memmove", "memset", "memcmp", "memchr",
    "strlen", "strcpy", "strncpy", "strcat", "strncat",
    "strcmp", "strncmp", "strchr", "strrchr",
    "strstr", "strtok", "strtok_r",
    "strcasecmp", "strncasecmp",
    # <stdlib.h>
    "malloc", "free", "calloc", "realloc",
    "abort", "exit", "_Exit",
    "atoi", "atol", "atoll",
    "strtol", "strtoul", "strtoll", "strtoull", "strtod",
    "rand", "srand", "abs", "labs", "llabs",
    "qsort", "bsearch", "atexit",
    # <signal.h>
    "signal", "raise", "kill", "sigaction",
    # <ctype.h>
    "isalpha", "isdigit", "isspace", "isalnum",
    "isupper", "islower", "toupper", "tolower",
    "isprint", "ispunct", "isxdigit",
    # <math.h>
    "sin", "cos", "tan", "sqrt", "fabs", "ceil", "floor", "pow",
    # <unistd.h>
    "read", "write", "close", "lseek",
})


# C-standard / POSIX types that bare-metal stubs redefine but system headers
# will provide.  Any typedef that defines one of these names is stripped from
# the preprocessed type_decls section of the dynamic harness so our explicit
# system #include directives win.
_SYSTEM_TYPEDEF_NAMES: frozenset[str] = frozenset({
    # C11 <stddef.h>
    "max_align_t", "size_t", "ptrdiff_t", "wchar_t",
    # C99 <wchar.h>
    "wint_t", "wctrans_t", "wctype_t",
    # C99 <stdint.h> — Linux's linux/types.h re-defines these (``typedef u8
    # uint8_t;`` etc.) and the chain references kernel primitives (``u8``).
    # The harness includes <stdint.h>, which provides authoritative
    # definitions — strip the kernel's variants so they don't conflict.
    "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int_least8_t", "int_least16_t", "int_least32_t", "int_least64_t",
    "uint_least8_t", "uint_least16_t", "uint_least32_t", "uint_least64_t",
    "int_fast8_t", "int_fast16_t", "int_fast32_t", "int_fast64_t",
    "uint_fast8_t", "uint_fast16_t", "uint_fast32_t", "uint_fast64_t",
    "intmax_t", "uintmax_t", "intptr_t", "uintptr_t",
    # BSD-style historical aliases that glibc <sys/types.h> provides; Linux
    # linux/types.h re-defines them (``typedef u8 u_int8_t;`` etc.). Same
    # rationale as the stdint block — strip the kernel variant.
    "u_int8_t", "u_int16_t", "u_int32_t", "u_int64_t",
    # POSIX <sys/types.h>
    "FILE", "fpos_t", "clock_t", "time_t",
    "pid_t", "uid_t", "gid_t", "mode_t", "nlink_t",
    "off_t", "ino_t", "dev_t", "blkcnt_t", "blksize_t",
    "rlim_t", "id_t", "suseconds_t", "useconds_t",
    "ssize_t", "socklen_t", "sa_family_t",
})


# Linux-kernel UAPI typedefs to preserve through the ``__``-prefix
# strip. Despite the leading ``__`` (the convention the generic strip
# rule uses to identify glibc internals like ``__fpos_t``), these are
# kernel-side primitives or POSIX-shape types defined by
# include/uapi/asm-generic/{int-ll64,posix_types,types}.h. Stripping
# any of them cascade-breaks the entire ``__u8 → u8 → uint8_t`` /
# ``__kernel_daddr_t → struct ustat`` chains and orphans every
# function signature referencing them.
#
# Three families:
#   1. Integer primitives (``__u8``, ``__s16``, ``__be32``, ``__sum16``)
#   2. POSIX/UAPI shape types (``__kernel_off_t``, ``__kernel_daddr_t``,
#      ``__kernel_pid_t``, …) — all defined under ``__kernel_<name>``
#   3. Misc kernel-specific (``__poll_t``, ``__bitwise``, …)
_KERNEL_PRIMITIVE_PAT = re.compile(
    r'^(?:'
    r'__[us](?:8|16|32|64|128)'         # __u8, __s16, __u128, ...
    r'|__[lb]e(?:16|32|64)'             # __le16, __be32, ...
    r'|__sum(?:16|32|64)'               # __sum16 (checksum types)
    r'|__wsum'
    r'|__kernel_\w+'                    # __kernel_off_t, __kernel_pid_t, …
    r'|__poll_t'
    r'|__bitwise'
    r'|__rwonce_type'
    r'|__nocast'
    r')$'
)


def _strip_glibc_internal_typedefs(text: str, *, kernel_mode: bool = False) -> str:
    """
    Remove typedef declarations that define names starting with '__' OR that
    define known C-standard / POSIX types (see _SYSTEM_TYPEDEF_NAMES).

    Preprocessed bare-metal sources (e.g. VibeOS) expand their own libc stub
    headers which redefine glibc-internal types (__fsid_t, __dev_t, …) and
    C-standard types (max_align_t, size_t, …).  When the dynamic harness then
    includes <signal.h>, <stdio.h>, etc., GCC sees conflicting redefinitions.
    Stripping these typedefs from the preprocessed section lets the system
    headers win without any conflict.

    Handles both simple and struct typedefs, including one-level nested braces:
        typedef unsigned long int __dev_t;
        typedef struct { int __val[2]; } __fsid_t;
        typedef union { long long __ll; long double __ld; } max_align_t;

    Also strips orphan typedefs whose BODY references a name that was
    already stripped earlier in this scan — e.g.
    ``typedef __gnuc_va_list va_list;`` after ``typedef __builtin_va_list
    __gnuc_va_list;`` is removed.  Without this cascade, the harness
    contains a typedef pointing at an undefined identifier and CBMC's
    frontend errors with ``syntax error before 'va_list'``.  Source-order
    scanning is sufficient because C typedefs may only reference names
    declared earlier in the same translation unit.

    When ``kernel_mode`` is True (CBMC harness path for preprocessed
    kernel TUs that don't prepend libc system headers), BOTH primary
    strip rules are suppressed:
      * ``_SYSTEM_TYPEDEF_NAMES`` — the kernel provides ``size_t``,
        ``ssize_t``, ``off_t``, … via ``typedef __kernel_size_t size_t;``
        chains. There is no ``<stddef.h>`` to fill them back in.
      * ``__``-prefix — the entire purpose of stripping ``__``-typedefs
        was to resolve duplicate-definition conflicts when ``<signal.h>``
        / ``<stdio.h>`` / etc. are prepended on top of preprocessed
        sources that already define glibc internals. With no libc
        prepend, no conflict. Kernel ``__name_t`` typedefs (which are
        legion: ``__sighandler_t``, ``__sigset_t``, ``__sigaction``,
        ``__kernel_*``, the various ``__u*/__s*`` primitives) all
        survive and reference each other consistently.

    The cascade strip is still active in both modes — it operates on
    whatever names actually got stripped, so it remains a no-op when
    nothing was stripped.
    """
    result: list[str] = []
    stripped_names: set[str] = set()
    i = 0
    pat = re.compile(r'\btypedef\b')
    while i < len(text):
        m = pat.search(text, i)
        if m is None:
            result.append(text[i:])
            break
        j = m.end()
        depth = 0
        while j < len(text):
            ch = text[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == ';' and depth == 0:
                break
            j += 1
        if j >= len(text):
            result.append(text[i:])
            break
        typedef_text = text[m.start():j + 1]
        name_m = re.search(r'\b(\w+)\s*;$', typedef_text)
        target = name_m.group(1) if name_m else None

        # Primary strip rule (libc-conflict mitigation only):
        #   * Name starts with ``__`` — glibc internal that conflicts
        #     with prepended ``<signal.h>`` / ``<stdio.h>`` etc.
        #   * OR name is in ``_SYSTEM_TYPEDEF_NAMES`` — C-standard /
        #     POSIX type that prepended ``<stddef.h>`` / ``<stdint.h>``
        #     / ``<sys/types.h>`` provides.
        # Kernel-primitive ``__``-typedefs (``__u8``/``__s8``/``__le16``/
        # ``__kernel_*``) are exempted via ``_KERNEL_PRIMITIVE_PAT``.
        # In ``kernel_mode`` (preprocessed kernel TU, no libc prepend),
        # BOTH branches are suppressed: no conflict means no need to
        # strip; and the kernel's own definitions are the only ones
        # that can fill in ``size_t``, ``__sighandler_t``, etc.
        strip = False
        reason = ""
        if not kernel_mode and target and (
            (target.startswith('__') and not _KERNEL_PRIMITIVE_PAT.match(target))
            or target in _SYSTEM_TYPEDEF_NAMES
        ):
            strip = True
            reason = "removed"

        # Cascading strip rule: typedef body references a *glibc-internal*
        # name that was already stripped (e.g. ``typedef __gnuc_va_list
        # va_list;`` after __gnuc_va_list is gone).  We restrict the
        # cascade to ``__``-prefixed referents because:
        #   * Those are glibc-internal types the system headers will NOT
        #     reintroduce, so a typedef referencing them really is orphaned.
        #   * C-standard types (size_t, ptrdiff_t, ...) DO get reintroduced
        #     by ``<stdint.h>``, ``<stddef.h>``, etc., so a user typedef
        #     like ``typedef struct { size_t size; ... } block_header_t;``
        #     remains valid even after our primary rule strips ``size_t``.
        # We exclude the target name itself from the match set so a typedef
        # that happens to reuse its target identifier in its body isn't
        # mis-classified.
        if not strip and target and stripped_names:
            body_tokens = set(re.findall(r'\b\w+\b', typedef_text))
            body_tokens.discard(target)
            referenced = body_tokens & stripped_names
            # Restrict cascade to glibc-internal references only.
            referenced_internal = {n for n in referenced if n.startswith('__')}
            if referenced_internal:
                strip = True
                reason = f"removed: references stripped {sorted(referenced_internal)[0]}"

        if strip and target:
            result.append(text[i:m.start()])
            result.append(f'/* typedef {target} {reason} */')
            stripped_names.add(target)
        else:
            result.append(text[i:j + 1])
        i = j + 1
    return ''.join(result)


def _strip_stdlib_decls(text: str) -> str:
    """
    Remove forward declarations (no body) for standard C/POSIX functions.

    Kernel headers (e.g. VibeOS printf.h) sometimes re-declare standard
    functions with non-standard signatures (e.g. ``int snprintf(char*, int, …)``
    instead of ``int snprintf(char*, size_t, …)``).  These conflict with the
    system ``<stdio.h>`` we include in the dynamic harness.  Strip any
    declaration — a statement ending in ``;`` at brace depth 0 that contains
    a ``(`` and whose function name is in _SYSTEM_FUNCTION_NAMES — from the
    preprocessed type_decls.

    The scanner is comment- and string-literal-aware: a ``;`` inside a
    ``/* … */`` block comment, a ``// …`` line comment, or a string/char
    literal does NOT count as a statement boundary.  Without this, source-
    file doc comments like ``Postcondition: returns n (<= len); 0 if empty``
    split the surrounding declaration text in the middle of a comment,
    which both (a) emits a stray ``/* foo decl removed */`` marker inside
    the comment, prematurely closing the outer ``*/``, and (b) lets the
    regex match a system-function name (``read``, ``write``, …) that only
    appears in commentary, corrupting unrelated declarations.
    """
    # Match function declarations at brace depth 0: lines/blocks ending in ';'
    # that look like "... funcname ( ... );"
    _DECL_PAT = re.compile(r'\b(\w+)\s*\(')
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # Find the next ';' at depth 0, skipping over comments and string/char literals
        j = i
        depth = 0
        while j < n:
            ch = text[j]
            # /* ... */ block comment
            if ch == '/' and j + 1 < n and text[j + 1] == '*':
                end = text.find('*/', j + 2)
                j = n if end == -1 else end + 2
                continue
            # // ... line comment (consume up to newline; do not consume the newline)
            if ch == '/' and j + 1 < n and text[j + 1] == '/':
                end = text.find('\n', j + 2)
                j = n if end == -1 else end
                continue
            # "..." string literal (handle escapes)
            if ch == '"':
                k = j + 1
                while k < n:
                    if text[k] == '\\' and k + 1 < n:
                        k += 2
                        continue
                    if text[k] == '"':
                        k += 1
                        break
                    k += 1
                j = k
                continue
            # '...' char literal (handle escapes)
            if ch == "'":
                k = j + 1
                while k < n:
                    if text[k] == '\\' and k + 1 < n:
                        k += 2
                        continue
                    if text[k] == "'":
                        k += 1
                        break
                    k += 1
                j = k
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == ';' and depth == 0:
                break
            j += 1
        if j >= n:
            result.append(text[i:])
            break
        stmt = text[i:j + 1]
        # Strip comments and string/char literals before checking for a
        # system-function declaration. A ``read(`` token in a doc comment
        # must not be treated as a declaration of ``read``.
        stmt_code = _strip_c_comments_and_strings(stmt)
        m = _DECL_PAT.search(stmt_code)
        if m and m.group(1) in _SYSTEM_FUNCTION_NAMES and '{' not in stmt_code:
            result.append(f'/* {m.group(1)} decl removed */')
        else:
            result.append(stmt)
        i = j + 1
    return ''.join(result)


def _strip_c_comments_and_strings(text: str) -> str:
    """Return *text* with /* ... */, // ..., and "..."/'...' contents
    replaced by whitespace of equal length. Preserves overall offsets and
    line counts so any downstream regex sees only the code portion.
    Used by _strip_stdlib_decls so a system-function name appearing only
    inside a comment doesn't get matched as a declaration.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            stop = n if end == -1 else end + 2
            # Replace with spaces, keeping newlines so line counts match.
            chunk = text[i:stop]
            out.append(''.join('\n' if c == '\n' else ' ' for c in chunk))
            i = stop
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            end = text.find('\n', i + 2)
            stop = n if end == -1 else end
            out.append(' ' * (stop - i))
            i = stop
            continue
        if ch in ('"', "'"):
            quote = ch
            k = i + 1
            while k < n:
                if text[k] == '\\' and k + 1 < n:
                    k += 2
                    continue
                if text[k] == quote:
                    k += 1
                    break
                k += 1
            chunk = text[i:k]
            out.append(''.join('\n' if c == '\n' else ' ' for c in chunk))
            i = k
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _is_simple_value(val: str) -> bool:
    """Return True if *val* is a simple numeric or NULL literal usable in __CPROVER_assume."""
    val = val.strip()
    if val in ("NULL", "true", "false"):
        return True
    # Integers (possibly negative)
    try:
        int(val)
        return True
    except ValueError:
        pass
    # Hex
    try:
        int(val, 16)
        return True
    except ValueError:
        pass
    # Simple float
    try:
        float(val)
        return True
    except ValueError:
        pass
    return False
