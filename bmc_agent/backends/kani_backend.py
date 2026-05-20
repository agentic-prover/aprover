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


# Type names we treat as resolvable in a standalone harness without needing a
# matching definition in the source file. Primitives are covered above; this
# set lists the std and core types we routinely encounter plus a few generics
# whose name (not body) is matched against unresolved-type reports.
_STDLIB_RESOLVABLE_TYPES = {
    # alloc / std collections
    "Vec", "String", "Box", "Cow", "Rc", "Arc",
    "HashMap", "HashSet", "BTreeMap", "BTreeSet",
    "VecDeque", "LinkedList", "BinaryHeap",
    # core / std enums
    "Option", "Result",
    # numeric
    "Wrapping", "NonZeroU8", "NonZeroU16", "NonZeroU32", "NonZeroU64",
    "NonZeroI8", "NonZeroI16", "NonZeroI32", "NonZeroI64",
    # range / ord
    "Range", "RangeInclusive", "RangeFrom", "RangeTo",
    "Ordering", "Reverse",
    # cells & locks
    "RefCell", "Cell", "Mutex", "RwLock",
    # phantom / unit
    "PhantomData", "Unit",
    # string-related
    "str", "OsStr", "OsString", "Path", "PathBuf",
    # tuple constructors used in type-name extraction
    "Self",  # impl methods only; if the body uses Self::foo we drop separately
}


# External crate types we fully alias to a stdlib equivalent in the harness
# preamble so Kani can compile signatures that mention them. Each entry maps
# the foreign name to a (stdlib-resolved) alias declaration the preamble
# emits when the name appears in source.
_EXTERNAL_TYPE_ALIASES = {
    "FxHashMap": "type FxHashMap<K, V> = std::collections::HashMap<K, V>;",
    "FxHashSet": "type FxHashSet<T> = std::collections::HashSet<T>;",
    "IndexMap": "type IndexMap<K, V> = std::collections::HashMap<K, V>;",
    "IndexSet": "type IndexSet<T> = std::collections::HashSet<T>;",
    "AHashMap": "type AHashMap<K, V> = std::collections::HashMap<K, V>;",
    "AHashSet": "type AHashSet<T> = std::collections::HashSet<T>;",
}


class HarnessUnresolvableTypes(Exception):
    """Raised by ``generate_harness`` when *func* references types neither
    defined in its source file nor in our resolvable allow-list.

    The engine should catch this, mark the function as
    ``harness-skipped-unresolvable-types``, and move on — emitting a doomed
    Kani compile invocation would just produce 100s of noise E0412 errors
    per file (CCC's encoder/codegen impl-method files are the canonical
    case).
    """
    def __init__(self, function_name: str, unresolved_types: list[str]):
        self.function_name = function_name
        self.unresolved_types = unresolved_types
        super().__init__(
            f"function '{function_name}' references types not resolvable in a "
            f"standalone harness: {', '.join(unresolved_types)}"
        )


def _extract_type_names(rust_type: str) -> set[str]:
    """Pull the set of named types out of a Rust type expression.

    Looks for tokens that match CamelCase or simple identifiers in type
    position. Skips references/pointers/lifetimes/generics syntax. Returns
    the bare type names (no path, no generic args).
    """
    import re as _re
    if not rust_type:
        return set()
    # Strip refs, lifetimes, pointers, brackets, parens, generic args.
    stripped = _re.sub(r"&(?:'\w+\s+)?(?:mut\s+)?", "", rust_type)
    stripped = _re.sub(r"\*(?:mut|const)\s+", "", stripped)
    # Capture identifiers in type position (path-qualified or bare).
    names = set()
    for tok in _re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", stripped):
        # Skip Rust keywords / control flow that may appear in types.
        if tok in {"dyn", "impl", "mut", "where", "fn", "Self"}:
            continue
        names.add(tok)
    return names


def _types_defined_in_source(source: str) -> set[str]:
    """Scan source text for type definitions.

    Recognises:
      * ``struct T``, ``enum T``, ``union T``, ``type T = ...``, ``trait T``
        (each optionally prefixed by ``pub``/``pub(...)``)
      * tuple-struct constructors of those types (e.g. enum variants) are
        NOT added separately -- the variant ``EncodeResult::Word`` resolves
        through the type ``EncodeResult`` itself.

    Matches anywhere in the file (not just at line start) so that
    multi-item single-line source still resolves correctly.
    """
    import re as _re
    if not source:
        return set()
    out: set[str] = set()
    for m in _re.finditer(
        r"\b(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|union|type|trait)\s+([A-Za-z_][A-Za-z0-9_]*)",
        source,
    ):
        out.add(m.group(1))
    return out


