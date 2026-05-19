"""Kani backend for Rust programs.

Implements :class:`BMCBackend` against the Kani Rust BMC verifier.  Kani is
itself a CBMC-driven tool, so the result shape (``CBMCResult``) is shared
with the C backend — the rest of the pipeline does not need to know which
backend produced a verdict.

What this module supports today:

* :meth:`check` runs the Kani CLI on a self-contained Rust harness file and
  parses the textual verdict (see :mod:`bmc_agent.kani`).
* :meth:`generate_harness` accepts a :class:`FunctionInfo`-shaped object
  whose signature already uses Rust types (e.g. ``i32``, ``*mut i32``) and
  emits a ``#[kani::proof]`` function that nondeterministically initialises
  the parameters, asserts the spec's precondition via :func:`kani::assume`,
  calls the function under test, and asserts the postcondition.

What it does **not** yet support (and surfaces as a clear error):

* C-source-to-Rust translation.  AProver's Phase 1 parser is C-only;
  feeding it Rust requires a Rust parser that does not yet exist.  The
  harness generator therefore expects callers to supply Rust-shape
  ``FunctionInfo`` objects directly, which is the contract the (future)
  Rust spec generator will provide.
* Aggregate types (structs, enums) and trait-bound generics in the spec
  language.  These will be added incrementally; the current DSL
  translation covers primitives and raw pointers.
"""

from __future__ import annotations

from pathlib import Path

from bmc_agent.backends.bmc_backend import BMCBackend
from bmc_agent.kani import run_kani


# Rust types we know how to nondeterministically initialise with kani::any().
_PRIMITIVE_RUST_TYPES = {
    "i8", "i16", "i32", "i64", "i128", "isize",
    "u8", "u16", "u32", "u64", "u128", "usize",
    "bool", "char",
    "f32", "f64",
}


# Default fixed-size bound for nondeterministic slice and array
# initialisation in harnesses.  BMC verification is bounded by
# construction, so we explore all slice contents and lengths up to this
# cap.  Picked small to keep verification time reasonable; raise via
# config.kani_slice_bound when a spec needs more reach.
_DEFAULT_SLICE_BOUND = 4


def _is_pointer_type(rust_type: str) -> bool:
    """True iff *rust_type* is a Rust raw pointer or reference type."""
    t = rust_type.strip()
    return t.startswith("*const ") or t.startswith("*mut ") or t.startswith("&")


def _is_slice_type(rust_type: str) -> bool:
    """True iff *rust_type* is a shared slice reference like ``&[T]``.

    Mutable slices (``&mut [T]``) are recognised by the same prefix
    check after stripping ``mut ``.
    """
    t = rust_type.strip()
    if t.startswith("&mut "):
        t = t[len("&mut "):].strip()
    elif t.startswith("&"):
        t = t[1:].strip()
    else:
        return False
    return t.startswith("[") and t.endswith("]")


def _slice_element_type(rust_type: str) -> str:
    """Return ``T`` from ``&[T]`` / ``&mut [T]``.  Caller must have already
    verified the type is a slice via :func:`_is_slice_type`."""
    t = rust_type.strip()
    if t.startswith("&mut "):
        t = t[len("&mut "):].strip()
    elif t.startswith("&"):
        t = t[1:].strip()
    # t is now "[T]"
    return t[1:-1].strip()


def _is_vec_type(rust_type: str) -> bool:
    """True iff *rust_type* is ``Vec<T>`` (no leading reference)."""
    t = rust_type.strip()
    return t.startswith("Vec<") and t.endswith(">")


def _vec_element_type(rust_type: str) -> str:
    """Return ``T`` from ``Vec<T>``.  Caller verifies via :func:`_is_vec_type`."""
    t = rust_type.strip()
    return t[len("Vec<") : -1].strip()


def _is_option_type(rust_type: str) -> bool:
    """True iff *rust_type* is ``Option<T>``."""
    t = rust_type.strip()
    return t.startswith("Option<") and t.endswith(">")


def _option_inner_type(rust_type: str) -> str:
    t = rust_type.strip()
    return t[len("Option<") : -1].strip()


