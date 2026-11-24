"""Tests for bmc_agent.spec_gen_tools — v2.2 tool handlers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bmc_agent.spec_gen_tools import (
    SpecToolContext,
    TOOL_USE_PROMPT_ADDENDUM,
    build_spec_gen_tools,
)


def _mini_parsed(funcs=None, struct_defs=None):
    """Minimal ParsedCFile-like object for handler tests."""
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    funcs = funcs or {}
    parsed = MagicMock()
    parsed.functions = {name: fi.signature for name, fi in funcs.items()}
    parsed.struct_definitions = struct_defs or {}
    parsed.call_graph = {n: getattr(fi, "callees", set()) for n, fi in funcs.items()}
    parsed.get_function_info = lambda n: funcs.get(n)
    return parsed


def _fi(name, body="", params=None, callees=None, source="x.c"):
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name=name, return_type="int",
        parameters=params or [("int", "x")],
    )
    return FunctionInfo(
        name=name, signature=sig, body=body,
        callees=set(callees or []), source_file=source,
    )


# ---------- build_spec_gen_tools ------------------------------------------


def test_build_returns_5_tools():
    ctx = SpecToolContext(parsed=_mini_parsed(), corpus_paths=[],
                          all_specs_so_far={})
    tools, handlers = build_spec_gen_tools(ctx)
    assert len(tools) == 5
    assert set(handlers.keys()) == {
        "lookup_function", "find_more_callers", "lookup_struct",
        "lookup_caller_spec", "grep_corpus",
    }
    # Every tool has a non-empty description.
    for t in tools:
        assert t.description.strip()


def test_build_tool_names_match_handlers():
    ctx = SpecToolContext(parsed=_mini_parsed(), corpus_paths=[],
                          all_specs_so_far={})
    tools, handlers = build_spec_gen_tools(ctx)
    tool_names = {t.name for t in tools}
    assert tool_names == set(handlers.keys())


# ---------- lookup_function -----------------------------------------------


def test_lookup_function_found():
    fi = _fi("foo", body="{ return x + 1; }",
             params=[("int", "x"), ("int", "y")])
    ctx = SpecToolContext(parsed=_mini_parsed({"foo": fi}),
                          corpus_paths=[], all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["lookup_function"]({"name": "foo"})
    assert out["name"] == "foo"
    assert out["return_type"] == "int"
    assert out["parameters"] == [{"type": "int", "name": "x"},
                                 {"type": "int", "name": "y"}]
    assert out["body"] == "{ return x + 1; }"
    assert out["body_truncated"] is False


def test_lookup_function_missing():
    ctx = SpecToolContext(parsed=_mini_parsed(),
                          corpus_paths=[], all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["lookup_function"]({"name": "nonexistent"})
    assert "error" in out
    assert "not found" in out["error"]


def test_lookup_function_empty_name():
    ctx = SpecToolContext(parsed=_mini_parsed(),
                          corpus_paths=[], all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    assert "error" in handlers["lookup_function"]({"name": ""})
    assert "error" in handlers["lookup_function"]({})


def test_lookup_function_truncates_large_body():
    fi = _fi("big", body="{ " + "X" * 10000 + " }")
    ctx = SpecToolContext(parsed=_mini_parsed({"big": fi}),
                          corpus_paths=[], all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["lookup_function"]({"name": "big"})
    assert len(out["body"]) <= 4001  # _MAX_FUNCTION_BODY_CHARS + slack
    assert out["body_truncated"] is True


# ---------- find_more_callers ---------------------------------------------


def test_find_more_callers_finds_direct(tmp_path):
    f = tmp_path / "a.c"
    f.write_text(
        "int caller_one(int x) { return foo(x); }\n"
        "int caller_two(int y) { return foo(y + 1); }\n"
        "int caller_three(int z) { return foo(z * 2); }\n"
    )
    ctx = SpecToolContext(
        parsed=_mini_parsed(), corpus_paths=[f],
        all_specs_so_far={},
    )
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["find_more_callers"]({"name": "foo", "k": 5})
    assert "direct_callers" in out
    assert len(out["direct_callers"]) >= 2


def test_find_more_callers_falls_back_to_address_taken(tmp_path):
    f = tmp_path / "a.c"
    f.write_text(
        "static int cb(void) { return 0; }\n"
        "static const struct ops o = { cb };\n"
    )
    ctx = SpecToolContext(parsed=_mini_parsed(), corpus_paths=[f],
                          all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["find_more_callers"]({"name": "cb"})
    assert "address_taken_sites" in out
    assert len(out["address_taken_sites"]) >= 1


def test_find_more_callers_empty_name():
    ctx = SpecToolContext(parsed=_mini_parsed(), corpus_paths=[],
                          all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["find_more_callers"]({})
    assert "error" in out


# ---------- lookup_struct -------------------------------------------------


def test_lookup_struct_found():
    struct_defs = {"match_file": [("int", "id"), ("char *", "name")]}
    ctx = SpecToolContext(parsed=_mini_parsed(struct_defs=struct_defs),
                          corpus_paths=[], all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["lookup_struct"]({"tag": "match_file"})
    assert out["tag"] == "match_file"
    assert out["fields"] == [{"type": "int", "name": "id"},
                             {"type": "char *", "name": "name"}]
    assert out["field_count"] == 2


def test_lookup_struct_missing():
    ctx = SpecToolContext(parsed=_mini_parsed(struct_defs={}),
                          corpus_paths=[], all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["lookup_struct"]({"tag": "unknown"})
    assert "error" in out


def test_lookup_struct_empty_tag():
    ctx = SpecToolContext(parsed=_mini_parsed(),
                          corpus_paths=[], all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    assert "error" in handlers["lookup_struct"]({"tag": ""})


# ---------- lookup_caller_spec --------------------------------------------


def test_lookup_caller_spec_found():
    from bmc_agent.spec import Spec
    s = Spec(function_name="foo", precondition="!null(p)", postcondition="true")
    ctx = SpecToolContext(parsed=_mini_parsed(),
                          corpus_paths=[],
                          all_specs_so_far={"foo": s})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["lookup_caller_spec"]({"name": "foo"})
    assert out.get("function_name") == "foo"
    assert out.get("precondition") == "!null(p)"


def test_lookup_caller_spec_missing():
    ctx = SpecToolContext(parsed=_mini_parsed(),
                          corpus_paths=[],
                          all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["lookup_caller_spec"]({"name": "missing"})
    assert "error" in out
    assert "no spec yet" in out["error"]


# ---------- grep_corpus ---------------------------------------------------


def test_grep_corpus_finds_matches(tmp_path):
    f = tmp_path / "a.c"
    f.write_text(
        "int line_one_with_FOO_marker(void);\n"
        "int line_two_unrelated(void);\n"
        "int line_three_with_FOO_again(void);\n"
    )
    ctx = SpecToolContext(parsed=_mini_parsed(), corpus_paths=[f],
                          all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["grep_corpus"]({"pattern": r"FOO", "k": 10})
    assert len(out["matches"]) == 2
    assert out["matches"][0]["line"] == 1
    assert out["matches"][1]["line"] == 3


def test_grep_corpus_respects_k(tmp_path):
    f = tmp_path / "a.c"
    f.write_text("\n".join(["match_me " + str(i) for i in range(20)]))
    ctx = SpecToolContext(parsed=_mini_parsed(), corpus_paths=[f],
                          all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["grep_corpus"]({"pattern": "match_me", "k": 3})
    assert len(out["matches"]) == 3


def test_grep_corpus_invalid_regex_returns_error():
    ctx = SpecToolContext(parsed=_mini_parsed(), corpus_paths=[],
                          all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["grep_corpus"]({"pattern": "[unclosed"})
    assert "error" in out


def test_grep_corpus_empty_pattern_returns_error():
    ctx = SpecToolContext(parsed=_mini_parsed(), corpus_paths=[],
                          all_specs_so_far={})
    _, handlers = build_spec_gen_tools(ctx)
    out = handlers["grep_corpus"]({})
    assert "error" in out


# ---------- prompt addendum ------------------------------------------------


def test_addendum_mentions_all_tools():
    """The prompt should describe each tool so the LLM knows what's available."""
    text = TOOL_USE_PROMPT_ADDENDUM
    for tool_name in ("lookup_function", "find_more_callers",
                      "lookup_struct", "lookup_caller_spec", "grep_corpus"):
        assert tool_name in text


def test_addendum_includes_call_cap():
    """The prompt should tell the LLM about the max-5 cap."""
    assert "5 tool calls" in TOOL_USE_PROMPT_ADDENDUM
