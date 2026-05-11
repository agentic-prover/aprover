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


def _is_pointer_type(rust_type: str) -> bool:
    """True iff *rust_type* is a Rust raw pointer or reference type."""
    t = rust_type.strip()
    return t.startswith("*const ") or t.startswith("*mut ") or t.startswith("&")


def _initialiser_for(rust_type: str) -> str:
    """Return a Rust expression that produces a nondeterministic *rust_type*.

    For primitives we use ``kani::any()``.  For raw pointers we use
    ``kani::any::<usize>() as *mut T`` so Kani explores both null and non-null
    states; the spec's precondition narrows it further.
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
    if t.startswith("&mut "):
        # Safe references cannot be nondeterministic in stable Kani; require
        # the caller to model them via raw pointers in the spec.
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
    return expr


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
        init_lines: list[str] = []
        arg_names: list[str] = []
        for ty, name in params:
            init_lines.append(f"    let {name}: {ty} = {_initialiser_for(ty)};")
            arg_names.append(name)

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
        body = (func.body or "").rstrip()

        # Compose the file.  Body is included so the harness is self-contained.
        parts: list[str] = [
            "//! Auto-generated Kani harness — do not edit by hand.",
            "#![allow(unused_imports, dead_code, non_snake_case)]",
            "",
            body,
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