def _function_references_unresolvable_types(
    func, parsed_file, file_source: str | None
) -> set[str]:
    """Return the set of types referenced by *func*'s signature or body that
    cannot be resolved in a standalone harness compilation.

    A type counts as resolvable if it is:
    * a Rust primitive (`i32`, `bool`, `char`, etc.),
    * an std/core/alloc type listed in :data:`_STDLIB_RESOLVABLE_TYPES`,
    * an external-crate type with a known alias in
      :data:`_EXTERNAL_TYPE_ALIASES` (the alias is injected in the harness),
    * the function under test itself,
    * defined in the source file (`struct Foo`, `enum Foo`, `type Foo = ...`).

    Returns an empty set when every referenced type resolves.
    """
    referenced: set[str] = set()
    # Signature types
    for ty, _ in (func.signature.parameters or []):
        referenced |= _extract_type_names(ty)
    referenced |= _extract_type_names(func.signature.return_type or "")
    # Body types: scan for CamelCase identifiers used in type position.
    # This is coarse — a CamelCase token in a string literal would falsely
    # register — but the downstream "is it defined in source?" check handles
    # benign cases, and the cost of being slightly conservative (skip more)
    # is exactly the trade we want when CCC's encoder files import dozens of
    # types from sibling parser/state modules.
    body = getattr(func, "body", "") or ""
    import re as _re
    for tok in _re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", body):
        if len(tok) >= 2:  # skip single-letter generics
            referenced.add(tok)

    # What's locally defined?
    defined_locally: set[str] = set()
    if file_source:
        defined_locally = _types_defined_in_source(file_source)
    # parsed_file may know about other fns; those names aren't types.

    unresolved: set[str] = set()
    for name in referenced:
        if name in _PRIMITIVE_RUST_TYPES:
            continue
        if name in _STDLIB_RESOLVABLE_TYPES:
            continue
        if name in _EXTERNAL_TYPE_ALIASES:
            continue
        if name in defined_locally:
            continue
        if name == func.name:
            continue
        # CamelCase identifiers from body might be enum variants, constants,
        # or struct constructors (`Some::Variant`, `Result::Ok`). Skip the
        # ones that look like values rather than types — i.e. that appear
        # used as `::Variant(...)` or `::CONST` rather than as types.
        if body:
            # If the identifier never appears in a type-position context,
            # treat it as a value reference.
            type_position = _re.search(
                rf"\b{name}\b(?:\s*<|::|\s*\{{|\s*\(|\s*,|\s*\)|$)",
                body,
            )
            if not type_position:
                continue
            # If every appearance is part of `Other::name(...)` (a path-
            # qualified variant / function call, where `name` is a value, not
            # a type), skip. We approximate "appears as a type" by looking
            # for one of:
            #   ` : name`  (binding / field type)
            #   `-> name`  (return type)
            #   `< name`   (generic arg head — open bracket then optional ws)
            #   `, name`   (next generic arg or tuple field)
            #   `( name`   (start of tuple-struct ctor signature)
            # We explicitly exclude `:: name` (preceding `::` means the
            # name is a path component, not a type itself).
            preceded_by_double_colon = _re.search(
                rf"::\s*{name}\b", body
            )
            type_anchor = _re.search(
                rf"(?:^|[^:])(?::\s+|->\s*|<\s*|,\s*|\(\s*){name}\b",
                body,
            )
            if not type_anchor:
                continue
            # If the only "type-like" occurrence is actually a `::name` path
            # reference (preceded_by_double_colon) AND no bare-type pattern
            # exists, also skip.
            if preceded_by_double_colon and not _re.search(
                rf"(?:^|[ \t\(,])(?::\s+|->\s*|<\s*)?{name}\b",
                body,
            ):
                continue
        unresolved.add(name)
    return unresolved


def _harness_preamble_for_external_types(referenced_types: set[str]) -> list[str]:
    """Return type-alias declarations for any external-crate types referenced
    by the function. Emitted at the top of the harness so Kani can resolve
    them without us shipping the upstream crate.
    """
    out: list[str] = []
    for ty in sorted(referenced_types):
        alias = _EXTERNAL_TYPE_ALIASES.get(ty)
        if alias:
            out.append(alias)
    return out


# Default fixed-size bound for nondeterministic slice and array
# initialisation in harnesses.  BMC verification is bounded by
# construction, so we explore all slice contents and lengths up to this
# cap.  Picked small to keep verification time reasonable; raise via
# config.kani_slice_bound when a spec needs more reach.
_DEFAULT_SLICE_BOUND = 4