def _is_str_ref_type(rust_type: str) -> bool:
    """True iff *rust_type* is ``&str`` or ``&mut str``.

    Rust string slices are references into UTF-8 byte storage; we model
    them as a bounded ``[u8; N]`` array constrained to ASCII so
    ``std::str::from_utf8`` succeeds without expensive UTF-8 validation
    inside the symbolic engine.
    """
    t = rust_type.strip()
    if t == "&str" or t == "&mut str":
        return True
    # Tolerate the lifetime-annotated form: &'a str / &'a mut str.
    if t.startswith("&"):
        body = t[1:].strip()
        if body.startswith("'"):
            # Skip the lifetime token: &'a str -> "a str"
            try:
                _, rest = body.split(None, 1)
            except ValueError:
                return False
            rest = rest.strip()
            return rest == "str" or rest == "mut str"
    return False


def _initialiser_for(rust_type: str) -> str:
    """Return a Rust expression that produces a nondeterministic *rust_type*.

    For primitives we use ``kani::any()``.  For raw pointers we use
    ``kani::any::<usize>() as *mut T`` so Kani explores both null and non-null
    states; the spec's precondition narrows it further.

    Slice types (``&[T]``, ``&mut [T]``) are NOT single-expression
    initialisable — they need a backing array and a separately-bounded
    length — so this function rejects them with ``NotImplementedError``.
    Use :func:`_param_init_block` instead, which emits the
    multi-statement setup.
    """
    t = rust_type.strip()
    if t in _PRIMITIVE_RUST_TYPES:
        return f"kani::any::<{t}>()"
    if t.startswith("*mut "):
        inner = t[len("*mut "):].strip()
        return f"kani::any::<usize>() as *mut {inner}"
    if t.startswith("*const "):
        inner = t[len("*const "):].strip()
        return f"kani::any::<usize>() as *const {inner}"
    if _is_slice_type(t):
        # Slice initialisation requires multi-line setup; route through
        # _param_init_block instead.
        raise NotImplementedError(
            f"slice type {t!r} cannot be initialised in a single expression; "
            f"use _param_init_block"
        )
    if t.startswith("&mut "):
        raise NotImplementedError(
            f"&mut references in Kani harnesses are not yet supported; "
            f"use a *mut raw pointer instead (got {t!r})"
        )
    if t.startswith("&"):
        raise NotImplementedError(
            f"& references in Kani harnesses are not yet supported; "
            f"use a *const raw pointer instead (got {t!r})"
        )
    raise NotImplementedError(
        f"don't know how to nondeterministically initialise Rust type {t!r}; "
        f"only primitives and raw pointers are currently supported"
    )


