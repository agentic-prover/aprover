"""Tests for the tree-sitter-based Rust parser.

The parser is the M1 frontend for AProver's Rust pipeline.  These tests
cover signature extraction (return types, parameters, generics,
lifetimes, modifiers), body extraction, and callee collection, mirroring
the contract that the C parser already exposes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bmc_agent.rust_parser import (
    ParsedRustFile,
    RustFunctionInfo,
    RustFunctionSignature,
    parse_rust_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(src: str) -> ParsedRustFile:
    return parse_rust_file("synthetic.rs", source_text=src)


# ---------------------------------------------------------------------------
# Basic signature extraction
# ---------------------------------------------------------------------------


def test_parses_simple_fn():
    p = _parse("fn add(a: i32, b: i32) -> i32 { a + b }")
    assert "add" in p.functions
    sig = p.functions["add"]
    assert sig.name == "add"
    assert sig.return_type == "i32"
    assert sig.parameters == [("i32", "a"), ("i32", "b")]
    assert sig.is_pub is False
    assert sig.modifiers == []


def test_unit_return_when_arrow_absent():
    p = _parse("fn noop() { }")
    assert p.functions["noop"].return_type == "()"


def test_pub_visibility_detected():
    p = _parse("pub fn x() -> i32 { 0 }")
    assert p.functions["x"].is_pub is True


def test_modifiers_unsafe_async_const():
    p = _parse(
        "unsafe fn a() {}\n"
        "async fn b() {}\n"
        "const fn c() -> i32 { 0 }\n"
    )
    assert p.functions["a"].modifiers == ["unsafe"]
    assert p.functions["b"].modifiers == ["async"]
    assert p.functions["c"].modifiers == ["const"]


# ---------------------------------------------------------------------------
# Parameter handling
# ---------------------------------------------------------------------------


def test_reference_and_raw_pointer_params():
    p = _parse(
        "fn f(p: *mut u8, q: *const i32, s: &[u8], t: &mut Vec<u8>) {}"
    )
    params = p.functions["f"].parameters
    assert params == [
        ("*mut u8", "p"),
        ("*const i32", "q"),
        ("&[u8]", "s"),
        ("&mut Vec<u8>", "t"),
    ]


def test_function_type_parameter_preserved():
    p = _parse("fn f(op: fn(u64, u64) -> u64) -> i32 { 0 }")
    params = p.functions["f"].parameters
    assert params == [("fn(u64, u64) -> u64", "op")]


def test_tuple_return_type_preserved():
    p = _parse("fn f(x: u8) -> (u8, usize) { (x, 1) }")
    assert p.functions["f"].return_type == "(u8, usize)"


def test_reference_return_type_with_lifetime():
    p = _parse("fn f<'a>(x: &'a [u8]) -> &'a u8 { &x[0] }")
    sig = p.functions["f"]
    assert sig.return_type == "&'a u8"
    assert sig.type_parameters == "<'a>"
    assert sig.parameters == [("&'a [u8]", "x")]


def test_generic_and_where_clause():
    p = _parse(
        "pub fn f<T: Clone>(x: T) -> T where T: std::fmt::Debug { x.clone() }"
    )
    sig = p.functions["f"]
    assert sig.type_parameters == "<T: Clone>"
    assert sig.where_clause == "where T: std::fmt::Debug"
    assert sig.return_type == "T"
    assert sig.parameters == [("T", "x")]


# ---------------------------------------------------------------------------
# Body & callee extraction
# ---------------------------------------------------------------------------


def test_body_includes_braces_verbatim():
    src = "fn id(x: i32) -> i32 {\n    x\n}\n"
    p = _parse(src)
    body = p.function_bodies["id"]
    assert body.startswith("{")
    assert body.endswith("}")
    assert "x" in body


def test_callees_include_free_scoped_method_and_macro():
    src = (
        "fn caller(x: i32) -> i32 {\n"
        "    let a = helper(x);\n"
        "    let b = x.clone();\n"
        "    let c = std::cmp::max(a, b);\n"
        "    println!(\"{}\", c);\n"
        "    other_helper(a)\n"
        "}\n"
    )
    p = _parse(src)
    callees = p.call_graph["caller"]
    # Free function call.
    assert "helper" in callees
    assert "other_helper" in callees
    # Scoped path.
    assert "std::cmp::max" in callees
    # Method call records the field_expression text (function_target).
    assert "x.clone" in callees
    # Macro recorded by name only.
    assert "println" in callees


# ---------------------------------------------------------------------------
# Multi-function & filtering behaviour
# ---------------------------------------------------------------------------


def test_multiple_top_level_fns():
    src = (
        "fn a() -> i32 { 1 }\n"
        "pub fn b() -> i32 { 2 }\n"
        "unsafe fn c() -> i32 { 3 }\n"
    )
    p = _parse(src)
    assert set(p.functions) == {"a", "b", "c"}


def test_impl_methods_with_self_are_recorded():
    """Self-methods (&self/&mut self/self) are now RECORDED (not skipped) so the
    cargo-mode harness gen can construct the receiver field-by-field (build A1)."""
    src = (
        "struct S;\n"
        "impl S {\n"
        "    fn inner(&self) -> i32 { 0 }\n"
        "    fn inner_mut(&mut self) -> i32 { 0 }\n"
        "    fn inner_owned(self) -> i32 { 0 }\n"
        "}\n"
        "fn outer() -> i32 { 0 }\n"
    )
    p = _parse(src)
    assert "outer" in p.functions and p.functions["outer"].has_self_receiver is False
    for m in ("inner", "inner_mut", "inner_owned"):
        assert m in p.functions, m
        assert p.functions[m].has_self_receiver is True, m
    assert p.functions["inner"].receiver_is_mut is False
    assert p.functions["inner_mut"].receiver_is_mut is True
    assert p.functions["inner_owned"].receiver_is_mut is False


def test_impl_static_methods_are_collected():
    """Static methods inside impl blocks (no self) are harnessable as free fns."""
    src = (
        "struct S;\n"
        "impl S {\n"
        "    pub fn helper(x: i32) -> i32 { x + 1 }\n"
        "    pub fn other(y: u32, z: u32) -> u32 { y.wrapping_add(z) }\n"
        "    pub fn with_self(&self) -> i32 { 0 }\n"
        "}\n"
    )
    p = _parse(src)
    assert "helper" in p.functions
    assert "other" in p.functions
    assert "with_self" in p.functions
    assert p.functions["with_self"].has_self_receiver is True
    assert p.functions["helper"].has_self_receiver is False
    assert p.functions["helper"].is_pub is True
    assert p.functions["other"].parameters == [("u32", "y"), ("u32", "z")]


def test_inline_mod_static_fns_are_collected():
    """Functions inside `mod foo { ... }` blocks are reached."""
    src = (
        "mod inner_mod {\n"
        "    pub fn nested(x: i32) -> i32 { x }\n"
        "}\n"
        "fn top() -> i32 { 0 }\n"
    )
    p = _parse(src)
    assert "top" in p.functions
    assert "nested" in p.functions


def test_trait_signature_without_body_is_skipped():
    src = (
        "trait T { fn declared(&self) -> i32; }\n"
        "fn defined() -> i32 { 0 }\n"
    )
    p = _parse(src)
    assert "defined" in p.functions
    # The trait method declaration has no body, so it should not appear.
    assert "declared" not in p.functions


# ---------------------------------------------------------------------------
# FunctionInfo aggregation
# ---------------------------------------------------------------------------


def test_get_function_info_assembles_fields():
    p = _parse("fn f(x: i32) -> i32 { let _ = g(x); x }\nfn g(x: i32) -> i32 { x }\n")
    info = p.get_function_info("f")
    assert info is not None
    assert isinstance(info, RustFunctionInfo)
    assert info.name == "f"
    assert info.signature.return_type == "i32"
    assert "g" in info.callees
    assert info.body.startswith("{")
    assert info.source_file == "synthetic.rs"


def test_get_function_info_missing_returns_none():
    p = _parse("fn f() {}")
    assert p.get_function_info("nope") is None


def test_all_function_infos_returns_one_per_function():
    p = _parse("fn a() {}\nfn b() {}\nfn c() {}\n")
    infos = p.all_function_infos()
    assert {i.name for i in infos} == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Source-text vs path-on-disk
# ---------------------------------------------------------------------------


def test_parses_from_disk(tmp_path: Path):
    f = tmp_path / "x.rs"
    f.write_text("fn disk_fn() -> i32 { 42 }\n")
    p = parse_rust_file(f)
    assert "disk_fn" in p.functions
    assert p.path == str(f)


def test_path_attribution_when_source_supplied():
    p = parse_rust_file("/imaginary/file.rs", source_text="fn x() {}")
    assert p.path == "/imaginary/file.rs"
    assert p.preprocessed_source == "fn x() {}"