# Element types we can safely nondet-init with ``kani::any()`` inside an
# array. Kani auto-derives ``Arbitrary`` for primitives and tuples of
# primitives but NOT for user-defined enums/structs (those need an
# explicit derive). When the slice element is something else (e.g.
# ``ExprToken`` from CCC asm_expr.rs), an empty-slice fallback keeps the
# harness compilable so the function gets a verdict instead of E0277.
def _element_is_arbitrary(elem_type: str) -> bool:
    e = elem_type.strip()
    if e in _PRIMITIVE_RUST_TYPES:
        return True
    # Strip a single ``&`` or ``&mut `` borrow — Kani auto-implements
    # Arbitrary for refs whose pointee is Arbitrary, but raw byte arrays
    # of references are not what we want anyway (no storage to point
    # into), so be conservative.
    return False


def _is_pointer_type(rust_type: str) -> bool:
    """True iff *rust_type* is a Rust raw pointer or reference type."""
    t = rust_type.strip()
    return t.startswith("*const ") or t.startswith("*mut ") or t.startswith("&")


def _is_slice_type(rust_type: str) -> bool:
    """True iff *rust_type* is a shared slice reference like ``&[T]``.

    Mutable slices (``&mut [T]``) are recognised by the same prefix
    check after stripping ``mut ``.

    Fixed-size array references ``&[T; N]`` are NOT slices -- those
    contain a size expression after a semicolon inside the brackets.
    Strip the leading reference + lifetime first to handle the
    ``&'static [T]`` shape too.
    """
    t = rust_type.strip()
    if t.startswith("&mut "):
        t = t[len("&mut "):].strip()
    elif t.startswith("&"):
        t = t[1:].strip()
        # Drop a leading lifetime token (e.g. "'static ", "'a ").
        if t.startswith("'"):
            sp = t.find(" ")
            if sp != -1:
                t = t[sp + 1:].strip()
    else:
        return False
    if not (t.startswith("[") and t.endswith("]")):
        return False
    # Distinguish `[T]` (slice) from `[T; N]` (fixed-size array).
    # The slice form has NO top-level semicolon between the brackets.
    inner = t[1:-1]
    depth = 0
    for ch in inner:
        if ch == "<" or ch == "[":
            depth += 1
        elif ch == ">" or ch == "]":
            depth -= 1
        elif ch == ";" and depth == 0:
            return False  # fixed-size array, not a slice
    return True


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


def _scan_pointer_newtypes(crate_root: "Path | str | None") -> dict[str, dict]:
    """Walk a Rust crate's src tree looking for pointer-newtype wrappers:
    structs with a single field of pointer type.

    Returns a dict mapping the bare type name (e.g. ``"SendPtr"``) to:
        {"field": "ptr" | None, "kind": "named" | "tuple", "mut": True|False}

    Used by ``_param_init_block`` to construct nondeterministic instances
    of crate-local pointer wrappers. Concrete v22 case: llm.rs defines
    ``pub struct SendPtr<T> { pub ptr: *mut T }`` -- harnesses for fns
    taking ``SendPtr<f32>`` need to emit
    ``SendPtr { ptr: kani::any::<usize>() as *mut f32 }``.
    """
    import re as _re_n
    from pathlib import Path as _Path
    if not crate_root:
        return {}
    root = _Path(str(crate_root))
    if not root.is_dir():
        return {}
    out: dict[str, dict] = {}
    # Named-field form:  struct NAME<T> { pub field: *mut T }
    rx_named = _re_n.compile(
        r"\bpub\s+struct\s+([A-Z][A-Za-z0-9_]*)\s*<[^>]+>\s*\{\s*pub\s+(\w+)\s*:\s*\*(mut|const)\s+\w+\s*[,}]",
        _re_n.MULTILINE,
    )
    # Tuple form:  struct NAME<T>(pub *mut T)
    rx_tuple = _re_n.compile(
        r"\bpub\s+struct\s+([A-Z][A-Za-z0-9_]*)\s*<[^>]+>\s*\(\s*pub\s+\*(mut|const)\s+\w+",
        _re_n.MULTILINE,
    )
    for f in root.rglob("*.rs"):
        try:
            txt = f.read_text(errors="ignore")
        except Exception:
            continue
        for m in rx_named.finditer(txt):
            name, field, mut = m.group(1), m.group(2), m.group(3)
            if name in out:
                continue
            out[name] = {"field": field, "kind": "named", "mut": mut == "mut"}
        for m in rx_tuple.finditer(txt):
            name, mut = m.group(1), m.group(2)
            if name in out:
                continue
            out[name] = {"field": None, "kind": "tuple", "mut": mut == "mut"}
    return out