def _param_init_block(
    rust_type: str,
    name: str,
    slice_bound: int = _DEFAULT_SLICE_BOUND,
) -> list[str]:
    """Return the Kani init statements needed to bind *name* to a
    nondeterministic value of *rust_type*.

    For primitives and raw pointers this is the single line emitted by
    :func:`_initialiser_for`.  For slice types we emit four lines:
    a backing fixed-size array, a separately-nondeterministic length
    capped at *slice_bound*, and a borrow producing the slice itself.
    Kani then explores every combination of contents, length, and
    downstream indices up to that bound.

    The backing names are prefixed with ``_`` and suffixed with the
    parameter name so a harness with multiple slice parameters does not
    collide.
    """
    t = rust_type.strip()
    if _is_slice_type(t):
        elem = _slice_element_type(t)
        backing = f"_backing_{name}"
        length = f"_len_{name}"
        borrow = "&mut " if t.startswith("&mut ") else "&"
        return [
            f"    let mut {backing}: [{elem}; {slice_bound}] = kani::any();",
            f"    let {length}: usize = kani::any();",
            f"    kani::assume({length} <= {slice_bound});",
            f"    let {name}: {t} = {borrow}{backing}[..{length}];",
        ]
    if _is_vec_type(t):
        # Build a Vec<T> by materialising a bounded backing array and
        # using slice.to_vec() to copy into a heap allocation.  Kani
        # can model this — vec construction from a fixed-size slice is
        # well-supported — and the resulting Vec's length is the
        # nondeterministic _len_ we previously assumed bounded.
        elem = _vec_element_type(t)
        backing = f"_backing_{name}"
        length = f"_len_{name}"
        return [
            f"    let {backing}: [{elem}; {slice_bound}] = kani::any();",
            f"    let {length}: usize = kani::any();",
            f"    kani::assume({length} <= {slice_bound});",
            f"    let {name}: Vec<{elem}> = {backing}[..{length}].to_vec();",
        ]
    if _is_option_type(t):
        # Option<T> alternates between Some(any T) and None on a
        # nondeterministic discriminant; Kani then explores both arms.
        inner = _option_inner_type(t)
        flag = f"_some_{name}"
        return [
            f"    let {flag}: bool = kani::any();",
            f"    let {name}: Option<{inner}> = if {flag} "
            f"{{ Some({_initialiser_for(inner)}) }} else {{ None }};",
        ]
    if t.startswith("&mut "):
        inner = t[len("&mut "):].strip()
        # &mut [T] slice path already handled above (_is_slice_type).
        # Here we handle the remaining &mut shapes one at a time:
        #
        # &mut Vec<T> — accumulator output param. Allocate a backing
        # Vec on the harness stack and take an &mut borrow to it. The
        # callee can push to it; the harness doesn't need to inspect
        # the post-call contents (Kani's panic/overflow checks are
        # what we care about). Used by CCC's copy_literal_bytes_raw.
        if _is_vec_type(inner):
            elem = _vec_element_type(inner)
            backing = f"_owned_{name}"
            return [
                f"    let mut {backing}: Vec<{elem}> = Vec::new();",
                f"    let {name}: &mut Vec<{elem}> = &mut {backing};",
            ]
        # &mut String — same shape. Used by CCC's
        # copy_literal_bytes_to_string.
        if inner == "String":
            backing = f"_owned_{name}"
            return [
                f"    let mut {backing}: String = String::new();",
                f"    let {name}: &mut String = &mut {backing};",
            ]
        # &mut <primitive> — bind a mutable scalar, take &mut.
        if inner in _PRIMITIVE_RUST_TYPES:
            backing = f"_owned_{name}"
            return [
                f"    let mut {backing}: {inner} = kani::any::<{inner}>();",
                f"    let {name}: &mut {inner} = &mut {backing};",
            ]
        # Anything else (&mut SomeStruct) — fall through to the existing
        # NotImplementedError in _initialiser_for. We deliberately don't
        # try to nondet-init arbitrary user structs.
    if _is_str_ref_type(t):
        # &str is a borrow into UTF-8 storage. We build a bounded u8
        # backing array, constrain every byte to ASCII (< 0x80) so
        # str::from_utf8 succeeds without Kani exploring multi-byte
        # UTF-8 validity, then borrow as a &str of nondeterministic
        # length. Index access (s.len(), s.starts_with(...)) works
        # naturally on the resulting slice.
        backing = f"_backing_{name}"
        length = f"_len_{name}"
        return [
            f"    let {backing}: [u8; {slice_bound}] = kani::any();",
            f"    let {length}: usize = kani::any();",
            f"    kani::assume({length} <= {slice_bound});",
            f"    for _i in 0..{slice_bound} {{",
            f"        kani::assume({backing}[_i] < 0x80);",
            f"    }}",
            f"    let {name}: {t} = "
            f"std::str::from_utf8(&{backing}[..{length}]).unwrap();",
        ]
    return [f"    let {name}: {t} = {_initialiser_for(t)};"]


