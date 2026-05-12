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
    """
    if parsed_file is not None and parsed_file.function_bodies:
        return _extract_type_decls_using_bodies(source_text, parsed_file)
    return _extract_type_decls_heuristic(source_text)


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
        if callee_spec and callee_spec.postcondition.strip() not in ("true", "", "1"):
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


def _generate_nd_decls(
    func: FunctionInfo,
    cbmc_unwind: int = 4,
    nonnull_params: Optional[set] = None,
    precondition: Optional[str] = None,
    raw_bytes: bool = False,
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
            elif clean_base == "char" and star_count == 1:
                # Single-indirection char*.  Two strategies:
                #
                #  - Default (raw_bytes=False): bounded null-terminated string,
                #    so strlen-style traversal loops terminate within the CBMC
                #    unwinding bound.  Right for textual APIs (printf, strcpy).
                #
                #  - raw_bytes=True: raw byte buffer with no NUL termination
                #    constraint.  Right for wire-format parsers (protobuf upb
                #    varints, length-prefixed blobs) that read N raw bytes from
                #    ptr[0..N) regardless of NULs.  The NUL-string mode
                #    over-constrains the input (no embedded NULs) and
                #    under-sizes the buffer when the callee reads past strlen.
                #
                # char** (e.g. argv) uses the default treatment in either mode.
                buf_name = f"_{pname}_buf"
                if raw_bytes:
                    lines.append(
                        f"    /* raw byte buffer for '{pname}' "
                        f"({cbmc_unwind + 1} bytes, no NUL termination) */"
                    )
                    lines.append(f"    char {buf_name}[{cbmc_unwind + 1}];")
                    lines.append(f"    {ptype_stripped} {pname} = {buf_name};")
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
            clean_val = var_value.strip()

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
                _strip_static_inline_defs(_strip_inline_asm(type_decls))
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
        type_decls = _strip_stdlib_decls(
            _strip_glibc_internal_typedefs(
                _strip_static_inline_defs(_strip_inline_asm(type_decls))
            )
        )

        # --- 2. Identify callees to stub ---
        # "local" callees: defined in this parsed file
        local_callees = func.callees & set(parsed_file.functions.keys())
        # "extern" callees: not in this file but known from other parsed files
        extern_callees = set()
        if extern_sigs:
            extern_callees = (func.callees - local_callees) & set(extern_sigs.keys())
        all_stub_callees = local_callees | extern_callees

        # --- 3. Generate stubs for each callee ---
        stub_sections: list[str] = []
        for callee_name in sorted(all_stub_callees):
            callee_spec = spec.callee_specs.get(callee_name)
            stub_src = _generate_stub(callee_name, callee_spec, parsed_file, extern_sigs)
            stub_sections.append(stub_src)

        defined_callees = all_stub_callees  # used below for substitution

        # --- 4. Build the function body with callee calls substituted ---
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

        if assert_stmts:
            body_lines.append(f"    /* Step 4: postcondition assertions */")
            body_lines.extend(f"    {s}" for s in assert_stmts)

        # Silence unused-variable warnings if the postcondition didn't
        # reference `result` (e.g. trivial postcondition).
        if result_line_present and not any(
            "result" in s for s in assert_stmts
        ):
            body_lines.append("    (void)result;")

        sections = [
            f"/* Auto-generated CBMC harness (real-libc mode) for: {fn_name} */",
            f"/* Source: {func.source_file} */",
            "",
            f'#include "{include_target}"',
            "",
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


def _strip_inline_asm(text: str) -> str:
    """Remove asm/asm volatile/__asm__/etc. statements so the harness compiles on x86."""
    result: list[str] = []
    i = 0
    pat = re.compile(r'\b(asm|__asm__|__asm)\b')
    while i < len(text):
        m = pat.search(text, i)
        if m is None:
            result.append(text[i:])
            break
        result.append(text[i:m.start()])
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
            if j < len(text) and text[j] == ';':
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
    # POSIX <sys/types.h>
    "FILE", "fpos_t", "clock_t", "time_t",
    "pid_t", "uid_t", "gid_t", "mode_t", "nlink_t",
    "off_t", "ino_t", "dev_t", "blkcnt_t", "blksize_t",
    "rlim_t", "id_t", "suseconds_t", "useconds_t",
    "ssize_t", "socklen_t", "sa_family_t",
})


def _strip_glibc_internal_typedefs(text: str) -> str:
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
    """
    result: list[str] = []
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
        if name_m and (
            name_m.group(1).startswith('__')
            or name_m.group(1) in _SYSTEM_TYPEDEF_NAMES
        ):
            result.append(text[i:m.start()])
            result.append(f'/* typedef {name_m.group(1)} removed */')
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
    """
    # Match function declarations at brace depth 0: lines/blocks ending in ';'
    # that look like "... funcname ( ... );"
    _DECL_PAT = re.compile(r'\b(\w+)\s*\(')
    result: list[str] = []
    i = 0
    while i < len(text):
        # Find the next ';' at depth 0
        j = i
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
        stmt = text[i:j + 1]
        # Does this statement look like a function declaration (has parens, no body)?
        m = _DECL_PAT.search(stmt)
        if m and m.group(1) in _SYSTEM_FUNCTION_NAMES and '{' not in stmt:
            result.append(f'/* {m.group(1)} decl removed */')
        else:
            result.append(stmt)
        i = j + 1
    return ''.join(result)


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
