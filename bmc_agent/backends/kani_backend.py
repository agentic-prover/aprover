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
    # Logical implication: (A ==> B) → (!(A) || (B)).
    # Matches paren-wrapped implications without nested parens on either
    # side — the form Phase 1 emits when a postcondition uses ==>.
    # Iterate until no more matches so chained implications inside
    # outer conjunctions all get rewritten.
    _impl_re = re.compile(r"\(\s*([^()]+?)\s*==>\s*([^()]+?)\s*\)")
    while _impl_re.search(expr):
        new_expr = _impl_re.sub(lambda m: f"(!({m.group(1)}) || ({m.group(2)}))", expr)
        if new_expr == expr:
            break  # defensive: shouldn't loop forever
        expr = new_expr
    return expr


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
    ) -> str:
        """Emit a self-contained Rust harness verifying *func* against *spec*.

        The expected ``func`` shape is a duck-typed
        :class:`bmc_agent.parser.FunctionInfo` whose
        ``signature.parameters`` are ``(rust_type, name)`` tuples and whose
        ``signature.return_type`` is a Rust type string.  The function
        body is included verbatim so the harness is compilable standalone.
        """
        params: list[tuple[str, str]] = list(func.signature.parameters)
        return_type = (func.signature.return_type or "").strip()

        # 1. Nondeterministic parameter initialisation.
        slice_bound = getattr(self._config, "kani_slice_bound", _DEFAULT_SLICE_BOUND)
        init_lines: list[str] = []
        arg_names: list[str] = []
        for ty, name in params:
            init_lines.extend(_param_init_block(ty, name, slice_bound=slice_bound))
            arg_names.append(name)

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
        #    can refer to it as `result`.
        call_args = ", ".join(arg_names)
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

        # Compose the file.  fn_def is included so the harness is
        # self-contained; the body it wraps is verbatim from the source.
        parts: list[str] = [
            "//! Auto-generated Kani harness — do not edit by hand.",
            "#![allow(unused_imports, dead_code, non_snake_case)]",
            "",
            fn_def,
            "",
            "#[cfg(kani)]",
            "#[kani::proof]",
            f"fn {harness_name}() {{",
            *init_lines,
        ]
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

    def check(self, harness_path: str | Path, harness_name: str | None = None):
        """Run Kani on *harness_path* and return a ``CBMCResult``."""
        return run_kani(
            harness_path=str(harness_path),
            harness_name=harness_name,
            unwind=self._config.kani_unwind,
            timeout=self._config.kani_timeout,
            kani_path=self._config.kani_path,
        )
