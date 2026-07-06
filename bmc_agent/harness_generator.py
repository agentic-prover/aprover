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
from bmc_agent.dsl_to_cbmc import (
    postcond_to_assert,
    precond_to_assume,
)
from bmc_agent.parser import FunctionInfo, FunctionSignature, ParsedCFile
from bmc_agent.spec import Spec


# ---------------------------------------------------------------------------
# Helpers: extract non-function declarations from a source file
# ---------------------------------------------------------------------------


def _strip_conflicting_libc_typedefs(text: str) -> str:
    """Drop re-emitted libc struct typedefs (div_t/ldiv_t/lldiv_t/imaxdiv_t) that
    collide with <stdlib.h>/<inttypes.h>. Preprocessed .i sources expand these; re-
    emitting the anonymous-struct typedef makes a DISTINCT type -> gcc/CBMC \"conflicting
    types for div_t\" when the harness also includes the header. Only strip when the
    providing header is present in the source context (safe for freestanding)."""
    import re as _re
    for _name in ("div_t", "ldiv_t", "lldiv_t", "imaxdiv_t"):
        text = _re.sub(r"typedef\s+struct\s*\{[^{}]*\}\s*" + _name + r"\s*;",
                       "/* AMC: dropped libc typedef " + _name + " */", text, flags=_re.S)
    return text


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
    return _strip_conflicting_libc_typedefs(_strip_cpp_linemarkers(text))


# Match cpp ``# N "filename" [flags]`` line directives anchored at line start.
# The optional trailing digits are the cpp ``flags`` ( 1=enter file, 2=exit,
# 3=system, 4=extern). Whole line is removed.
_CPP_LINEMARKER_RE = re.compile(r'^\s*#\s+\d+\s+"[^"]*"(?:\s+[0-9 ]+)?\s*$', re.MULTILINE)

# Same shape, but anchored only at line start — strips the directive prefix
# even when downstream code is concatenated on the same line (no trailing
# newline before the next token). Observed on Linux-kernel preprocessed
# .i files where lines like ``# 232 "/path/compiler.h"static inline ...``
# appear; the strict end-of-line variant above can't touch these, so the
# directive survives and CBMC parses ``"path"static`` as a syntax error.
# We strip just the prefix (``# N "filename" [flags]``) and leave the
# trailing code in place.
_CPP_LINEMARKER_PREFIX_RE = re.compile(r'^\s*#\s+\d+\s+"[^"]*"(?:\s+[0-9 ]+)?', re.MULTILINE)


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
    counts stay aligned with whatever was before the strip. Then run a
    second pass that also handles directives concatenated with code on
    the same line — see _CPP_LINEMARKER_PREFIX_RE.
    """
    text = _CPP_LINEMARKER_RE.sub("", text)
    text = _CPP_LINEMARKER_PREFIX_RE.sub("", text)
    return text


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
    Exclude function definitions from *source_text*, leaving a forward
    declaration in place so downstream code (TU-scope dispatch tables)
    can still take addresses by name.

    Preferred path: use ``parsed_file.function_definitions`` (full text from the
    tree-sitter ``function_definition`` node, including return type and body),
    extended backward over any attribute/annotation preamble.  Multi-line
    return types and attribute-on-own-line declarations are excised cleanly.

    Fallback path (for parsers that didn't populate ``function_definitions``):
    locate each function body and walk backward to pick up the return-type
    line.  Only reliably handles single-line signatures.

    Each excised function definition is replaced with a forward
    declaration emitted at the original byte position. Without this,
    Linux drivers' TU-scope module-registration tables (``static struct
    usb_serial_driver ch341_device = { .open = ch341_open, ... };``)
    reference functions before any prior declaration, and CBMC errors
    out with ``failed to find symbol 'ch341_open'``. Emitting a
    forward decl in the original spot preserves the source ordering
    so the dispatch table sees the name already declared.
    """
    # (start, end, forward_decl) tuples; forward_decl can be empty.
    excise: list[tuple[int, int, str]] = []
    function_defs = getattr(parsed_file, "function_definitions", None) or {}

    def _make_forward_decl(name: str) -> str:
        sig = parsed_file.functions.get(name)
        if sig is None:
            return ""
        params = _params_str(sig.parameters)
        return f"{sig.return_type.strip()} {name}({params});"

    for func_name, body_text in parsed_file.function_bodies.items():
        if not body_text:
            continue

        full_def = function_defs.get(func_name)
        if full_def:
            def_start = source_text.find(full_def)
            if def_start != -1:
                preamble_start = _find_decl_preamble(source_text, def_start)
                excise.append((
                    preamble_start,
                    def_start + len(full_def),
                    _make_forward_decl(func_name),
                ))
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

        excise.append((sig_start, body_end, _make_forward_decl(func_name)))

    if not excise:
        return source_text.strip()

    # Merge overlapping spans, sort, then build output. When merging,
    # concatenate the forward decls of the merged spans (separated by
    # newlines) so each removed function still gets a declaration.
    excise.sort()
    merged: list[list] = []
    for start, end, fwd in excise:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            if fwd:
                merged[-1][2] = (merged[-1][2] + "\n" + fwd).strip()
        else:
            merged.append([start, end, fwd])

    parts: list[str] = []
    pos = 0
    for start, end, fwd in merged:
        if pos < start:
            parts.append(source_text[pos:start])
        if fwd:
            parts.append(fwd + "\n")
        pos = end
    if pos < len(source_text):
        parts.append(source_text[pos:])

    return "".join(parts).strip()


# Standard libc functions the DYNAMIC (GCC) harness should resolve against the
# real C library rather than stub. The harness now links real libc (we strip
# the freestanding stub-libc include dirs), so renaming e.g. ``printf`` to
# ``printf_stub`` only produces ``undefined reference to printf_stub`` at link.
# Excluding these from the external-callee set leaves the calls intact so they
# bind to the real implementations. (CBMC harnesses are unaffected — this set
# is only consulted by generate_dynamic_harness.)
_LIBC_FUNCS = frozenset({
    "printf", "fprintf", "snprintf", "sprintf", "vprintf", "vfprintf",
    "vsnprintf", "vsprintf", "puts", "fputs", "putchar", "putc", "fputc",
    "fwrite", "fread", "fopen", "fclose", "fflush", "perror",
    "memcpy", "memmove", "memset", "memcmp", "memchr",
    "strlen", "strnlen", "strcmp", "strncmp", "strcpy", "strncpy",
    "strcat", "strncat", "strchr", "strrchr", "strstr", "strdup", "strndup",
    "strtol", "strtoul", "strtoll", "strtoull", "strtod", "atoi", "atol", "atoll",
    "malloc", "calloc", "realloc", "free", "abort", "exit", "_Exit",
    "qsort", "bsearch", "abs", "labs", "llabs", "rand", "srand",
    "isalpha", "isdigit", "isalnum", "isspace", "isupper", "islower",
    "toupper", "tolower",
})