def _translate_dsl(predicate: str, result_var: str = "result") -> str:
    """Translate one BMC-Agent DSL predicate string into a Rust expression.

    Supports the same vocabulary as :mod:`bmc_agent.dsl_to_cbmc`:

    * ``valid(ptr)``              → ``!ptr.is_null()``
    * ``valid_range(ptr, lo, hi)``→ ``!ptr.is_null()`` (range bounds are
      enforced via the harness, not the predicate)
    * ``valid_string(ptr)``        → ``!ptr.is_null()``
    * ``null(ptr)``                → ``ptr.is_null()``
    * ``owns(ptr)``                → ``!ptr.is_null()``
    * ``\\result``                  → the configured result variable name

    Boolean ``&&`` / ``||`` / ``!`` are passed through unchanged; arithmetic
    comparisons are passed through unchanged.  Anything else is left
    verbatim — Kani will reject malformed harnesses at compile time.
    """
    expr = predicate.strip()
    if not expr or expr.lower() == "true":
        return "true"

    import re

    # \result → result_var
    expr = expr.replace("\\result", result_var)
    # in_bounds(slice, idx) → idx < slice.len()
    # The Rust DSL uses in_bounds for slice indexing; Phase 1 emits
    # this for raw and reference slices alike since Rust slices carry
    # their length intrinsically.  Translate to a length comparison so
    # Kani can encode it as kani::assume / kani::assert.
    expr = re.sub(
        r"\bin_bounds\(\s*([^,)]+?)\s*,\s*([^)]+?)\s*\)",
        lambda m: f"({m.group(2)}) < {m.group(1)}.len()",
        expr,
    )
    # null(ptr) → ptr.is_null()
    expr = re.sub(r"\bnull\(\s*([^)]+?)\s*\)", lambda m: f"{m.group(1)}.is_null()", expr)
    # valid_range(ptr, lo, hi) → !ptr.is_null()
    expr = re.sub(
        r"\bvalid_range\(\s*([^,)]+?)\s*,\s*[^,)]+?\s*,\s*[^)]+?\s*\)",
        lambda m: f"!{m.group(1)}.is_null()",
        expr,
    )
    # valid_string(ptr) → !ptr.is_null()
    expr = re.sub(
        r"\bvalid_string\(\s*([^)]+?)\s*\)",
        lambda m: f"!{m.group(1)}.is_null()",
        expr,
    )
    # owns(ptr) → !ptr.is_null()
    expr = re.sub(r"\bowns\(\s*([^)]+?)\s*\)", lambda m: f"!{m.group(1)}.is_null()", expr)
    # valid(ptr) → !ptr.is_null() (must come last; the others are more specific)
    expr = re.sub(r"\bvalid\(\s*([^)]+?)\s*\)", lambda m: f"!{m.group(1)}.is_null()", expr)
    # Logical implication: (A ==> B) → (!(A) || (B)).  The expression
    # tree may contain nested parens inside A or B (e.g.
    # ``(result.1 == 3 ==> (result.0 >= 0x80))``), which a flat regex
    # cannot match, so we paren-balance manually: for each ==>, walk
    # left/right to find the enclosing parentheses and rewrite the
    # substring as a whole.
    return _rewrite_implications(expr)