def _param_init_block(
    rust_type: str,
    name: str,
    slice_bound: int = _DEFAULT_SLICE_BOUND,
    pointer_newtypes: dict | None = None,
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
    # Pointer-newtype check FIRST: when t looks like `Foo<X>` and Foo is a
    # known pointer-newtype in this crate (e.g. SendPtr<f32>), emit the
    # struct constructor instead of trying to call kani::any::<Foo<X>>()
    # which would fail unless Foo derives Arbitrary.
    if pointer_newtypes:
        import re as _re_pn
        m = _re_pn.match(r"([A-Z][A-Za-z0-9_]*)\s*<\s*([A-Za-z0-9_:<>, *&]+)\s*>\s*$", t)
        if m and m.group(1) in pointer_newtypes:
            tname, inner = m.group(1), m.group(2).strip()
            info = pointer_newtypes[tname]
            mut_kw = "mut" if info["mut"] else "const"
            ptr_expr = f"kani::any::<usize>() as *{mut_kw} {inner}"
            if info["kind"] == "named":
                ctor = f"{tname} {{ {info['field']}: {ptr_expr} }}"
            else:
                ctor = f"{tname}({ptr_expr})"
            return [f"    let {name}: {t} = {ctor};"]
    if _is_slice_type(t):
        elem = _slice_element_type(t)
        backing = f"_backing_{name}"
        length = f"_len_{name}"
        borrow = "&mut " if t.startswith("&mut ") else "&"
        if not _element_is_arbitrary(elem):
            # Element type isn't a primitive Kani knows how to nondet-init
            # (e.g. user-defined enum ``ExprToken`` with `&'static str`
            # variants). Without ``impl kani::Arbitrary for T`` the
            # original ``[T; N] = kani::any()`` produces E0277. Fall back
            # to an empty Vec backing so the harness compiles and the
            # function gets *some* verdict — even if degenerate, an empty
            # slice exercises the early-return / "no tokens" branch.
            return [
                f"    let mut {backing}: Vec<{elem}> = Vec::new();",
                f"    let {name}: {t} = {borrow}{backing}[..];",
            ]
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
        if not _element_is_arbitrary(elem):
            # Same Arbitrary fallback as the slice case above. Empty Vec
            # keeps the harness compilable when T is a user-defined type.
            return [
                f"    let {name}: Vec<{elem}> = Vec::new();",
            ]
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
    if (
        t.startswith("&")
        and not t.startswith("&mut ")
        and not _is_str_ref_type(t)  # &str handled separately below
        and not _is_slice_type(t)    # &[T] handled above
    ):
        # Immutable `&T` mirrors the `&mut T` logic below but without
        # the `mut` qualifier. Handles &Vec<T>, &String, &<primitive>,
        # &'static T (via Box::leak), and &SomeStruct (when the struct
        # impls kani::Arbitrary -- otherwise the resulting harness will
        # fail to compile with E0277).
        # Strips any lifetime annotation ('static, 'a, etc.) for the
        # inner-type lookup, but preserves it in the local binding type
        # because the called function may require a specific lifetime.
        inner_raw = t[1:].strip()
        lifetime = ""
        if inner_raw.startswith("'"):
            sp = inner_raw.find(" ")
            if sp != -1:
                lifetime = inner_raw[:sp].strip()
                inner = inner_raw[sp + 1:].strip()
            else:
                inner = inner_raw
        else:
            inner = inner_raw
        backing = f"_owned_{name}"
        if _is_vec_type(inner):
            elem = _vec_element_type(inner)
            ctor = f"Vec::<{elem}>::new()"
        elif inner == "String":
            ctor = "String::new()"
        else:
            # Both primitives and generic structs go through kani::any.
            # For non-Arbitrary structs the harness will fail to compile
            # with a clear E0277; that's still better than the previous
            # blanket NotImplementedError (which never even tried).
            ctor = f"kani::any::<{inner}>()"
        if lifetime == "'static":
            # 'static lifetimes require leaking the backing so it lives
            # forever. Used by crc-rs's algorithm tables.
            return [
                f"    let {name}: &'static {inner} = Box::leak(Box::new({ctor}));",
            ]
        # No lifetime, or non-static lifetime (Rust will infer at call site).
        return [
            f"    let {backing}: {inner} = {ctor};",
            f"    let {name}: &{inner} = &{backing};",
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

    If a top-level ``==>`` has no enclosing parens (e.g. the LLM emits
    ``A ==> B && C ==> D`` at the outermost expression scope), we wrap
    the whole expression in parens once so the algorithm can proceed.
    Concrete example we observed: arrayvec's raw_ptr_add spec emitted
    ``mem::size_of::<T>() == 0 ==> (...) && mem::size_of::<T>() != 0
    ==> (...)`` at top level. Previously the rewriter bailed out and the
    raw ``==>`` token leaked into Kani.

    Each ``==>`` is then rewritten in place; nested implications are
    handled by iteration.
    """
    if "==>" not in expr:
        return expr

    # Auto-wrap the expression in parens if any top-level ==> is not
    # already inside an enclosing pair (only need to wrap once: nested
    # implications get their own enclosing group from the rewriter as
    # it processes outer ones).
    def _has_unbracketed_implication(s: str) -> bool:
        depth = 0
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and s[i:i+3] == "==>":
                return True
            i += 1
        return False

    if _has_unbracketed_implication(expr):
        expr = f"({expr})"

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


def _extract_old_snapshots(postcondition: str) -> "tuple[str, list[str]]":
    """Rewrite ``old(EXPR)`` references in *postcondition* into snapshot
    variables and return the rewritten post + the list of snapshot
    declarations needed before the function call.

    Background: the spec generator's functional spec emits ``old(EXPR)``
    to reference the value of EXPR at function entry — standard
    verification-logic syntax (CBMC's __CPROVER_old, JML's \\old, Eiffel's
    old, Why3's old, etc.). Kani's plain ``kani::assert`` has no
    pre-state mechanism, so we have to materialise the snapshot in the
    harness: capture the value before calling the function, name it,
    and substitute the name into the post.

    Implementation:
    - Bracket-match each ``old(...)`` (paren-balanced, supports nesting).
    - For each occurrence, generate ``let _pre_N = (STRIPPED_EXPR);``
      where N is a counter and STRIPPED_EXPR is the inner expression
      with any nested ``old(...)`` wrappers removed (everything at the
      snapshot point is already pre-state).
    - Replace each ``old(EXPR)`` in the post with ``_pre_N``.
    - For expressions that look like slice indexing (contain ``[``)
      append ``.to_vec()`` so the snapshot owns the data and survives
      mutations to the original buffer.

    The output ``snapshot_lines`` are intended to be inlined into the
    harness *before* the function-under-test call, so the post can
    reference them after the call returns.

    Returns ``(rewritten_post, snapshot_lines)``. If the input
    contains no ``old(`` token, returns ``(postcondition, [])``
    unchanged.
    """
    import re as _re
    if "old(" not in postcondition:
        return postcondition, []

    # Find top-level old(...) calls with paren-balanced extraction.
    # A simple regex like ``old\(.*?\)`` would mis-match nested parens,
    # which the LLM frequently produces (e.g. ``old(buf[..old(buf.len())])``).
    snapshots: list[str] = []
    rewritten_chars: list[str] = []
    i = 0
    n = len(postcondition)
    counter = [0]

    def strip_inner_old(expr: str) -> str:
        # Recursively remove ``old(`` and matching ``)`` from the inner
        # expression. At snapshot time, everything is already pre-state,
        # so nested old() is the identity function.
        out: list[str] = []
        j = 0
        while j < len(expr):
            if expr[j:j+4] == "old(":
                depth = 1
                j += 4
                inner_start = j
                while j < len(expr) and depth > 0:
                    if expr[j] == "(":
                        depth += 1
                    elif expr[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                # j is at the matching ')'. Recursively strip.
                inner = strip_inner_old(expr[inner_start:j])
                out.append(inner)
                j += 1  # skip ')'
            else:
                out.append(expr[j])
                j += 1
        return "".join(out)

    while i < n:
        if postcondition[i:i+4] == "old(" and (i == 0 or not postcondition[i-1].isalnum() and postcondition[i-1] != "_"):
            # Found a top-level old() call (the lookbehind avoids matching
            # identifiers like ``cold(`` or ``_old(`` that happen to end in
            # "old"). Bracket-match the closing paren.
            depth = 1
            j = i + 4
            inner_start = j
            while j < n and depth > 0:
                if postcondition[j] == "(":
                    depth += 1
                elif postcondition[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                # Unmatched — give up on this old(), pass through verbatim.
                rewritten_chars.append(postcondition[i])
                i += 1
                continue
            inner_expr = strip_inner_old(postcondition[inner_start:j])
            var = f"_pre_{counter[0]}"
            counter[0] += 1
            # Slice-index expressions need to_vec() so the snapshot owns
            # the bytes and doesn't borrow from the about-to-be-mutated
            # buffer. Detect by ``[`` presence; scalar exprs like
            # ``buf.len()`` stay as-is (assume Copy).
            if "[" in inner_expr:
                snapshots.append(f"    let {var} = ({inner_expr}).to_vec();")
            else:
                snapshots.append(f"    let {var} = ({inner_expr});")
            rewritten_chars.append(var)
            i = j + 1
        else:
            rewritten_chars.append(postcondition[i])
            i += 1

    return "".join(rewritten_chars), snapshots


def _transitive_callees(direct_callees, parsed_file) -> "set[str]":
    """Expand *direct_callees* into its transitive closure under
    ``parsed_file.call_graph``.

    Background: ``func.callees`` only records direct call targets,
    but the strip helper keeps sibling fns ONLY if they appear in
    the keep set. When a kept sibling itself calls another sibling
    (``eval_add -> eval_mul -> eval_unary``), the indirectly-reached
    helper gets stripped and the harness fails to compile with
    ``E0425 cannot find function in this scope``. Walking the
    call graph closes this gap.

    Edge cases:
    - parsed_file is None (test fixtures): return the direct set verbatim.
    - call_graph absent: same fallback.
    - cycles: handled by the visited set.
    """
    closure = set(direct_callees or set())
    if parsed_file is None:
        return closure
    call_graph = getattr(parsed_file, "call_graph", None)
    if not call_graph:
        return closure
    worklist = list(closure)
    while worklist:
        name = worklist.pop()
        for callee in call_graph.get(name, set()) or set():
            if callee not in closure:
                closure.add(callee)
                worklist.append(callee)
    return closure


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
    # Match `use crate::...;`, `pub use crate::...;`, `pub(crate) use crate::...;`,
    # `pub(super) use ...;`, etc. The visibility modifier is optional and may
    # include a parenthesised qualifier. ``[^;]*`` with DOTALL handles the
    # multi-line ``{...}`` import-list form.
    pattern = _re.compile(
        r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?use\s+(?:crate|super|self)\s*::"
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

        # Generic-function gate. If the function carries a `<T, U, ...>`
        # type-parameter list AND any of those parameters appears in a
        # parameter type or the return type, the harness can't be
        # monomorphised (we'd emit `let x: T = kani::any::<T>()` which
        # rustc rejects with E0412 because T isn't in scope at harness
        # body). Examples we observed: bitflags::iter::new<B>(flags: B),
        # twox-hash's xxhash<F: Trait>(...). Raise so the engine records
        # the skip cleanly.
        type_params = getattr(func.signature, "type_parameters", "") or ""
        if type_params:
            import re as _re_gen
            # Extract bare identifier names from "<T, U: Bound, V: Trait + 'a>".
            # Track depth on (), [], {}, AND <> — but skip `>` when preceded by
            # `-` (the `->` arrow inside trait bounds like `F: Fn(...) -> u32`).
            inner = type_params.strip().lstrip("<").rstrip(">")
            type_param_names: list[str] = []
            depth = 0
            current = ""
            for i, ch in enumerate(inner + ","):
                if ch in ("(", "[", "{", "<"):
                    depth += 1
                    current += ch
                elif ch in (")", "]", "}"):
                    depth -= 1
                    current += ch
                elif ch == ">":
                    # Arrow `->` -- not a closing angle bracket.
                    if i > 0 and (inner + ",")[i - 1] == "-":
                        current += ch
                    else:
                        depth -= 1
                        current += ch
                elif ch == "," and depth == 0:
                    name = current.strip().split(":")[0].strip()
                    # Skip lifetimes ('a, 'static) and const generics
                    if name and not name.startswith("'") and not name.startswith("const"):
                        type_param_names.append(name)
                    current = ""
                else:
                    current += ch
            # Check if any T-name appears as a bare identifier in any param
            # type or the return type.
            sig_types = [pt for pt, _ in params] + [return_type]
            sig_blob = " ".join(sig_types)
            used_generics = [
                t for t in type_param_names
                if _re_gen.search(rf"\b{_re_gen.escape(t)}\b", sig_blob)
            ]
            if used_generics:
                raise NotImplementedError(
                    f"function {func.name!r} uses unmonomorphised type "
                    f"parameter(s) {used_generics} in its signature -- Kani "
                    f"requires concrete types in proof harnesses"
                )

        # Pre-emit type-resolvability gate. Functions whose signature or body
        # references types not defined in this source file (and not stdlib /
        # known external alias) can't compile standalone — the historical
        # pattern is CCC's impl-block methods that reference Operand /
        # EncodeResult / RelocType from sibling parser.rs / state.rs files.
        # Emitting a harness for those produces ~500 noise E0412 reports per
        # sweep; gate them out cleanly and let the pipeline mark them as
        # "harness-skipped-unresolvable-types".
        file_source_for_gate = _load_full_source(func, parsed_file)
        unresolved = _function_references_unresolvable_types(
            func, parsed_file, file_source_for_gate
        )
        if unresolved:
            # Drop external aliases we WILL inject; if everything left is
            # genuinely unresolvable, raise a marker so the engine records
            # the skip without trying to compile.
            real_unresolved = unresolved - set(_EXTERNAL_TYPE_ALIASES.keys())
            if real_unresolved:
                raise HarnessUnresolvableTypes(
                    function_name=func.name,
                    unresolved_types=sorted(real_unresolved),
                )

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
        # Discover crate-local pointer-newtype wrappers (e.g. SendPtr<T>
        # in llm.rs) so we can emit constructors instead of failing on
        # `kani::any::<SendPtr<f32>>()`.
        from bmc_agent.kani import find_crate_root as _find_crate_root_pn
        _crate_root_pn = None
        if getattr(self._config, "kani_real_crate", False):
            _src_for_pn = getattr(func, "source_file", "") or ""
            if _src_for_pn:
                _crate_root_pn = _find_crate_root_pn(_src_for_pn)
        _ptr_newtypes = _scan_pointer_newtypes(_crate_root_pn) if _crate_root_pn else {}
        for ty, name in params:
            init_lines.extend(_param_init_block(
                ty, name, slice_bound=slice_bound,
                pointer_newtypes=_ptr_newtypes,
            ))
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
        # Cargo-mode: if the function was extracted from `impl Foo { fn name() {...} }`,
        # qualify the call as `Foo::name(args)` so it resolves in the host
        # crate's namespace. In standalone mode, the function is inlined as a
        # free fn so the bare name works. Also rewrite `Self` in the return
        # type for the same reason.
        impl_type = getattr(getattr(func, "signature", None), "impl_type", "") or ""
        if impl_type:
            # Generic impl types need turbofish syntax for method calls:
            #   Crc<u8, Table<L>>::new  →  Crc::<u8, Table<L>>::new
            # without the `::`, rustc parses `<` as a comparison operator
            # and the harness fails to compile. Detect the generic case
            # by the presence of `<` in impl_type.
            if "<" in impl_type:
                generic_start = impl_type.index("<")
                base = impl_type[:generic_start]
                generics = impl_type[generic_start:]
                call_target = f"{base}::{generics}::{func.name}"
            else:
                call_target = f"{impl_type}::{func.name}"
        else:
            call_target = func.name
        # `Self` in return type only resolves inside an impl. In a free-fn
        # harness body we need to substitute the impl type.
        if impl_type and "Self" in return_type:
            return_type = return_type.replace("Self", impl_type)
        # `unsafe fn` requires the call site to be in an `unsafe` block.
        # Examples we observed: llm.rs declares `pub unsafe fn
        # attention_forward(...)`; without wrapping, rustc emits E0133.
        is_unsafe_fn = "unsafe" in (getattr(func.signature, "modifiers", []) or [])
        if return_type in ("", "()"):
            if is_unsafe_fn:
                call_line = f"    unsafe {{ {call_target}({call_args}); }}"
            else:
                call_line = f"    {call_target}({call_args});"
            result_binding = ""
        else:
            if is_unsafe_fn:
                call_line = f"    let result: {return_type} = unsafe {{ {call_target}({call_args}) }};"
            else:
                call_line = f"    let result: {return_type} = {call_target}({call_args});"
            result_binding = "result"

        # 4. Postcondition → kani::assert.  Functional specs may reference
        #    pre-call state via ``old(EXPR)``; extract those into snapshot
        #    bindings emitted before the function call. This lets the
        #    LLM-generated post for state-mutating fns (pad_to,
        #    write_elf64_phdr*, etc.) actually compile under Kani, which
        #    has no native pre-state operator.
        raw_post = spec.postcondition or "true"
        # `Self` only resolves inside an impl block. The harness body is a
        # free fn appended at top-level, so substitute the implementing type
        # before translating the DSL. Examples we observed:
        #   `result == Self::default()`  → `result == Adler32::default()`
        #   `result.len() == Self::CAP`  → `result.len() == Foo::CAP`
        import re as _re_self
        if impl_type and "Self" in raw_post:
            raw_post = _re_self.sub(r"\bSelf\b", impl_type, raw_post)
        rewritten_post, old_snapshots = _extract_old_snapshots(raw_post)
        # Same substitution for the precondition.
        raw_pre = spec.precondition or ""
        if impl_type and raw_pre and "Self" in raw_pre:
            raw_pre = _re_self.sub(r"\bSelf\b", impl_type, raw_pre)
        post_expr = _translate_dsl(rewritten_post, result_var=result_binding or "result")
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
        # External-crate alias preamble (FxHashMap = std::HashMap, etc.).
        # Emitted ahead of the cleaned source so the aliases are in scope for
        # every subsequent reference. Only injected when the function actually
        # references one of these types — otherwise it's dead boilerplate.
        referenced_for_preamble: set[str] = set()
        for ty, _ in (func.signature.parameters or []):
            referenced_for_preamble |= _extract_type_names(ty)
        referenced_for_preamble |= _extract_type_names(func.signature.return_type or "")
        body_for_preamble = getattr(func, "body", "") or ""
        if body_for_preamble:
            import re as _re2
            for tok in _re2.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", body_for_preamble):
                if tok in _EXTERNAL_TYPE_ALIASES:
                    referenced_for_preamble.add(tok)
        external_aliases = _harness_preamble_for_external_types(referenced_for_preamble)
        if external_aliases:
            parts.append("// External-crate type aliases (injected by harness generator):")
            parts.extend(external_aliases)
            parts.append("")

        if file_source is not None:
            cleaned = _strip_crate_local_use_statements(file_source)
            cleaned = _strip_pub_in_path_visibility(cleaned)
            # Compute the transitive closure of callees from
            # parsed_file.call_graph so helper fns reachable indirectly
            # (e.g. eval_add -> eval_mul -> eval_unary) survive the
            # sibling-strip step. Without this, the stripper drops
            # `eval_unary` because it isn't in `eval_add.callees`
            # directly, even though eval_mul (which IS kept) calls it.
            direct_callees = set(getattr(func, "callees", set()) or set())
            keep_callees = _transitive_callees(direct_callees, parsed_file)
            cleaned = _strip_crate_local_fn_items(
                cleaned,
                keep_fn_name=func.name,
                keep_callees=keep_callees,
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
        # Snapshot pre-call state for any ``old(EXPR)`` in the post. Must
        # come AFTER the precondition assumption (so we snapshot in-spec
        # states only) and BEFORE the call (otherwise we'd capture
        # post-call state, defeating the point).
        if old_snapshots:
            parts.extend(old_snapshots)
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
        source_path: str | Path | None = None,
    ):
        """Run Kani on *harness_path* and return a ``CBMCResult``.

        ``unwind_override`` / ``timeout_override`` let the engine's
        retry path tighten loop bounds or extend the wall-clock when
        a previous run timed out.

        When ``config.kani_real_crate`` is True AND ``source_path`` is
        supplied AND that path lies in a Cargo crate, the harness is
        verified via ``cargo kani --tests`` from the crate root instead
        of as a standalone Kani invocation. This resolves cross-crate
        imports naturally (ruff_python_ast, tree-sitter types, etc.) and
        is the only way to verify functions in modern multi-crate Rust
        workspaces.
        """
        from bmc_agent.kani import find_crate_root, run_kani_cargo

        unwind = unwind_override if unwind_override is not None else self._config.kani_unwind
        timeout = timeout_override if timeout_override is not None else self._config.kani_timeout

        if getattr(self._config, "kani_real_crate", False) and source_path:
            crate_root = find_crate_root(source_path)
            if crate_root is not None and harness_name:
                # Cargo-mode minimum timeout: cargo kani's compile+Kani-MIR
                # pass routinely takes 30-90s alone before the SMT phase even
                # starts. The default 120s often kills the run mid-MIR-compile
                # on first invocation. Bump to at least 600s for cargo-mode
                # (the cargo target/ cache makes subsequent runs faster, but
                # the first one needs the headroom).
                if timeout_override is None:
                    timeout = max(timeout, 600)
                # Append the harness to its function-under-test's source
                # file so it can access private items, then run cargo kani.
                # The host crate's compilation context resolves all
                # cross-crate imports naturally.
                harness_src = Path(harness_path).read_text()
                return run_kani_cargo(
                    crate_root=crate_root,
                    source_path=source_path,
                    harness_src=harness_src,
                    harness_name=harness_name,
                    unwind=unwind,
                    timeout=timeout,
                )

        return run_kani(
            harness_path=str(harness_path),
            harness_name=harness_name,
            unwind=unwind,
            timeout=timeout,
            kani_path=self._config.kani_path,
        )