def _strip_comments_and_strings(text: str) -> str:
    """Blank out C comments and string/char literals (preserving newlines and
    length) so a brace/paren scan over the result reflects real code structure.
    Used by the file-scope variable extractor below; not a general formatter."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if two == "//":
            while i < n and text[i] != "\n":
                out.append(" "); i += 1
            continue
        if two == "/*":
            out.append("  "); i += 2
            while i < n and text[i:i + 2] != "*/":
                out.append("\n" if text[i] == "\n" else " "); i += 1
            if i < n:
                out.append("  "); i += 2
            continue
        if c in "\"'":
            quote = c
            out.append(" "); i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    out.append("  "); i += 2; continue
                out.append("\n" if text[i] == "\n" else " "); i += 1
            if i < n:
                out.append(" "); i += 1
            continue
        out.append(c); i += 1
    return "".join(out)


def _extract_file_scope_var_defs(
    source_text: str, wanted_names: "set[str]", exclude_names: "set[str]"
) -> list[str]:
    """Return file-scope (brace-depth-0) VARIABLE definitions whose declared
    name is in *wanted_names* and not in *exclude_names*.

    Motivation: the dynamic harness embeds function bodies that may reference a
    module-scope variable (e.g. ``static const char *dtb_error = "No error";``
    in dtb.c, read by ``dtb_get_error``). The brace-counting type-decl
    extractor mishandles real sources (it ignores comments/strings) and can
    drop such definitions, producing ``error: 'dtb_error' undeclared`` at GCC
    compile. This recovers exactly the referenced ones.

    Depth is tracked over a comment/string-masked copy so braces inside
    function bodies, strings, or comments don't fool the scan. ``static`` is
    stripped so the variable links in the single-TU harness; declarations
    (no initializer, e.g. ``extern``) are skipped — they don't define storage.
    """
    masked = _strip_comments_and_strings(source_text)
    # Walk statements at depth 0 (outside any {...} and any (...)).
    defs: list[str] = []
    seen: set[str] = set()
    depth_brace = depth_paren = 0
    start = 0
    for i, ch in enumerate(masked):
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
            start = i + 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == ";" and depth_brace == 0 and depth_paren == 0:
            seg_masked = masked[start:i + 1]
            seg_real = source_text[start:i + 1]
            start = i + 1
            # The segment runs from the previous statement terminator, so it
            # can carry leading blank/comment lines (blanked in `masked`) and
            # preprocessor directives that aren't ';'-terminated. Drop those so
            # only the declaration itself is emitted (not stray #includes).
            _ml = seg_masked.splitlines()
            _rl = seg_real.splitlines()
            _k = 0
            while _k < len(_ml):
                if _ml[_k].strip() == "" or (_k < len(_rl) and _rl[_k].lstrip().startswith("#")):
                    _k += 1
                else:
                    break
            seg_masked = "\n".join(_ml[_k:])
            seg_real = "\n".join(_rl[_k:])
            # A variable DEFINITION at file scope: has an '=' initializer and
            # no '(' (so it's not a function prototype / function-pointer-init
            # we can't safely reproduce). Find the declared name (token just
            # before '=').
            if "(" in seg_masked or "=" not in seg_masked:
                continue
            m = re.search(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*=", seg_masked)
            if not m:
                continue
            name = m.group(1)
            if name in exclude_names or name in seen or name not in wanted_names:
                continue
            seen.add(name)
            cleaned = re.sub(r"\bstatic\b\s*", "", seg_real).strip()
            defs.append(f"/* file-scope var referenced by closure */\n{cleaned}")
    return defs


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


_RET_TYPE_STORAGE_CLASS_RE = re.compile(
    r'\b(static|inline|extern|register|_Noreturn)\b\s*'
)


def _ret_type_bare(ret_type: str) -> str:
    """Strip storage-class / inline keywords from a function's return type.

    The signature-keeping parser fallback (regex-based, used when
    tree-sitter isn't installed) keeps ``static`` / ``inline`` in
    ``return_type``. Using that raw return type to declare a local
    variable in the harness — ``static void result = fut(...);`` —
    produces invalid C: ``void`` cannot be a variable type, plus the
    assignment from a void expression is also illegal. CBMC rejects
    with "void-typed symbol not permitted" / CONVERSION ERROR.

    Use this helper everywhere a return type is being substituted into
    a local-variable declaration in a generated harness; keep the raw
    return type only where we re-emit the FUNCTION definition itself.
    """
    return _RET_TYPE_STORAGE_CLASS_RE.sub('', ret_type).strip()


def _sanitize_for_c_comment(text: str, max_len: int = 200) -> str:
    """Make a witness-value string safe to embed inside ``/* ... */``.

    CBMC's tokenizer warns on unterminated character literals even when
    they appear inside a comment, and a stray apostrophe (e.g., from a
    Python-repr witness like ``{'name': 'unknown'}``) can produce
    "missing terminating ' character" → CONVERSION ERROR → exit 6 on
    the entire reachability check. Replace problematic chars with
    safe substitutes and truncate to keep comment lines bounded.

    Substitutions:
      ``'`` → backtick (``\\u0060``)
      ``"`` → backtick
      ``*/`` → ``*\\/`` (cannot close the enclosing comment)
    """
    if not text:
        return ""
    t = text.replace("'", "`").replace('"', "`").replace("*/", "*\\/")
    if len(t) > max_len:
        t = t[:max_len] + "..."
    return t


def _params_str(params: list[tuple[str, str]]) -> str:
    """Build a C parameter list string, handling variadic '...' correctly.

    Also handles 2D-array parameter types. The tree-sitter parser
    renders ``pod_neighbor_io_t pnio[][2]`` as type=``pod_neighbor_io_t*[2]``,
    name=``pnio``. Naive emit ``pod_neighbor_io_t*[2] pnio`` is
    invalid C syntax. The right C declaration for "pointer to array of
    N elements" is ``T (*pname)[N]``. Detect the ``*[N]`` tail and emit
    the parenthesised form.
    """
    if not params:
        return "void"
    parts = []
    for ptype, pname in params:
        if ptype == "...":
            parts.append("...")
        elif pname:
            # Handle ``T*[N]`` tail (parser quirk for 2D array params).
            m = re.search(r"\*\s*\[(\d+|\s*)\]\s*$", ptype)
            if m and "*" in ptype:
                # Strip the *[N] tail to get the element type, then emit
                # T (*pname)[N].
                base = ptype[:m.start()].rstrip("*").strip()
                n = m.group(1).strip()
                parts.append(f"{base} (*{pname})[{n}]")
            else:
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
    size_helper_max_loc: int = 200,
) -> tuple[bool, str]:
    """Decide whether *callee_name* should be inlined (real body
    embedded in the harness) instead of stubbed.

    Returns ``(eligible, reason)``. Conservative by design — when a
    rule rejects, the caller falls back to the existing stub path, so
    rejection cannot regress correctness.

    Two eligibility tiers:

    **Tier 1 — Small pure helper** (the original rule). Strict but cheap:

      - callee body is defined in the parsed file (not extern);
      - signature is ``static`` (file-local linkage);
      - body is at most ``max_loc`` non-empty, non-comment lines;
      - no loops in body (``for`` / ``while`` / ``do``);
      - body does not call any allocator-family function;
      - body is not directly recursive;
      - body does not dispatch through a function pointer
        (``(*foo)(...)`` patterns).

    Targets the "stub disconnect" FP class on small helpers
    (jv_get_kind, xmlIsBlank_ch, BUF_ERROR, …).

    **Tier 2 — Size-calculator helper** (added 2026-05-23 after the
    archive_acl_text_len compositional-bug analysis):

      - callee is ``static`` and defined in this TU;
      - return type contains ``size_t`` / ``ssize_t`` (the canonical
        size-calc signature);
      - body has ≤ ``size_helper_max_loc`` LoC (default 200, much
        higher than tier 1's 30) to accommodate the typical iterate-
        and-accumulate pattern;
      - body has at most ONE loop (typical size-calc walks one
        collection once);
      - other disqualifiers (allocators, recursion, fn-pointer
        dispatch) still apply.

    Rationale: compositional bugs in the form "helper computes wrong
    size → caller allocates undersize buffer → caller over-writes"
    require the helper's body to run during caller verification. The
    canonical instance is libarchive's d45b5b4b (archive_acl_text_len
    undercount → archive_acl_to_text_l/w OOB write). Tier 1's "no
    loops" rule blocked these because the helper iterates over the
    collection it's sizing. Tier 2 carves out the specific shape
    (size_t / ssize_t return + at most one loop) where inlining is
    both useful and tractable for CBMC.
    """
    cfi = parsed_file.get_function_info(callee_name)
    if cfi is None:
        return False, "callee not defined in parsed file (extern)"
    # Variadic callees (``va_list`` / ``...``) cannot be modelled by CBMC:
    # inlining the body triggers a CONVERSION ERROR that poisons the WHOLE
    # caller's verification (the printf-family ``vprintf_internal`` case — the
    # overnight sweep's 64 printf CONVERSION errors). Always stub a variadic
    # callee instead; stubbing is the sound compositional default (a stub never
    # produces a false-clean), so this only recovers tractability, never soundness.
    if any(pt == "..." or "va_list" in (pt or "")
           for pt, _ in (getattr(cfi.signature, "parameters", None) or [])):
        return False, "variadic callee (va_list) — CBMC cannot model the body; always stub"
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

    # Detect size-calc shape: static + return type contains size_t/ssize_t.
    ret_type = (cfi.signature.return_type or "").strip().lower()
    is_size_helper = ("size_t" in ret_type) or ("ssize_t" in ret_type)

    # Pick the effective LoC cap based on tier.
    effective_max_loc = size_helper_max_loc if is_size_helper else max_loc
    if len(nonempty) > effective_max_loc:
        return False, f"body has {len(nonempty)} LoC (cap {effective_max_loc})"

    # Loop count. Tier 1 (pure helper) rejects ALL loops. Tier 2
    # (size helper) allows at most ONE loop — the typical
    # iterate-and-accumulate pass.
    loop_matches = re.findall(r"\b(for|while|do)\s*[({]", body_clean)
    if is_size_helper:
        # Size helpers commonly have 1 outer iteration + 1 inner string-
        # length / digit-count loop. Cap at 3 to accommodate nested or
        # sibling loops without opening the door to arbitrary loop nests.
        if len(loop_matches) > 3:
            return False, f"size-helper body has {len(loop_matches)} loops (cap 3)"
    else:
        if loop_matches:
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


_STUB_OUT_BOUND = 16  # bound for stubbed-callee output counts; buffer = bound*16 bytes


def _emit_stub_output_param_init(params) -> list[str]:
    """D(i)/G5 structural invariant: model a stubbed callee's OUTPUT params as if
    the callee behaved correctly, instead of leaving them nondeterministic.

    - output count/size (``T *len``, name looks like a length): bound to
      ``[0, _STUB_OUT_BOUND]`` so a caller loop driven by it stays in range.
    - output buffer (``T **out``): hand back a generously-sized allocation so a
      count-bounded copy loop in the caller cannot run past it.

    This kills the dominant array-reader false-positive class (e.g. a havoc'd
    ``TIFFReadDirEntryArray`` returned an unbounded count + an independent
    buffer, so the caller's ``*mb++ = *ma++`` loop read OOB). It is an
    UNDER-approximation (assumes the callee is correct), so it adds no false
    positives; bugs *inside* the stubbed callee are covered by its own harness.
    """
    out: list[str] = []
    n = _STUB_OUT_BOUND
    for ptype, pname in params:
        if not pname:
            continue
        t = ptype.strip()
        pointee = t.split("*", 1)[0]
        if "const" in pointee:  # const pointee = input-only param
            continue
        stars = t.count("*")
        if stars >= 2:
            out.append(
                f"    if ({pname}) {{ *{pname} = __CPROVER_allocate((size_t)({n}) * 16 + 16, 0); }}"
                f"  /* D-i: sized output buffer */"
            )
        elif stars == 1 and _is_likely_length_field(pname) and _looks_like_integer_type(t[:-1]):
            out.append(
                f"    if ({pname}) {{ __CPROVER_assume(*{pname} >= 0 && *{pname} <= (long)({n})); }}"
                f"  /* D-i: bounded output count */"
            )
    return out


_C_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def _c_expressible_postcondition(post: str, param_names: list, return_var: str = "result") -> "str | None":
    """If *post* is a clean C boolean expression over the callee's params + the
    return value, return it (with ``\\result`` → ``result``); else None.

    "Clean" = every identifier token is a parameter, the return var, or a C
    constant — no ACSL (``\\old``), no prose ("sum of ... through"), no unmodelled
    predicates (``valid``, ``sum``). Those cases return None so the caller falls
    back to the conservative comment path. Used only when
    ``assume_callee_postcondition`` is set."""
    expr = re.sub(r"\\result\b", return_var, (post or "")).strip()
    if not expr or "\\" in expr:        # ACSL \old / \forall etc. — not plain C
        return None
    # DSL-only operators that are NOT valid C (implication / iff). Without this
    # they slip past the identifier check and become a malformed __CPROVER_assume
    # that CBMC mis-parses → spurious unsoundness. Reject → caller falls back to
    # the agentic harness, which can translate them.
    if any(op in expr for op in ("==>", "<==>", "<=>")):
        return None
    bound = set(param_names) | {return_var, "NULL", "true", "false"}
    for tok in _C_IDENT_RE.findall(expr):
        if tok not in bound:            # any non-param/result identifier word → reject
            return None
    return expr


def _emit_copy_source_return_stub(
    callee_name: str,
    ret_type: str,
    struct_definitions: Optional[dict],
    copy_field_maxlen: Optional[dict],
) -> Optional[list[str]]:
    """If ``callee_name`` returns a pointer to a struct (with a visible body)
    one of whose ``char *`` fields is a copy SOURCE in the function under test
    (i.e. it appears in ``copy_field_maxlen``), emit a return-value model that
    points at a STATIC-backed struct whose copy-source field is a WIDENED
    NUL-terminated string -- so a ``strcpy(fixed_buf, ret->field)`` in the
    caller can overflow (FN dual of (buf,len), for callee-RETURN sources like
    vibeos ``temp = vfs_lookup(...); strcpy(path_copy, temp->data)``).

    The NULL return is still explored (soundness: the caller's ``if (!ret)``
    early-out path stays reachable). This is STRICTLY MORE FAITHFUL than the
    default nondet-pointer return, which also admits a non-NULL-but-invalid
    garbage pointer (itself a historic FP source) -- here the non-NULL case is
    a valid allocated object. Returns ``None`` (caller keeps default havoc)
    when no qualifying struct/field is found.
    """
    if not copy_field_maxlen or not struct_definitions:
        return None
    rt = ret_type.strip()
    if rt.count("*") != 1:                      # single-indirection return only
        return None
    base = re.sub(r"\bconst\b", "", rt.rstrip("*")).strip()
    struct_name = _resolve_struct_name(base, struct_definitions)
    if not struct_name:
        return None
    fields = struct_definitions.get(struct_name) or []
    # Find a char*-typed field that is a copy source in the caller.
    target = None
    for ftype, fname in fields:
        if fname in copy_field_maxlen:
            ft = re.sub(r"\bconst\b", "", ftype or "").strip()
            if ft.count("*") == 1 and re.sub(r"\*", "", ft).strip() == "char":
                target = (fname, copy_field_maxlen[fname])
                break
    if target is None:
        return None
    fname, maxlen = target
    tag = re.sub(r"\W", "_", callee_name)
    buf = f"_ret_{tag}_{fname}"
    ln = f"_ret_{tag}_{fname}_len"
    rt = ret_type.strip()
    # typedef alias vs bare tag: struct_definitions keys can be either; emit the
    # form that names the type so ``malloc(sizeof(...))`` is well-typed.
    type_for_size = struct_name if base == struct_name else f"struct {struct_name}"
    # Use malloc (NOT a static array): malloc'd memory has NONDET contents (a
    # static array is zero-initialised -> buf[0]==0 -> empty string -> the
    # overflow is unreachable) AND heap lifetime (survives the stub return, so
    # no dead-object on the caller's deref). CBMC's malloc may also return NULL,
    # which keeps the caller's ``if (!ret) return`` early-out path reachable
    # (soundness) -- strictly more faithful than the default nondet-garbage ptr.
    return [
        f"    /* copy-source RETURN modeling: '{fname}' is strcpy'd into a fixed",
        f"       buffer in the caller -- widen it ({maxlen} chars, nondet contents)",
        f"       so the overflow is reachable; NULL is still explored (soundness). */",
        f"    {rt} result = malloc(sizeof({type_for_size}));",
        f"    if (result) {{",
        f"        char *{buf} = malloc((unsigned int){maxlen} + 1);",
        f"        if ({buf}) {{",
        f"            unsigned int {ln};",
        f"            __CPROVER_assume({ln} <= (unsigned int){maxlen});",
        f"            {buf}[{ln}] = '\\0';",
        f"            result->{fname} = {buf};",
        f"        }}",
        f"    }}",
        f"    return result;",
    ]


def _copy_field_plan(config, fn) -> dict:
    """The function-under-test's copy-source FIELD -> widened-length map (used to
    widen matching struct-pointer stub returns). Empty when disabled/none."""
    if not getattr(config, "enable_string_copy_source_modeling", True):
        return {}
    cap = getattr(config, "string_copy_source_max_len", 0)
    if not cap or cap <= 0:
        return {}
    try:
        from .string_copy_sink import plan_copy_source_widening
        _p, fmax, _floor = plan_copy_source_widening(
            fn, cap, getattr(config, "string_copy_source_max_dest", 256)
        )
        return fmax
    except Exception:
        return {}


def _generate_stub(
    callee_name: str,
    callee_spec: Optional[Spec],
    parsed_file: ParsedCFile,
    extern_sigs: Optional[dict] = None,
    assume_postcondition: bool = False,
    verified_sound: Optional[set] = None,
    copy_field_maxlen: Optional[dict] = None,
    struct_definitions: Optional[dict] = None,
) -> str:
    """Generate a C stub function for a callee.

    *extern_sigs* is an optional dict mapping callee names to FunctionSignature
    objects sourced from other parsed files (multi-file mode).  When the callee
    is not in *parsed_file.functions* we check here before giving up.

    The callee's PRE (pre_validity + pre_protocol if split, else the flat
    precondition) is assumed via ``__CPROVER_assume(...)`` inside the stub.
    v2 spec-gen's caller-grounding makes the previous bug-hunt mode's
    assert-validity-at-call-site mechanism obsolete: a caller-derived PRE
    already excludes caller-contract-slip inputs at spec time, so there
    is nothing for an assertion to fire on at verification time.
    """
    sig = parsed_file.functions.get(callee_name)
    if sig is None and extern_sigs:
        sig = extern_sigs.get(callee_name)
    if sig is None:
        # Last-resort lookup: the universal stub-contract registry
        # carries canonical (return_type, params) for well-known OSS
        # primitives whose body isn't in any parsed TU. Synthesise a
        # FunctionSignature so the rest of this function can produce
        # a real stub with the registry's postconditions, instead of
        # falling through to a generic void havoc.
        try:
            from bmc_agent.universal_stub_contracts import canonical_signature
            from bmc_agent.parser import FunctionSignature
            canon = canonical_signature(callee_name)
            if canon is not None:
                _ret_t, _params = canon
                sig = FunctionSignature(
                    name=callee_name,
                    return_type=_ret_t,
                    parameters=list(_params),
                )
        except Exception:
            sig = None
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

    if callee_spec and callee_spec.precondition.strip() not in ("true", "", "1"):
        param_names = [pname for _, pname in params]
        import os as _os
        _bounds_only = _os.environ.get("BMC_ASSERT_BOUNDS_ONLY", "") in ("1", "true")
        if _bounds_only or _os.environ.get("BMC_ASSERT_CALLEE_PRECOND", "") in ("1", "true"):
            # ASSERT the callee precondition at the call site so caller-misuse
            # (e.g. passing an out-of-bounds size to my_memcpy) is CAUGHT, not
            # trusted. assert-context translation => valid_range carries a real
            # __CPROVER_r_ok bounds check. Fixes the compositional bug-finding
            # soundness hole where assume(precond) masks misuse.
            # BOUNDS-ONLY refinement: assert only the memory-safety bounds
            # clauses (those carrying r_ok); ASSUME the structural/functional
            # clauses (is_valid, etc.) — the caller establishes those via data
            # flow, and asserting them spuriously fails when the harness can't
            # reconstruct the full invariant (the assert-mode FA source).
            from bmc_agent.dsl_to_cbmc import precond_to_assert as _p2a
            _pre = _p2a(callee_spec.precondition, param_names)
            if _pre:
                lines.append("    /* callee precondition: assert bounds (misuse), assume structural */")
                for stmt in _pre:
                    if _bounds_only and "r_ok" not in stmt:
                        lines.append(f"    {stmt.replace('assert(', '__CPROVER_assume(')}")
                    else:
                        lines.append(f"    {stmt}")
        else:
            assume_stmts = precond_to_assume(callee_spec.precondition, param_names)
            if assume_stmts:
                lines.append("    /* Assume callee precondition */")
                for stmt in assume_stmts:
                    lines.append(f"    {stmt}")

    # D(i)/G5: model output params (bounded count, sized buffer) so the stub
    # respects the structural invariant the real callee enforces.
    out_init = _emit_stub_output_param_init(params)
    if out_init:
        lines.append("    /* D-i: structural-invariant output-param modeling */")
        lines.extend(out_init)

    ret_type_bare = _ret_type_bare(ret_type)
    if ret_type_bare == "void":
        lines.append("    /* void return — nothing to havoc */")
    else:
        # Copy-source RETURN modeling: if this callee returns a struct pointer
        # whose char* field is strcpy'd into a fixed buffer in the caller, point
        # the return at a static struct with that field WIDENED so the overflow
        # is reachable (callee-return source variant of the string-copy FN fix).
        _copy_ret = _emit_copy_source_return_stub(
            callee_name, ret_type, struct_definitions, copy_field_maxlen
        )
        if _copy_ret is not None:
            lines.extend(_copy_ret)
            lines.append("}")
            return "\n".join(lines)
        # Havoc the return value: declare it, constrain by postcondition.
        # Strip storage class — ``static void result;`` would be invalid
        # (void can't be a variable type) and ``static int result;`` is
        # legal but unintended (file-scope-like lifetime is meaningless
        # for a havoc value).
        lines.append(f"    {ret_type_bare} result;")

        # Built-in stub contracts for well-known allocator-family externs.
        # Without these, CBMC stubs return arbitrary garbage pointers that
        # alias unrelated memory regions, producing a large class of
        # false-positive NULL-deref / OOB findings. The contracts model
        # the documented behavior: return NULL or a valid pointer to N bytes.
        builtin_contract = _builtin_stub_return_contract(
            callee_name, ret_type, params
        )
        # Universal stub postconditions — additional OSS-primitive
        # contracts not covered by the libc/kernel-allocator table
        # above. These are POSTCONDITIONS only (model what real callees
        # return); see bmc_agent/universal_stub_contracts.py for the
        # soundness rule.
        if not builtin_contract:
            try:
                from bmc_agent.universal_stub_contracts import derive_stub_contract
                builtin_contract = derive_stub_contract(
                    callee_name, ret_type, params,
                )
            except Exception:
                builtin_contract = []
        # Kernel-API return-convention contracts. Kernel functions like
        # ``usb_control_msg``, ``usb_submit_urb``, etc. return ``0`` on
        # success or a negative ERRNO (-4095 … -1) on failure. CBMC's
        # default nondet stub returns arbitrary positive ints, producing
        # false-positive callers that branch on impossible success
        # values (e.g. ``result == 2`` after a NULL-buffer call).
        # Acted on TODO #2 from the 2026-05-18 ch341.c run.
        if not builtin_contract:
            builtin_contract = _kernel_api_return_contract(callee_name, ret_type)
        if builtin_contract:
            lines.append("    /* Built-in stub contract (allocator-family or kernel API) */")
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
                # Propagate a CLEAN FUNCTIONAL postcondition (C boolean over
                # params+result) as a real assume, so the contract reaches the
                # caller. Fires when EITHER:
                #   * assume_postcondition (unconditional; only safe behind an
                #     explicit soundness gate, e.g. assertion-driven synthesis), OR
                #   * this callee is in verified_sound — it has been PROVEN to
                #     satisfy its postcondition this run, so assuming it is sound
                #     (excludes only impossible returns) and kills stub-disconnect FPs.
                # Prose/ACSL postconditions are not C-expressible → fall through to
                # the conservative comment path below, unchanged.
                do_assume = assume_postcondition or (
                    verified_sound is not None and callee_name in verified_sound)
                direct = (_c_expressible_postcondition(callee_spec.postcondition, param_names)
                          if do_assume else None)
                if direct:
                    lines.append("    /* [assume_callee_postcondition] functional contract */")
                    lines.append(f"    __CPROVER_assume({direct});")
                else:
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


_CLAUSE_STRUCT_DEREF_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*->"
)


# Any identifier reference. Used to spot bare references like ``acl != NULL``
# that lack a ``->`` so the struct-deref filter misses them but still
# require the identifier to resolve in the current TU's scope.
_CLAUSE_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


# Identifiers we never treat as locals, regardless of length / shape.
# C/CBMC keywords + standard constants + commonly-referenced primitive
# types/macros that the LLM threads through clauses.
_CLAUSE_RESERVED_IDENTS: frozenset[str] = frozenset({
    "NULL", "true", "false", "TRUE", "FALSE",
    "sizeof", "offsetof", "typeof",
    "void", "char", "short", "int", "long", "float", "double", "signed",
    "unsigned", "_Bool", "bool", "size_t", "ssize_t", "ptrdiff_t",
    "wchar_t", "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "uintptr_t", "intptr_t", "off_t",
    "struct", "union", "enum", "const", "volatile", "restrict",
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "return", "break", "continue", "goto",
    "static", "extern", "inline", "register", "auto",
})


# Pseudo-formal-logic patterns the LLM sometimes emits in learned clauses
# (e.g. ``forall struct archive_rb_node *n in t: valid(n)``). These are
# not valid C ``__CPROVER_assume`` expressions — CBMC rejects with
# "syntax error before 'struct'" / "syntax error before ':'" → exit code 6.
# Note: CBMC's actual quantifier intrinsics are __CPROVER_forall /
# __CPROVER_exists with brace-block syntax; bare ``forall`` / ``exists``
# without that prefix are the LLM-emitted broken pattern.
_PSEUDO_LOGIC_RE = re.compile(
    r"(?<!__CPROVER_)\b(forall|exists)\b"
)


def _clause_is_syntactically_safe(clause: str) -> bool:
    """Best-effort syntactic check: reject learned clauses that contain
    pseudo-formal-logic constructs the LLM sometimes drafts but CBMC
    can't parse. Returning False makes the caller skip the clause —
    safer than failing the entire harness on a single bad assume.
    """
    return _PSEUDO_LOGIC_RE.search(clause) is None


def _is_param_style_ident(ident: str) -> bool:
    """Heuristic: name looks like a function parameter rather than a
    global / macro / type. Matches single-letter names, underscore-
    prefixed names, and other ≤4-char identifiers — the shapes that
    realistically collide between sibling functions in the same project.
    Longer names (``g_init``, ``archive_match_globals_xx``, ``xmlMalloc``)
    are assumed to be globals.
    """
    return len(ident) <= 4 or ident.startswith("_")


_CLAUSE_FN_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\s*\(")

# CBMC's parser accepts these in clauses without needing the function
# to be declared in the TU.
_CLAUSE_SAFE_CALL_PREFIXES = ("__CPROVER_",)

# Stdlib functions that have a fixed, universally-agreed-on signature
# and are always safe to call from any TU. Anything not in this set
# is rejected from project-scope clauses to avoid arity mismatches
# across TUs (see issue: archive_mstring_get_mbs(NULL, NULL) was
# distilled from a TU where the function had 2 args, then emitted
# into cab/cpio/iso/rar5 harnesses where the real header declares 3
# args, causing CONVERSION ERROR on every harness).
_CLAUSE_SAFE_CALL_NAMES = frozenset({
    "strlen", "wcslen", "strcmp", "wcscmp", "memcmp",
    "sizeof",  # not technically a call but parses similarly
})


def _clause_contains_unsafe_function_call(clause: str) -> "tuple[bool, str]":
    """Return (True, fn_name) when the clause contains a function call
    that's not a CBMC builtin or a fixed-signature stdlib function.

    Project-scope clauses must not contain calls to project-defined
    functions because the declared signature can differ across TUs.
    The clause is distilled from one TU and re-emitted across all of
    them — if the function takes 2 args in TU-A but 3 args in TU-B,
    CBMC's type-checker rejects the harness with CONVERSION ERROR.

    Function-scope clauses are NOT subject to this check — they bind
    to a single function whose TU is known at distillation time.
    """
    for m in _CLAUSE_FN_CALL_RE.finditer(clause):
        name = m.group(1)
        # CBMC builtins (__CPROVER_*) parse against CBMC's own signature
        # table, not the TU's declarations.
        if any(name.startswith(p) for p in _CLAUSE_SAFE_CALL_PREFIXES):
            continue
        # Reserved C keywords that take parenthesized arguments
        # (sizeof, _Alignof, typeof) — parse universally.
        if name in _CLAUSE_RESERVED_IDENTS:
            continue
        # Fixed-signature stdlib functions.
        if name in _CLAUSE_SAFE_CALL_NAMES:
            continue
        # Cast expressions look like calls but the "name" is a type.
        # Heuristic: cast-target identifiers are usually uppercase or
        # contain underscores in a type-ish way. Conservative pass:
        # treat anything that looks like a known type qualifier as safe.
        if name in ("struct", "union", "enum", "const", "volatile"):
            continue
        return True, name
    return False, ""


def _clause_references_only_known_idents(
    clause: str, param_names: set[str],
) -> bool:
    """A learned clause is safe to emit only if every parameter-style
    identifier referenced in it (whether as a struct-deref root like
    ``acl->field`` or a bare comparison like ``acl != NULL``) is in
    the current function's parameter set. Identifiers that look like
    globals / macros / types / function calls are passed through.

    Reason: project-wide clauses are distilled from one function's
    counterexample (e.g., ``acl != NULL`` from ``archive_acl_*``) and
    then re-emitted across every function's harness. When the current
    function doesn't have a parameter named ``acl``, CBMC fails with
    "failed to find symbol 'acl'" (exit code 6) and the whole TU dies
    at parse — wiping out all findings on that file. Observed on the
    libarchive postfix8 sweep: a single bad bare-ident clause caused
    701 CBMC parse failures across 6 files.

    The check rejects parameter-style identifiers (see
    ``_is_param_style_ident``) for BARE identifiers, but applies a
    STRICTER rule for struct-deref roots: any struct-deref root that
    isn't in the current function's params or a known builtin / type
    is rejected, regardless of length.

    Why the length asymmetry: a bare identifier like
    ``ARCHIVE_MATCH_MAGIC`` could be a global macro that resolves in
    every TU, so we need the length heuristic to avoid over-rejecting
    project-defined constants. But ``X->field`` is by construction a
    pointer to a struct — you can't dereference a macro — so the root
    MUST be a local variable / parameter. If the root isn't in the
    current function's params and isn't a known builtin, the clause
    will fail to compile in this TU. Reject it.

    Regression that motivated the asymmetry: postfix9b's rar5.c
    sweep emitted a learned clause referencing ``iso9660`` (a 7-char
    param name from iso9660.c's functions) as a struct-deref root.
    The old "≤4 chars or _-prefixed" length filter let it pass; CBMC
    then rejected all 101 rar5 harnesses with
    ``failed to find symbol 'iso9660'``. Dropping the length filter
    for struct-deref roots closes that variant.

    UPPER_CASE macros, function-call identifiers (followed by ``(``),
    and longer names are still passed through in the BARE-identifier
    check. Skipping a clause is always preferable to crashing the
    harness — the clause is an OPTIMIZATION, not a soundness
    invariant.
    """
    # Pattern 1: struct-deref roots (``X->field``). Strict — by
    # construction a struct-deref root is a local variable, so any
    # root not in the current function's params is a guaranteed
    # parse failure in this TU.
    for root in _CLAUSE_STRUCT_DEREF_RE.findall(clause):
        # Allow CBMC builtins (e.g. ``__CPROVER_<thing>->field``).
        if root.startswith("__CPROVER_"):
            continue
        # Allow well-known globals / types that legitimately have
        # struct shape (the ``struct`` keyword itself, or stdlib
        # globals). None known for now, but the slot is here for
        # future extension.
        if root in _CLAUSE_RESERVED_IDENTS:
            continue
        if root not in param_names:
            return False

    # Pattern 2: bare identifier references (``X != NULL``, ``X > 0``).
    for m in _CLAUSE_IDENT_RE.finditer(clause):
        ident = m.group(1)
        # Skip function-call identifiers (``foo(...)``) — those resolve
        # at link time against any declared function in the TU, not
        # against the current function's parameters.
        tail = clause[m.end():].lstrip()
        if tail.startswith("("):
            continue
        # Reserved words, primitive types, NULL/true/false.
        if ident in _CLAUSE_RESERVED_IDENTS:
            continue
        # CBMC builtins.
        if ident.startswith("__CPROVER_"):
            continue
        # ALL_CAPS or UpperCamel_WITH_UNDERSCORE → almost certainly a
        # macro / enum constant (e.g. ``ARCHIVE_ENTRY_ACL_TYPE_NFS4``,
        # ``SIZE_MAX``, ``ARCHIVE_OK``).
        if ident.isupper():
            continue
        if ident[0].isupper() and "_" in ident:
            continue
        # Long lowercase identifiers (``xmlMalloc``, ``archive_acl_clear``)
        # are assumed globals/functions, not local params.
        if not _is_param_style_ident(ident):
            continue
        if ident not in param_names:
            return False
    return True


def clause_has_param_style_ident(clause: str) -> bool:
    """Return True if the clause contains any param-style identifier
    (struct-deref root or bare ident). Used by feedback_loop.py as a
    write-time gate: a clause that references function-local-style
    identifiers should never be promoted to project scope, because
    sibling functions in the same module often share parameter names
    by convention (e.g. ``acl`` across archive_acl.c) — the clause
    only happens to be true for one function, not the whole project.

    Implemented as the negation of ``_clause_references_only_known_idents``
    against an empty param set: any param-style ident "not in {}" trips
    the False, meaning the clause has a function-local-style reference.
    """
    return not _clause_references_only_known_idents(clause, set())


def _emit_learned_clauses(
    config: "Config", func_name: str, scope: str,
    param_names: "set[str] | None" = None,
) -> list[str]:
    """Return `__CPROVER_assume(...)` statements for clauses learned
    from previous realism rejections (feedback loop arm (b)/(c)).

    ``scope`` is "project" or "function". Function clauses are bound
    to the named function; project clauses apply to every harness.
    Returns empty list when the feedback loop is disabled or the store
    is empty.

    When ``param_names`` is supplied, project-scope clauses whose root
    identifier is not in the set are skipped — those would otherwise
    produce ``failed to find symbol 'X'`` CBMC errors (exit code 6) in
    functions whose parameters are named differently from the function
    where the clause was distilled.
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

    # Syntactic-validity gate: rejects pseudo-formal-logic clauses
    # (``forall ... in ... : ...``) the LLM sometimes emits — CBMC
    # rejects those with "syntax error before 'struct'" → exit code 6.
    # Applied to BOTH project and function scope.
    syntactic_filtered: list[str] = []
    for c in clauses:
        if not c.strip():
            continue
        if _clause_is_syntactically_safe(c):
            syntactic_filtered.append(c)
        else:
            from bmc_agent.logger import get_logger
            get_logger("harness").debug(
                "Skipping %s clause for '%s' — contains pseudo-formal-logic "
                "tokens (forall/exists without __CPROVER_ prefix): %s",
                scope, func_name, c[:160],
            )
    clauses = syntactic_filtered

    if scope == "project":
        # Project-scope clauses MUST NOT contain calls to project-defined
        # functions. The declared signature can differ across TUs and a
        # 2-arg vs 3-arg mismatch produces CONVERSION ERROR on every
        # harness in the affected TUs. Stdlib + CBMC builtins are
        # exempted.
        sig_filtered: list[str] = []
        for c in clauses:
            unsafe, fn = _clause_contains_unsafe_function_call(c)
            if unsafe:
                from bmc_agent.logger import get_logger
                get_logger("harness").debug(
                    "Skipping project clause for '%s' — contains call to "
                    "project-defined function '%s()' whose signature may "
                    "vary across TUs: %s",
                    func_name, fn, c[:120],
                )
                continue
            sig_filtered.append(c)
        clauses = sig_filtered

    if scope == "project" and param_names is not None:
        filtered: list[str] = []
        for c in clauses:
            if not c.strip():
                continue
            if _clause_references_only_known_idents(c, param_names):
                filtered.append(c)
            else:
                from bmc_agent.logger import get_logger
                get_logger("harness").debug(
                    "Skipping project clause for '%s' — root identifier not "
                    "in current params %s: %s",
                    func_name, sorted(param_names), c[:120],
                )
        clauses = filtered

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


def _re_global_idents(text: str) -> set[str]:
    """Bare identifiers appearing in ``text`` (used to gate which globals are
    in scope for the current harness)."""
    return set(re.findall(r"(?<![A-Za-z0-9_])([A-Za-z_]\w*)(?![A-Za-z0-9_])", text))


def _emit_global_invariant_assumptions(parsed_file: "ParsedCFile", config) -> list[str]:
    """Return ``__CPROVER_assume(...)`` statements for EVIDENCE-grounded global
    invariants derived from the source (bmc_agent/global_invariants.py).

    This is the proactive, deterministic counterpart to the realism-driven
    project clauses (Step 1.6): instead of waiting for a false-positive
    counterexample, it scans the TU's own global write-sets and emits
    ``g != NULL`` / ``g == K`` for const tables (proven) and init-set
    singletons (init-trusted, taint-gated). Off unless
    ``config.enable_global_invariants``.

    Only globals actually referenced by some function body in the parsed file
    are emitted, mirroring ``_emit_library_init_assumptions`` — a global
    defined in this TU is in scope, but emitting assumes for ones no harness
    touches is needless noise.
    """
    if not getattr(config, "enable_global_invariants", False):
        return []
    try:
        from bmc_agent.global_invariants import (
            extract_global_invariants, emit_assume_statements,
        )
        source = getattr(parsed_file, "preprocessed_source", None)
        if not source:
            path = getattr(parsed_file, "path", None)
            if path:
                with open(path, errors="replace") as fh:
                    source = fh.read()
        if not source:
            return []
        bodies = getattr(parsed_file, "function_bodies", None) or {}
        combined_bodies = "\n".join(b for b in bodies.values() if b)
        referenced = _re_global_idents(combined_bodies) if combined_bodies else None
        fn_param_names: dict[str, set[str]] = {}
        for fname, sig in (getattr(parsed_file, "functions", None) or {}).items():
            try:
                fn_param_names[fname] = {
                    pn for _, pn in sig.parameters if pn and pn.strip()
                }
            except Exception:
                continue
        invs = extract_global_invariants(
            source, referenced_names=referenced, fn_param_names=fn_param_names,
        )
        return emit_assume_statements(invs)
    except Exception as exc:  # never fail harness-gen on an optimization
        from bmc_agent.logger import get_logger
        get_logger("harness").debug("global-invariant extraction failed: %s", exc)
        return []


def _emit_dynamic_global_invariant_inits(
    parsed_file: "ParsedCFile", config, referenced: "set[str] | None",
) -> list[str]:
    """Return C statements that ALLOCATE init-trusted pointer globals in the
    DYNAMIC harness, so it doesn't spuriously crash on a NULL global that an
    init function would have set.

    The CBMC harness assumes ``g != NULL`` (Step 1.5c) for init-trusted globals;
    the dynamic harness includes the source global definition (``mem_root =
    NULL``) but never runs the init function, so a real callee that walks the
    global (``vfs_lookup`` over ``mem_root``) NULL-derefs and reports a false
    ``confirmed_dynamic``. Mirror the CBMC assumption concretely: for each
    init-trusted pointer global, ``if (!g) g = calloc(1, sizeof(*g));`` (zeroed
    => empty/valid object). Only ``init-trusted`` tier (proven const tables are
    already non-NULL; nothing to allocate).
    """
    if not getattr(config, "enable_global_invariants", False):
        return []
    try:
        from bmc_agent.global_invariants import extract_global_invariants
        source = getattr(parsed_file, "preprocessed_source", None)
        if not source:
            path = getattr(parsed_file, "path", None)
            if path:
                with open(path, errors="replace") as fh:
                    source = fh.read()
        if not source:
            return []
        out: list[str] = []
        for inv in extract_global_invariants(source, referenced_names=referenced):
            if inv.tier == "init-trusted" and inv.clause.endswith("!= NULL"):
                g = inv.name
                out.append(
                    f"    if (!{g}) {{ {g} = calloc(1, sizeof(*{g})); }}"
                    f"  /* init-trusted global: real code runs init() first */"
                )
        return out
    except Exception as exc:
        from bmc_agent.logger import get_logger
        get_logger("harness").debug("dynamic global-invariant init failed: %s", exc)
        return []


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
            # ALL_CAPS_WITH_UNDERSCORE identifiers are conventionally
            # ``#define``d constants or enum members — ggml's ``QK_K``,
            # ``QK4_0`` family, Linux's ``PAGE_SIZE``/``L1_CACHE_BYTES``,
            # etc. Real callers obey ``assert(k % QK_K == 0)``; the
            # bmc-agent harness, passing nondet params, would otherwise
            # explore the violating state and trip a "confirmed" bug
            # that is actually an internal precondition the function
            # imposes on its caller. Regression: ggml-cpu/quants.c run
            # 2026-05-19 raised 7 confirmed_dynamic findings of exactly
            # this shape on quantize_row_q5_K / q6_K / q4_K / iq4_nl /
            # iq4_xs / tq1_0 / tq2_0.
            non_param = {
                n for n in non_param
                if not (
                    "_" in n
                    and n.isupper()
                    and not n.startswith("_")  # exclude __builtin_*, _Static_*
                )
            }
            # Also accept identifiers introduced as ``static const int <n> = ...;``
            # or ``static const size_t <n> = ...;`` etc., before the assert.
            # ggml-quants.c uses ``static const int qk = QK_MXFP4;`` then
            # ``assert(k % qk == 0)`` — qk is lowercase but functionally a
            # compile-time constant. Without this, the assert is rejected and
            # the harness explores k-not-multiple-of-qk, tripping the assert
            # as a "confirmed bug" that is in fact an internal precondition.
            if non_param:
                _local_const_re = _re.compile(
                    r"\bstatic\s+const\s+(?:int|size_t|int8_t|int16_t|int32_t|int64_t|"
                    r"uint8_t|uint16_t|uint32_t|uint64_t|long|short|signed|unsigned)"
                    r"(?:\s+[a-z_]+)?\s+([A-Za-z_][A-Za-z0-9_]*)\s*="
                )
                # Scan only the prelude up to where we matched the assert.
                local_consts = set(_local_const_re.findall(body[:start]))
                non_param = non_param - local_consts
            if non_param:
                continue
            if expr in seen:
                continue
            seen.add(expr)
            out.append(f"__CPROVER_assume({expr});")
            continue
        # Allow ``static const <type> <name> = <expr>;`` declarations to
        # appear in the prelude — these are compile-time constants used
        # to anchor the precondition assert (ggml-quants pattern:
        # ``static const int qk = QK_MXFP4; assert(k % qk == 0);``).
        # Skip the declaration entirely so the scanner can continue
        # toward the assert.
        _const_decl = _re.match(
            r"static\s+const\s+(?:int|size_t|int8_t|int16_t|int32_t|int64_t|"
            r"uint8_t|uint16_t|uint32_t|uint64_t|long|short|signed|unsigned)"
            r"(?:\s+[a-z_]+)?\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^;]+;",
            body[pos:],
        )
        if _const_decl:
            pos += _const_decl.end()
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


# Kernel API name prefixes whose int-returning members follow the standard
# Linux ERRNO convention: return value is 0 on success, or a negative
# error code in roughly [-4095, -1] on failure. CBMC's default nondet
# stub allows arbitrary positive integers, which produces a large class
# of false positives when caller code branches on "success means N bytes
# transferred". Modelled after the recurring ch341 TODO from the
# 2026-05-18 sweep — generalises across USB / netdev / device-driver
# code that wraps these APIs.
_KERNEL_INT_API_PREFIXES: tuple[str, ...] = (
    # USB core
    "usb_control_msg", "usb_bulk_msg", "usb_interrupt_msg",
    "usb_submit_urb", "usb_unlink_urb", "usb_kill_urb",
    "usb_clear_halt", "usb_set_interface", "usb_reset_configuration",
    "usb_reset_device", "usb_autopm_get_interface", "usb_autopm_set_interface",
    "usb_register_dev", "usb_deregister",
    "usb_set_intfdata", "usb_serial_generic_write",
    # Kernel device / driver core
    "device_register", "device_add", "device_create_file",
    "driver_register", "driver_create_file",
    # Wait queue / IRQ
    "wait_event_interruptible", "wait_event_killable",
    "request_irq", "request_threaded_irq",
    # Misc int-returning APIs that follow the 0/-ERRNO convention
    "kstrtoint", "kstrtouint", "kstrtol", "kstrtoul",
    "copy_from_user", "copy_to_user",     # 0 on success, nonzero bytes-remaining on partial
    "get_user", "put_user",
)


def _kernel_api_return_contract(name: str, ret_type: str) -> list[str]:
    """Return ``__CPROVER_assume`` statements that constrain a kernel-API
    stub's int return value to follow the standard 0/-ERRNO convention:

        result == 0 (success) || result in [-4095, -1] (negative ERRNO).

    Empty list when the function isn't in the recognised API set or the
    return type isn't a plain int.

    Match is exact-name OR exact-prefix-of-name (so ``usb_control_msg``
    catches the ``usb_control_msg``, ``usb_control_msg_send``,
    ``usb_control_msg_recv`` family). This avoids false matches on
    unrelated names (a hypothetical ``usb_bulk_msg_buffer_size`` won't
    match because ``usb_bulk_msg`` is also a prefix of it — that's
    intentional; if it ever causes a problem we can switch to exact
    match).

    Two functions in the set (``copy_from_user`` / ``copy_to_user``)
    use a different convention — they return the number of BYTES NOT
    COPIED, so 0 = success and any positive value = partial copy. We
    accept this widening: constraining them to 0/-ERRNO would
    over-restrict and miss legitimate partial-copy bugs. We treat them
    the same as the other entries for now and revisit if it causes
    false positives.
    """
    rt = (ret_type or "").strip().rstrip("*").strip()
    # Include unsigned long / size_t for the copy_from_user / copy_to_user
    # family, which return a bytes-not-copied count (unsigned). Without
    # this, project-local wrappers like ``neuron_copy_from_user`` get
    # NO contract and return unconstrained nondet, which then violates
    # the caller's LLM-emitted POST that assumes "0 or negative" (kernel
    # driver convention). See findings/empirical_validity_protocol_2026-05-22.md.
    if rt not in (
        "int", "signed int", "long", "signed long",
        "ssize_t", "int32_t", "int64_t",
        "unsigned long", "size_t",
    ):
        return []
    matched = False
    for prefix in _KERNEL_INT_API_PREFIXES:
        # Forward match: ``name`` is exactly ``prefix`` or
        # ``prefix_<family-member-suffix>``.
        if name == prefix or (
            name.startswith(prefix)
            and (name[len(prefix):] == "" or name[len(prefix):].startswith("_"))
        ):
            matched = True
            break
        # Suffix match: project-local wrappers ``neuron_copy_from_user``,
        # ``__copy_to_user``, etc. inherit the contract of the wrapped
        # API. Require a leading ``_`` so we don't catch unrelated names
        # like ``mycopy_to_user`` whose semantics we don't know.
        if name.endswith("_" + prefix):
            matched = True
            break
    if not matched:
        return []
    # For SIGNED return types the constraint ``result <= 0 &&
    # result >= -4095`` cleanly encodes "0 or -ERRNO".
    # For UNSIGNED return types (size_t / unsigned long — used by
    # copy_from_user / copy_to_user), the literal ``-4095`` is
    # interpreted as the unsigned bit-pattern ``ULONG_MAX - 4094``;
    # then ``result <= 0 && result >= ULONG_MAX-4094`` is
    # UNSATISFIABLE, which turns the stub into
    # ``__CPROVER_assume(false)`` — silently pruning all callers
    # (and hiding real bugs downstream). Cast through signed so the
    # negative-ERRNO range is interpretable for either signedness.
    is_unsigned = rt in ("unsigned long", "size_t")
    if is_unsigned:
        return [
            "__CPROVER_assume(result == 0 || "
            "((signed long)result <= -1 && (signed long)result >= -4095));",
        ]
    return [
        "__CPROVER_assume(result <= 0 && result >= -4095);",
    ]


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
        # Linux kernel allocator family. All take (size, ...) as the
        # first arg and return a buffer of ``size`` writable bytes
        # (or NULL on OOM). The ``_noprof`` suffix is the
        # mem-alloc-profiling-disabled variant introduced in recent
        # kernels. Without these entries the harness sees kmalloc'd
        # buffers as unconstrained nondet pointers, producing spurious
        # R_OK assertion failures on the destination of copy_from_user
        # even when the real code is correct (see findings/
        # empirical_validity_protocol_2026-05-22.md).
        "kmalloc", "kmalloc_noprof", "__kmalloc_noprof",
        "kzalloc", "kzalloc_noprof",
        "vmalloc", "vmalloc_noprof", "vzalloc", "vzalloc_noprof",
        "devm_kmalloc", "devm_kzalloc",
    }
    calloc_like = {
        "calloc", "g_malloc_n", "g_malloc0_n",
        # Kernel (n, size) allocators.
        "kcalloc", "kcalloc_noprof",
        "kmalloc_array", "kmalloc_array_noprof",
        "kvmalloc_array", "kvmalloc_array_noprof",
        "vmalloc_array_noprof", "vzalloc_array_noprof",
        "devm_kcalloc",
    }
    realloc_like = {
        "realloc", "xmlRealloc", "OPENSSL_realloc",
        "g_realloc", "CRYPTO_realloc",
        "krealloc", "krealloc_noprof",
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
    #
    # Add a lower bound matching the kernel-ERRNO range (-4095). Without
    # the lower bound, CBMC is free to pick LONG_MIN-sized values whose
    # int truncation flips POSITIVE on assignment to ``int ret`` —
    # silently breaking FUT POSTs of the shape ``ret == 0 || ret < 0``.
    # The Linux convention is -ERRNO ∈ [-4095, -1], so the bound is
    # both sound and matches every real callee in the inferred family.
    if not has_positive:
        if has_zero and has_negative:
            return ["__CPROVER_assume(result <= 0 && result >= -4095);"]
        if has_zero and not has_negative:
            return ["__CPROVER_assume(result == 0);"]
        if has_negative and not has_zero:
            return ["__CPROVER_assume(result < 0 && result >= -4095);"]

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
        ptype_stripped = _strip_restrict_quals(ptype)
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


def _detect_naive_pairs(
    parsed_file: "ParsedCFile",
) -> list[tuple[str, str]]:
    """Detect ``(optimized, naive)`` function pairs in *parsed_file*.

    Returns a list of ``(opt_name, naive_name)`` tuples for each pair
    where:
      - both functions are defined in the file
      - their names match the pattern: ``<base>`` and ``<base>_naive``
      - their signatures are compatible: same return type, same number
        of parameters with matching positional names, parameter types
        equal up to ``const`` / ``volatile`` / ``restrict`` qualifier
        differences.

    Used by the M4 equivalence harness mode to verify that an
    optimized kernel agrees with its reference up to ulps at bounded
    input sizes. Classic example: llm.c ships ``matmul_forward`` and
    ``matmul_forward_naive`` side by side precisely for this kind of
    check.
    """
    pairs: list[tuple[str, str]] = []
    fn_names = set(parsed_file.functions.keys())

    def _normalise(t: str) -> str:
        t = re.sub(r"\b(const|volatile|restrict|__restrict)\b", "", t)
        return re.sub(r"\s+", " ", t).strip()

    for name in sorted(fn_names):
        if not name.endswith("_naive"):
            continue
        opt_name = name[:-len("_naive")]
        if opt_name not in fn_names:
            continue
        opt_sig = parsed_file.functions[opt_name]
        naive_sig = parsed_file.functions[name]
        if opt_sig.return_type.strip() != naive_sig.return_type.strip():
            continue
        if len(opt_sig.parameters) != len(naive_sig.parameters):
            continue
        compatible = True
        for (ot, on), (nt, nn) in zip(opt_sig.parameters, naive_sig.parameters):
            if (on or "").strip() != (nn or "").strip():
                compatible = False
                break
            if _normalise(ot) != _normalise(nt):
                compatible = False
                break
        if compatible:
            pairs.append((opt_name, name))
    return pairs


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


# Kernel framework back-pointer fields. The kernel-conventional rule is
# that these are populated during probe() before the driver registers
# its netdev/pci/phy callbacks, so any callback that the framework can
# dispatch sees them non-NULL. Limited to names whose meaning is
# unambiguous and well-established across drivers.
_NETDEV_BACKPOINTER_FIELD_NAMES = {
    "pci_dev",  # struct pci_dev * — set in pci_probe before alloc_netdev
    "netdev",   # struct net_device * — driver back-pointer to net_device
    "pdev",     # alias for pci_dev in many drivers
    "mii_bus",  # struct mii_bus * — set by mdio_alloc / probe
    "mmio_addr",  # void __iomem * — ioremap result, set in probe
    "phydev",   # struct phy_device * — set by phy_connect_direct
}

# Pointee types that legitimize assuming ``priv->dev != NULL``. ``dev``
# alone is a too-generic name to assume; require the field to point to
# the kernel's netdev/device structures (the canonical back-pointer
# convention).
_NETDEV_DEV_POINTEE_TYPES = {
    "struct net_device", "struct device", "struct pci_dev",
}

# Field-name shortlist for kernel MMIO-base pointer fields. The harness
# can't recover the actual BAR size from the source; we model it as a
# 4 KiB region, large enough for every Realtek r8125 register offset
# observed in practice (offsets up to ~0xF00). Drivers accessing
# registers outside this range will produce a genuine OOB pointer-
# arithmetic finding rather than a harness-modelling FP; tune this
# constant upward if a real driver legitimately uses larger offsets.
_MMIO_FIELD_NAMES = {
    "mmio_addr",
    # Common aliases observed across kernel NIC drivers:
    "hw_addr", "regs", "io_base",
}
_MMIO_BAR_REGION_BYTES = 4096

# Primitive pointee types whose ``sizeof(*p)`` is known to CBMC and to us,
# so we can safely allocate a backing array of ``cbmc_unwind+1`` elements
# for a struct's ``T *`` field under ``infer_field_validity``. Excludes
# ``char``, ``unsigned char``, ``uint8_t``, ``int8_t`` (already handled
# upstream as NUL-string / raw-byte buffers) and ``void`` (unknown size).
# Includes the common ML / numerics-codebase pointee shapes (float, double,
# the standard int widths).
_PRIMITIVE_POINTEE_TYPES = {
    "float", "double",
    "int", "unsigned int", "signed int",
    "long", "unsigned long", "signed long",
    "long long", "unsigned long long", "signed long long",
    "short", "unsigned short", "signed short",
    "int16_t", "uint16_t",
    "int32_t", "uint32_t",
    "int64_t", "uint64_t",
    "size_t", "ssize_t",
    "intptr_t", "uintptr_t",
    "ptrdiff_t",
}


def _emit_cast_chain_init(
    pname: str, obj_name: str, func_body: Optional[str] = None,
    struct_definitions: Optional[dict] = None,
    max_depth: int = 4,
) -> list[str]:
    """Emit typed allocations for ``(struct X *)(<pname>-><chain>)`` cast
    patterns found in *func_body*.

    Addresses the dominant wrong-struct-cast artifact in libarchive (and
    any framework that uses opaque ``void *`` data fields filled in by a
    separate registrar/factory function): the harness leaves nested
    struct-pointer field chains nondet, the function body then casts the
    chain's terminal field to a concrete struct pointer, and CBMC reports
    a NULL deref on the cast result because the harness never produced a
    valid object.

    Strategy: for each matching cast, emit one local struct for each
    chain element (one for each intermediate struct field, one for the
    cast-target type) and chain them via ``.field = &next_obj`` /
    ``(void *)&next_obj``. Order is from innermost to outermost so each
    backing exists when assigned.

    The emitted block uses unique-suffix names so multiple casts in the
    same function don't clash.

    Example for cab_checksum_finish (body has
    ``cab = (struct cab *)(a->format->data);``)::

        /* cast-chain init: a->format->data → struct cab */
        struct cab _a_castdata0_target;
        struct archive_format_descriptor _a_castdata0_field1;
        _a_castdata0_field1.data = (void *)&_a_castdata0_target;
        _a_obj.format = &_a_castdata0_field1;

    Returns ``[]`` if no cast pattern matches or func_body is empty.
    """
    if not func_body or not pname or not obj_name:
        return []
    # Match: (struct X *) ( pname -> field1 -> field2 -> ... -> fieldN )
    # Where the inner expression starts at pname-> and is a chain of
    # field accesses. Up to 4 chain hops kept conservative.
    chain_re = re.compile(
        r"\(\s*struct\s+(\w+)\s*\*\s*\)\s*\(\s*"
        + re.escape(pname)
        + r"\s*->\s*(\w+(?:\s*->\s*\w+){0,3})\s*\)"
    )
    out: list[str] = []
    seen_chains: set[tuple[str, tuple[str, ...]]] = set()
    for idx, m in enumerate(chain_re.finditer(func_body)):
        target_struct = m.group(1)
        chain_str = m.group(2)
        chain = tuple(s.strip() for s in chain_str.split("->") if s.strip())
        if not chain:
            continue
        key = (target_struct, chain)
        if key in seen_chains:
            continue
        seen_chains.add(key)
        target_var = f"_{pname}_castchain{idx}_target"
        out.append(
            f"    /* cast-chain init: {pname}->{chain_str} → struct {target_struct} */"
        )
        out.append(f"    struct {target_struct} {target_var};")
        # Walk the chain inside-out. The terminal field assigns to
        # &target_var. Intermediate fields assign to &next_intermediate.
        if len(chain) == 1:
            # Single hop: pname->field is the cast target.
            # ALWAYS non-NULL: libarchive's framework guarantees outer
            # struct fields are non-NULL when callbacks are dispatched.
            # The leaf-level bugs (e.g. cfdata->memimage NULL) surface
            # via nondet fields of the typed backing, not via outer-NULL.
            field = chain[0]
            out.append(
                f"    {obj_name}.{field} = (struct {target_struct} *)&{target_var};"
            )
            continue
        # Multi-hop: build intermediate struct backings.
        # The LAST chain element holds the target as ``void *`` (typical
        # for libarchive's ``data`` field) or as a typed pointer.
        # We don't know each intermediate type, so we emit forward
        # declarations of the chain via the field assignments — the
        # backing structs themselves use OPAQUE typedefs that the C
        # compiler resolves from the surrounding TU.
        last_field = chain[-1]
        prev = target_var
        # Emit intermediate backings in reverse order. We need the type
        # of each intermediate struct. Without struct_definitions, we
        # can't know it, so emit a generic-pointer chain via
        # ``__CPROVER_assume`` on field-non-NULL and rely on the cast.
        # ACTUALLY simpler: for chains of length >= 2, the typical
        # pattern is ``a->format->data`` where ``format`` is a struct
        # pointer (typed) and ``data`` is ``void *``. Build one
        # intermediate backing of the right type by best-effort regex.
        intermediates: list[tuple[str, str]] = []  # (varname, type_decl)
        # For each intermediate hop, we need the struct type holding
        # that field. We don't have it locally — fall back to a void *
        # link via __CPROVER_assume on field validity. Best-effort:
        # emit a struct-of-unknown-tag backing the C compiler will
        # resolve.
        # In practice the most common libarchive shape is exactly
        # 2-deep (``a->format->data``). Handle that as a special case
        # using ``struct archive_format_descriptor`` (only valid when
        # pname's type is ``struct archive_read *``). Other chains
        # use a void *-cast workaround.
        if len(chain) == 2 and chain[-1] == "data":
            # Libarchive-specific shape — ALWAYS non-NULL chain.
            # Framework guarantees a->format and format->data are valid
            # when format-reader callbacks are dispatched. Leaf-level
            # bugs (cfdata->memimage NULL etc.) surface via nondet
            # fields of the typed backing struct.
            mid_field, terminal_field = chain
            mid_var = f"_{pname}_castchain{idx}_fmt"
            out.append(f"    struct archive_format_descriptor {mid_var};")
            out.append(f"    {mid_var}.{terminal_field} = (void *)&{target_var};")
            out.append(f"    {obj_name}.{mid_field} = &{mid_var};")
        else:
            # Generic shallow assignment: skip — the chain depth or
            # intermediate types aren't safe to guess. Fall back to a
            # single-hop init at the outermost field only.
            out = out[:-2]  # discard the comment + target_var decl
            continue

        # DEEP CHAIN EXPANSION: after the 2-hop cast-chain landed,
        # walk target_var's pointer-to-struct fields that are dereffed
        # in func_body. For each, emit a typed backing recursively up
        # to MAX_DEPTH levels. Addresses seed bugs where the buggy state
        # is N hops deep (e.g. seed `32b62cf7`: cab_checksum_finish derefs
        # `cab->entry_cfdata->memimage`. With entry_cfdata nondet, CBMC
        # crashes at the first deref; with entry_cfdata as typed backing,
        # CBMC reaches memimage and can explore memimage=NULL — the
        # seed-bug-trigger state).
        if not struct_definitions:
            continue
        from collections import deque
        worklist = deque([(target_struct, target_var, 0)])
        emitted_pairs = set()  # (struct_tag, field) to avoid duplicate work
        MAX_DEPTH = max_depth
        while worklist:
            cur_tag, cur_var, depth = worklist.popleft()
            if depth >= MAX_DEPTH:
                continue
            fields = struct_definitions.get(cur_tag) or []
            for ftype, fname in fields:
                # Only expand pointer-to-struct fields.
                t = ftype.strip()
                if t.count("*") != 1:
                    continue
                base = re.sub(r"\bconst\b", "", t[:-1]).strip()
                if not (base.startswith("struct ") or base.startswith("union ")):
                    continue
                inner_tag = _resolve_struct_name(base, struct_definitions)
                if not inner_tag or inner_tag == cur_tag:
                    continue  # skip unresolvable or self-ref
                # Only emit if the function body actually accesses this
                # chain (avoid allocating unused fields).
                if f"{cur_var.lstrip('_')}.{fname}" not in func_body and \
                   f"->{fname}" not in func_body:
                    continue
                pair_key = (cur_tag, fname)
                if pair_key in emitted_pairs:
                    continue
                emitted_pairs.add(pair_key)
                inner_var = f"_{cur_var}_{fname}"
                out.append(
                    f"    /* deep-chain: {cur_var}.{fname} → struct {inner_tag} */"
                )
                out.append(f"    struct {inner_tag} {inner_var};")
                # Always non-NULL for deep-chain backings: if we used
                # disjunctive here, CBMC would pick the all-NULL
                # assignment for every property (cheapest crash) and the
                # leaf-level seed-bug states (e.g. memimage=NULL with the
                # chain otherwise valid) would never be explored. The
                # outer cast-chain stays disjunctive — that's where the
                # "real caller might pass NULL" discovery happens. Inner
                # backings model the framework's invariant: once dispatch
                # reaches the callback with a valid outer struct, the
                # registrar-set inner pointers are also valid.
                out.append(
                    f"    {cur_var}.{fname} = &{inner_var};"
                )
                worklist.append((inner_tag, inner_var, depth + 1))
    return out


_FUNCPTR_TYPE_SUFFIXES = ("proc", "func", "callback", "handler", "hook")


def _is_funcptr_field_type(t: str) -> bool:
    """Heuristic: does this struct-field type denote a function pointer?

    Matches explicit ``ret (*)(...)`` syntax and conventional callback
    typedef names (``*Proc``, ``*Func``, ``*Callback``, ``*Handler``,
    ``*Hook`` — e.g. libtiff's ``TIFFReadWriteProc``/``TIFFSeekProc``).
    Deliberately conservative: a wrong match only suppresses an
    (almost always spurious) NULL-call dereference, it never invents a bug.
    Ambiguous suffixes like ``_ptr``/``fn``/``cb`` are excluded so we don't
    over-assume on data-pointer typedefs and hide a real NULL-data deref.
    """
    s = t.strip().lower()
    if "(*" in s:  # explicit function-pointer declarator
        return True
    ident = re.sub(r"\bconst\b", "", s).strip()
    if " " in ident or "*" in ident or not ident:
        return False  # not a bare typedef name
    return ident.endswith(_FUNCPTR_TYPE_SUFFIXES)


def _funcptr_stub_def(typedef_name: str, source_text: str) -> Optional[str]:
    """Resolve ``typedef RET (*NAME)(PARAMS);`` from source and emit a matching
    CBMC stub ``static RET _cbmc_fpstub_NAME(PARAMS){ RET _r; return _r; }``.

    A callback struct field assigned this stub gives CBMC a signature-matched
    call candidate. Proven necessary: an ``__CPROVER_assume(field != NULL)``
    alone leaves CBMC with "no candidates for dereferenced function pointer"
    and the call deref still FAILs (2026-05-30). Params are copied verbatim
    (CBMC accepts unnamed params in a definition). Returns None if the typedef
    can't be resolved (caller then falls back to the non-NULL assume).
    """
    m = re.search(
        r"typedef\s+(?P<ret>[A-Za-z_][\w\s\*]*?)\(\s*\*\s*"
        + re.escape(typedef_name)
        + r"\s*\)\s*\((?P<params>[^;{}]*)\)\s*;",
        source_text,
    )
    if not m:
        return None
    ret = re.sub(r"\s+", " ", m.group("ret").strip())
    params = m.group("params").strip() or "void"
    name = f"_cbmc_fpstub_{typedef_name}"
    if ret == "void":
        return f"static void {name}({params}) {{ (void)0; }}"
    return f"static {ret} {name}({params}) {{ {ret} _r; return _r; }}"


def _inject_funcptr_stubs(harness_text: str, source_text: str) -> str:
    """Post-pass: for every ``_cbmc_fpstub_<NAME>`` referenced in the harness,
    inject a signature-matched stub definition (resolved from source) at file
    scope. Any NAME whose typedef can't be resolved has its assignment line
    rewritten to the harmless ``__CPROVER_assume(<lhs> != NULL)`` fallback."""
    refs = set(re.findall(r"_cbmc_fpstub_(\w+)", harness_text))
    if not refs:
        return harness_text
    # Resolve typedefs from the harness itself first (it embeds the type
    # declarations, so it is self-contained even when source_text is only the
    # .c file and the callback typedefs live in an included header), then fall
    # back to source_text.
    resolve_src = harness_text + "\n" + (source_text or "")
    defs: list[str] = []
    for nm in sorted(refs):
        d = _funcptr_stub_def(nm, resolve_src)
        if d:
            defs.append(d)
        else:
            harness_text = re.sub(
                r"([ \t]*)([\w.\[\]>-]+)\s*=\s*_cbmc_fpstub_" + re.escape(nm) + r"\s*;[^\n]*",
                r"\1__CPROVER_assume(\2 != NULL);",
                harness_text,
            )
    if not defs:
        return harness_text
    block = (
        "/* --- D-ii: signature-matched callback stubs (assigned to nondet\n"
        "   function-pointer struct fields so CBMC has a valid call candidate) --- */\n"
        + "\n".join(defs) + "\n\n"
    )
    idx = harness_text.rfind("void main(void)")
    return block + harness_text if idx == -1 else harness_text[:idx] + block + harness_text[idx:]


def _emit_struct_field_init(
    obj_name: str, ftype: str, fname: str, cbmc_unwind: int,
    enclosing_struct_tag: Optional[str] = None,
    infer_field_validity: bool = False,
    infer_struct_field_validity: bool = False,
    struct_definitions: Optional[dict] = None,
    func_body: Optional[str] = None,
    copy_field_maxlen: Optional[dict] = None,
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
    # Function-pointer / callback fields (D-ii: caller-established precondition).
    # A handle's callback fields are installed by its constructor/open routine
    # before any method runs — e.g. TIFF.tif_readproc/tif_seekproc are set by
    # TIFFClientOpen (tif_open.c) before any directory read, and TIFFClientOpen
    # rejects a NULL readproc. Leaving them nondet lets CBMC explore the
    # impossible NULL state and report a spurious NULL-call dereference — the
    # dominant G4 false positive (tif_readproc trio adjudicated FP 2026-05-29).
    # Assume non-NULL. Conservative: a wrong match only suppresses an
    # (almost always spurious) NULL-call deref, it can never invent a bug.
    if _is_funcptr_field_type(t):
        ts = re.sub(r"\bconst\b", "", t).strip()
        # Bare typedef name -> assign a signature-matched stub (defined at file
        # scope by _inject_funcptr_stubs). An `!= NULL` assume is INSUFFICIENT
        # for a called function pointer (CBMC: "no candidates"). Explicit
        # `(*)(...)` / qualified forms fall back to the harmless assume.
        if re.fullmatch(r"[A-Za-z_]\w*", ts):
            return [f"    {obj_name}.{fname} = _cbmc_fpstub_{ts};"
                    f"  /* D-ii: matched callback stub */"]
        return [f"    __CPROVER_assume({obj_name}.{fname} != NULL);"
                f"  /* callback field non-NULL (D-ii precondition) */"]
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
        # Kernel framework back-pointers set unconditionally during
        # probe() before register_netdev/register_pci_driver makes the
        # device visible to any registered ndo_*/ethtool_ops/phy
        # callback. Assume non-NULL so CBMC doesn't explore the
        # unreachable NULL state that produces spurious "NULL deref on
        # tp->pci_dev / tp->dev" reports (rtl8125 OOT batch,
        # 2026-05-18). The list is intentionally short and
        # name-anchored to fields whose kernel-conventional NAME is the
        # back-pointer; we avoid blanket-assuming for generic field
        # names. ``dev`` is qualified by pointee type to avoid
        # over-constraining unrelated fields named ``dev`` whose type
        # is something else (e.g. driver-specific opaque struct).
        if (
            fname in _NETDEV_BACKPOINTER_FIELD_NAMES
            or (fname == "dev" and base in _NETDEV_DEV_POINTEE_TYPES)
        ):
            # CBMC's pointer_dereference property checks 5 subtypes:
            # NULL, deallocated, dead, out-of-bounds, invalid-int-addr.
            # ``!= NULL`` alone fixes only subtype #1. Use ``r_ok`` so
            # the kernel framework invariant is modelled as "set to a
            # valid object" — closer to the actual probe() postcondition.
            #
            # MMIO pointers (``void *`` or ``void __iomem *``) need
            # special treatment: ``sizeof(*p)`` on ``void *`` is 1 byte
            # (GCC extension), so the region's read-bound is 1 and any
            # register access at offset > 1 looks OOB. Drivers
            # legitimately access registers across the whole BAR (4 KiB
            # or larger). Give MMIO-named fields a typical-BAR-sized
            # backing region instead (rtl8125_dash round-2 FP, 2026-05-18:
            # rtl8125_clear_ipc2_soc_imr_bit accesses offset 0xD20 on a
            # 4 KiB BAR2, which the 1-byte region rejected as OOB).
            base_clean = re.sub(r"\b(const|volatile|__iomem)\b", "", base).strip()
            is_void_pointer = (base_clean == "void")
            if is_void_pointer and fname in _MMIO_FIELD_NAMES:
                bar_size = _MMIO_BAR_REGION_BYTES
                backing_name = f"_{obj_name}_{fname}_iomem"
                out.append(
                    f"    /* MMIO backing region for {obj_name}.{fname} "
                    f"({bar_size} bytes, typical BAR size) */"
                )
                out.append(f"    static char {backing_name}[{bar_size}];")
                out.append(
                    f"    {obj_name}.{fname} = (void *){backing_name};"
                )
                return out
            out.append(
                f"    __CPROVER_assume({obj_name}.{fname} != NULL && "
                f"__CPROVER_r_ok({obj_name}.{fname}, "
                f"sizeof(*{obj_name}.{fname})));  "
                f"/* framework back-pointer set in probe() */"
            )
            return out
        buf_size = cbmc_unwind + 1
        backing = f"_{obj_name}_{fname}_buf"
        if base == "char":
            # NUL-terminated string backing for char *.
            # Copy-sink SOURCE field: widen so a strcpy/strcat into a smaller
            # fixed buffer can overflow (FN dual of (buf,len)).
            str_max = cbmc_unwind
            if copy_field_maxlen and copy_field_maxlen.get(fname, 0) > str_max:
                str_max = copy_field_maxlen[fname]
                out.append(f"    /* copy-sink source field '{fname}': widened to {str_max} chars */")
            len_var = f"_{obj_name}_{fname}_len"
            out.append(f"    char {backing}[{str_max + 1}];")
            out.append(f"    unsigned int {len_var};")
            out.append(
                f"    __CPROVER_assume({len_var} <= (unsigned int){str_max});"
            )
            out.append(f"    {backing}[{len_var}] = '\\0';")
            out.append(f"    {obj_name}.{fname} = {backing};")
        elif base in ("unsigned char", "uint8_t", "int8_t"):
            # Raw byte buffer for binary fields.
            btype = "unsigned char" if base != "int8_t" else "signed char"
            out.append(f"    {btype} {backing}[{buf_size}];")
            out.append(f"    {obj_name}.{fname} = ({t}){backing};")
        elif (
            struct_definitions
            and (base.startswith("struct ") or base.startswith("union "))
            and _resolve_struct_name(base, struct_definitions)
        ):
            # Typed recursive struct-pointer init. When the pointee struct
            # body is in struct_definitions, allocate a typed backing
            # struct (non-NULL by construction) instead of leaving the
            # field nondet OR using the byte-buffer disjunctive init.
            # This matches how real callers always have these pointer
            # fields pointing to a valid allocated object (e.g.
            # archive_read_support_format_iso9660 sets
            # ``a->format->data = calloc(..., struct iso9660)`` before
            # any format-reader callback runs). Without this, libarchive
            # format-vtable callbacks (parse_rockridge, isJolietSVD,
            # init_unpack, lzx_huffman_init) hit spurious CBMC NULL-deref
            # on the field, which the classifier correctly rejects as
            # unreachable — but the harness state IS reachable in real
            # code, so the seed bug downstream is also lost.
            inner_tag = _resolve_struct_name(base, struct_definitions)
            inner_obj = f"_{obj_name}_{fname}_obj"
            base_for_decl = re.sub(r"^\s*const\s+", "", base).strip()
            out.append(
                f"    /* typed backing for struct-pointer field "
                f"{obj_name}.{fname} ({base})  */"
            )
            out.append(f"    {base_for_decl} {inner_obj};")
            out.append(f"    {obj_name}.{fname} = &{inner_obj};")
            # Recurse one level — bound length fields, terminate self-refs.
            inner_fields = struct_definitions.get(inner_tag) or []
            for f_t, f_n in inner_fields:
                # Recursion guard: pass struct_definitions=None to inner
                # call so we don't unbounded-recurse on cyclic struct
                # graphs (e.g. ``struct archive_read`` contains a
                # ``struct archive_format_descriptor *`` which contains
                # a ``struct archive *`` etc.). One level is enough to
                # unblock the dominant seed-bug pattern.
                out.extend(
                    _emit_struct_field_init(
                        inner_obj, f_t, f_n, cbmc_unwind,
                        enclosing_struct_tag=inner_tag,
                        infer_field_validity=infer_field_validity,
                        infer_struct_field_validity=infer_struct_field_validity,
                        struct_definitions=None,  # NO further recursion
                        func_body=None,
                        copy_field_maxlen=copy_field_maxlen,
                    )
                )
            return out
        elif (
            func_body
            and t.strip() in ("void *", "const void *")
            and infer_struct_field_validity
        ):
            # void *data field — scan the function body for a cast pattern
            # like ``(struct X *)(<param>->data)`` and use X as the typed
            # backing if found. This addresses the format-data idiom in
            # libarchive: every format reader does
            # ``X *x = (struct X *)(a->format->data);`` where X is
            # format-specific (struct iso9660, struct cab, struct rar5,
            # struct cpio). Without this, CBMC explores ``->data == NULL``
            # which no real caller produces.
            cast_pat = re.compile(
                r"\(\s*struct\s+(\w+)\s*\*\s*\)\s*\(?\s*\w+\s*->\s*"
                + re.escape(fname) + r"\b"
            )
            m = cast_pat.search(func_body)
            if m and struct_definitions and m.group(1) in struct_definitions:
                inner_tag = m.group(1)
                inner_obj = f"_{obj_name}_{fname}_obj"
                out.append(
                    f"    /* typed-cast backing for void * field "
                    f"{obj_name}.{fname}: '(struct {inner_tag} *)' */"
                )
                out.append(f"    struct {inner_tag} {inner_obj};")
                out.append(f"    {obj_name}.{fname} = (void *)&{inner_obj};")
                inner_fields = struct_definitions.get(inner_tag) or []
                for f_t, f_n in inner_fields:
                    out.extend(
                        _emit_struct_field_init(
                            inner_obj, f_t, f_n, cbmc_unwind,
                            enclosing_struct_tag=inner_tag,
                            infer_field_validity=infer_field_validity,
                            infer_struct_field_validity=infer_struct_field_validity,
                            struct_definitions=None,
                            func_body=None,
                            copy_field_maxlen=copy_field_maxlen,
                        )
                    )
                return out
        elif infer_struct_field_validity and (
            base.startswith("struct ") or base.startswith("union ")
        ):
            # M1.3 — struct-pointer field validity. Mirror the M1
            # disjunctive init pattern but use a fixed-size byte buffer
            # (256 bytes) cast to the struct pointer type instead of
            # ``sizeof(struct X)``. This avoids the "incomplete type"
            # error CBMC raises for forward-declared structs.
            #
            # Real bug class this addresses: most kernel-driver FAILs
            # this session were ``ptr->field->subfield`` chains where
            # the harness left struct-pointer fields nondet. Examples:
            # ``galloc->hash_values[i]`` in ggml-alloc, ``ctx->state``
            # in dma_ctx, ``dma_ctx->ring->qid`` in neuron_dma.
            #
            # Conservative scope: only applied to struct/union pointer
            # fields under ``infer_field_validity``. Function pointers,
            # array-of-pointer fields, and complex generic templates
            # remain nondet.
            sel_var = f"_{obj_name}_{fname}_is_null"
            backing_ptr = f"{backing}_p"
            out.append(
                f"    unsigned char *{backing_ptr} = "
                f"(unsigned char *)malloc(256);"
            )
            out.append(f"    __CPROVER_assume({backing_ptr} != NULL);")
            out.append(f"    unsigned char {sel_var};")
            out.append(
                f"    {obj_name}.{fname} = {sel_var} ? "
                f"({t})0 : ({t}){backing_ptr};"
            )
        elif infer_field_validity and base in _PRIMITIVE_POINTEE_TYPES:
            # Disjunctive NULL-or-backing init for primitive-pointer
            # fields. Without this, CBMC's nondet model picks "non-NULL
            # but invalid" for ``float *``, ``int *``, ``double *`` etc.,
            # which causes any deref inside an ``if (field != NULL)`` guard
            # to fail despite the source having a correct guard. The
            # disjunctive init explores both: NULL (function skips) AND
            # valid backing (function's deref succeeds). Size is
            # ``cbmc_unwind + 1`` elements -- matches the bound used
            # elsewhere for nondet length fields, so kernels iterating
            # ``for (i=0; i<num_parameters; i++) f(field[i])`` stay in
            # bounds when ``num_parameters`` was also bounded by the
            # length-field heuristic above.
            #
            # Backing is allocated via ``malloc`` (not a stack array) so
            # ``free(field)`` patterns ALSO verify -- CBMC requires the
            # argument to ``free`` be a dynamic object. A stack-array
            # backing would correctly verify memset/memcpy patterns but
            # would fail ``free`` with "free argument must be dynamic
            # object". Real motivation: llm.c ships ``gpt2_zero_grad``
            # (memset pattern) AND ``gpt2_free`` (free pattern) side by
            # side; both must verify cleanly under the same field-init.
            # The malloc cost is symbolic-only (no real allocation
            # happens in CBMC's symbolic execution), so size impact is
            # nil.
            sel_var = f"_{obj_name}_{fname}_is_null"
            backing_ptr = f"{backing}_p"
            out.append(
                f"    {t}{backing_ptr} = "
                f"({t})malloc(sizeof({base}) * {buf_size});"
            )
            out.append(f"    __CPROVER_assume({backing_ptr} != NULL);")
            out.append(f"    unsigned char {sel_var};")
            out.append(
                f"    {obj_name}.{fname} = {sel_var} ? "
                f"({t})0 : {backing_ptr};"
            )
        # Other pointer types (void *, struct pointers, function pointers,
        # arrays of pointers) stay nondet — modelling them would risk
        # over-constraining or compile errors on incomplete types.
        return out

    # Integer / size fields with length-suggesting names. Only emit
    # the bound if the field's TYPE is a primitive integer — a name
    # match against a struct/union field (``winsize_mutex`` is a
    # ``struct mutex``; ``winsize`` is a ``struct winsize``) would
    # produce an invalid ``__CPROVER_assume(<struct> >= 0)`` and
    # CBMC errors with ``implicit arithmetic conversion not
    # permitted``.
    if _is_likely_length_field(fname) and _looks_like_integer_type(t):
        out.append(
            f"    __CPROVER_assume({obj_name}.{fname} >= 0 && "
            f"{obj_name}.{fname} <= (long)({cbmc_unwind}));"
        )
    return out


def _looks_like_integer_type(t: str) -> bool:
    """Return True if *t* names a primitive integer type, so a
    ``__CPROVER_assume(x >= 0 && x <= N)`` against it is meaningful.
    Returns False for struct/union types (where the assume would be a
    type error) and for arrays, function pointers, etc.

    We're conservative: only the names we recognise as integer
    primitives return True. Unknown typedef names (which COULD be
    integer aliases) return False so we don't risk an invalid assume.
    """
    s = re.sub(r"\b(const|volatile|register|signed|unsigned)\b", "", t).strip()
    # Pointer / array / function-pointer types are never length fields
    # for our purposes.
    if "*" in s or "[" in s or "(" in s:
        return False
    # struct / union / enum prefixes — definitely aggregate types.
    if re.match(r"\b(struct|union|enum)\b", s):
        return False
    # Recognised integer primitives and common kernel/POSIX aliases.
    s = s.strip()
    return s in {
        "char", "short", "int", "long", "long long",
        "size_t", "ssize_t", "ptrdiff_t",
        "int8_t", "int16_t", "int32_t", "int64_t",
        "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        # Kernel primitives — preserved through the kernel-mode glibc
        # strip and used in driver struct field declarations.
        "__u8", "__u16", "__u32", "__u64",
        "__s8", "__s16", "__s32", "__s64",
        "u8", "u16", "u32", "u64",
        "s8", "s16", "s32", "s64",
        "__be16", "__be32", "__be64",
        "__le16", "__le32", "__le64",
    }


# ML-shaped size parameter names recognised by the scale-down mode.
# Matches both the conventional terse 1-3 letter names used in C ML
# code (Karpathy llm.c, GPT-NeoX C, ggml) and the longer
# self-explanatory variants used in Rust ports / production code.
_ML_SIZE_PARAM_NAMES = {
    "B", "T", "C", "NH", "V", "Vp", "OC", "N", "L", "H", "D",
    "n_dims", "n_heads", "n_layers", "n_tokens",
    "batch_size", "seq_len", "num_heads", "channels",
    "vocab_size", "padded_vocab_size", "num_layers",
    "num_tokens", "num_parameters", "num_activations",
}
_INT_PARAM_TYPES = {
    "int", "unsigned int", "signed int",
    "long", "unsigned long", "signed long",
    "short", "unsigned short", "signed short",
    "size_t", "ssize_t", "ptrdiff_t",
    "int32_t", "uint32_t", "int64_t", "uint64_t",
}


def _scale_down_assumes(func: FunctionInfo, size_limit: int) -> list[str]:
    """Emit __CPROVER_assume statements that bound ML-shaped int value
    parameters to [0, size_limit]. Detects parameter names matching the
    ML parametric-size convention (B, T, C, NH, V, Vp, OC, ...) AND
    whose type is a plain integer (not a pointer). Returns a list of
    bare assume strings (caller adds indentation).
    """
    if size_limit <= 0:
        return []
    out: list[str] = []
    for ptype, pname in func.signature.parameters:
        if not pname or pname not in _ML_SIZE_PARAM_NAMES:
            continue
        t = ptype.strip()
        # Skip pointer params; only bound value parameters.
        if "*" in t:
            continue
        # Normalise the type to one of our known integer shapes.
        t_norm = re.sub(r"\s+", " ", t).strip()
        if t_norm not in _INT_PARAM_TYPES:
            continue
        out.append(
            f"__CPROVER_assume({pname} >= 0 && {pname} <= {size_limit});"
        )
    return out


# Per-token restrict-like qualifiers that appear between the base
# type and the pointer ``*`` in C source. Stripping them before
# pointer detection means ``const float * GGML_RESTRICT x`` is
# recognised as a pointer parameter (without the strip the type
# string ends in ``GGML_RESTRICT`` and the harness emits a value
# parameter, leaving ``x`` as an uninitialised wild pointer).
_RESTRICT_LIKE_QUALIFIERS = {
    "restrict", "__restrict", "__restrict__",
    "GGML_RESTRICT",  # ggml-specific macro
    "KANI_RESTRICT",  # Kani helper macro
    "__pure", "__const",
}


def _strip_restrict_quals(ptype: str) -> str:
    """Strip restrict-like qualifiers and attribute macros from a
    parameter type string. Preserves ``const`` / ``volatile`` (handled
    separately in downstream paths). Idempotent."""
    out = ptype
    for q in _RESTRICT_LIKE_QUALIFIERS:
        out = re.sub(rf"\b{re.escape(q)}\b", "", out)
    # Collapse whitespace.
    return re.sub(r"\s+", " ", out).strip()


def _max_literal_subscript(body: str, pname: str) -> Optional[int]:
    """Return the maximum *constant* integer subscript on ``pname[K]`` in
    ``body``, or None if no literal subscripts are present.

    Conservative: only matches integer literals (decimal, hex, octal),
    not constant expressions or macros. ``param[0]`` through
    ``param[15]`` resolves to 15; ``param[i]``, ``param[B*T]`` are
    skipped. Used to size the harness backing buffer for top-level
    pointer params like llm.c's ``fill_in_parameter_sizes(size_t*
    param_sizes, ...)`` which writes ``param_sizes[0..15]`` against a
    fixed-size table.
    """
    # Strip strings/chars/comments to avoid false matches inside literals.
    body_clean = _strip_c_comments(body or "")
    if not body_clean:
        return None
    # Match `pname[K]` where K is an integer literal. Allow surrounding
    # whitespace. Anchor on a word boundary on the left so longer names
    # ending in pname aren't matched.
    pat = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(pname) +
        r"\s*\[\s*(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)\s*[uUlL]*\s*\]"
    )
    max_idx: Optional[int] = None
    for m in pat.finditer(body_clean):
        tok = m.group(1)
        try:
            n = int(tok, 0)  # auto-detects 0x / 0 prefixes
        except ValueError:
            continue
        if max_idx is None or n > max_idx:
            max_idx = n
    return max_idx


_BYTE_TYPE_LITERALS = {
    "char", "unsigned char", "signed char",
    "uint8_t", "int8_t", "u_char", "uchar", "byte",
}
# Project byte typedefs by naming convention: stbtt_uint8, png_byte, u8, s8,
# my_uint8_t, ... End-anchored so uint16/uint32 (multi-byte) never match.
_BYTE_TYPEDEF_RE = re.compile(
    r"(?:^|_)(?:u?int8(?:_t)?|[su]8|byte|uchar)$", re.IGNORECASE
)


def _is_byte_shaped_type(base_type: str) -> bool:
    """True if ``base_type`` is a 1-byte char/uint8 type, directly OR via a
    common project typedef (``stbtt_uint8`` = ``unsigned char``, etc.).

    The harness's byte-pointer branch (sized backing buffer) keyed only on the
    literal type names, so a ``T *`` whose ``T`` is a byte TYPEDEF fell through
    to the generic single-SCALAR default — under-sizing accessor params like
    ``ttUSHORT(stbtt_uint8 *p)`` that read ``p[0..k]`` (see the vibeos ttf
    calibration). Resolving the typedef routes them to a real buffer.
    """
    b = re.sub(r"\bconst\b", "", base_type or "").strip()
    if b in _BYTE_TYPE_LITERALS:
        return True
    return bool(_BYTE_TYPEDEF_RE.search(b))


_BUF_LEN_SIZE_NAMES = {
    "len", "length", "size", "n", "nbytes", "buflen", "bufsize", "datalen",
    "data_len", "msglen", "pktlen", "count", "num", "nmemb", "sz",
}


def _detect_buf_len_pairs(parameters) -> "list[tuple[str, str, str, str]]":
    """Find ``(byte-buffer, length)`` parameter pairs by the dominant C
    convention: a single-indirection BYTE pointer IMMEDIATELY followed by a
    size-named integer — ``icmp_handle(const uint8_t *pkt, uint32_t len)``,
    ``parse(const uint8_t *data, size_t n)``. Returns
    ``(buf_name, buf_type, len_name, len_type)`` tuples.

    Adjacency + a size name keeps pairing unambiguous (no guessing which length
    binds which buffer in multi-arg signatures). Restricted to BYTE-shaped
    pointers (the memcpy/parser case where ``len`` is a byte count); element
    arrays / struct pointers are left to ``infer_array_param_bounds``.
    """
    params = [(pt.strip(), pn) for pt, pn in parameters if pn]
    pairs: list[tuple[str, str, str, str]] = []
    for i in range(len(params) - 1):
        ptype, pname = params[i]
        ltype, lname = params[i + 1]
        st = _strip_restrict_quals(ptype)
        if st.count("*") != 1:           # single indirection only
            continue
        base = re.sub(r"\bconst\b", "", st.rstrip("*")).strip()
        # RAW-byte pointers only (uint8_t*/unsigned char*/int8_t*/byte typedefs).
        # Plain ``char *`` is the NUL-terminated-string convention, NOT an
        # n-byte buffer — pairing it with a count would wrongly assume the
        # string spans `len` bytes. Mirrors the raw/textual split in the
        # single-pointer byte branch.
        if base in ("char", "wchar_t") or not _is_byte_shaped_type(base):
            continue
        if "*" in ltype or lname.lower() not in _BUF_LEN_SIZE_NAMES:
            continue
        pairs.append((pname, st, lname, ltype.strip()))
    return pairs


# Scalar ELEMENT pointee types for (elem-buf, len) coupling. Excludes char/void
# and byte-shaped types (handled by the string / _detect_buf_len_pairs paths) and
# struct/union pointers (left to infer_array_param_bounds).
_SCALAR_ELEM_TYPES = {
    "int", "unsigned", "unsigned int", "signed", "signed int",
    "short", "unsigned short", "short int", "unsigned short int",
    "long", "unsigned long", "long int", "unsigned long int",
    "long long", "unsigned long long", "float", "double", "long double",
    "size_t", "ssize_t", "ptrdiff_t",
    "int16_t", "uint16_t", "int32_t", "uint32_t", "int64_t", "uint64_t",
    "intptr_t", "uintptr_t", "wchar_t",
}

def _detect_elem_buf_len_pairs(parameters):
    """Real-code precondition modeling: pair a SCALAR-ELEMENT pointer (int*, long*,
    float*, ...) with an immediately-following size-named integer, the dominant
    ``f(T *buf, size_t n)`` convention (sum/fill/dot/...). Returns
    ``(buf_name, buf_type, elem_base, len_name, len_type)``. Sized to
    ``n*sizeof(elem)`` at emission so in-bounds element access is safe and an
    off-by-one past ``n`` is still caught. char*/void*/byte and structs excluded
    (handled elsewhere)."""
    params = [(pt.strip(), pn) for pt, pn in parameters if pn]
    out = []
    # Associate each size-named integer with the RUN of consecutive scalar-element
    # pointers IMMEDIATELY preceding it -- the copy/transform idiom
    # cp(int*d,const int*s,size_t n) binds BOTH d and s to n, not just the adjacent
    # s. Walking the run (vs the immediate neighbour only) sizes every shared
    # buffer, so an off-by-one WRITE to the destination is caught, not just the read.
    for j in range(1, len(params)):
        ltype, lname = params[j]
        if "*" in ltype or lname.lower() not in _BUF_LEN_SIZE_NAMES:
            continue
        k = j - 1
        while k >= 0:
            ptype, pname = params[k]
            st = _strip_restrict_quals(ptype)
            if st.count("*") != 1:
                break
            base = re.sub(r"\bconst\b", "", st.rstrip("*")).strip()
            if base not in _SCALAR_ELEM_TYPES:
                break
            out.append((pname, st, base, lname, ltype.strip()))
            import os as _os_abl
            if _os_abl.environ.get("BMC_ABLATE_ELEM_PAIRING_RUN"):
                break   # ablation: adjacent pointer only (pre run-walk behavior)
            k -= 1
    return out


def _generate_nd_decls(
    func: FunctionInfo,
    cbmc_unwind: int = 4,
    nonnull_params: Optional[set] = None,
    precondition: Optional[str] = None,
    raw_bytes: bool = False,
    struct_definitions: Optional[dict] = None,
    infer_field_validity: bool = False,
    infer_struct_field_validity: bool = False,
    infer_array_param_bounds: bool = False,
    infer_array_param_bounds_max: int = 64,
    scale_down: bool = False,
    scale_down_size: int = 4,
    force_opaque_structs: Optional[set] = None,
    copy_source_max_len: int = 0,
    copy_source_max_dest: int = 256,
) -> list[str]:
    """
    Generate nondeterministic variable declarations for each parameter.

    For pointer parameters, we allocate a local struct/array on the stack and
    point to it. char* and const char* parameters receive a bounded
    null-terminated string (max length = cbmc_unwind - 1, i.e. one slot of
    headroom: a `while(*s)` traversal needs L+1 guard checks, so L<=unwind-1
    keeps it provably within the unwinding bound instead of tripping the
    loop-unwinding assertion as an artifact).

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
    # Deferred assumes coupling a buffer param to its length param, realizing a
    # relational precondition valid_range(ptr, 0, L). Emitted AFTER all params
    # are declared (L may be declared later in signature order). See below.
    _coupling_assumes: list = []

    # String-copy SOURCE widening: inputs that flow into an unbounded copy SINK
    # (strcpy/strcat/stpcpy) are modeled LONGER than the default so a
    # copy-into-fixed-buffer overflow is reachable (FN dual of (buf,len)). Each
    # source is widened to its resolved fixed-destination size (min unwind cost)
    # or the default cap when the dest size is unresolvable.
    copy_param_maxlen: dict = {}
    copy_field_maxlen: dict = {}
    if copy_source_max_len and copy_source_max_len > 0:
        from .string_copy_sink import plan_copy_source_widening
        copy_param_maxlen, copy_field_maxlen, _floor = plan_copy_source_widening(
            func, copy_source_max_len, copy_source_max_dest
        )

    # Collect pointer parameter names for paired-buffer analysis.
    pointer_pnames: set[str] = set()
    for ptype, pname in func.signature.parameters:
        if not pname:
            continue
        if _strip_restrict_quals(ptype).endswith("*") or "*" in pname:
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

    # (buf, len) pairs: size the byte buffer to the length param so reads up to
    # len are in-bounds AND an off-by-one PAST len is genuinely caught (buf is
    # exactly len bytes, not an over-sized fixed buffer that would mask it).
    # `f(const uint8_t *pkt, uint32_t len)` etc. Without this, the harness gives
    # pkt a fixed cbmc_unwind+1 buffer while len is unconstrained-large -> every
    # length-driven read reports a spurious OOB (the vibeos net icmp/ip/tcp_handle
    # FPs). Emit the length FIRST (bounded) then `buf = malloc(len)`.
    _bl_max = int(infer_array_param_bounds_max) if infer_array_param_bounds_max else 64
    for buf_name, buf_type, len_name, len_type in _detect_buf_len_pairs(
        func.signature.parameters
    ):
        if buf_name in paired_emitted or len_name in paired_emitted:
            continue
        lines.append(
            f"    /* (buf,len) pair: {buf_name} sized to {len_name} "
            f"(reads in-bounds; off-by-one past {len_name} still caught) */"
        )
        lines.append(f"    {len_type} {len_name};")
        # Bound len to [0, MAX] for ANY integer type: a negative signed len
        # casts to a huge unsigned, failing the <= MAX test.
        lines.append(
            f"    __CPROVER_assume((unsigned long long)({len_name}) <= {_bl_max}ull);"
        )
        lines.append(f"    {buf_type} {buf_name} = ({buf_type})malloc({len_name});")
        lines.append(f"    __CPROVER_assume({buf_name} != NULL);")
        paired_emitted.add(buf_name)
        paired_emitted.add(len_name)

    # (elem-buf, len) coupling: size a scalar-element buffer to n*sizeof(elem) so
    # in-bounds element reads are SAFE (kills the unconstrained-buffer FP, e.g.
    # sum(int*a,size_t n)) while an off-by-one past n stays a real OOB. Uses
    # __CPROVER_allocate (exact object size; immune to a module malloc stub).
    _len_declared: set = set()
    for buf_name, buf_type, elem_base, len_name, len_type in _detect_elem_buf_len_pairs(
        func.signature.parameters
    ):
        if buf_name in paired_emitted:
            continue
        _bb = f"_{buf_name}_bytes"
        lines.append(
            f"    /* (elem-buf,len) pair: {buf_name} sized to {len_name}*sizeof({elem_base}) "
            f"(element reads in-bounds; off-by-one past {len_name} still caught) */"
        )
        # Declare + bound the length ONCE, even when several buffers share it
        # (cp(dst,src,n): both sized to n, but `n` is declared a single time).
        if len_name not in paired_emitted and len_name not in _len_declared:
            lines.append(f"    {len_type} {len_name};")
            lines.append(
                f"    __CPROVER_assume((unsigned long long)({len_name}) <= {_bl_max}ull);"
            )
            _len_declared.add(len_name)
        lines.append(f"    size_t {_bb} = (size_t)({len_name}) * sizeof({elem_base});")
        lines.append(
            f"    {buf_type} {buf_name} = ({buf_type})__CPROVER_allocate({_bb} ? {_bb} : 1, 0);"
        )
        lines.append(f"    __CPROVER_assume({buf_name} != NULL);")
        paired_emitted.add(buf_name)
        paired_emitted.add(len_name)

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
        # Strip restrict-like qualifiers / project attribute macros
        # (``GGML_RESTRICT``, ``__restrict``, ...) BEFORE checking for
        # pointer-ness. Otherwise ``const float * GGML_RESTRICT x`` ends
        # in ``GGML_RESTRICT`` and falls through to value-param emit,
        # leaving x as a wild uninitialised pointer (root cause of the
        # ggml-quants.c dequantize_row_*.pointer_dereference.1 FP class).
        ptype_stripped = _strip_restrict_quals(ptype)
        # Strip the pointer-const qualifier: ``T *const p`` (a const POINTER)
        # otherwise leaves base_type as ``T *const`` -> matches no type branch
        # -> falls through to a bare nondet pointer (no backing buffer) ->
        # spurious memory-safety false alarms. The pointee-constness (leading
        # ``const``) is preserved for the param-type signature match.
        import re as _re_pc
        ptype_stripped = _re_pc.sub(r"\*\s*const\b", "*", ptype_stripped)

        if ptype_stripped.endswith("*") or "*" in pname:
            # Pointer parameter
            base_type = ptype_stripped.rstrip("*").strip()
            local_name = f"_{pname}_val"
            clean_base = re.sub(r"\bconst\b", "", base_type).strip()

            # Count pointer depth: char* is depth 1, char** is depth 2
            star_count = ptype_stripped.count("*")

            if base_type.lower() in ("void", "const void"):
                if infer_field_validity or infer_array_param_bounds:
                    # When field-validity/array-param-bounds modes are on,
                    # emit a malloc'd byte buffer instead of NULL so the
                    # body's downstream cast (``const block_iq1_m *x =
                    # (const block_iq1_m *)vx;`` and then ``x[i].field``)
                    # has a valid backing region to deref. Without this,
                    # every dot-product / vec-* function in
                    # ggml-cpu/quants.c traps on the first body deref of
                    # the cast pointer. The buffer is sized as
                    # ``scale_down_size**3`` (default 64) under scale-down
                    # or ``cbmc_unwind+1`` otherwise -- enough for typical
                    # block-quantized struct iteration.
                    _coupled_len = None
                    if precondition:
                        _mv = _re_pc.search(
                            r"valid_range\s*\(\s*" + _re_pc.escape(pname)
                            + r"\s*,\s*[^,]+,\s*([^)]+?)\s*\)", precondition)
                        if _mv:
                            _le = (_mv.group(1) or "").strip()
                            if _re_pc.fullmatch(r"[A-Za-z_]\w*", _le) and _le != pname:
                                _coupled_len = _le
                    backing_name = f"_{pname}_void_backing"
                    if _coupled_len is not None:
                        # Faithful valid_range(pname,0,L) realization: the backing
                        # OBJECT SIZE must equal the length param L. A fixed-size
                        # buffer + assume(L<=N) does NOT work (CBMC can't relate a
                        # symbolic sub-length to a constant-size object's region
                        # check -> spurious OOB; verified). malloc tied to L proves
                        # SAFE for all L. Use a fresh nondet size local (no decl-
                        # order dep) and DEFER assume(L == size) to after all params.
                        _sz_name = f"_{pname}_sz"
                        lines.append(
                            f"    /* {ptype_stripped} {pname} — void* sized to "
                            f"valid_range length '{_coupled_len}' (object size == length) */")
                        lines.append(f"    size_t {_sz_name};  /* nondet object size */")
                        lines.append(f"    __CPROVER_assume({_sz_name} <= 4096);")  # bound size: keep cex/reproducer lengths sane; off-by-one still triggers for size>=8
                        lines.append(
                            f"    unsigned char *{backing_name} = (unsigned char *)"
                            f"__CPROVER_allocate({_sz_name} ? {_sz_name} : 1, 0);")  # CBMC-native exact-size object; immune to a module-defined malloc stub (which yields an INVALID backing -> spurious deref FAILUREs on SAFE code)
                        lines.append(f"    __CPROVER_assume({backing_name} != NULL);")
                        lines.append(
                            f"    {ptype_stripped} {pname} = ({ptype_stripped}){backing_name};")
                        _coupling_assumes.append(
                            f"    __CPROVER_assume((size_t)({_coupled_len}) == {_sz_name});"
                            f"  /* tie len to valid_range({pname},0,{_coupled_len}) object size */")
                    else:
                        bbytes = (
                            scale_down_size ** 3 if scale_down
                            else (cbmc_unwind + 1) * 64
                        )
                        lines.append(
                            f"    /* {ptype_stripped} {pname} — void* param, "
                            f"backed by {bbytes}-byte buffer */")
                        lines.append(
                            f"    unsigned char *{backing_name} = "
                            f"(unsigned char *)malloc({bbytes});")
                        lines.append(f"    __CPROVER_assume({backing_name} != NULL);")
                        lines.append(
                            f"    {ptype_stripped} {pname} = "
                            f"({ptype_stripped}){backing_name};")
                else:
                    # FIX(spec-absorption / vacuous-proof): when the inferred
                    # precondition already asserts valid_range(p, 0, L), materialize an
                    # L-sized backing object here too -- even with infer_field_validity
                    # and infer_array_param_bounds off. Leaving the void* as NULL while a
                    # later __CPROVER_assume(p != NULL) is emitted makes the path UNSAT,
                    # so CBMC generates 0 VCCs and proves EVERYTHING vacuously, absorbing
                    # real OOB bugs (memset/memcpy/ip_checksum off-by-one). Gated on the
                    # spec so truly-opaque void* params (no validity atom) keep the old
                    # NULL behavior.
                    _coupled_len2 = None
                    if precondition:
                        _mv2 = _re_pc.search(
                            r"valid_range\s*\(\s*" + _re_pc.escape(pname)
                            + r"\s*,\s*[^,]+,\s*([^)]+?)\s*\)", precondition)
                        if _mv2:
                            _le2 = (_mv2.group(1) or "").strip()
                            if _re_pc.fullmatch(r"[A-Za-z_]\w*", _le2) and _le2 != pname:
                                _coupled_len2 = _le2
                    if _coupled_len2 is not None:
                        backing_name = f"_{pname}_void_backing"
                        _sz_name = f"_{pname}_sz"
                        lines.append(
                            f"    /* {ptype_stripped} {pname} — void* sized to "
                            f"valid_range length '{_coupled_len2}' "
                            f"(object size == length; vacuous-proof fix) */")
                        lines.append(f"    size_t {_sz_name};  /* nondet object size */")
                        lines.append(f"    __CPROVER_assume({_sz_name} <= 4096);")  # bound size: keep cex/reproducer lengths sane; off-by-one still triggers for size>=8
                        lines.append(
                            f"    unsigned char *{backing_name} = (unsigned char *)"
                            f"__CPROVER_allocate({_sz_name} ? {_sz_name} : 1, 0);")  # CBMC-native exact-size object; immune to a module-defined malloc stub (which yields an INVALID backing -> spurious deref FAILUREs on SAFE code)
                        lines.append(f"    __CPROVER_assume({backing_name} != NULL);")
                        lines.append(
                            f"    {ptype_stripped} {pname} = ({ptype_stripped}){backing_name};")
                        _coupling_assumes.append(
                            f"    __CPROVER_assume((size_t)({_coupled_len2}) == {_sz_name});"
                            f"  /* tie len to valid_range({pname},0,{_coupled_len2}) object size */")
                    else:
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
                        f"    __CPROVER_assume({nul_name} <= (unsigned int){max(1, cbmc_unwind - 1)});"
                        f"  /* (a) L<=unwind-1: while(*s) needs L+1 guard checks */"
                    )
                    lines.append(f"    {backing_name}[{nul_name}] = '\\0';")
                lines.append(f"    {inner_type} *{cursor_name} = {backing_name};")
                lines.append(f"    {ptype_stripped} {pname} = &{cursor_name};")
                if pname in nonnull_params:
                    lines.append(
                        f"    /* {pname} is non-null by construction (addr of cursor) */"
                    )
            elif (_is_byte_shaped_type(base_type) or clean_base == "wchar_t") and star_count == 1:
                # Single-indirection byte/wchar-shaped pointer. Strategies:
                #
                #  - Default for ``char`` / ``wchar_t`` (raw_bytes=False):
                #    bounded null-terminated string, so strlen/wcslen-style
                #    traversal loops terminate within the CBMC unwinding
                #    bound. Right for textual APIs.
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
                # ``wchar_t`` is NEVER emitted as a raw-byte buffer even
                # under raw_bytes — it's a wide-string type, the C
                # convention for wide-char data is null-terminated. Without
                # this branch ``wchar_t *`` falls through to the generic
                # pointer init which allocates a SINGLE wchar (no buffer,
                # no terminator), producing
                # ``add_pattern_wcs.pointer_arithmetic.5`` style FPs on
                # every ``wcslen``/``wcschr`` call in the function body.
                #
                # char** (e.g. argv) uses the default treatment in either mode.
                buf_name = f"_{pname}_buf"
                is_textual = (clean_base in ("char", "wchar_t"))
                emit_raw = raw_bytes or not is_textual
                if emit_raw and clean_base != "wchar_t":
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
                    is_wide = (clean_base == "wchar_t")
                    backing_t = "wchar_t" if is_wide else "char"
                    nul_literal = "L'\\0'" if is_wide else "'\\0'"
                    descriptor = "wide " if is_wide else ""
                    # Copy-sink SOURCE: widen the max length so a strcpy/strcat
                    # into a smaller fixed buffer can overflow (else baked-in
                    # length <= cbmc_unwind hides the bug). Width = the resolved
                    # destination size (or default cap), per plan_copy_source_widening.
                    # (a) Headroom under the unwind bound: a NUL-terminated string
                    # of length L needs L+1 guard checks in a `while(*s)` loop, so
                    # bound L at cbmc_unwind-1 to keep the traversal provably within
                    # `--unwind cbmc_unwind`. Without this, the loop-unwinding
                    # assertion trips as an artifact (see (b) _recheck_unwind_artifact).
                    str_max = max(1, cbmc_unwind - 1)
                    if copy_param_maxlen.get(pname, 0) > str_max:
                        str_max = copy_param_maxlen[pname]
                        lines.append(f"    /* copy-sink source '{pname}': widened to {str_max} chars to expose fixed-buffer overflow */")
                    lines.append(f"    /* bounded null-terminated {descriptor}string for '{pname}' (max {str_max} chars) */")
                    lines.append(f"    {backing_t} {buf_name}[{str_max + 1}];")
                    lines.append(f"    unsigned int {len_name};")
                    lines.append(f"    __CPROVER_assume({len_name} <= (unsigned int){str_max});")
                    lines.append(f"    {buf_name}[{len_name}] = {nul_literal};")
                    lines.append(f"    {ptype_stripped} {pname} = {buf_name};")
                if pname in nonnull_params:
                    lines.append(f"    __CPROVER_assume({pname} != NULL);  /* call-site: never passed NULL */")
            elif star_count == 1 and _resolve_struct_name(base_type, struct_definitions) and struct_definitions.get(_resolve_struct_name(base_type, struct_definitions)):
                # Single-pointer to a known struct WITH a visible body —
                # emit per-field initialisation instead of just leaving the
                # struct nondet. Empirical pattern: opaque-struct pointer
                # args (`Curl_URL *u`, `nghttp2_bufs *bufs`, `ASN1_STRING
                # *s`) produced 100+ spurious CEs each because every field
                # access was unconstrained.
                #
                # The ``struct_definitions.get(tag)`` guard rejects opaque
                # types whose body isn't in this TU — e.g. libarchive's
                # ``struct archive_string_conv`` (defined in archive_string.c
                # but only forward-declared in archive_acl.c). Stack-allocating
                # such a struct produces ``incomplete type not permitted
                # here`` at CBMC type-check. Falls through to the nondet-
                # pointer default below, which is correct for opaque types.
                struct_tag = _resolve_struct_name(base_type, struct_definitions)
                fields = struct_definitions[struct_tag]
                obj_name = f"_{pname}_obj"
                lines.append(
                    f"    /* struct-pointer init for '{pname}' ({base_type}, "
                    f"{len(fields)} field{'s' if len(fields) != 1 else ''}) */"
                )
                # Emit a stack-allocated instance (CBMC fills it with
                # nondet); we then constrain individual fields below.
                # Strip a leading ``const`` from the backing-storage
                # declaration: ``const struct X _obj; ... _obj.field =
                # backing;`` is illegal (assignment to const). The
                # pointer type below keeps its const qualifier so the
                # parameter signature matches; mutable-to-const
                # conversion at the &_obj initialiser is legal.
                base_for_decl = re.sub(
                    r"^\s*const\s+", "", base_type
                ).strip()
                lines.append(f"    {base_for_decl} {obj_name};")
                lines.append(f"    {ptype_stripped} {pname} = &{obj_name};")
                for ftype, fname in fields:
                    lines.extend(
                        _emit_struct_field_init(
                            obj_name, ftype, fname, cbmc_unwind,
                            enclosing_struct_tag=struct_tag,
                            infer_field_validity=infer_field_validity,
                            infer_struct_field_validity=infer_struct_field_validity,
                            struct_definitions=struct_definitions,
                            func_body=getattr(func, "body", None),
                            copy_field_maxlen=copy_field_maxlen,
                        )
                    )
                # POST-PASS: cast-pattern-driven typed init for field chains.
                # Scan func.body for ``(struct X *)(<pname>-><chain>)`` casts
                # and emit typed backing for the chain. This addresses the
                # dominant wrong-struct-cast artifact in libarchive format
                # readers: ``cab = (struct cab *)(a->format->data)`` —
                # without this, ``a->format->data`` stays nondet/NULL and
                # CBMC reports a false deref. The registrar in real code
                # always sets these to typed allocations.
                lines.extend(
                    _emit_cast_chain_init(
                        pname, obj_name,
                        func_body=getattr(func, "body", None),
                        struct_definitions=struct_definitions,
                    )
                )
                if pname in nonnull_params:
                    lines.append(
                        f"    /* {pname} is non-null by construction (addr of {obj_name}) */"
                    )
            elif (
                infer_array_param_bounds
                and star_count == 1
                and (clean_base in _PRIMITIVE_POINTEE_TYPES
                     or clean_base in ("void", "char", "unsigned char",
                                       "signed char", "uint8_t", "int8_t"))
            ):
                # Top-level primitive-pointer param sized from the body.
                # Default fallback below would emit a single-element
                # local + addr-of, which produces a pointer-OOB FP when
                # the function writes ``param[1..N]`` against a fixed-size
                # table (e.g. llm.c's fill_in_parameter_sizes writes
                # param_sizes[0..15]). Body-scan finds the max literal
                # subscript and sizes accordingly; cap at
                # infer_array_param_bounds_max to prevent runaway sizing
                # on macro-resolved subscripts the parser couldn't see.
                max_idx = _max_literal_subscript(func.body or "", pname)
                if max_idx is not None:
                    bound = min(max_idx + 1, infer_array_param_bounds_max)
                elif scale_down:
                    # Under scale-down, ML kernels iterate over B*T*C-style
                    # ranges with size params bounded to scale_down_size.
                    # Size the backing buffer to scale_down_size^3 so a 3D
                    # tensor index ``out[b*T*OC + t*OC + o]`` with each
                    # coord in [0, scale_down_size) doesn't escape. Capped
                    # by infer_array_param_bounds_max to prevent runaway.
                    bound = min(
                        scale_down_size ** 3,
                        infer_array_param_bounds_max,
                    )
                else:
                    bound = cbmc_unwind + 1
                buf_name = f"_{pname}_buf"
                lines.append(
                    f"    /* sized backing for '{pname}' "
                    f"({bound} elements; "
                    f"{'literal-subscript scan' if max_idx is not None else 'cbmc_unwind+1 fallback'}) */"
                )
                _buf_elem = ("unsigned char" if clean_base == "void" else clean_base)
                lines.append(f"    {_buf_elem} {buf_name}[{bound}];")
                lines.append(
                    f"    {ptype_stripped} {pname} = ({ptype_stripped}){buf_name};"
                )
                # RELATIONAL precondition realization: valid_range(pname, 0, L)
                # means pname is a valid buffer of L elements. The backing
                # buffer above has `bound` elements; without coupling, L stays
                # free nondet and the function reads pname[0..L) past the buffer
                # -> spurious OOB false alarm (CBMC ties plain --function).
                # Constrain L <= bound so the length matches the allocation
                # (bounded, sound for bug-finding). Deferred: L may be a later param.
                if precondition:
                    import re as _re_vr
                    _m_vr = _re_vr.search(
                        r"valid_range\s*\(\s*" + _re_vr.escape(pname)
                        + r"\s*,\s*[^,]+,\s*([^)]+?)\s*\)",
                        precondition)
                    if _m_vr:
                        _len_expr = (_m_vr.group(1) or "").strip()
                        if _re_vr.fullmatch(r"[A-Za-z_]\w*", _len_expr) and _len_expr != pname:
                            _coupling_assumes.append(
                                f"    __CPROVER_assume((unsigned long)({_len_expr}) <= {bound});"
                                f"  /* couple len to valid_range({pname},0,{_len_expr}) buffer (sized {bound}) */")
                if pname in nonnull_params:
                    lines.append(
                        f"    __CPROVER_assume({pname} != NULL);  /* call-site: never passed NULL */"
                    )
            elif "const" in ptype_stripped.lower():
                # Same opaque-struct guard as the default branch below:
                # ``const struct X *foo`` for opaque X otherwise produces
                # ``const struct X _val;`` → incomplete type.
                _base_strip_c = re.sub(r'\bconst\b', '', base_type).strip()
                _is_opaque_c = False
                if _base_strip_c.startswith('struct ') or _base_strip_c.startswith('union '):
                    _kw_len_c = 7 if _base_strip_c.startswith('struct ') else 6
                    _tag_c = _base_strip_c[_kw_len_c:].strip()
                    if not struct_definitions or not struct_definitions.get(_tag_c):
                        _is_opaque_c = True
                    elif force_opaque_structs and _tag_c in force_opaque_structs:
                        # Autonomous-mode override: even if the struct is in
                        # struct_definitions, the retry registry told us
                        # to treat it as opaque (probably because a prior
                        # CBMC run on this function hit
                        # ``incomplete type not permitted here``).
                        _is_opaque_c = True
                if _is_opaque_c:
                    lines.append(
                        f"    /* opaque {_base_strip_c}: nondet pointer ({_tag_c} body not in TU) */"
                    )
                    lines.append(f"    {ptype_stripped} {pname};")
                    if pname in nonnull_params:
                        lines.append(
                            f"    __CPROVER_assume({pname} != NULL);  /* call-site: never passed NULL */"
                        )
                else:
                    lines.append(f"    {clean_base} {local_name};")
                    lines.append(f"    {ptype_stripped} {pname} = &{local_name};")
            else:
                # Opaque-struct guard: if base_type is a struct/union whose
                # body isn't visible in this TU (only forward-declared),
                # stack-allocating ``{base} {local};`` produces
                # ``incomplete type not permitted here`` at CBMC type-check.
                # Emit a nondet pointer instead — the function body will
                # treat the pointee opaquely too, which is the right model
                # for opaque types like libarchive's
                # ``struct archive_string_conv``.
                base_strip = re.sub(r'\bconst\b', '', base_type).strip()
                is_opaque_struct = False
                if base_strip.startswith('struct ') or base_strip.startswith('union '):
                    kw_len = 7 if base_strip.startswith('struct ') else 6
                    tag = base_strip[kw_len:].strip()
                    if not struct_definitions or not struct_definitions.get(tag):
                        is_opaque_struct = True
                    elif force_opaque_structs and tag in force_opaque_structs:
                        # Autonomous-mode override (see ``const`` branch
                        # above for rationale).
                        is_opaque_struct = True
                if is_opaque_struct:
                    lines.append(
                        f"    /* opaque {base_strip}: nondet pointer ({tag} body not in TU) */"
                    )
                    lines.append(f"    {ptype_stripped} {pname};")
                    if pname in nonnull_params:
                        lines.append(
                            f"    __CPROVER_assume({pname} != NULL);  /* call-site: never passed NULL */"
                        )
                else:
                    lines.append(f"    {base_type} {local_name};")
                    lines.append(f"    {ptype_stripped} {pname} = &{local_name};")
                    if pname in nonnull_params:
                        lines.append(f"    __CPROVER_assume({pname} != NULL);  /* call-site: never passed NULL */")
        else:
            # Value parameter
            lines.append(f"    {ptype_stripped} {pname};")
            # (a)-extension / Path 2: bound a length/size/count-style integer value
            # param to the unwind ONLY when the body actually uses it as a LOOP BOUND
            # (e.g. for(i=0;i<n;i++), while(n--)). Then explicit mem*/strncpy-style
            # traversals are provably within `--unwind` -> the loop-unwinding assertion
            # HOLDS instead of tripping as an artifact. Gated on loop-bound use so an
            # arithmetic-only length (n*size overflow candidate) is NOT constrained and
            # overflow bugs are preserved. Bounded analysis; residual off-by-one -> (b).
            _lb_body = getattr(func, "body", "") or ""
            import re as _re_lb
            _is_loop_bound = bool(_re_lb.search(
                r'(?:<=?|!=)\s*' + _re_lb.escape(pname) + r'\b'
                + r'|\b' + _re_lb.escape(pname) + r'\s*(?:--|>\s*0)',
                _lb_body))
            if (_is_likely_length_field(pname)
                    and _looks_like_integer_type(ptype_stripped)
                    and _is_loop_bound):
                lines.append(
                    f"    __CPROVER_assume((unsigned long){pname} <= (unsigned long){cbmc_unwind});"
                    f"  /* (a) loop-bound length <= unwind: keeps traversal within --unwind */"
                )
            if pname in cursor_size_assumes:
                lines.append(cursor_size_assumes[pname])
    if _coupling_assumes:
        lines.append("    /* relational valid_range(ptr,0,len) coupling */")
        lines.extend(_coupling_assumes)
    return lines


# ---------------------------------------------------------------------------
# Main harness generator
# ---------------------------------------------------------------------------


class HarnessGenerator:
    """Generates CBMC harnesses for individual C functions."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def _function_post_relaxations(self, func_name: str) -> list[str]:
        """Read persisted ``function_post_relaxations`` for *func_name* —
        applied to the FUT's own postcondition before it becomes an
        ``assert(...)`` in main(). Triggered by realism rejection of a
        FUT-POST violation; orthogonal to caller-grounded spec gen.
        """
        try:
            from bmc_agent.feedback_loop import LearnedConstraintsStore
            store = LearnedConstraintsStore(self.config.artifact_dir)
            return store.function_post_relaxations(func_name)
        except Exception:
            return []

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
                stub_src = _generate_stub(
                    cname,
                    callee_spec,
                    parsed_file,
                    assume_postcondition=getattr(self.config, "assume_callee_postcondition", False),
                    verified_sound=(getattr(self.config, "verified_sound_functions", None)
                                    if getattr(self.config, "assume_verified_callee_postcondition", False) else None),
                    copy_field_maxlen=_copy_field_plan(self.config, caller),
                    struct_definitions=getattr(parsed_file, "struct_definitions", None),
                )
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
            infer_field_validity=getattr(self.config, "infer_field_validity", False),
            infer_struct_field_validity=getattr(self.config, "infer_struct_field_validity", False),
            infer_array_param_bounds=(getattr(self.config, "infer_array_param_bounds", False) or getattr(self.config, "scale_down", False)),
            infer_array_param_bounds_max=getattr(self.config, "infer_array_param_bounds_max", 64),
            scale_down=getattr(self.config, "scale_down", False),
            scale_down_size=getattr(self.config, "scale_down_size", 4),
            force_opaque_structs=set(getattr(self.config, "session_opaque_param_structs", None) or []) or None,
            copy_source_max_len=(getattr(self.config, "string_copy_source_max_len", 0)
                                 if getattr(self.config, "enable_string_copy_source_modeling", True) else 0),
            copy_source_max_dest=getattr(self.config, "string_copy_source_max_dest", 256),
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
            f"/* Reachability harness: can `{fn_name}` produce state\n"
            f"   {_sanitize_for_c_comment(str(counterexample.variable_assignments), max_len=1500)}\n"
            f"   at call to `{callee_name}`? */\n"
            f"/* Generated by AMC Phase 3                            */"
        )

        # When source is preprocessed, type_decls already carries glibc's
        # full type machinery (struct __locale_struct, struct _IO_FILE, ...).
        # Re-including <stdlib.h>/<stdio.h>/<string.h> would cause CBMC to
        # expand glibc again and conflict with the inlined definitions
        # (CBMC exit 6: "redefinition of body of 'struct __locale_struct'").
        # Match the main harness's policy (line 4030-4040 of this file) and
        # only emit the assert/stddef/stdint minimum, which don't pull in
        # the conflicting types. Was the root cause of the recurring
        # "cbmc exited with code 6" errors on libarchive sweeps — both
        # reachability and feasibility checks errored, forcing LLM-only
        # reachability fallback that then confabulated.
        preprocessed2 = parsed_file.preprocessed_source is not None
        if preprocessed2:
            inc2 = ["#include <assert.h>", "#include <stddef.h>", "#include <stdint.h>"]
        else:
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
        _ret_bare_reach = _ret_type_bare(ret_type)
        if _ret_bare_reach == "void":
            harness_body_lines.append(f"    {fn_name}({call_args});")
        else:
            harness_body_lines.append(f"    {_ret_bare_reach} _caller_result = {fn_name}({call_args});")
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

            # Witness values can contain apostrophes / quotes (CBMC sometimes
            # reports values as Python repr like ``{'name': 'unknown'}``).
            # Embedding those raw inside a /* … */ comment confuses CBMC's
            # tokenizer ("missing terminating ' character" → CONVERSION ERROR
            # → exit 6 on the reachability check). Sanitize for comment use
            # only; the original value is still used for the __CPROVER_assume
            # branch where _is_simple_value gates out anything non-numeric.
            comment_val = _sanitize_for_c_comment(clean_val)

            # --- Filter out CBMC-internal variable names ---
            # 1. CBMC builtins: __CPROVER_*
            if clean_var.startswith("__CPROVER_"):
                lines.append(f"    /* cex (cbmc-internal): {clean_var} = {comment_val} */")
                continue
            # 2. CBMC-internal allocation names: _varname (underscore + varname)
            if clean_var.startswith("_"):
                lines.append(f"    /* cex (cbmc-internal): {clean_var} = {comment_val} */")
                continue
            # 3. CBMC SSA / object-validity variables: contain '$'
            if "$" in clean_var:
                lines.append(f"    /* cex (cbmc-internal): {clean_var} = {comment_val} */")
                continue
            # 4. Must reference a known parameter (directly or via -> / .)
            base_name = clean_var.split("->")[0].split(".")[0]
            if base_name not in param_names:
                lines.append(f"    /* cex (not a param): {clean_var} = {comment_val} */")
                continue

            # Only emit assumes for simple numeric/NULL values
            if _is_simple_value(clean_val):
                lines.append(
                    f"    __CPROVER_assume({clean_var} == {clean_val}); "
                    f"/* cex: {clean_var} = {comment_val} */"
                )
            else:
                lines.append(
                    f"    /* cex: {clean_var} = {comment_val} (complex — skipped) */"
                )

        # assert(0) fires iff the __CPROVER_assume constraints above were
        # satisfiable and this stub was actually called.  CBMC reports a CEx
        # iff such a path exists, confirming the callee state is reachable.
        lines.append("    assert(0); /* reachability witness */")

        _ret_bare_stub = _ret_type_bare(ret_type)
        if _ret_bare_stub == "void":
            lines.append("    /* void return */")
        else:
            lines.append(f"    {_ret_bare_stub} result;")
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
        # Only externals we can build a real-signature stub for get
        # substituted. The rest (variadic externs like ``archive_set_error``
        # whose canonical signature we don't have) are left in place — CBMC
        # treats undefined externs as nondet-return, which is the right
        # semantics for "feasibility check" anyway. Substituting them to a
        # zero-arg ``_stub`` (the historical fallback) produced
        # ``wrong number of function arguments`` CBMC errors → exit code 6
        # → entire feasibility check skipped, lifting the trust burden onto
        # LLM-only reachability (which we've seen confabulate).
        stub_sections: list[str] = []
        substituted_externals: set[str] = set()
        for cname in sorted(external_callees):
            callee_spec = (all_specs or {}).get(cname)
            stub_src = _generate_stub(
                cname,
                callee_spec,
                parsed_file,
                assume_postcondition=getattr(self.config, "assume_callee_postcondition", False),
                    verified_sound=(getattr(self.config, "verified_sound_functions", None)
                                    if getattr(self.config, "assume_verified_callee_postcondition", False) else None),
                copy_field_maxlen=_copy_field_plan(self.config, func),
                struct_definitions=getattr(parsed_file, "struct_definitions", None),
            )
            # _generate_stub emits a sentinel comment when it can't find a
            # signature and falls through to ``void X_stub(void)``. Skip
            # those — they'd cause arg-count mismatch with real callsites.
            if stub_src.startswith("/* Auto-stub for unknown external:"):
                continue
            stub_sections.append(stub_src)
            substituted_externals.add(cname)

        # --- 4. Build real function definitions for local closure ---
        # Substitute only external callee calls we have real stubs for
        # (local callees are real bodies — see step 5).
        local_func_defs: list[str] = []
        for cname in sorted(local_closure):
            cfi = parsed_file.get_function_info(cname)
            if cfi is None:
                continue
            cbody = _substitute_callee_calls(cfi.body, substituted_externals)
            cparams = _params_str(cfi.signature.parameters)
            local_func_defs.append(
                f"/* inlined local callee: {cname} */\n"
                f"{cfi.signature.return_type} {cname}({cparams})\n{cbody}"
            )

        # --- 5. Build func definition (substitute only stubable externals) ---
        func_body = _substitute_callee_calls(func.body, substituted_externals)
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
                    # Opaque-struct guard: stack-allocating ``{base} {local};``
                    # produces ``incomplete type not permitted here`` at CBMC
                    # type-check when the struct body isn't visible in this
                    # TU (forward-decl only) — e.g. libarchive's opaque
                    # ``struct archive_entry``. Match the main harness's
                    # treatment: emit a nondet pointer and let CBMC model the
                    # pointee opaquely.
                    _base_strip = re.sub(r'\bconst\b', '', base_type).strip()
                    _is_opaque = False
                    if _base_strip.startswith('struct ') or _base_strip.startswith('union '):
                        _kw_len = 7 if _base_strip.startswith('struct ') else 6
                        _tag = _base_strip[_kw_len:].strip()
                        _struct_defs = getattr(parsed_file, "struct_definitions", None) or {}
                        if not _struct_defs.get(_tag):
                            _is_opaque = True
                    if _is_opaque:
                        nondet_decls.append(
                            f"    /* opaque {_base_strip}: nondet pointer (body not in TU) */"
                        )
                        nondet_decls.append(f"    {ptype_s} {pname};")
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

        # Same preprocessed-source gate as the reachability harness above
        # and the main harness (line ~4030): skip system includes when the
        # source is preprocessed to avoid struct __locale_struct / _IO_FILE
        # redefinition conflicts that crash CBMC with exit 6.
        preprocessed_feas = parsed_file.preprocessed_source is not None
        if preprocessed_feas:
            inc_lines = ["#include <assert.h>", "#include <stddef.h>", "#include <stdint.h>"]
        else:
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
        _ret_bare_feas = _ret_type_bare(ret_type)
        # postcond_to_assert translates the postcondition's ``result``
        # keyword into a literal C identifier ``result`` — so the
        # return-capture variable MUST be named ``result`` (matching the
        # main harness's _ret_var convention at line ~4413). Using
        # ``_result`` produced "failed to find symbol 'result'" CBMC
        # errors on functions whose postconditions reference the return
        # value (e.g., set_timefilter_date). When a param itself is
        # named ``result``, fall back to ``_amc_ret`` to avoid the
        # redefinition that hit libarchive's isint / isint_w.
        _ret_var_feas = "result"
        if any(pn == _ret_var_feas for _, pn in sig.parameters):
            _ret_var_feas = "_amc_ret"
        if _ret_bare_feas == "void":
            harness_lines.append(f"    {fn_name}({call_args});")
        else:
            harness_lines.append(f"    {_ret_bare_feas} {_ret_var_feas} = {fn_name}({call_args});")
            harness_lines.append(f"    (void){_ret_var_feas};")
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
        # The dynamic harness links real libc, so don't stub/rename standard
        # library functions — bind them to the real implementations. (Stubbing
        # them yields ``undefined reference to printf_stub`` etc. at link.)
        external_callees -= _LIBC_FUNCS

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
                # Gate to GENUINE file-scope globals. CBMC's cex also lists the
                # function's LOCALS and return capture (result, n64, i, byte, p,
                # pattern, ...). Emitting those as bare ``name = val;`` references
                # undeclared identifiers, so the whole dynamic harness fails to
                # compile -> false ``not_triggered`` (it never even ran the FUT).
                # Only a name with an actual file-scope definition is a real global.
                if not _extract_file_scope_var_defs(source_text, {clean_var}, entry_param_names):
                    continue
                if _is_simple_value(clean_val):
                    global_assigns.append(
                        f"    {clean_var} = {clean_val};  /* witness */"
                    )

            # 6b. Allocate init-trusted pointer globals so the dynamic harness
            # doesn't NULL-deref a global that an init function would have set
            # (the CBMC harness assumes `g != NULL`; mirror it concretely). Gate
            # by globals the included closure actually references, so the calloc
            # only names symbols that are in scope (else the compile breaks).
            _closure_text = "\n".join(
                (parsed_file.function_bodies.get(n, "") or "") for n in all_local
            )
            _closure_refs = _re_global_idents(_closure_text) if _closure_text else None
            global_assigns.extend(
                _emit_dynamic_global_invariant_inits(parsed_file, self.config, _closure_refs)
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
                # Faithful (pointer,length) sizing from spec valid_range + cex value
                # (values from the solver, structure from the harness -- no ground
                # truth, no LLM). Inlined (the spec lookup is cheap; avoids a non-unique
                # injection anchor).
                _fb = None
                _esp = (all_specs or {}).get(fn_name) if all_specs else None
                _pre = (getattr(_esp, "precondition", "") or "") if _esp else ""
                _m = re.search(r"valid_range\s*\(\s*" + re.escape(pname)
                               + r"\s*,\s*[^,]+,\s*([^)]+?)\s*\)", _pre)
                if _m:
                    _ex = _m.group(1).strip()
                    _mm = re.fullmatch(r"([A-Za-z_]\w*)\s*(?:([+\-])\s*(\d+))?", _ex)
                    if _mm:
                        _b = _mm.group(1)
                        if re.fullmatch(r"\d+", _b):
                            _fb = int(_b)
                        else:
                            _wv = counterexample.variable_assignments.get(_b, "")
                            _mn = re.match(r"\s*(\d+)", _wv)
                            if _mn:
                                _fb = int(_mn.group(1))
                                if _mm.group(2):
                                    _fb += int(_mm.group(3)) if _mm.group(2) == "+" else -int(_mm.group(3))
                                _fb = max(_fb, 0)
                    elif re.fullmatch(r"\d+", _ex):
                        _fb = int(_ex)
                if _fb is not None:
                    nbytes = max(min(_fb, 65536), 1)
                    buf_var = f"_amc_buf_{pname}"
                    # ASAN-instrumented, 16-aligned STACK array sized to the concrete cex
                    # length. NOT malloc (a module may define its own malloc stub -> no
                    # ASAN redzone). 16-aligned so alignment-gated fast paths run. Zero-init
                    # via {0} (NOT memset -- memset may BE the function under test).
                    call_arg_lines.append(
                        f"    _Alignas(16) unsigned char {buf_var}[{nbytes}] = {{0}};"
                    )
                    call_arg_lines.append(f"    {ptype_s} {arg_var} = ({ptype_s}){buf_var};")
                elif witness.strip() in ("NULL", "0", ""):
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
        _session_typedefs = set(getattr(self.config, "session_strip_typedefs", None) or [])
        _session_structs = set(getattr(self.config, "session_strip_structs", None) or [])
        type_decls = _strip_stdlib_decls(
            _strip_glibc_internal_struct_bodies(
                _strip_glibc_internal_typedefs(
                    _strip_static_inline_defs(
                        _strip_inline_asm(_strip_gcc_addr_space_quals(type_decls))
                    ),
                    extra_strip=_session_typedefs or None,
                ),
                extra_strip=_session_structs or None,
            )
        )
        func_def   = _strip_inline_asm(func_def)
        local_func_defs = [_strip_inline_asm(d) for d in local_func_defs]

        # --- 8b. Recover file-scope variables the embedded bodies reference ---
        # The closure/entry bodies may read module-scope variables (e.g.
        # ``static const char *dtb_error`` in dtb.c). The type-decl extractor
        # can drop these, yielding ``error: 'X' undeclared`` at GCC compile.
        # Pull in exactly the referenced ones, excluding anything already
        # provided (functions, params, names present in type_decls).
        referenced_ids: set[str] = set()
        for _b in [func_def, *local_func_defs]:
            referenced_ids |= set(re.findall(r"\b([A-Za-z_]\w*)\b", _b))
        already_defined = (
            local_closure | external_callees | {fn_name} | entry_param_names
            | set(re.findall(r"\b([A-Za-z_]\w*)\b", type_decls))
        )
        file_scope_var_defs = _extract_file_scope_var_defs(
            source_text, wanted_names=referenced_ids, exclude_names=already_defined,
        )

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

        if file_scope_var_defs:
            sections.append(
                "/* --- File-scope variables referenced by the closure --- */\n"
                + "\n".join(file_scope_var_defs)
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
        #
        # Step A — fault-site classification (commit 40ea2df+):
        # `_amc_fut_called` is set to 1 immediately before the function under
        # test is invoked. The signal handler prints this flag alongside
        # the signal name, so the dyn-val parser can distinguish:
        #   fut_called=0 — fault fired in the harness setup; the FUT was
        #                  never called → reclassify as harness-setup-fault
        #                  (not a real-bug-shaped signal)
        #   fut_called=1 — fault fired inside (or after) the FUT call →
        #                  real-bug-shaped signal
        # The marker is async-signal-safe (just a volatile int) and adds
        # negligible runtime overhead.
        sections.append(
            "/* AMC signal handler */\n"
            "static volatile const char *_amc_signal_name = \"UNKNOWN\";\n"
            "static volatile int _amc_fut_called = 0;\n"
            "static void _amc_handler(int sig) {\n"
            "    if (sig == 11) _amc_signal_name = \"SIGSEGV\";\n"
            "    else if (sig == 6)  _amc_signal_name = \"SIGABRT\";\n"
            "    else if (sig == 8)  _amc_signal_name = \"SIGFPE\";\n"
            "    else if (sig == 4)  _amc_signal_name = \"SIGILL\";\n"
            "    printf(\"DYNAMIC:CONFIRMED signal=%s fut_called=%d\\n\","
            " (const char *)_amc_signal_name, (int)_amc_fut_called);\n"
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
        # Step A fault-site checkpoint: set the flag immediately before
        # invoking the function under test. If the signal handler fires
        # later, it can report fut_called=1; if earlier, fut_called=0.
        main_lines.append("    _amc_fut_called = 1;  /* Step A checkpoint */")
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

        # Complete-harness mode: when the function under test IS the program's
        # `main` (e.g. an SV-COMP harness that already sets up bounded inputs,
        # calls the SUT, and asserts via reach_error), the program is ALREADY a
        # faithful verification harness. The default expand-strip-stub reassembly
        # corrupts it: it stubs the (public, non-static) SUT -> masks the bug, and
        # loses the harness's input bounds -> builtin loops (strlen/memcmp) don't
        # terminate -> CBMC stalls on an unwinding artifact before reaching
        # reach_error. Emit the preprocessed program verbatim so CBMC verifies the
        # real code (exactly as --standalone does); the agentic cex / realism /
        # dynamic-validation layer still runs on the result.
        import os as _os_fm
        if fn_name == "main" and _os_fm.environ.get("BMC_FAITHFUL_MAIN", "1") != "0":
            _full_src = (
                parsed_file.preprocessed_source
                if parsed_file.preprocessed_source is not None
                else _read_source(func.source_file)
            )
            if _full_src and _full_src.strip():
                return _full_src

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
        # In kernel mode the preprocessed TU carries
        # ``__attribute__((__aligned__(...)))`` annotations whose
        # arguments contain GCC's binary-conditional ``?:`` operator
        # (kernel cacheline-padding macros). CBMC rejects ``?:`` as a
        # constant expression. Strip these annotations before any
        # further processing.
        if _preprocessed:
            type_decls = _strip_aligned_attributes(type_decls)
        _intermediate = _strip_inline_asm(
            _strip_static_assert(
                _rewrite_auto_type(_strip_gcc_addr_space_quals(type_decls))
            )
        )
        # Kernel TU: strip ALL static inlines defined in headers,
        # since most are unrelated kernel infrastructure that
        # exercises CBMC-unsupported features (anonymous-tag
        # struct inclusion, GCC statement-expression macros) and
        # produces CONVERSION ERROR at type-check time. The FUT's
        # direct callees are stubbed separately via the
        # callee-stub path, so stripping them here is safe: CBMC
        # treats unresolved calls as nondet, which is exactly what
        # a stub provides.
        _intermediate = _strip_static_inline_defs(_intermediate)
        _session_typedefs2 = set(getattr(self.config, "session_strip_typedefs", None) or [])
        _session_structs2 = set(getattr(self.config, "session_strip_structs", None) or [])
        # Kernel-TU detection: a kernel/CIL translation unit (ldv) has NO glibc
        # stdio internals. `_preprocessed` is False for pre-preprocessed .i inputs,
        # so it is NOT a reliable kernel signal. Use the absence of glibc `_IO_FILE`:
        # kernel TUs must NOT have their `__`-prefixed structs/typedefs stripped
        # (those are real kernel internals, e.g. __raw_tickets -> arch_spinlock),
        # while userspace TUs (AWS, with _IO_FILE) still strip glibc bodies.
        _is_kernel_tu = ("_IO_FILE" not in source_text)
        _km = _preprocessed or _is_kernel_tu
        type_decls = _strip_stdlib_decls(
            _strip_glibc_internal_struct_bodies(
                _strip_glibc_internal_typedefs(
                    _intermediate,
                    kernel_mode=_km,
                    extra_strip=_session_typedefs2 or None,
                ),
                kernel_mode=_km,
                extra_strip=_session_structs2 or None,
            ),
            kernel_mode=_km,
        )
        type_decls = _dedupe_typedefs(type_decls)
        if _is_kernel_tu:
            # durable: raw source decls (types+globals verbatim), no lossy extract
            type_decls = _kernel_raw_decls(source_text, parsed_file)

        # --- 2. Identify callees to stub ---
        # "local" callees: defined in this parsed file
        local_callees = func.callees & set(parsed_file.functions.keys())
        # "extern" callees: not in this file but known from other parsed files
        extern_callees = set()
        if extern_sigs:
            extern_callees = (func.callees - local_callees) & set(extern_sigs.keys())
        # "registry" callees: not in local OR extern_sigs, but in the
        # universal-stub-contract registry. These are typically OSS
        # primitives (``__archive_read_ahead``, ``archive_entry_pathname``,
        # …) whose body lives in a separate .c file we don't have parsed
        # in single-file sweeps. Without a stub the call goes through to
        # CBMC's unresolved-extern handler (pure nondet), which produces
        # the stub-callee-disconnect FP class observed on cpio.c. With a
        # stub built from the registry's canonical signature, the
        # ``_builtin_stub_return_contract``/``universal_stub_contracts``
        # postconditions kick in and the FPs disappear.
        try:
            from bmc_agent.universal_stub_contracts import known_callees as _known_stub_callees
            registry_callees = (
                (func.callees - local_callees - extern_callees) & _known_stub_callees()
            )
        except Exception:
            registry_callees = set()

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
        # Inlining advisor: reconsider callees the mechanical rule marked
        # STUB. May PROMOTE some to inline; never demotes. Gated on BOTH
        # ``inline_pure_callees`` and ``enable_inlining_advisor`` — if the
        # user explicitly disabled inlining (the first flag), the advisor
        # doesn't override that, regardless of the advisor flag's default.
        # Safe degradation: any failure (no LLM client, parse error, etc.)
        # leaves the mechanical-rule decision intact.
        if (
            getattr(self.config, "inline_pure_callees", True)
            and getattr(self.config, "enable_bmc_config_agent", False)
        ):
            # Merged BMC-config agent already decided inline-vs-stub at the
            # flag-selection phase (reading real callee bodies via tools). Apply
            # its promotions here in place of the single-call InliningAdvisor.
            # Promote-only: never demotes a mechanically-inlined callee.
            overrides = getattr(self.config, "agent_inline_overrides", None) or {}
            for cname in overrides.get(fn_name, set()):
                if cname in local_callees:
                    inline_local_callees.add(cname)
        elif (
            getattr(self.config, "inline_pure_callees", True)
            and getattr(self.config, "enable_inlining_advisor", False)
        ):
            try:
                from bmc_agent.inlining_advisor import InliningAdvisor
                from bmc_agent.llm import LLMClient
                advisor = InliningAdvisor(self.config, LLMClient(self.config))
                candidates = sorted(local_callees - inline_local_callees)
                if candidates:
                    decisions = advisor.decide(
                        candidates=candidates,
                        parsed_file=parsed_file,
                        caller_name=fn_name,
                    )
                    for cname, decision in decisions.items():
                        if decision.inline:
                            inline_local_callees.add(cname)
            except Exception as exc:
                logger.warning(
                    "inlining advisor failed for '%s' — keeping mechanical decisions: %s",
                    fn_name, exc,
                )
        # Transitive inlining (SV-COMP / bug-finding soundness): when a callee
        # is inlined (e.g. the bmc-config agent inlines the *harness*), the
        # harness's own callees — crucially the function-under-test — would
        # otherwise fall back to a nondet sibling-placeholder STUB. A stub
        # cannot exhibit the real (possibly buggy) body, so a reachability bug
        # in the SUT is MASKED (observed: aws_array_eq_c_str stubbed -> 0/7
        # bugs). Recursively pull every same-file, non-variadic, defined callee
        # reachable from an inlined function into the inline set so its REAL
        # body is verified. Gated on SV-COMP mode to avoid changing the general
        # compositional-scaling behaviour.
        import os as _os_ti
        # Body-text call-edge augmentation (BMC_CONE_PROP, default ON): the
        # parser call_graph misses call edges hidden behind CIL temporaries /
        # indirection (e.g. driver_open -> mutex_lock). Those gaps drop a
        # side-effecting driver fn from the reach_error cone -> it is stubbed ->
        # its ldv_mutex write vanishes -> the lock-protocol bug can never fire
        # ("miss", verified on cpia2). Recover the missing edges by scanning each
        # body for `name(` tokens that name a defined function; used to augment
        # BOTH the forward transitive-inline expansion AND the backward cone, so
        # every function on a main->sink path stays inlined with its real body
        # (side-effects preserved) while purely-computational off-path code is
        # still stubbed (no OOM). Toggle off with BMC_CONE_PROP=0.
        _text_edges: dict = {}
        if _os_ti.environ.get("BMC_CONE_PROP", "1") != "0" and _is_kernel_tu:
            import re as _re_cp
            _callre_cp = _re_cp.compile(r"\b([A-Za-z_]\w*)\s*\(")
            _names_cp = set((parsed_file.functions or {}).keys())
            for _fnx, _fix in (parsed_file.functions or {}).items():
                _bodyx = (getattr(_fix, "body", "") or "")
                if not _bodyx:
                    continue
                _tc = set(_callre_cp.findall(_bodyx)) & _names_cp
                _tc.discard(_fnx)
                if _tc:
                    _text_edges[_fnx] = _tc
        # SV-COMP / ldv driver SEED: for a kernel TU whose entry is the LDV
        # `main` sequence harness, `main`'s DIRECT local callees ARE the driver
        # functions under test (dp83640_probe/remove/hwtstamp/...). They are
        # large, non-static, loop-bearing => _should_inline_callee rejects them =>
        # they fall back to nondet `_stub`s => the bug inside them is UNREACHABLE
        # (masked: "0 real bugs" on a bug task). The transitive-inline frontier
        # below only expands from ALREADY-inlined fns, so an empty seed never
        # reaches the driver. Seed the inline set with every defined, non-variadic
        # DIRECT callee of the entry so the whole driver call-tree is inlined with
        # REAL bodies while the kernel API (declared-only, no body in this TU)
        # stays stubbed automatically. This is the "inline the driver, stub the
        # kernel" lever. Gated on kernel TU + BMC_TRANSITIVE_INLINE so AWS
        # userspace and the general compositional path are untouched.
        if (_os_ti.environ.get("BMC_TRANSITIVE_INLINE") and _is_kernel_tu
                and fn_name == "main"):
            for _dc in sorted(local_callees):
                if _dc in inline_local_callees:
                    continue
                _dcfi = parsed_file.get_function_info(_dc)
                if _dcfi is None or not (_dcfi.body or "").strip():
                    continue
                _dc_variadic = bool(any(
                    (pt == "..." or "va_list" in (pt or ""))
                    for pt, _ in (getattr(_dcfi.signature, "parameters", None) or [])))
                if _dc_variadic:
                    continue
                inline_local_callees.add(_dc)
        if _os_ti.environ.get("BMC_TRANSITIVE_INLINE") and inline_local_callees:
            _cg = getattr(parsed_file, "call_graph", None) or {}
            if _text_edges:
                _cg = dict(_cg)
                for _k, _v in _text_edges.items():
                    _cg[_k] = (_cg.get(_k) or set()) | _v
            _defined = set(parsed_file.functions.keys())
            _frontier = list(inline_local_callees)
            while _frontier:
                _fn = _frontier.pop()
                for _callee in _cg.get(_fn, set()):
                    if (_callee in _defined and _callee not in inline_local_callees
                            and _callee != fn_name):
                        _ok, _ = _should_inline_callee(_callee, parsed_file,
                                                       max_loc=10**9, size_helper_max_loc=10**9)
                        _cfi = parsed_file.get_function_info(_callee)
                        # _should_inline_callee still rejects variadic / non-static-
                        # but-undefined; for SV-COMP we want any defined non-variadic
                        # body, so accept on body presence unless it is variadic.
                        _variadic = bool(_cfi and any(
                            (pt == "..." or "va_list" in (pt or ""))
                            for pt, _ in (getattr(_cfi.signature, "parameters", None) or [])))
                        if _cfi and (_cfi.body or "").strip() and not _variadic:
                            inline_local_callees.add(_callee)
                            _frontier.append(_callee)
        # CONE-SLICE (BMC_CONE_SLICE, bug-finding only): the full-inline kernel
        # harness bit-blasts past the mandatory memory cap (SAT-OOM) even at low
        # unwind. Shrink the formula by inlining ONLY functions in the reach_error
        # BACKWARD CONE — those that transitively call a "checked" assert-bearing
        # LDV model function (ldv_blast_assert / mutex_* / ldv_check_final_state /
        # reach_error / __VERIFIER_error). Functions outside the cone are stubbed
        # (nondet). SOUNDNESS: a nondet stub OVER-approximates the callee (adds
        # reachable states) -> it can introduce a spurious counterexample but
        # CANNOT mask a real reach_error -> SOUND FOR BUG-FINDING. It is NOT sound
        # for PROVING safety (a nondet stub may false-alarm), so this lever must
        # only be applied to bug/false tasks; gated on BMC_CONE_SLICE which the
        # driver sets only when hunting a bug. Gated additionally on kernel TU +
        # BMC_TRANSITIVE_INLINE so AWS / general compositional paths are untouched.
        if (_os_ti.environ.get("BMC_CONE_SLICE")
                and _os_ti.environ.get("BMC_TRANSITIVE_INLINE")
                and _is_kernel_tu and fn_name == "main" and inline_local_callees):
            _cg2 = getattr(parsed_file, "call_graph", None) or {}
            if _text_edges:
                _cg2 = dict(_cg2)
                for _k, _v in _text_edges.items():
                    _cg2[_k] = (_cg2.get(_k) or set()) | _v
            # CORE assertion/check sinks (these CALL ldv_error/reach_error): always
            # in the cone so every fn that reaches an assertion stays inlined.
            _checked = {"ldv_blast_assert", "ldv_error", "ldv_assert",
                        "ldv_check_final_state", "reach_error",
                        "__VERIFIER_error", "__VERIFIER_assert",
                        # GFP-flag / atomic-context allocation CHECKS (assert)
                        "ldv_check_alloc_flags", "ldv_check_alloc_nonatomic",
                        "ldv_check_alloc_flags_and_return_some_page",
                        "ldv_after_alloc"}
            # Lock-state-MODELING primitives below are NOT assertion sinks; they
            # were force-added to _checked only to pull their callers into the
            # cone (they write the "in atomic" global that ldv_check_alloc_* reads
            # via def-use the call-graph can't see) — needed to avoid FALSE ALARMS
            # when PROVING safe tasks. But locking is pervasive: keeping them makes
            # the cone engulf the WHOLE driver -> zero formula reduction -> timeout
            # (mousedev/orinoco/pch_udc). For BUG-FINDING the force-inline is
            # unnecessary: a nondet stub OVER-approximates the lock-state global
            # (the real buggy value is included), so a real reach_error stays
            # reachable — SOUND (no masked bugs), at the cost of possibly more
            # spurious cex (filtered by realism/feasibility). Drop them under
            # BMC_CONE_TIGHT to get the minimal sound bug-finding cone.
            if not _os_ti.environ.get("BMC_CONE_TIGHT"):
                _checked |= {
                        "mutex_lock", "mutex_unlock", "mutex_lock_interruptible",
                        "mutex_lock_killable", "mutex_trylock",
                        "atomic_dec_and_mutex_lock",
                        "ldv_spin_lock", "ldv_spin_unlock",
                        "spin_lock", "spin_unlock",
                        "spin_lock_irqsave", "spin_unlock_irqrestore",
                        "spin_lock_irq", "spin_unlock_irq",
                        "spin_lock_bh", "spin_unlock_bh",
                        "_raw_spin_lock", "_raw_spin_unlock"}
            # Forward-reachability: fn is in cone iff it can transitively reach
            # any _checked fn via the call graph. Compute by fixpoint over the
            # reverse: start with _checked, repeatedly add any caller of a
            # cone member.
            _cone = set(_checked)
            _changed = True
            while _changed:
                _changed = False
                for _caller, _callees in _cg2.items():
                    if _caller in _cone:
                        continue
                    if _callees & _cone:
                        _cone.add(_caller)
                        _changed = True
            # Restrict the inline set to cone members. ALWAYS keep the SV-COMP
            # assumption primitives + the checked fns (handled below / via
            # _kernel_raw_decls). Everything inlined-but-out-of-cone is dropped
            # back to a nondet stub.
            _keep_always = _checked | {"assume_abort_if_not", "__VERIFIER_assume",
                                       "assume_abort_if_unreachable",
                                       "__VERIFIER_assume_abort", "abort"}
            _before = len(inline_local_callees)
            inline_local_callees = {c for c in inline_local_callees
                                    if c in _cone or c in _keep_always}
            if _os_ti.environ.get("BMC_CONE_PROP", "1") != "0":
                # Inline the COMPLETE backward-from-sink cone, not just its
                # intersection with the forward inline-candidate set. The
                # candidate set (_should_inline_callee + parser-graph reachability)
                # has gaps that drop functions which DO reach an assertion (8 of
                # cpia2's 18-fn cone) -> they get stubbed -> the bug path is broken
                # -> "miss". Every cone member can reach a property sink, so it is
                # part of the property-relevant slice and must keep its REAL body.
                # Restrict to defined, non-variadic, body-bearing fns (declared-only
                # kernel API can't be inlined and is correctly left as a stub).
                for _cm in _cone:
                    if _cm in inline_local_callees or _cm == fn_name:
                        continue
                    _cmfi = parsed_file.get_function_info(_cm)
                    if _cmfi is None or not (_cmfi.body or "").strip():
                        continue
                    _cm_variadic = bool(any(
                        (pt == "..." or "va_list" in (pt or ""))
                        for pt, _ in (getattr(_cmfi.signature, "parameters", None) or [])))
                    if _cm_variadic:
                        continue
                    inline_local_callees.add(_cm)
            try:
                logger.info("cone-slice: inline set %d -> %d (reach_error backward cone)",
                            _before, len(inline_local_callees))
            except Exception:
                pass
            try:
                import sys as _sysdbg
                print("CONE-SLICE-RESULT %d -> %d tight=%r" % (_before, len(inline_local_callees), bool(_os_ti.environ.get("BMC_CONE_TIGHT"))), file=_sysdbg.stderr, flush=True)
            except Exception:
                pass
        # SV-COMP assumption primitives MUST be inlined with their real bodies
        # (e.g. assume_abort_if_not { if(!c) abort(); }); declared-only -> CBMC
        # treats them as no-ops -> harness preconditions lost -> spurious reach_error
        # (false alarms on safe tasks). Force-inline any defined variant.
        # NOTE: for kernel TUs the primitive is kept in inline_local_callees here
        # (so its call sites are NOT rewritten to _stub), but its body is NOT
        # re-emitted in the inline-body loop below — _kernel_raw_decls already
        # keeps the real body verbatim (its _KEEP_BODY set), and a second emission
        # gives "function body 'assume_abort_if_not' defined twice" (mousedev).
        if _os_ti.environ.get("BMC_TRANSITIVE_INLINE"):
            for _prim in ("assume_abort_if_not", "__VERIFIER_assume",
                          "assume_abort_if_unreachable", "__VERIFIER_assume_abort"):
                if _prim in (parsed_file.functions or {}) and _prim not in inline_local_callees:
                    _pf = parsed_file.get_function_info(_prim)
                    if _pf and (_pf.body or "").strip():
                        inline_local_callees.add(_prim)
        stubbed_local_callees = local_callees - inline_local_callees
        all_stub_callees = stubbed_local_callees | extern_callees | registry_callees

        # --- 3. Generate stubs for each callee that wasn't inlined ---
        stub_sections: list[str] = []
        for callee_name in sorted(all_stub_callees):
            callee_spec = spec.callee_specs.get(callee_name)
            stub_src = _generate_stub(
                callee_name,
                callee_spec,
                parsed_file,
                extern_sigs,
                assume_postcondition=getattr(self.config, "assume_callee_postcondition", False),
                    verified_sound=(getattr(self.config, "verified_sound_functions", None)
                                    if getattr(self.config, "assume_verified_callee_postcondition", False) else None),
                copy_field_maxlen=_copy_field_plan(self.config, func),
                struct_definitions=getattr(parsed_file, "struct_definitions", None),
            )
            stub_sections.append(stub_src)

        # --- 3a. Emit inlined-callee bodies verbatim ---
        # The inlined callee may itself call into ``all_stub_callees``; rewrite
        # those nested calls so the inlined body compiles in the harness.
        # For kernel TUs, _kernel_raw_decls already keeps the verbatim body of the
        # verification/assumption primitives (its _KEEP_BODY set); re-emitting any
        # of them here would be "function body X defined twice" (mousedev:
        # assume_abort_if_not). Keep them in inline_local_callees (so call sites
        # stay un-rewritten and resolve to the kept body) but skip re-emission.
        _KERNEL_KEEP_BODY_NAMES = {
            "ldv_blast_assert", "ldv_error", "ldv_assert", "__VERIFIER_error",
            "__VERIFIER_assert", "reach_error", "abort", "assume_abort_if_not",
            "__VERIFIER_assume", "ldv_assume",
        }
        inline_func_defs: list[str] = []
        for cname in sorted(inline_local_callees):
            if _is_kernel_tu and cname in _KERNEL_KEEP_BODY_NAMES:
                continue  # body already kept verbatim by _kernel_raw_decls
            cfi = parsed_file.get_function_info(cname)
            if cfi is None:
                continue
            cbody = _substitute_callee_calls(cfi.body, all_stub_callees)
            cparams = _params_str(cfi.signature.parameters)
            inline_func_defs.append(
                f"/* Inlined real callee body: {cname} */\n"
                f"{'' if _is_kernel_tu else 'static '}{cfi.signature.return_type} {cname}({cparams})\n{cbody}"
            )

        # --- 3b. Emit placeholder bodies for sibling functions ---
        # Any function defined in the same file as the FUT but not called
        # by it. Linux drivers register a dispatch table at TU scope
        # (``static struct usb_serial_driver ch341_device = { .open =
        # ch341_open, .close = ch341_close, ... };``) that takes the
        # address of every entry-point function. With the FUT body
        # excised from type_decls and only the FUT's body added back,
        # those address-of references fail to resolve and CBMC errors:
        # ``failed to find symbol 'ch341_open'``.
        #
        # The fix is to emit a no-op body with the ORIGINAL name (not
        # the ``_stub`` suffix used for callees, because the dispatch
        # table references the original). The body is a havoc: declare
        # a result of the return type, return it. Sibling bodies are
        # never actually invoked in the per-function harness — the
        # dispatch table is at TU scope, not called from anywhere in
        # ``main()`` — so the body shape is irrelevant; we just need
        # the symbol to exist.
        sibling_func_defs: list[str] = []
        already_defined = (
            {fn_name}
            | inline_local_callees
            | {f"{c}_stub" for c in all_stub_callees}
        )
        for sib_name in sorted(parsed_file.functions.keys()):
            if sib_name in already_defined:
                continue
            if sib_name in all_stub_callees:
                continue
            sib_sig = parsed_file.functions[sib_name]
            sib_params = _params_str(sib_sig.parameters)
            sib_ret = sib_sig.return_type.strip()
            sib_ret_bare = _ret_type_bare(sib_ret)
            body_lines = [
                f"/* Sibling placeholder (referenced by TU-scope dispatch table): {sib_name} */",
                f"{sib_ret} {sib_name}({sib_params}) {{",
            ]
            # D(i)/G5: model output params (bounded count, sized buffer) so a
            # havoc'd sibling (e.g. TIFFReadDirEntryArrayWithLimit) doesn't hand
            # the caller an unbounded count + undersized buffer -> spurious OOB.
            body_lines.extend(_emit_stub_output_param_init(sib_sig.parameters))
            if sib_ret_bare == "void":
                body_lines.append("    /* void sibling — no return */")
            else:
                body_lines.append(f"    {sib_ret_bare} _r;")
                body_lines.append("    return _r;")
            body_lines.append("}")
            sibling_func_defs.append("\n".join(body_lines))

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
            infer_field_validity=getattr(self.config, "infer_field_validity", False),
            infer_struct_field_validity=getattr(self.config, "infer_struct_field_validity", False),
            infer_array_param_bounds=(getattr(self.config, "infer_array_param_bounds", False) or getattr(self.config, "scale_down", False)),
            infer_array_param_bounds_max=getattr(self.config, "infer_array_param_bounds_max", 64),
            scale_down=getattr(self.config, "scale_down", False),
            scale_down_size=getattr(self.config, "scale_down_size", 4),
            force_opaque_structs=set(getattr(self.config, "session_opaque_param_structs", None) or []) or None,
            copy_source_max_len=(getattr(self.config, "string_copy_source_max_len", 0)
                                 if getattr(self.config, "enable_string_copy_source_modeling", True) else 0),
            copy_source_max_dest=getattr(self.config, "string_copy_source_max_dest", 256),
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
        # Apply persisted POST relaxations BEFORE callee->stub rewriting
        # so the drop comparison sees the same shape the LLM emitted.
        post_relax = self._function_post_relaxations(fn_name)
        if post_relax:
            from bmc_agent.spec import drop_clauses as _drop_post
            postcond_for_assert = _drop_post(postcond_for_assert, post_relax)
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
        preprocessed = (parsed_file.preprocessed_source is not None) or _is_kernel_tu  # kernel/.i TU -> minimal libc includes (avoid dev_t/sys-types redef)
        if preprocessed:
            inc_lines: list[str] = []
            # The kernel preprocessor expands ``NULL`` to ``((void *)0)``
            # everywhere it appears in the original source, but our own
            # harness emitter uses the literal token ``NULL`` (in
            # self-ref-chain termination, nondet pointer init, etc.).
            # Without ``<stddef.h>`` prepended (no libc in kernel mode)
            # that token is undefined; CBMC reports ``failed to find
            # symbol 'NULL'``. Provide a local fallback.
            #
            # Also: check whether the preprocessed source already
            # provides a full body for ``struct pci_dev`` (different
            # kernel TUs include different parts of the PCI subsystem;
            # neuron_pid.c only sees the forward-decl, but neuron_cdev.c
            # pulls in the full body via deeper includes). If the body
            # IS present, our placeholder would clash; if it isn't,
            # M1's ``sizeof(*pdev)`` in struct-field validity init
            # errors on the incomplete type.
            # NOTE: ``parsed_file.preprocessed_source`` is None for pre-preprocessed
            # .i inputs (ldv/CIL), so it is an UNRELIABLE kernel signal — using it
            # made ``_has_pci_dev_body`` False on ldv even when the source defines
            # ``struct pci_dev`` itself, so the opaque placeholder was injected and
            # collided -> "redefinition of body of 'struct pci_dev'" CONVERSION
            # ERROR (mfd mask). Use the reliable ``source_text`` (same var as
            # ``_has_atomic_t`` below).
            _src_text = parsed_file.preprocessed_source or source_text or ""
            _has_pci_dev_body = bool(
                re.search(r"\bstruct\s+pci_dev\s*\{", source_text)
            )
            # CIL/full-kernel TUs define their OWN atomic_t (e.g.
            # ``typedef struct __anonstruct_atomic_t_6 atomic_t;``); injecting our
            # model then yields "type symbol atomic_t defined twice". Only inject
            # the model when the source does NOT define it (hand-written driver).
            _has_atomic_t = bool(
                re.search(r"(typedef\b[^;]{0,400}\batomic_t\s*;|\}\s*atomic_t\s*;)", source_text)
            )
            # CIL/full-kernel TUs that include the relevant kernel headers
            # define these current-task / device helpers THEMSELVES (e.g.
            # ``__inline static struct task_struct *get_current(void)``), and the
            # CIL definition returns ``struct task_struct *`` — NOT the ``void *``
            # our injected prototype declares. Injecting the prototype then yields
            # "function symbol 'get_current' redefined with a different type"
            # CONVERSION ERROR (mousedev). Only inject each helper prototype when
            # the source does NOT already declare/define it. ``\b<name>\s*\(``
            # matches a declaration or definition (and the prototype line), not a
            # call (calls also match, which only makes us conservatively SKIP the
            # injection — safe, since a called symbol is declared somewhere).
            def _src_declares(_name: str) -> bool:
                return bool(re.search(rf"\b{_name}\b", source_text))
            def _src_decl_or_def(_name: str) -> bool:
                # True iff the source TU itself declares OR defines this
                # function (type-prefixed ``name( ... ) ;`` or ``{``), as
                # opposed to merely *calling* it. We only suppress our
                # fallback prototype when the source genuinely declares the
                # symbol with its own (possibly conflicting) signature; if
                # the symbol is only called (body+decl stripped), we KEEP the
                # fallback so it stays declared. Excludes assignments (``=``)
                # and statements with a leading ``;`` so call sites don't match.
                return bool(re.search(
                    rf"(?m)^[^\n;=]*\b{re.escape(_name)}\s*\([^;{{}}]*\)\s*[;{{]",
                    source_text,
                ))
            def _filter_conflicting_protos(block: str, src: str) -> str:
                # Drop any fallback *prototype* line whose function the source
                # already declares/defines, to avoid CBMC "function symbol 'X'
                # redefined with a different type" CONVERSION ERRORs (e.g. the
                # kernel's own ``int __ilog2_u32(u32)`` vs our
                # ``unsigned long __ilog2_u32(unsigned int)``). #defines,
                # typedefs and comments never match the prototype shape, so
                # they are preserved verbatim. General across the whole
                # intrinsic-stub block, not a per-name allowlist.
                _proto = re.compile(
                    r"^\s*(?:extern\s+|static\s+|__inline\s+|inline\s+)*"
                    r"[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;{]*\)\s*;\s*$"
                )
                kept = []
                dropped = []
                for ln in block.split("\n"):
                    m = _proto.match(ln)
                    if m and _src_decl_or_def(m.group(1)):
                        dropped.append(m.group(1))
                        continue
                    kept.append(ln)
                if dropped:
                    from bmc_agent.logger import get_logger
                    get_logger("harness").info(
                        "kernel-intrinsic proto filter: dropped %d fallback "
                        "prototype(s) the source already declares: %s",
                        len(dropped), ", ".join(sorted(set(dropped))),
                    )
                return "\n".join(kept)
            _has_get_current = _src_declares("get_current")
            _has_task_tgid_nr = _src_declares("task_tgid_nr")
            _has_task_pid_nr = _src_declares("task_pid_nr")
            _has_device_init_wakeup = _src_declares("device_init_wakeup")
            _km_prologue = (
                "/* Kernel-mode harness — provide minimal stddef/assert */\n"
                "#ifndef NULL\n"
                "#define NULL ((void *)0)\n"
                "#endif\n"
                "\n"
                "/* Common kernel macros referenced by LLM-emitted specs.\n"
                " * These are normally provided by ``<linux/kernel.h>``,\n"
                " * ``<asm/page.h>``, ``<linux/errno.h>``, etc. The harness\n"
                " * doesn't pull those in, so the LLM's spec atoms that\n"
                " * mention them by symbol (``valid_range(buf, 0,\n"
                " * PAGE_SIZE)``, ``result != -EFAULT``, ...) would fail\n"
                " * to compile. Provide weak defaults so the harness\n"
                " * compiles and the spec atom translates to a usable\n"
                " * constraint.\n"
                " */\n"
                "#ifndef PAGE_SIZE\n"
                "#define PAGE_SIZE 4096\n"
                "#endif\n"
                "#ifndef PAGE_SHIFT\n"
                "#define PAGE_SHIFT 12\n"
                "#endif\n"
                "#ifndef EFAULT\n"
                "#define EFAULT 14\n"
                "#endif\n"
                "#ifndef EINVAL\n"
                "#define EINVAL 22\n"
                "#endif\n"
                "#ifndef ENOMEM\n"
                "#define ENOMEM 12\n"
                "#endif\n"
                "#ifndef EAGAIN\n"
                "#define EAGAIN 11\n"
                "#endif\n"
                "#ifndef EIO\n"
                "#define EIO 5\n"
                "#endif\n"
                "#ifndef ENODEV\n"
                "#define ENODEV 19\n"
                "#endif\n"
                "\n"
                "/* Kernel-intrinsic stubs.\n"
                " *\n"
                " * Kernel TUs reference a long tail of kernel-internal helpers\n"
                " * (atomic ops, allocator hot paths, user-access primitives, etc.)\n"
                " * that are typically ``static inline`` in headers. The kernel-mode\n"
                " * type-decl extractor strips those bodies because most reference\n"
                " * CBMC-unsupported features (anonymous-tag struct inclusion,\n"
                " * statement-expression macros). The FUT then references them as\n"
                " * unresolved symbols → CONVERSION ERROR.\n"
                " *\n"
                " * Provide non-strict ``extern``-style stubs for the most common\n"
                " * names so CBMC at least compiles the harness. CBMC then treats\n"
                " * the calls as nondet, which is the right semantics for stub\n"
                " * functions in BMC.\n"
                " */\n"
                "void *kmalloc_noprof(unsigned long size, unsigned int flags);\n"
                "void *kzalloc_noprof(unsigned long size, unsigned int flags);\n"
                "void *kcalloc_noprof(unsigned long n, unsigned long size, unsigned int flags);\n"
                "void *kmalloc_array_noprof(unsigned long n, unsigned long size, unsigned int flags);\n"
                "void *vmalloc_noprof(unsigned long size);\n"
                "void *vzalloc_noprof(unsigned long size);\n"
                "void kfree(const void *ptr);\n"
                "void vfree(const void *ptr);\n"
                "int mem_alloc_profiling_enabled(void);\n"
                "int __access_ok(const void *addr, unsigned long size);\n"
                "void stac(void);\n"
                "void clac(void);\n"
                "void barrier(void);\n"
                "unsigned long __ilog2_u32(unsigned int n);\n"
                "unsigned long __ilog2_u64(unsigned long long n);\n"
                "long copy_from_user_nofault(void *to, const void *from, unsigned long n);\n"
                "long copy_to_user_nofault(void *to, const void *from, unsigned long n);\n"
                "unsigned long _copy_from_user(void *to, const void *from, unsigned long n);\n"
                "unsigned long _copy_to_user(void *to, const void *from, unsigned long n);\n"
                "int _test_bit(unsigned long nr, const volatile void *addr);\n"
                "int const_test_bit(unsigned long nr, const volatile void *addr);\n"
                "void set_bit(unsigned long nr, volatile unsigned long *addr);\n"
                "void clear_bit(unsigned long nr, volatile unsigned long *addr);\n"
                "int test_and_set_bit(unsigned long nr, volatile unsigned long *addr);\n"
                "int test_and_clear_bit(unsigned long nr, volatile unsigned long *addr);\n"
                # cdev/inode helpers from <linux/cdev.h> / <linux/fs.h>.
                # ``iminor`` extracts the minor part of an inode's
                # device number; called by char-device handlers'
                # ``open`` callbacks. Without this stub, ncdev_open
                # (and many other driver open functions) fail with
                # ``function 'iminor' is not declared``.
                "unsigned int iminor(const struct inode *inode);\n"
                "unsigned int imajor(const struct inode *inode);\n"
                # NOTE: get_device/put_device/device_init_wakeup come
                # from <linux/device.h> with struct device* signatures;
                # do not stub them here, the preprocessed kernel headers
                # already declare the right shape.
                ""

                + ("/* atomic_t model (source lacks its own) */\n"
                 "typedef struct { int counter; } atomic_t;\n"
                 "typedef struct { long long counter; } atomic64_t;\n"
                 "void atomic_set(atomic_t *v, int i);\n"
                "int  atomic_read(const atomic_t *v);\n"
                "void atomic_inc(atomic_t *v);\n"
                "void atomic_dec(atomic_t *v);\n"
                "int  atomic_add_return(int i, atomic_t *v);\n"
                "int  atomic_sub_return(int i, atomic_t *v);\n"
                "void atomic64_set(atomic64_t *v, long long i);\n"
                 "long long atomic64_read(const atomic64_t *v);\n"
                 if not _has_atomic_t else "")
                + "/* get_current / task_tgid_nr stubs (kernel current-task helpers).\n"
                " * Each is injected ONLY when the source does not already declare it\n"
                " * (CIL TUs define get_current returning struct task_struct* -> our\n"
                " * void* prototype would collide: 'redefined with a different type'). */\n"
                + ("void *get_current(void);\n" if not _has_get_current else "")
                + ("int task_tgid_nr(void *task);\n" if not _has_task_tgid_nr else "")
                + ("int task_pid_nr(void *task);\n" if not _has_task_pid_nr else "")
                + ("int device_init_wakeup(void *dev, int val);\n" if not _has_device_init_wakeup else "")
                + (
                    "/* Opaque-struct full-definition backing for struct pci_dev.\n"
                    " * Forward-decl only in this TU; the full body lives in\n"
                    " * drivers/pci/pci.h which isn't pulled in. M1's struct-\n"
                    " * field validity init emits ``sizeof(*pdev)`` which\n"
                    " * errors on an incomplete type. Placeholder >=2K. */\n"
                    "struct pci_dev { unsigned char _opaque[2048]; };\n"
                    if not _has_pci_dev_body else ""
                )
            )
            sections.append(_filter_conflicting_protos(_km_prologue, source_text))
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

        # Sibling placeholders — provide a definition for every same-file
        # function the FUT doesn't call directly, so address-of references
        # from TU-scope dispatch tables (Linux driver registration structs)
        # link successfully.
        if sibling_func_defs and not _is_kernel_tu:  # kernel: _kernel_raw_decls already declares all fns; placeholders would duplicate (e.g. ldv_blast_assert)
            sections.append("/* --- Sibling placeholders (for TU-scope dispatch tables) --- */")
            sections.extend(sibling_func_defs)

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

        # Step 1.5c: evidence-grounded global invariants derived from the
        # source's own global write-sets (proactive; see
        # _emit_global_invariant_assumptions).
        # MATERIALIZE init-trusted pointer globals FIRST: a global like
        # ``static vfs_node_t *mem_root = NULL;`` keeps its NULL initializer in
        # the harness (the init function that sets it never runs), so a bare
        # ``__CPROVER_assume(mem_root != NULL)`` is ``assume(NULL != NULL)`` =
        # ``assume(false)`` -> the whole function verifies VACUOUSLY, silently
        # masking every bug in it. ``if (!g) g = calloc(1, sizeof(*g));`` gives
        # the assume a satisfiable witness (same materialization the dynamic
        # harness uses), so verification is real, not vacuous. The
        # incomplete-tree CEXs this surfaces are harness artifacts the realism
        # gate filters (a zeroed kernel-init object is not attacker-reachable).
        gi_inits = _emit_dynamic_global_invariant_inits(parsed_file, self.config, None)
        gi_assumes = _emit_global_invariant_assumptions(parsed_file, self.config)
        if gi_inits or gi_assumes:
            harness_body_lines.append("")
            harness_body_lines.append("    /* Step 1.5c: evidence-grounded global invariants */")
            for s in gi_inits:
                harness_body_lines.append(f"    {s}")
            for s in gi_assumes:
                harness_body_lines.append(f"    {s}")

        # Step 1.6: project-wide invariants learned from prior realism
        # rejections (feedback loop arm (c)). Off unless enable_feedback_loop.
        # Gate by current function's param names so clauses distilled from
        # a different function (with differently-named params, e.g. ``a`` vs
        # ``_a``) aren't blindly applied and break CBMC parse.
        proj_clauses = _emit_learned_clauses(
            self.config, fn_name, "project",
            param_names=set(param_names),
        )
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

        # Step 1.9: scale-down bounds on ML parametric-size value params.
        # Bounds B, T, C, NH, V, Vp, OC, ... to [0, scale_down_size] so
        # float-arithmetic kernels (matmul, attention, layernorm) become
        # tractable instead of CBMC enumerating B*T*C inner-loop iterations
        # at arbitrarily-large sizes.
        if getattr(self.config, "scale_down", False):
            sd_assumes = _scale_down_assumes(
                func, getattr(self.config, "scale_down_size", 4),
            )
            if sd_assumes:
                harness_body_lines.append("")
                harness_body_lines.append(
                    "    /* Step 1.9: scale-down bounds on ML parametric-size params */"
                )
                for s in sd_assumes:
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
        # If any param is named ``result`` (e.g. libarchive's ``isint(start,
        # end, int *result)``), the return-value variable can't also be
        # named ``result`` — the param backing storage declared earlier
        # already binds that name, and CBMC reports
        # ``symbol 'main::1::result' redefined with a different type``.
        # Detected on libarchive archive_acl.c::isint/isint_w/isint_w
        # (2026-05-23 sweep).
        _ret_var = "result"
        if any(pn == _ret_var for _, pn in sig.parameters):
            _ret_var = "_amc_ret"
        harness_body_lines.append("")
        harness_body_lines.append("    /* Step 3: call the function under test */")
        _ret_bare_main = _ret_type_bare(ret_type)
        if _ret_bare_main == "void":
            harness_body_lines.append(f"    {fn_name}({call_args});")
        else:
            harness_body_lines.append(f"    {_ret_bare_main} {_ret_var} = {fn_name}({call_args});")
            harness_body_lines.append(f"    (void){_ret_var};  /* suppress unused-variable warning */")

        # Step 4: assert postcondition
        harness_body_lines.append("")
        harness_body_lines.append(
            "    /* Step 4: assert postcondition */\n"
            "    /* (CBMC also checks OOB, null deref, overflow automatically) */"
        )
        for stmt in assert_stmts:
            for sub_line in stmt.splitlines():
                harness_body_lines.append(f"    {sub_line}")

        # When the function under test IS the program entry `main` (e.g. an
        # SV-COMP harness `int main(void){ X_harness(); return 0; }`), the
        # program is ALREADY a complete verification harness. Synthesizing a
        # second `void main(void)` wrapper that calls main() collides with the
        # real main -> CBMC "function symbol 'main' redefined" CONVERSION ERROR
        # -> the task silently returns "no bug". Verify the existing main
        # directly instead (no synthesized wrapper).
        if fn_name != "main":
            harness_main = (
                "void main(void) {\n"
                + "\n".join(harness_body_lines)
                + "\n}"
            )
            sections.append(
                f"/* --- Harness entry point --- */\n"
                + harness_main
            )

        harness = "\n\n".join(sections) + "\n"
        # Neutralize the SOURCE's own ``int main()`` (e.g. an SV-COMP driver
        # ``int main(){ X_harness(); return 0; }``, or any program-with-main)
        # when we have synthesized our own ``void main(void)`` entry above
        # (fn_name != "main"). Otherwise CBMC reports "function symbol 'main'
        # redefined with a different type" CONVERSION ERROR (exit 6) and the
        # whole per-function verification is skipped -> silent 0 bugs. The
        # real-libc path already guards this (check_<fn> + --function); the
        # compositional path did not. Rename the int-returning main (def +
        # any forward decl) to a dead name; our void main stays the entry.
        if fn_name != "main":
            import re as _re_dm
            harness = _re_dm.sub(r"\bint(\s+)main(\s*\()", r"int\1__amc_src_main\2", harness)
        # D-ii: inject signature-matched stubs for any assigned callback fields.
        harness = _inject_funcptr_stubs(harness, source_text)
        return harness

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

        # When the auto-retry path has populated
        # ``config.session_stub_functions`` (RetryAction.STUB_CALLEE
        # fired on a prior TIMEOUT), produce a STUBBED copy of the
        # source and ``#include`` that instead. The stubbed source
        # replaces selected callee bodies with nondet returns, cutting
        # CBMC's state space dramatically for the retry. We write a
        # fresh temp file (don't mutate the original) so the stub is
        # reproducible from the harness — anyone looking at the
        # harness sees both files side-by-side. Source path lives next
        # to the original tmp so the relative ``-I`` resolution works.
        stub_names = set(
            getattr(self.config, "session_stub_functions", None) or []
        )
        if stub_names:
            try:
                src_text = src_path.read_text(encoding="utf-8")
                stubbed_text, stubbed_set = _replace_function_bodies_with_stubs(
                    src_text, stub_names
                )
                if stubbed_set:
                    import tempfile as _tempfile
                    stub_tag = "_".join(sorted(stubbed_set))[:60]
                    with _tempfile.NamedTemporaryFile(
                        suffix=".c",
                        prefix=f"amc_stubbed_{src_path.stem}_{stub_tag}_",
                        mode="w", encoding="utf-8", delete=False,
                    ) as stub_tmp:
                        stub_tmp.write(stubbed_text)
                        include_target = stub_tmp.name
            except Exception:
                # Best-effort: if we can't read or rewrite, fall back to
                # the original source (the CBMC retry then re-times-out,
                # which is no worse than the pre-fix behaviour).
                pass

        # Nondeterministic input declarations — reuse the existing helper.
        nonnull = _infer_nonnull_params(func, all_funcs, parsed_file)
        nd_decls = _generate_nd_decls(
            func,
            self.config.cbmc_unwind,
            nonnull_params=nonnull,
            precondition=spec.precondition,
            raw_bytes=getattr(self.config, "raw_bytes", False),
            struct_definitions=getattr(parsed_file, "struct_definitions", None),
            infer_field_validity=getattr(self.config, "infer_field_validity", False),
            infer_struct_field_validity=getattr(self.config, "infer_struct_field_validity", False),
            infer_array_param_bounds=(getattr(self.config, "infer_array_param_bounds", False) or getattr(self.config, "scale_down", False)),
            infer_array_param_bounds_max=getattr(self.config, "infer_array_param_bounds_max", 64),
            scale_down=getattr(self.config, "scale_down", False),
            scale_down_size=getattr(self.config, "scale_down_size", 4),
            force_opaque_structs=set(getattr(self.config, "session_opaque_param_structs", None) or []) or None,
            copy_source_max_len=(getattr(self.config, "string_copy_source_max_len", 0)
                                 if getattr(self.config, "enable_string_copy_source_modeling", True) else 0),
            copy_source_max_dest=getattr(self.config, "string_copy_source_max_dest", 256),
        )

        # Precondition assume + postcondition assert via existing DSL.
        param_names = [pname for _, pname in sig.parameters if pname]
        assume_stmts = precond_to_assume(spec.precondition, param_names)
        # Name-collision guard for the return-value variable: if the
        # function has a parameter literally named ``result``
        # (libarchive isint/isint_w take a ``int *result`` output
        # param), we can't use ``result`` as the captured return-var
        # — CBMC trips ``CONVERSION ERROR: symbol 'main::1::result'
        # redefined with a different type``. Rename to ``_amc_ret``
        # and thread that through both the call site AND the
        # postcondition's ``\result`` placeholder rewrite.
        _ret_var = "result"
        if any(pn == _ret_var for _, pn in sig.parameters):
            _ret_var = "_amc_ret"
        assert_stmts = postcond_to_assert(
            spec.postcondition, param_names, return_var=_ret_var,
        )

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

        # Step 1.5c: evidence-grounded global invariants derived from the
        # source's own global write-sets (proactive; see
        # _emit_global_invariant_assumptions). Materialize init-trusted NULL
        # pointer globals BEFORE the assume so it isn't vacuous (assume(NULL !=
        # NULL) => the whole function verifies vacuously, masking every bug).
        gi_inits = _emit_dynamic_global_invariant_inits(parsed_file, self.config, None)
        gi_assumes = _emit_global_invariant_assumptions(parsed_file, self.config)
        if gi_inits or gi_assumes:
            body_lines.append("    /* Step 1.5c: evidence-grounded global invariants */")
            body_lines.extend(f"    {s}" for s in gi_inits)
            body_lines.extend(f"    {s}" for s in gi_assumes)

        # Step 1.6: project-wide invariants learned from prior realism
        # rejections (feedback loop arm (c)). Off unless enable_feedback_loop.
        # Gate project clauses by the current function's params (same reason
        # as the main harness above: avoid "failed to find symbol" errors).
        proj_clauses = _emit_learned_clauses(
            self.config, fn_name, "project",
            param_names=set(param_names),
        )
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

        # Step 1.9: scale-down bounds on ML parametric-size value params
        # (mirrors the non-real-libc path).
        if getattr(self.config, "scale_down", False):
            sd_assumes = _scale_down_assumes(
                func, getattr(self.config, "scale_down_size", 4),
            )
            if sd_assumes:
                body_lines.append(
                    "    /* Step 1.9: scale-down bounds on ML parametric-size params */"
                )
                body_lines.extend(f"    {s}" for s in sd_assumes)

        if assume_stmts:
            body_lines.append("    /* Step 2: precondition assumptions */")
            body_lines.extend(f"    {s}" for s in assume_stmts)

        body_lines.append(f"    /* Step 3: call function under test */")
        if ret_type == "void":
            body_lines.append(f"    {fn_name}({call_args});")
            result_line_present = False
        else:
            # Strip storage-class / function-specifier qualifiers
            # (``static``, ``extern``, ``inline``) from the result-var
            # type. The parser preserves them as part of return_type so
            # the harness's function-pointer typedefs etc. match the
            # original, but a function-scope ``static`` on a local var
            # requires a constant initializer — using ``static
            # unsigned long result = parseoct(p, n);`` triggers a CBMC
            # CONVERSION ERROR.
            result_ret = re.sub(
                r"^\s*(?:static|extern|inline|register)\s+",
                "",
                ret_type,
            ).strip()
            # ``_ret_var`` was computed at the top of the function
            # alongside ``assert_stmts``, so postcond_to_assert already
            # rewrote ``\result`` placeholders to match.
            body_lines.append(f"    {result_ret} {_ret_var} = {fn_name}({call_args});")
            result_line_present = True

        # Step 3.5: when the postcondition will dereference the
        # return value (e.g. ``result->doc == doc``) and the function
        # returns a pointer, extern callees we don't have a stub
        # contract for can return a NON-NULL but invalid pointer.
        # The deref then trips a spurious
        # `main.pointer_dereference.*` failure that no real caller would
        # ever hit because they NULL-check first. Mirror that real-caller
        # pattern: assume the returned pointer is either NULL or a
        # 1-byte-readable region. From feedback-loop arm-(a) TODO #1
        # on xmlXPtrNewContext (xpointer.c).
        if (
            result_line_present
            and "*" in ret_type
            and any(f"{_ret_var}->" in s for s in (assert_stmts or []))
        ):
            body_lines.append(
                "    /* Step 3.5: harness-safe NULL-check on returned pointer "
                "(prevents spurious main.pointer_dereference.* on extern returns) */"
            )
            body_lines.append(
                f"    __CPROVER_assume({_ret_var} == NULL || __CPROVER_r_ok({_ret_var}, 1));"
            )

        if assert_stmts:
            body_lines.append(f"    /* Step 4: postcondition assertions */")
            body_lines.extend(f"    {s}" for s in assert_stmts)

        # Silence unused-variable warnings if the postcondition didn't
        # reference the return value (e.g. trivial postcondition).
        if result_line_present and not any(
            _ret_var in s for s in assert_stmts
        ):
            body_lines.append(f"    (void){_ret_var};")

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

        # Detect if the included source defines its own `main()` (e.g.
        # karpathy/llm.c's train_gpt2.c is a training PROGRAM, not just a
        # library — it has a `int main(int argc, char **argv)`). If we
        # call our harness `main` too, CBMC errors with
        # "function body 'main' defined twice".
        # Fix: use a unique harness name and pass --function to CBMC.
        # When the source has no main (libraries, kernel TUs), keep the
        # historical `main` name for backwards compatibility with the
        # VibeOS-era pipeline.
        try:
            src_text = Path(func.source_file).read_text(errors="ignore")
            import re as _re_main
            has_main = bool(_re_main.search(
                r"^\s*(?:static\s+|inline\s+)*int\s+main\s*\([^)]*\)\s*\{",
                src_text, _re_main.MULTILINE,
            ))
        except Exception:
            has_main = False
        if has_main:
            harness_entry = f"check_{fn_name}"
        else:
            harness_entry = "main"

        sections = [
            f"/* Auto-generated CBMC harness (real-libc mode) for: {fn_name} */",
            f"/* Source: {func.source_file} */",
            f"/* Harness entry: {harness_entry} */",
            "",
            f'#include "{include_target}"',
            "",
            stub_decl_section,
            f"int {harness_entry}(void) {{",
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
    # Walk the text looking for top-level ``_Static_assert(``
    # occurrences (NOT inside string or char literals or comments).
    # The kernel's BUILD_BUG_ON family expands to attribute strings
    # like ``__error__("BUILD_BUG_ON failed: ... _Static_assert(...) ...")``
    # — those embedded ``_Static_assert`` tokens are part of an error
    # message and must NOT be rewritten, or we'd terminate the string
    # in the middle. The inner paren-counting scan was already
    # string-aware; the outer search was not.
    out: list[str] = []
    i = 0
    n = len(text)
    pat = re.compile(r'\b_Static_assert\s*\(')
    while i < n:
        # Skip leading runs of comment/string content to find the next
        # genuine ``_Static_assert(`` outside of any quoting.
        k = i
        match_pos = -1
        while k < n:
            ch = text[k]
            if ch == '/' and k + 1 < n and text[k + 1] == '*':
                end = text.find('*/', k + 2)
                k = n if end == -1 else end + 2
                continue
            if ch == '/' and k + 1 < n and text[k + 1] == '/':
                end = text.find('\n', k + 2)
                k = n if end == -1 else end
                continue
            if ch == '"':
                # Consume the entire string literal — even if it
                # contains ``_Static_assert(`` as part of an
                # attribute-style error message.
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
            # Try matching ``_Static_assert(`` at this position.
            mm = pat.match(text, k)
            if mm:
                match_pos = k
                m = mm
                break
            k += 1
        if match_pos == -1:
            out.append(text[i:])
            break
        out.append(text[i:match_pos])
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


def _strip_aligned_attributes(text: str) -> str:
    """Remove ``__attribute__((__aligned__(...)))`` annotations.

    Kernel cacheline-padding macros expand to alignment annotations
    whose arguments use the GCC binary conditional ``( EXPR ) ? : ( ALT )``
    (e.g. ``__attribute__((__aligned__(( + 0) ? : (1 << (6)))))``).
    CBMC's frontend rejects ``?:`` inside a constant-expression context
    with CONVERSION ERROR ``expected constant expression, but got
    'irep(... gcc_conditional_expression ...)'``.

    Alignment is a memory-layout concern with no effect on CBMC's
    semantic verification, so the simplest fix is to strip the
    annotation entirely. The scanner handles ``__attribute__((...))``
    wherever it appears (declaration, field, etc.) by matching the
    outer ``__attribute__(( ... ))`` and walking balanced parens.
    """
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # Find next ``__attribute__``
        idx = text.find('__attribute__', i)
        if idx == -1:
            result.append(text[i:])
            break
        # Quick filter: look for the `(__aligned__` or `(aligned` keyword
        # in the annotation; if absent, skip this annotation. Otherwise
        # strip it entirely (we don't try to preserve unrelated attribute
        # clauses bundled in the same annotation; in practice they're
        # rare on the lines that exercise this bug).
        j = idx + len('__attribute__')
        # Expect ``((``
        while j < n and text[j] in ' \t':
            j += 1
        if j + 1 >= n or text[j] != '(' or text[j + 1] != '(':
            result.append(text[i:idx + len('__attribute__')])
            i = idx + len('__attribute__')
            continue
        # Walk balanced parens starting at j.
        depth = 0
        k = j
        while k < n:
            ch = text[k]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        clause = text[j:k]
        if '__aligned__' in clause or ' aligned' in clause or '(aligned' in clause:
            # Strip the entire ``__attribute__((...))`` clause.
            result.append(text[i:idx])
            i = k
        else:
            # Keep unrelated __attribute__ clauses verbatim.
            result.append(text[i:k])
            i = k
    return ''.join(result)


def _strip_inline_asm(text: str) -> str:
    """Remove asm/asm volatile/__asm__/etc. statements so the harness compiles on x86.

    Two shapes the kernel uses — handled uniformly by NEVER consuming
    the trailing ``;``:

      * Statement form:   ``asm volatile ("nop");``  ⇒ ``/* asm removed */;``
        (the ``;`` becomes an empty statement — valid C anywhere).
      * Clause form:      ``register unsigned long sp asm("rsp");``
        ⇒ ``register unsigned long sp /* asm removed */;`` (the ``;``
        terminates the surrounding declaration as before).

    Previously the stripper consumed the ``;`` in statement form. That
    broke if the asm statement was the *entire body of a control-flow
    branch* such as ``if (cond) X; else asm("hlt");`` — after stripping
    the ``else`` body, the next token was the enclosing block's ``}``
    and CBMC reported ``syntax error before '}'``. Leaving the ``;``
    in place gives the ``else`` a valid empty-statement body.

    The earlier ``register``-backward-scan distinction is retained as a
    comment-only doc — it's no longer necessary for correctness, but
    documents why we end up with the right output in both shapes.
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
            # Skip trailing whitespace but do NOT consume the ``;``.
            # Leaving the ``;`` in place ensures the resulting text is a
            # valid statement in every context: ``X; else asm("hlt");``
            # → ``X; else /* asm removed */;`` (empty statement);
            # ``register T x asm("rsp");`` →
            # ``register T x /* asm removed */;`` (the declaration's
            # terminator is preserved naturally).
            while j < len(text) and text[j] in ' \t':
                j += 1
            result.append('/* asm removed */')
            i = j
        else:
            result.append(text[m.start():j])
            i = j
    return ''.join(result)


def _strip_static_inline_defs(
    text: str, *, keep_names: Optional[set[str]] = None
) -> str:
    """
    Remove static inline function *definitions* from preprocessed type declarations.

    Bare-metal codebases (e.g. VibeOS) expand their own libc stubs (signal(),
    setjmp(), etc.) into the preprocessed source.  These conflict with the system
    headers we include in the dynamic harness.  Strip the definitions; forward
    declarations (ending in ';') are kept so callers still compile.

    Kernel TUs additionally inline ~thousands of static inlines from
    ``include/linux/*.h`` (e.g. ``is_ns_init_id``, ``uncached_acl_sentinel``,
    ``hlist_*_rcu``) that the FUT does not transitively call. Many of
    these touch struct features CBMC's frontend doesn't fully model
    (anonymous-tag struct inclusion via ``struct ns_tree;`` inside
    ``struct ns_common``), producing CONVERSION ERROR at type-check
    time. Stripping them is the simplest fix: CBMC treats their call
    sites — if any survive — as nondet stubs.

    The scanner is now comment- and string-literal-aware, so braces
    inside ``/* */``, ``// ``, ``"..."``, or ``'.'`` literals do not
    confuse the depth tracking. Without this, complex kernel macros
    (``_Generic`` selections, statement-expression nests) left orphan
    body fragments earlier and we had to disable the strip in kernel
    mode entirely.

    ``keep_names``: when non-None, an inline whose function name is in
    this set is preserved verbatim. Callers can use this to keep, for
    example, all inlines transitively reachable from the FUT.
    """
    keep_names = keep_names or set()
    pat = re.compile(r'\bstatic\s+(?:inline|__inline__)\b')
    name_pat = re.compile(r'\b([A-Za-z_]\w*)\s*\(')

    def _scan_forward_skipping_literals(t: str, start: int, stop_predicate) -> int:
        """Walk ``t`` from ``start`` skipping over /* */ comments,
        // line comments, "..." strings, and '...' char literals. At
        each non-skipped char, call ``stop_predicate(i, ch, depth)``.
        If it returns a non-None value, return it. Otherwise return
        len(t)."""
        i = start
        depth = 0
        n = len(t)
        while i < n:
            ch = t[i]
            # /* ... */
            if ch == '/' and i + 1 < n and t[i + 1] == '*':
                end = t.find('*/', i + 2)
                i = n if end == -1 else end + 2
                continue
            # // ...
            if ch == '/' and i + 1 < n and t[i + 1] == '/':
                end = t.find('\n', i + 2)
                i = n if end == -1 else end
                continue
            # "..."
            if ch == '"':
                k = i + 1
                while k < n:
                    if t[k] == '\\' and k + 1 < n:
                        k += 2; continue
                    if t[k] == '"':
                        k += 1; break
                    k += 1
                i = k; continue
            # '...'
            if ch == "'":
                k = i + 1
                while k < n:
                    if t[k] == '\\' and k + 1 < n:
                        k += 2; continue
                    if t[k] == "'":
                        k += 1; break
                    k += 1
                i = k; continue
            ret = stop_predicate(i, ch, depth)
            if ret is not None:
                return ret
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            i += 1
        return n

    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        m = pat.search(text, i)
        if m is None:
            result.append(text[i:])
            break
        # Find next '{' or ';' at depth 0 (literal-aware).
        body_start = _scan_forward_skipping_literals(
            text, m.end(),
            lambda idx, ch, d: idx if d == 0 and (ch == '{' or ch == ';') else None,
        )
        if body_start >= n:
            result.append(text[i:])
            break
        if text[body_start] == ';':
            # Forward declaration; keep verbatim.
            result.append(text[i:body_start + 1])
            i = body_start + 1
            continue
        # Function definition — find the matching closing '}' (literal-aware).
        body_end = _scan_forward_skipping_literals(
            text, body_start + 1,
            # depth starts at 0 inside the body (we're past the opening '{').
            # A '}' at depth 0 closes the body.
            lambda idx, ch, d: idx + 1 if d == 0 and ch == '}' else None,
        )
        # Extract the function name from the chunk between 'static inline' and '{'.
        head = text[m.end():body_start]
        # Strip out any nested ``(...)``-grouped attribute clauses to make
        # the regex robust against ``__attribute__((...))`` annotations.
        # Find the LAST identifier before the first '(' that introduces the
        # parameter list — walk paren depth from the end of head backward
        # to find the matching '(' of the function declarator.
        fn_name = None
        # Simpler: collect all IDENT-followed-by-'(' matches; pick the last.
        matches = list(name_pat.finditer(head))
        if matches:
            fn_name = matches[-1].group(1)
        if fn_name and fn_name in keep_names:
            # Keep this inline verbatim.
            result.append(text[i:body_end])
            i = body_end
        else:
            result.append(text[i:m.start()])
            result.append('/* static inline removed */')
            i = body_end
    return ''.join(result)


def _replace_function_bodies_with_stubs(
    text: str, fn_names: set[str]
) -> tuple[str, set[str]]:
    """For each function name in ``fn_names``, locate its DEFINITION in
    ``text`` and replace the body with a nondet stub.

    Returns (modified_text, stubbed_set) where ``stubbed_set`` is the
    subset of ``fn_names`` whose definition was found and stubbed. A
    name in fn_names that has no definition in text (only declarations,
    or not present at all) is returned in fn_names - stubbed_set so the
    caller can log or escalate.

    The stub body is ``{ <return-type> _amc_nondet; return _amc_nondet; }``
    for non-void return types, or ``{ return; }`` for void. CBMC treats
    ``_amc_nondet`` (uninitialized local) as nondeterministic.

    Comment / string-literal aware so braces inside ``/* { */`` or
    ``"foo}bar"`` don't confuse the depth tracking. Reuses the same
    scanner shape as ``_strip_static_inline_defs``.

    Conservative parsing: when the return-type extraction is uncertain
    (multi-token type with attributes, etc.), the body is replaced with
    ``{ __CPROVER_assume(0); /* AMC stub: unknown rettype */ }`` — this
    is still safe because CBMC reaches the assume(0) before exiting the
    stub, so the callee contributes no symbolic state. It does mean any
    POST-call code that depended on the return value sees nondet (which
    is the intended behavior anyway).
    """
    if not fn_names:
        return text, set()
    stubbed: set[str] = set()
    n = len(text)

    def _scan_forward_skipping_literals(t: str, start: int, stop_predicate) -> int:
        i = start
        depth = 0
        L = len(t)
        while i < L:
            ch = t[i]
            if ch == '/' and i + 1 < L and t[i + 1] == '*':
                end = t.find('*/', i + 2)
                i = L if end == -1 else end + 2
                continue
            if ch == '/' and i + 1 < L and t[i + 1] == '/':
                end = t.find('\n', i + 2)
                i = L if end == -1 else end
                continue
            if ch == '"':
                k = i + 1
                while k < L:
                    if t[k] == '\\' and k + 1 < L:
                        k += 2; continue
                    if t[k] == '"':
                        k += 1; break
                    k += 1
                i = k; continue
            if ch == "'":
                k = i + 1
                while k < L:
                    if t[k] == '\\' and k + 1 < L:
                        k += 2; continue
                    if t[k] == "'":
                        k += 1; break
                    k += 1
                i = k; continue
            ret = stop_predicate(i, ch, depth)
            if ret is not None:
                return ret
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            i += 1
        return L

    # Process each target function. We scan from the start each time so
    # earlier replacements don't shift positions for later searches.
    for fn_name in fn_names:
        # Locate ``\b<fn_name>\s*\(`` not preceded by struct/union/->/.
        # The simplest robust pattern: find ``\b<fn>\s*\(``, then look
        # backwards from the name to confirm it's a definition (not a
        # call, not a struct field).
        pat = re.compile(r'\b' + re.escape(fn_name) + r'\s*\(')
        for m in pat.finditer(text):
            name_start = m.start()
            paren_start = m.end() - 1  # the '('

            # Confirm this is a function definition:
            #   1. Walk forward from the matching ')' — next non-whitespace,
            #      non-attribute token must be '{' (definition) not ';' (decl)
            #   2. Walk backward from name_start — preceding char must NOT be
            #      ``.`` ``-`` ``>`` (struct field access) or alphanumeric
            #      (would suggest the match is a substring of a longer name)

            # Backward check: the ``\b`` in the regex already prevents
            # substring matches (e.g. ``myappend_id`` won't match
            # ``append_id``), so we only need to reject struct-field
            # access here. Skip whitespace; if the preceding non-ws is
            # ``.`` or ``->``, this is a call through a struct field,
            # not a definition.
            j = name_start - 1
            while j >= 0 and text[j] in ' \t\n\r':
                j -= 1
            if j >= 0:
                prev = text[j]
                if prev == '.':
                    continue  # ``foo.bar(`` — field access
                if prev == '>' and j > 0 and text[j - 1] == '-':
                    continue  # ``foo->bar(`` — field access

            # Forward check: find matching ')' (literal-aware), then next
            # non-whitespace must be '{'.
            paren_end = _scan_forward_skipping_literals(
                text, paren_start + 1,
                lambda idx, ch, d: idx if d == 0 and ch == ')' else None,
            )
            if paren_end >= n:
                continue
            # Skip whitespace + attributes/comments after ')'.
            k = paren_end + 1
            while k < n:
                ch = text[k]
                if ch in ' \t\n\r':
                    k += 1; continue
                if ch == '/' and k + 1 < n and text[k + 1] == '*':
                    e = text.find('*/', k + 2)
                    k = n if e == -1 else e + 2
                    continue
                break
            if k >= n or text[k] != '{':
                continue  # forward declaration, not a definition

            # We have a definition. Find the closing brace.
            body_open = k
            body_close = _scan_forward_skipping_literals(
                text, body_open + 1,
                lambda idx, ch, d: idx + 1 if d == 0 and ch == '}' else None,
            )
            if body_close >= n:
                continue

            # Walk backwards from name_start to find the start of the
            # declarator (i.e., where the return type begins). For
            # single-line declarators (the overwhelming common case),
            # the start is the latest of:
            #   * the start of the current line (so #include /
            #     preceding decls don't bleed in),
            #   * the position right after the last ``;`` (so a prior
            #     statement on the same line doesn't bleed in),
            #   * the position right after the last ``}`` (so the
            #     previous function's closing brace doesn't bleed in).
            line_start = text.rfind('\n', 0, name_start) + 1
            last_semi = text.rfind(';', 0, name_start)
            last_brace = text.rfind('}', 0, name_start)
            decl_start = max(line_start, last_semi + 1, last_brace + 1)
            # Trim leading whitespace so the return-type extraction
            # doesn't pick up indentation.
            while decl_start < name_start and text[decl_start] in ' \t\n\r':
                decl_start += 1

            # Extract and clean the return type: everything between
            # decl_start and name_start, minus the storage-class and
            # inline qualifiers. Handle pointer types by keeping ``*``.
            head_raw = text[decl_start:name_start].strip()
            # Drop storage / inline qualifiers.
            tokens = head_raw.split()
            keep = [t for t in tokens if t not in (
                'static', 'inline', '__inline__', 'extern', '__extern_inline'
            )]
            rettype = ' '.join(keep).strip()
            # Heuristic: if rettype is empty or contains an attribute
            # spec we don't want to copy ad-hoc, fall back to assume(0).
            uncertain = (
                not rettype
                or '__attribute__' in rettype
                or rettype.startswith('__')
            )

            if uncertain:
                stub_body = (
                    "{ __CPROVER_assume(0); "
                    f"/* AMC stub: unknown rettype for {fn_name} */ }}"
                )
            elif rettype == 'void':
                stub_body = f"{{ /* AMC stub: {fn_name} */ return; }}"
            else:
                stub_body = (
                    f"{{ /* AMC stub: {fn_name} */ "
                    f"{rettype} _amc_nondet; return _amc_nondet; }}"
                )

            # Splice: keep everything up through the opening paren of
            # the parameter list + the params + ')' + whitespace + new
            # stub body, drop original body.
            text = text[:body_open] + stub_body + text[body_close:]
            n = len(text)
            stubbed.add(fn_name)
            break  # only stub the first definition we find for this name

    return text, stubbed


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
    # <inttypes.h> — declarations reference the glibc-internal ``__gwchar_t``
    # typedef. Our typedef-strip rule removes ``__gwchar_t`` (because it
    # starts with ``__``) but used to leave these declarations behind,
    # producing ``syntax error before '*'`` when CBMC parses the harness.
    # Observed on every libarchive file (the codebase pulls in
    # ``<inttypes.h>`` transitively via ``archive_string.h``).
    "imaxabs", "imaxdiv",
    "strtoimax", "strtoumax", "wcstoimax", "wcstoumax",
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
    # BSD-historical <sys/types.h> aliases. libarchive's archive_platform.h
    # (under HAVE_CONFIG_H) ships its own ``typedef int register_t;`` which
    # collides with CBMC's built-in model that types ``register_t`` as
    # ``signed long int`` on x86_64-linux, producing
    # "type symbol 'register_t' defined twice". Same risk for the other
    # legacy BSD-shape typedefs glibc <sys/types.h> exposes. Strip the
    # libarchive variant and let CBMC's built-in width stand.
    "register_t", "caddr_t", "daddr_t", "loff_t", "key_t",
    "u_char", "u_short", "u_int", "u_long",
    "quad_t", "u_quad_t",
    # _LARGEFILE64_SOURCE typedefs (glibc, enabled transitively by
    # HAVE_CONFIG_H on libarchive). When stripped via the typedef rule,
    # the cascade-strip on _strip_stdlib_decls drops every <stdio.h>/
    # <unistd.h>/<sys/types.h> 64-bit-LFS forward declaration that uses
    # them (``ftello64``, ``lseek64``, ``truncate64``, ``fseeko64``,
    # ``stat64``, ``readdir64``, …). Empirically these were the top two
    # failure classes in the first libarchive sweep (3071 + 1318 of 4829
    # CBMC errors).
    "fpos64_t", "off64_t", "ino64_t", "blkcnt64_t", "fsblkcnt64_t",
    "fsfilcnt64_t", "rlim64_t",
})


# Subset of ``_SYSTEM_TYPEDEF_NAMES`` whose definitions CBMC's built-in
# libc model supplies after the source-side strip. Project structs whose
# fields use ONLY these typedefs still resolve correctly after the
# strip, so the cascade rule in ``_strip_glibc_internal_struct_bodies``
# must NOT fire on them — otherwise it incorrectly strips legitimate
# project structs (libarchive's ``struct archive_string {char *s;
# size_t length; ...}`` was being stripped because ``size_t`` is in the
# cascade set, which then left dependent structs with by-value fields
# of an incomplete type — the 2026-05-23 cab.c sweep #3 regression).
#
# The typedefs NOT in this set (off64_t, register_t, fpos64_t, the
# ``__``-prefix glibc internals) have no CBMC built-in model; struct
# fields using them DO break after the strip and the cascade SHOULD
# fire.
_SYSTEM_TYPEDEF_NAMES_CBMC_PROVIDES: frozenset[str] = frozenset({
    # C11 <stddef.h>
    "max_align_t", "size_t", "ptrdiff_t", "wchar_t",
    # C99 <wchar.h>
    "wint_t", "wctrans_t", "wctype_t",
    # C99 <stdint.h>
    "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int_least8_t", "int_least16_t", "int_least32_t", "int_least64_t",
    "uint_least8_t", "uint_least16_t", "uint_least32_t", "uint_least64_t",
    "int_fast8_t", "int_fast16_t", "int_fast32_t", "int_fast64_t",
    "uint_fast8_t", "uint_fast16_t", "uint_fast32_t", "uint_fast64_t",
    "intmax_t", "uintmax_t", "intptr_t", "uintptr_t",
    "u_int8_t", "u_int16_t", "u_int32_t", "u_int64_t",
    # POSIX <sys/types.h> that CBMC's model handles
    "fpos_t", "clock_t", "time_t",
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


def _kernel_raw_decls(text: str, parsed_file) -> str:
    """Durable kernel-TU type section. Take the RAW preprocessed source and turn
    function DEFINITIONS into PROTOTYPES (keep every function DECLARED), preserving
    types + GLOBALS verbatim. The raw .i parses cleanly in CBMC; the lossy
    extract/strip path drops globals (ldv_spin), mangles kernel structs
    (__raw_tickets->arch_spinlock), and duplicates type tags. Bodies for inlined/
    stubbed callees are re-emitted by those sections (NON-static for kernel TUs, so
    they are compatible with the prototype left here). VERIFICATION PRIMITIVES keep
    their full body — their body IS the property (ldv_blast_assert/reach_error) or
    the assumption semantics (assume_abort_if_not), so stripping them would mask the
    bug or drop a precondition."""
    import re as _re
    _KEEP_BODY = _re.compile(
        r"\b(ldv_blast_assert|ldv_error|ldv_assert|__VERIFIER_error|__VERIFIER_assert"
        r"|reach_error|abort|assume_abort_if_not|__VERIFIER_assume|ldv_assume)\b"
    )
    defs = getattr(parsed_file, "function_definitions", None) or {}
    for fdef in sorted([d for d in defs.values() if d and "{" in d], key=len, reverse=True):
        # SOUNDNESS/CODEGEN GUARD: a mis-bounded fdef string (parser brace-miscount
        # on GCC statement-expressions ``({ ... })`` / compound literals
        # ``(type){ ... }``) would, when rewritten to its prototype, corrupt the
        # source into invalid C (observed: ``ktime_to_us((;; }))`` -> CBMC PARSING
        # ERROR, exit 6, ``main`` skipped -> the real bug is masked). Only rewrite
        # defs whose braces balance; keep mis-bounded ones VERBATIM (the raw .i is
        # valid C, so CBMC parses the full body fine).
        if fdef.count("{") != fdef.count("}"):
            continue
        head = fdef[:fdef.index("{")]
        if _KEEP_BODY.search(head):
            continue  # keep the primitive's real body
        text = text.replace(fdef, head.rstrip() + ";", 1)  # def -> prototype (stays declared)
    return text


def _dedupe_typedefs(text: str) -> str:
    """Remove DUPLICATE top-level type definitions, keeping the first occurrence:
      * ``typedef <...> NAME ;``  (single line), and
      * ``struct|union|enum NAME { ... } ;`` (brace-aware, multi-line).
    CBMC rejects "type symbol X defined twice" even for identical bodies; the
    CIL/kernel reassembly can emit the same tag (atomic_t, dev_t, ...) more than
    once. Later full definitions are replaced by a forward declaration so any
    pointer uses still resolve."""
    import re as _re
    _tag_def = _re.compile(r'^\s*(struct|union|enum)\s+(\w+)\s*\{')
    _td_line = _re.compile(r'^\s*typedef\b[^;{}]*\b(\w+)\s*;\s*$')
    seen_tags = set()
    seen_td = set()
    out = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        mtag = _tag_def.match(ln)
        if mtag:
            kind, name = mtag.group(1), mtag.group(2)
            # consume the full braced body to the matching close + trailing ';'
            depth = 0
            j = i
            started = False
            while j < n:
                depth += lines[j].count('{') - lines[j].count('}')
                if '{' in lines[j]:
                    started = True
                if started and depth <= 0:
                    break
                j += 1
            key = (kind, name)
            if name and key in seen_tags:
                out.append('/* dup %s %s removed */ %s %s;' % (kind, name, kind, name))
            else:
                if name:
                    seen_tags.add(key)
                out.extend(lines[i:j+1])
            i = j + 1
            continue
        mtd = _td_line.match(ln)
        if mtd:
            name = mtd.group(1)
            if name in seen_td:
                out.append('/* dup typedef %s removed */' % name)
                i += 1
                continue
            seen_td.add(name)
        out.append(ln)
        i += 1
    return "\n".join(out)


def _strip_glibc_internal_struct_bodies(
    text: str,
    *,
    kernel_mode: bool = False,
    extra_strip: Optional[set[str]] = None,
) -> str:
    """Strip the BODY of glibc-internal struct definitions while keeping
    the forward declaration intact.

    Preprocessed sources that ``#include <stdio.h>`` etc. emit full
    definitions of ``struct _IO_FILE``, ``struct __pthread_mutex_s``,
    etc. CBMC's own libc internals re-define these with the same body
    when it sees ``FILE`` / ``pthread_mutex_t`` references in the
    harness, producing ``redefinition of body of 'struct _IO_FILE'``
    parse errors that abort the whole verification with exit code 6.
    The fix is to strip the body so CBMC's libc gets to define them
    uncontested, leaving a forward declaration that keeps pointer-to-
    struct typechecking happy.

    Discovered on a llama.cpp ggml-alloc.c run, 2026-05-18: all 87
    functions errored out with the same redefinition message.

    ``kernel_mode``: the kernel headers define their own version of
    these structs (often empty stubs); there's no libc prepend that
    would re-define them. Suppress the strip in that mode.
    """
    if kernel_mode:
        return text

    # Cascade: scan for ``/* typedef X removed */`` markers and build the
    # set of names whose definitions are gone AND whose definitions CBMC
    # won't re-supply via its own libc model. A struct/union body whose
    # FIELDS reference any of those names is broken (the field types
    # don't resolve), so we strip the body to a forward declaration too.
    #
    # CRITICAL SCOPE LIMIT (added 2026-05-23 after libarchive cab.c
    # regression): C-standard typedefs that ``_SYSTEM_TYPEDEF_NAMES``
    # strips (``size_t``, ``ssize_t``, ``wchar_t``, ``intN_t``, …) ARE
    # supplied by CBMC's built-in stddef/stdint headers. Struct fields
    # using them still resolve correctly after the strip. Cascading on
    # those would incorrectly strip project structs like libarchive's
    # ``struct archive_string { char *s; size_t length; ...}`` and
    # leave dependent structs (``struct archive_mstring { struct
    # archive_string aes_mbs; ...}``) with by-value fields of an
    # incomplete type. So we exclude the C-standard set from the
    # cascade and only fire it on the glibc-extension typedefs that
    # CBMC has no model for (``off64_t``, ``register_t``, the
    # ``__``-prefix internals).
    _STRIPPED_TYPEDEF_MARKER = re.compile(r'/\*\s*typedef\s+(\w+)\s+removed[^*]*\*/')
    _cascade_raw: set[str] = set(_STRIPPED_TYPEDEF_MARKER.findall(text))
    cascade_stripped: set[str] = {
        n for n in _cascade_raw
        # Exclude C-standard typedefs that CBMC re-supplies; cascading
        # on these false-positives project structs.
        if n not in _SYSTEM_TYPEDEF_NAMES_CBMC_PROVIDES
    }
    # Names of struct/union tags whose bodies we strip during this pass, in
    # source order. Feeds the by-value struct-name cascade below: a struct that
    # embeds one of these BY VALUE becomes an incomplete-type error once the
    # member's body is gone, so it must be stripped too.
    stripped_struct_names: set[str] = set()

    # Strip struct definitions whose names match either:
    #   * A glibc-internal prefix (_IO_, __, _G_), OR
    #   * A known POSIX/glibc struct that CBMC's built-in libc
    #     redefines. The allowlist below is empirically grown from
    #     observed "redefinition of body of 'struct X'" failures.
    #
    # Empirical: llama.cpp ggml-alloc.c trips on _IO_FILE, __pthread_*,
    # __locale_struct (prefix matched), AND timeval, timespec,
    # random_data, drand48_data (allowlist).
    _GLIBC_STRUCT_NAME = re.compile(
        r"\b(_IO_[A-Za-z0-9_]+|__[A-Za-z0-9_]+|_G_[A-Za-z0-9_]+)\b"
    )
    _GLIBC_KNOWN_STRUCTS = frozenset({
        # <sys/time.h> / <time.h>
        "timeval", "timespec", "itimerval", "itimerspec", "tm",
        "timezone", "tms", "utimbuf",
        # <sys/timex.h> — body references ``__syscall_slong_t`` etc.
        # which our typedef-strip removes; strip the whole body too.
        "timex", "ntptimeval",
        # <linux/stat.h> — Linux 4.11+ extended stat. Body references
        # __u64/__u32 (kernel primitives, exempt) plus some __ types
        # that get stripped; cheaper to strip the whole body. libarchive
        # never uses these directly — they only arrive via header
        # transitive includes.
        "statx", "statx_timestamp",
        # <sys/types.h>, <sys/stat.h>
        "stat", "stat64",
        # <sys/socket.h>, <netinet/in.h>
        "sockaddr", "sockaddr_in", "sockaddr_in6", "sockaddr_un",
        "sockaddr_storage", "msghdr", "cmsghdr", "iovec",
        # <netdb.h>
        "hostent", "addrinfo", "servent", "protoent", "netent",
        # <locale.h>
        "lconv",
        # <sys/resource.h>
        "rusage", "rlimit",
        # <pwd.h>, <grp.h>
        "passwd", "group",
        # <dirent.h>
        "dirent", "dirent64",
        # <signal.h>
        "sigaction", "siginfo_t", "sigevent",
        # <fcntl.h>
        "flock",
        # <termios.h>
        "termios",
        # <sys/utsname.h>
        "utsname",
        # <stdlib.h> RNG state
        "random_data", "drand48_data",
        # <sched.h>
        "sched_param",
        # <stdio.h> generic
        "fpos_t",
        # <pthread.h> — glibc defines these as unions on x86_64 with
        # platform-dependent bodies; CBMC's libc model has its own.
        "pthread_attr_t", "pthread_mutex_t", "pthread_mutexattr_t",
        "pthread_cond_t", "pthread_condattr_t",
        "pthread_rwlock_t", "pthread_rwlockattr_t",
        "pthread_barrier_t", "pthread_barrierattr_t",
        # <semaphore.h>
        "sem_t",
    })

    def _struct_name_is_glibc(name: str) -> bool:
        if _GLIBC_STRUCT_NAME.fullmatch(name):
            return True
        if name in _GLIBC_KNOWN_STRUCTS:
            return True
        if extra_strip is not None and name in extra_strip:
            return True
        return False
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # Skip over comments and string/char literals so we don't try
        # to interpret a struct keyword inside one.
        ch = text[i]
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            stop = n if end == -1 else end + 2
            result.append(text[i:stop])
            i = stop
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            end = text.find('\n', i + 2)
            stop = n if end == -1 else end
            result.append(text[i:stop])
            i = stop
            continue
        if ch == '"' or ch == "'":
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
            result.append(text[i:k])
            i = k
            continue
        # Look for "struct" or "union" at the current position. Match
        # only when the preceding character isn't an identifier char (so
        # we don't match the middle of a longer word). glibc defines
        # several POSIX types as ``union`` on x86_64 (``pthread_attr_t``,
        # ``pthread_mutex_t``, ``sem_t``), which CBMC's built-in libc
        # re-defines with its own body — same body-redefinition failure
        # as the struct case, fixed the same way.
        kw = None
        if text.startswith("struct", i) and (i == 0 or not text[i - 1].isalnum() and text[i - 1] != '_'):
            kw = "struct"
        elif text.startswith("union", i) and (i == 0 or not text[i - 1].isalnum() and text[i - 1] != '_'):
            kw = "union"
        if kw is not None:
            # Find the type tag name. There must be whitespace then an
            # identifier.
            j = i + len(kw)
            while j < n and text[j].isspace():
                j += 1
            name_m = re.match(r"\w+", text[j:])
            if not name_m:
                result.append(text[i])
                i += 1
                continue
            name = name_m.group(0)
            j += len(name)
            # Skip whitespace, then expect '{' for a body.
            k = j
            while k < n and text[k].isspace():
                k += 1
            if k >= n or text[k] != '{':
                # No body — leave the forward declaration alone.
                result.append(text[i])
                i += 1
                continue
            # We have ``struct NAME {``. Strip the body if either:
            #   (a) the name matches a glibc-internal pattern or a known
            #       POSIX/glibc struct (the primary rule), OR
            #   (b) the body references at least one typedef that the
            #       typedef-strip pass already removed — the body is
            #       otherwise unparseable. Cascade rule, added 2026-05-23
            #       after libarchive cab.c was blocked by zlib's
            #       ``struct gzFile_s { off64_t pos; }``.
            should_strip = _struct_name_is_glibc(name)
            if not should_strip and (cascade_stripped or stripped_struct_names):
                # Peek at the body: scan from ``k`` (the ``{``) to its
                # matching ``}`` so we can inspect the field declarations.
                _peek_m = k
                _peek_depth = 0
                while _peek_m < n:
                    _c = text[_peek_m]
                    if _c == '{':
                        _peek_depth += 1
                    elif _c == '}':
                        _peek_depth -= 1
                        if _peek_depth == 0:
                            _peek_m += 1
                            break
                    _peek_m += 1
                if _peek_depth == 0:
                    _body = text[k:_peek_m]
                    # (a) typedef cascade: body references a stripped typedef.
                    if cascade_stripped and (
                        set(re.findall(r'\b\w+\b', _body)) & cascade_stripped
                    ):
                        should_strip = True
                    # (b) struct-name cascade: body has a BY-VALUE member of a
                    # struct/union whose body we already stripped. ``struct X
                    # name;`` matches; a pointer member ``struct X *name;`` does
                    # NOT (an identifier, not ``*``, must follow the tag) — a
                    # pointer to an incomplete type is fine. (libucl ucl_parser.c,
                    # 2026-06-01: ``struct _xstate`` embeds ``struct _fpstate``
                    # &c. by value; the leaf fpstate structs were typedef-cascade
                    # stripped but _xstate was kept -> "incomplete type not
                    # permitted here".)
                    if not should_strip and stripped_struct_names:
                        for _mm in re.finditer(
                            r"\b(?:struct|union)\s+(\w+)\s+\w", _body
                        ):
                            if _mm.group(1) in stripped_struct_names:
                                should_strip = True
                                break
            if not should_strip:
                result.append(text[i])
                i += 1
                continue
            # Walk past the body, tracking brace depth. The body can have
            # nested braces (anonymous struct/union members are common in
            # _IO_FILE).
            depth = 0
            m = k
            while m < n:
                c = text[m]
                # Skip comments and strings inside the body too.
                if c == '/' and m + 1 < n and text[m + 1] == '*':
                    e = text.find('*/', m + 2)
                    m = n if e == -1 else e + 2
                    continue
                if c == '"' or c == "'":
                    q = c
                    p = m + 1
                    while p < n:
                        if text[p] == '\\' and p + 1 < n:
                            p += 2
                            continue
                        if text[p] == q:
                            p += 1
                            break
                        p += 1
                    m = p
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        m += 1
                        break
                m += 1
            if m >= n or depth != 0:
                # Couldn't find closing brace; leave the struct alone.
                result.append(text[i])
                i += 1
                continue
            # Skip trailing alias and ``;``. The full pattern is
            #   struct NAME { ... } [alias [, alias]...];
            # We need to strip up to and including the next ``;`` at
            # brace depth 0.
            tail = m
            while tail < n and text[tail] != ';':
                tail += 1
            if tail < n:
                tail += 1  # include the ``;``
            # Replace the entire ``<kw> NAME { ... };`` with just a
            # forward declaration so pointer typechecking still works.
            # Body-redefinition vs CBMC's built-in libc is avoided either
            # way — CBMC sees the harness's forward decl and is free to
            # pin its own body.
            result.append(f"{kw} {name}; /* glibc-internal body stripped */")
            stripped_struct_names.add(name)
            i = tail
            continue
        result.append(text[i])
        i += 1
    return ''.join(result)


def _strip_glibc_internal_typedefs(
    text: str,
    *,
    kernel_mode: bool = False,
    extra_strip: Optional[set[str]] = None,
) -> str:
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
        # Three name-extraction forms, tried in order:
        #   1. ``typedef … NAME;`` — simple typedef ending at name.
        #   2. ``typedef … NAME(<params>);`` — function-type typedef
        #      whose name precedes the parameter list. Without this
        #      branch, the regex picks up the last parameter name
        #      (e.g. ``__w`` in
        #      ``typedef int cookie_seek_function_t (void *, __off64_t *, int __w);``)
        #      or returns None, so the typedef escapes both the
        #      primary strip rule and the cascade — leaving an orphan
        #      reference to the just-stripped ``__off64_t`` that CBMC
        #      then rejects with ``syntax error before 'off64_t'``.
        #      Observed on libarchive's cab.c: 61/61 functions blocked
        #      until this fix landed.
        #   3. ``typedef <ret> (*NAME)(<params>);`` — function-POINTER
        #      typedef, name in parens before the parameter list.
        target = None
        # Form 1: simple typedef ``typedef … NAME;`` — name is the last
        # word before the trailing semicolon. Covers the common case
        # ``typedef unsigned long size_t;``.
        name_m = re.search(r'\b(\w+)\s*;\s*$', typedef_text)
        if name_m is not None:
            target = name_m.group(1)
        # Form 2: function-pointer typedef ``typedef <ret> (*NAME)(<params>);``
        # — the OUTERMOST ``(*NAME)`` is what we want; nested params with
        # function-pointer types could also match ``(*X)`` patterns, so we
        # only treat it as form 2 if the OPENING paren of the outer
        # ``(*NAME)`` appears BEFORE any other ``(``. Otherwise we may
        # have a function-type typedef with function-pointer params.
        if target is None:
            outer_fp = re.match(r'\s*typedef\s+[\w\s\*]+?\(\s*\*\s*(\w+)\s*\)\s*\(', typedef_text)
            if outer_fp is not None:
                target = outer_fp.group(1)
        # Form 2b: function-TYPE typedef with a PARENTHESIZED name:
        #   ``typedef <ret> (NAME)(<params>);`` -- like Form 2 but no ``*``.
        # e.g. ``typedef uint64_t(aws_hash_fn)(const void *key);``. Without
        # this, Form 3's non-greedy regex grabs the return type (``uint64_t``)
        # as the typedef name; the system-typedef strip rule then deletes the
        # whole line, leaving ``aws_hash_fn`` undefined (CBMC parse error).
        if target is None:
            paren_ft = re.match(r'\s*typedef\s+[\w\s\*]+?\(\s*(\w+)\s*\)\s*\(', typedef_text)
            if paren_ft is not None:
                target = paren_ft.group(1)
        # Form 3: function-type typedef ``typedef <ret> NAME(<params>);``.
        # Name is the identifier immediately preceding the parameter
        # list. Detected by the LAST ``\w+`` token that precedes a ``(``
        # at the typedef's top level (not inside nested params).
        if target is None:
            # Look for the IDENTIFIER followed by ``(`` with the rest
            # being a balanced parenthesised expression + final ``;``.
            ft = re.match(r'\s*typedef\s+.*?\b(\w+)\s*\(', typedef_text)
            if ft is not None:
                target = ft.group(1)

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
            or (extra_strip is not None and target in extra_strip)
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


def _strip_stdlib_decls(text: str, *, kernel_mode: bool = False) -> str:
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

    ``kernel_mode=True``: the harness has *no* libc prepend (kernel
    headers are already inlined in the preprocessed TU), so stripping
    standard-function declarations leaves CBMC without any prototype
    for ``memset`` / ``memcpy`` / ``strlen`` etc., breaking
    type-checking of bodies that call them (kernel ``memzero_explicit``
    wraps ``memset``). In this mode, return *text* unchanged.
    """
    if kernel_mode:
        return text
    # Match function declarations at brace depth 0: lines/blocks ending in ';'
    # that look like "... funcname ( ... );"
    _DECL_PAT = re.compile(r'\b(\w+)\s*\(')

    # Cascade: derive the set of already-stripped typedefs from the markers
    # the typedef pass left behind (``/* typedef X removed */``). Any forward
    # declaration that references one of these names points at a type that no
    # longer exists, so CBMC will fail to parse it (e.g. ``extern wint_t btowc
    # (int);`` after ``wint_t`` is gone). Strip those declarations too,
    # regardless of whether the function name is in _SYSTEM_FUNCTION_NAMES.
    # Without this, the harness keeps hundreds of <wchar.h>/<inttypes.h>
    # declarations referencing ``wint_t``/``__gwchar_t``/etc. — observed as
    # ~100% CBMC parse-error rate across libarchive.
    _STRIPPED_TYPEDEF_MARKER = re.compile(r'/\*\s*typedef\s+(\w+)\s+removed[^*]*\*/')
    cascade_stripped: set[str] = set(_STRIPPED_TYPEDEF_MARKER.findall(text))

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
        is_decl = m and '{' not in stmt_code
        # Primary rule: named-list match.
        if is_decl and m.group(1) in _SYSTEM_FUNCTION_NAMES:
            result.append(f'/* {m.group(1)} decl removed */')
        # Cascade rule: declaration references a typedef that was already
        # stripped. Only applies to ``extern`` declarations (forward decls
        # with no body) so we don't accidentally drop function definitions.
        elif (
            is_decl
            and cascade_stripped
            and 'extern' in stmt_code.split('(', 1)[0]
            and (set(re.findall(r'\b\w+\b', stmt_code)) & cascade_stripped)
        ):
            referenced = sorted(set(re.findall(r'\b\w+\b', stmt_code)) & cascade_stripped)[0]
            result.append(f'/* {m.group(1)} decl removed: references stripped {referenced} */')
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
    # Integers (possibly negative), with optional C integer suffix (u/U/l/L
    # combinations). CBMC emits unsigned/long witnesses like "27u", "0u", "3ul";
    # without this they fail int() and the arg silently defaults to 0 -- so a
    # length/size param is passed as n=0 and the function-under-test never
    # exercises the buggy path (the value is valid C, so it is emitted as-is).
    import re as _r_suf
    if _r_suf.fullmatch(r"-?\d+[uUlL]*", val):
        return True
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