def _rewrite_implications(expr: str) -> str:
    """Iteratively rewrite ``(A ==> B)`` to ``(!(A) || (B))``.

    Handles arbitrary nesting on either side of ``==>`` by scanning for
    the matching outer parens with explicit depth tracking, rather than
    relying on a regex (which cannot recognise paren-balanced grammars).

    Each occurrence of ``==>`` must lie inside a paren-balanced
    enclosing group; otherwise the original expression is returned
    unchanged so the user sees the LLM-emitted form in the Kani error
    rather than silent corruption.
    """
    while True:
        idx = expr.find("==>")
        if idx == -1:
            return expr

        # Walk left from idx to find the opening "(" at the same depth.
        start = -1
        depth = 0
        for j in range(idx - 1, -1, -1):
            ch = expr[j]
            if ch == ")":
                depth += 1
            elif ch == "(":
                if depth == 0:
                    start = j
                    break
                depth -= 1
        if start == -1:
            return expr  # unbalanced — bail out, surface the error to Kani.

        # Walk right from idx to find the matching ")" at the same depth.
        end = -1
        depth = 0
        for j in range(idx + len("==>"), len(expr)):
            ch = expr[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    end = j
                    break
                depth -= 1
        if end == -1:
            return expr  # unbalanced — bail out.

        lhs = expr[start + 1 : idx].strip()
        rhs = expr[idx + len("==>") : end].strip()
        replacement = f"(!({lhs}) || ({rhs}))"
        expr = expr[:start] + replacement + expr[end + 1 :]


def _load_full_source(func, parsed_file) -> "str | None":
    """Return the full source text of *func*'s defining file, or None.

    Used by the harness generator to include consts, use statements and
    sibling functions verbatim — anything the function under test might
    transitively depend on.  Prefers an in-memory copy on parsed_file
    (set when the spec generator passed source_text through) and falls
    back to reading from func.source_file on disk.  Failures are
    swallowed: the caller then falls back to reconstructing just the
    fn_def plus parsed siblings, which is correct for self-contained
    primitive functions but loses module-level consts.
    """
    if parsed_file is not None:
        src = getattr(parsed_file, "preprocessed_source", None)
        if src:
            return src
    source_file = getattr(func, "source_file", None)
    if source_file:
        try:
            return Path(source_file).read_text(encoding="utf-8", errors="replace")
        except (OSError, FileNotFoundError):
            return None
    return None


def _strip_crate_local_fn_items(
    source: str,
    keep_fn_name: str,
    keep_callees: "set[str] | None" = None,
) -> str:
    """Strip every top-level ``fn`` from ``source`` EXCEPT
    ``keep_fn_name`` and any names in ``keep_callees``.

    Sibling fns in real-world Rust crates routinely reference
    crate-local types in their signatures or bodies (e.g. CCC's
    ``pub fn is_zero_expr(expr: &crate::frontend::parser::ast::Expr)``).
    When the harness is compiled standalone these paths fail to
    resolve (E0433 "unresolved module"). Even after stripping
    ``use crate::*`` lines, the SAME types appear unprefixed in
    sibling signatures (``op: &BinOp``) and now resolve to nothing
    because the import that brought them into scope is gone.

    The pragmatic fix: drop every fn item that isn't the target
    or a recorded callee. The target's spec/harness only needs
    its own body in scope — modules-level consts and ``use std::*``
    preamble survive, the target fn survives, listed callees
    survive, everything else is comment-stripped.

    Uses tree-sitter Rust to find function_item boundaries — regex is
    not reliable for brace-matching inside Rust (lifetimes, generics,
    nested closures, raw strings).

    Regression: CCC const_arith.rs 2026-05-19 — wrap_result harness
    was polluted by 12 sibling fns whose signatures referenced
    ``BinOp`` / ``IrConst`` (originally imported via the now-stripped
    ``use crate::ir::reexports::IrConst;``). Removing all non-target
    fns reduces the harness to just module preamble + target.
    """
    if keep_callees is None:
        keep_callees = set()
    try:
        from bmc_agent.rust_parser import _load_language
        from tree_sitter import Parser as _TSParser
    except Exception:
        return source

    src_bytes = source.encode("utf-8", errors="replace")
    parser = _TSParser(_load_language())
    tree = parser.parse(src_bytes)

    # Gather (start, end, kind, name) for each top-level function_item
    # AND each impl_item. The Rust parser's M1 scope only analyses
    # top-level free fns, so any impl block in the file is by definition
    # NOT the target; their bodies typically call sibling fns we just
    # stripped, which then cascade into a wall of E0425 ("cannot find
    # function") errors. Strip every impl block; keep listed fns.
    ranges: list[tuple[int, int, str, str]] = []
    for top in tree.root_node.children:
        if top.type == "function_item":
            name_node = top.child_by_field_name("name")
            name = (
                src_bytes[name_node.start_byte:name_node.end_byte]
                .decode("utf-8", errors="replace")
                if name_node else ""
            )
            ranges.append((top.start_byte, top.end_byte, "fn", name))
        elif top.type == "impl_item":
            ranges.append((top.start_byte, top.end_byte, "impl", ""))

    if not ranges:
        return source

    # Walk back to front so byte offsets stay valid as we splice.
    out = bytearray(src_bytes)
    for start, end, kind, name in reversed(ranges):
        if kind == "fn" and (name == keep_fn_name or name in keep_callees):
            continue
        if kind == "fn":
            replacement = (
                f"// fn {name}(...) /* stripped: non-target sibling, kept "
                f"out of standalone harness */"
            ).encode("utf-8")
        else:  # impl
            replacement = (
                b"// impl ... { ... } /* stripped: parser M1 scope is "
                b"top-level free fns only; impl methods aren't the target */"
            )
        out[start:end] = replacement

    return bytes(out).decode("utf-8", errors="replace")


def _strip_pub_in_path_visibility(source: str) -> str:
    """Replace ``pub(super)`` / ``pub(crate)`` / ``pub(self)`` /
    ``pub(in ...)`` visibility modifiers with plain ``pub``.

    These are only meaningful inside a crate — outside one, ``pub(super)``
    fails with "too many leading `super` keywords" and the harness
    won't compile. Replacing with bare ``pub`` keeps the type publicly
    visible to the harness while losing the host-crate-specific scope
    restriction (which doesn't apply in standalone compilation anyway).

    Regression: CCC macro_defs.rs 2026-05-19 — every harness had
    ``pub(super) asm_mode: bool`` in a struct field declaration,
    failing rustc with E0433.
    """
    import re as _re
    return _re.sub(
        r"\bpub\s*\(\s*(?:super|crate|self|in\s+[A-Za-z_][\w:]*)\s*\)",
        "pub",
        source,
    )


def _strip_crate_local_use_statements(source: str) -> str:
    """Comment out ``use crate::*`` / ``use super::*`` / ``use self::*``
    lines so the harness compiles standalone.

    A Kani harness is a single .rs file compiled outside its host crate
    — there is no ``crate::*`` rooted at the host crate, so any
    ``use crate::frontend::parser::ast::BinOp;`` lands as
    E0432 ("unresolved import") and the entire compile aborts before
    Kani sees anything. ``std::``, ``alloc::``, ``core::``, and other
    absolute paths still resolve fine, so we leave them alone.

    Functions whose BODIES still reference the now-undefined types will
    fail to compile separately (E0412 unresolved type), and the
    pipeline will skip those with a parse error — that's the correct
    behaviour. The fix unblocks the orthogonal class of primitive
    functions whose bodies only touch i64/u64/bool/etc. but happen to
    sit in a module whose file preamble pulls in crate-local types.

    Regression: CCC const_arith.rs 2026-05-19 — 3 selected primitive
    functions (wrap_result, unsigned_op, bool_to_i64) all failed Kani
    parse because the file's two ``use crate::*;`` lines were copied
    verbatim into the harness.

    Multi-line follow-up: CCC macro_defs.rs 2026-05-19 — the file's
    ``use super::utils::{is_ident_start_byte, ...};`` spans 4 lines
    with the import list wrapped in braces. The original
    single-line regex caught only the first line and orphaned the
    closing ``};``, producing "unexpected closing delimiter" rustc
    errors on every harness. The DOTALL pattern below matches the
    whole statement up to the terminating ``;``.
    """
    import re as _re
    pattern = _re.compile(
        r"^[ \t]*(?:pub\s+)?use\s+(?:crate|super|self)\s*::"
        r"[^;]*;[ \t]*$",
        _re.MULTILINE | _re.DOTALL,
    )
    return pattern.sub(
        lambda m: "/* "
                  + m.group(0).strip().replace("*/", "* /")
                  + " — stripped: unresolved in standalone harness */",
        source,
    )


def _call_site_expr(rust_type: str, name: str) -> str:
    """Return the expression to pass *name* at the call site so the
    postcondition can still reference *name* afterwards.

    Owned, non-Copy types (Vec<T>, String, Box<T>) are moved when passed
    by value — referencing ``name`` in the postcondition would then fail
    to compile with E0382 (borrow of moved value).  For those we pass
    ``name.clone()`` so the original binding remains live.  Copy
    primitives, raw pointers, and references don't need this.
    """
    t = rust_type.strip()
    if t in _PRIMITIVE_RUST_TYPES:
        return name
    if t.startswith("*mut ") or t.startswith("*const "):
        return name  # raw pointers are Copy
    if t.startswith("&"):
        return name  # references (incl. &str / &[T]) survive past the call
    if _is_vec_type(t) or t == "String" or t.startswith("Box<"):
        return f"{name}.clone()"
    if _is_option_type(t):
        # Option<T> is Clone iff T is.  Conservative default: clone so
        # the postcondition can re-bind ``.is_some()`` / ``.unwrap()``.
        return f"{name}.clone()"
    return name


def _sibling_fn_definitions(func, parsed_file) -> list[str]:
    """Return reconstructed definitions for every other fn in *parsed_file*.

    The Kani harness must compile standalone, so any sibling function
    that *func* calls — and any function those siblings call in turn —
    needs to be in scope.  We don't try to be selective: emit all the
    file's parsed functions, deduplicating *func* itself.  Rust allows
    fn items in any order so layout doesn't matter.

    When *parsed_file* is None the result is empty; callers that don't
    pass it get the old single-fn behaviour.
    """
    if parsed_file is None:
        return []
    siblings: list[str] = []
    seen = {func.name}
    for sibling_info in parsed_file.all_function_infos():
        if sibling_info is None or sibling_info.name in seen:
            continue
        siblings.append(_reconstruct_fn_definition(sibling_info))
        siblings.append("")
        seen.add(sibling_info.name)
    return siblings


def _reconstruct_fn_definition(func) -> str:
    """Rebuild a complete ``fn name(params) -> ret { body }`` item.

    The tree-sitter Rust parser exposes ``func.body`` as just the
    ``{...}`` block — no fn header — so the harness file needs us to
    synthesise the header from the signature.  We rebuild it
    deterministically rather than relying on a verbatim signature-text
    field (which the parser does not provide today).

    Modifiers (``unsafe``/``async``/``const``), generic parameters, and
    where clauses are preserved when present on the signature so the
    reconstructed definition matches the call shape used in the harness.

    When ``func.body`` already starts with the keyword ``fn`` we assume
    the caller passed a hand-written full-definition string (the shape
    several existing tests use as a shortcut) and return it unchanged
    rather than double-wrapping.
    """
    body = (getattr(func, "body", "") or "").lstrip()
    if body.startswith("fn ") or body.startswith("pub fn ") or body.startswith("unsafe fn "):
        return body.rstrip()

    sig = func.signature
    params_text = ", ".join(f"{name}: {ty}" for ty, name in sig.parameters)
    return_text = f" -> {sig.return_type}" if sig.return_type and sig.return_type != "()" else ""

    modifiers = " ".join(getattr(sig, "modifiers", []) or [])
    if modifiers:
        modifiers += " "
    type_params = getattr(sig, "type_parameters", "") or ""
    where_clause = getattr(sig, "where_clause", "") or ""
    where_text = f" {where_clause}" if where_clause else ""

    header = f"{modifiers}fn {sig.name}{type_params}({params_text}){return_text}{where_text}"
    body_block = body if body else "{}"
    return f"{header} {body_block}".rstrip()


class KaniBackend(BMCBackend):
    """Kani backend for Rust programs.

    Construct with a :class:`Config`; the backend reads
    ``config.kani_path``, ``config.kani_unwind``, and ``config.kani_timeout``.
    """

    def __init__(self, config) -> None:
        self._config = config

    @property
    def language(self) -> str:
        return "rust"

    # ------------------------------------------------------------------
    # Harness generation
    # ------------------------------------------------------------------

    def generate_harness(
        self,
        func,
        spec,
        callee_specs: dict | None = None,
        parsed_file=None,
        all_funcs: dict | None = None,
        slice_bound_override: int | None = None,
    ) -> str:
        """Emit a self-contained Rust harness verifying *func* against *spec*.

        The expected ``func`` shape is a duck-typed
        :class:`bmc_agent.parser.FunctionInfo` whose
        ``signature.parameters`` are ``(rust_type, name)`` tuples and whose
        ``signature.return_type`` is a Rust type string.  The function
        body is included verbatim so the harness is compilable standalone.

        ``slice_bound_override`` lets the caller force a smaller buffer
        size than ``config.kani_slice_bound``. Used by the engine's
        timeout-retry path: when Kani times out at the default bound
        on a function with internal loops (UTF-8 validation,
        allocator-driven Vec/String code), regenerating with a smaller
        bound often turns a 120-s timeout into a sub-minute clean verdict.
        """
        params: list[tuple[str, str]] = list(func.signature.parameters)
        return_type = (func.signature.return_type or "").strip()

        # 1. Nondeterministic parameter initialisation.  For each parameter
        #    we also record the call-site expression — typically just the
        #    name, but ``.clone()`` for owned non-Copy types so the
        #    postcondition can still reference the original value.
        if slice_bound_override is not None:
            slice_bound = slice_bound_override
        else:
            slice_bound = getattr(self._config, "kani_slice_bound", _DEFAULT_SLICE_BOUND)
        init_lines: list[str] = []
        arg_names: list[str] = []  # names visible to postcondition
        call_args_list: list[str] = []  # expressions passed at call site
        for ty, name in params:
            init_lines.extend(_param_init_block(ty, name, slice_bound=slice_bound))
            arg_names.append(name)
            call_args_list.append(_call_site_expr(ty, name))

        # Reconstruct the function definition from sig + body.  The
        # tree-sitter parser stores ``func.body`` as just the {...} block
        # (no fn header), so the harness file must wrap it in a real
        # ``fn ... { body }`` item, or rustc will refuse to compile.  We
        # rebuild the signature deterministically rather than rely on
        # ``func.signature_text`` (which is not part of the parser
        # output today).
        fn_def = _reconstruct_fn_definition(func)

        # 2. Precondition → kani::assume.
        pre_expr = _translate_dsl(spec.precondition or "true")
        precondition_line = (
            f"    kani::assume({pre_expr});" if pre_expr != "true" else ""
        )

        # 3. Function call.  Track the return binding so the postcondition
        #    can refer to it as `result`.  ``call_args`` uses cloned forms
        #    for non-Copy owned parameters; the postcondition still uses
        #    ``arg_names`` to access the originals.
        call_args = ", ".join(call_args_list)
        if return_type in ("", "()"):
            call_line = f"    {func.name}({call_args});"
            result_binding = ""
        else:
            call_line = f"    let result: {return_type} = {func.name}({call_args});"
            result_binding = "result"

        # 4. Postcondition → kani::assert.
        post_expr = _translate_dsl(spec.postcondition or "true", result_var=result_binding or "result")
        post_line = (
            f"    kani::assert({post_expr}, \"postcondition violated\");"
            if post_expr != "true"
            else ""
        )

        harness_name = f"check_{func.name}"

        # Compose the file.  Prefer the full source verbatim so consts,
        # use statements, sibling fns, and helper items are all in scope.
        # Fall back to fn_def + reconstructed sibling fns when the
        # source text isn't available (e.g. test fixtures that don't
        # pass parsed_file).
        file_source = _load_full_source(func, parsed_file)
        parts: list[str] = [
            "//! Auto-generated Kani harness — do not edit by hand.",
            "#![allow(unused_imports, dead_code, non_snake_case)]",
            "",
        ]
        if file_source is not None:
            cleaned = _strip_crate_local_use_statements(file_source)
            cleaned = _strip_pub_in_path_visibility(cleaned)
            cleaned = _strip_crate_local_fn_items(
                cleaned,
                keep_fn_name=func.name,
                keep_callees=set(getattr(func, "callees", set()) or set()),
            )
            parts.append(cleaned.rstrip())
        else:
            parts.append(fn_def)
            sibling_defs = _sibling_fn_definitions(func, parsed_file)
            if sibling_defs:
                parts.append("")
                parts.extend(sibling_defs)
        parts.extend([
            "",
            "#[cfg(kani)]",
            "#[kani::proof]",
            f"fn {harness_name}() {{",
            *init_lines,
        ])
        if precondition_line:
            parts.append(precondition_line)
        parts.append(call_line)
        if post_line:
            parts.append(post_line)
        parts.append("}")
        parts.append("")  # trailing newline
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Checking
    # ------------------------------------------------------------------

    def check(
        self,
        harness_path: str | Path,
        harness_name: str | None = None,
        unwind_override: int | None = None,
        timeout_override: int | None = None,
    ):
        """Run Kani on *harness_path* and return a ``CBMCResult``.

        ``unwind_override`` / ``timeout_override`` let the engine's
        retry path tighten loop bounds or extend the wall-clock when
        a previous run timed out.
        """
        unwind = unwind_override if unwind_override is not None else self._config.kani_unwind
        timeout = timeout_override if timeout_override is not None else self._config.kani_timeout
        return run_kani(
            harness_path=str(harness_path),
            harness_name=harness_name,
            unwind=unwind,
            timeout=timeout,
            kani_path=self._config.kani_path,
        )
