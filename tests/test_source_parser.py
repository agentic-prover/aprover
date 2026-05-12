"""Tests for the language-dispatching source parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from bmc_agent.parser import ParsedCFile
from bmc_agent.rust_parser import ParsedRustFile
from bmc_agent.source_parser import (
    UnsupportedSourceLanguage,
    detect_language,
    parse_source_file,
)


def test_detects_c_extension():
    assert detect_language("foo.c") == "c"
    assert detect_language("dir/foo.C") == "c"


def test_detects_h_extension_as_c():
    assert detect_language("foo.h") == "c"


def test_detects_rust_extension():
    assert detect_language("foo.rs") == "rust"
    assert detect_language("dir/foo.RS") == "rust"


def test_unknown_extension_raises():
    with pytest.raises(UnsupportedSourceLanguage):
        detect_language("foo.py")
    with pytest.raises(UnsupportedSourceLanguage):
        detect_language("Makefile")


def test_dispatch_c_returns_parsed_c_file(tmp_path: Path):
    f = tmp_path / "x.c"
    f.write_text("int add(int a, int b) { return a + b; }\n")
    parsed = parse_source_file(f)
    assert isinstance(parsed, ParsedCFile)
    assert "add" in parsed.functions


def test_dispatch_rust_returns_parsed_rust_file(tmp_path: Path):
    f = tmp_path / "x.rs"
    f.write_text("pub fn add(a: i32, b: i32) -> i32 { a + b }\n")
    parsed = parse_source_file(f)
    assert isinstance(parsed, ParsedRustFile)
    assert "add" in parsed.functions
    sig = parsed.functions["add"]
    assert sig.return_type == "i32"
    assert sig.is_pub is True


def test_source_text_supported_for_rust():
    parsed = parse_source_file(
        "synthetic.rs",
        source_text="fn id(x: i32) -> i32 { x }\n",
    )
    assert isinstance(parsed, ParsedRustFile)
    assert "id" in parsed.functions


def test_source_text_supported_for_c():
    parsed = parse_source_file(
        "synthetic.c",
        source_text="int id(int x) { return x; }\n",
    )
    assert isinstance(parsed, ParsedCFile)
    assert "id" in parsed.functions


def test_rust_signature_is_static_attr_present():
    """RustFunctionSignature must expose is_static (always False) so any
    duck-typed downstream code that probes this C-only field doesn't blow up."""
    parsed = parse_source_file(
        "x.rs", source_text="fn f() {}\npub fn g() {}\n"
    )
    assert parsed.functions["f"].is_static is False
    assert parsed.functions["g"].is_static is False
