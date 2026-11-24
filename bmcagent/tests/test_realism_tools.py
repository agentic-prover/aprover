"""Tests for bmc_agent.realism_tools — walk_call_chain + friends."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bmc_agent.realism_tools import (
    RealismToolContext,
    TOOL_USE_PROMPT_ADDENDUM,
    build_realism_tools,
)


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


def _mini_parsed(funcs=None, call_graph=None):
    """Minimal ParsedCFile-like object for handler tests."""
    funcs = funcs or {}
    parsed = MagicMock()
    parsed.functions = {n: fi.signature for n, fi in funcs.items()}
    parsed.call_graph = call_graph or {n: fi.callees for n, fi in funcs.items()}
    parsed.get_function_info = lambda n: funcs.get(n)
    return parsed


# ---------- build_realism_tools -------------------------------------------


def test_build_returns_three_tools():
    ctx = RealismToolContext(parsed=_mini_parsed(), all_specs={})
    tools, handlers = build_realism_tools(ctx)
    assert len(tools) == 3
    assert set(handlers.keys()) == {
        "walk_call_chain", "lookup_function", "lookup_callee_postcondition",
    }


def test_build_tool_names_match_handlers():
    ctx = RealismToolContext(parsed=_mini_parsed(), all_specs={})
    tools, handlers = build_realism_tools(ctx)
    assert {t.name for t in tools} == set(handlers.keys())


# ---------- walk_call_chain -----------------------------------------------


def test_walk_call_chain_basic():
    """Simple chain: target ← caller_a ← caller_b."""
    funcs = {
        "target": _fi("target"),
        "caller_a": _fi("caller_a", callees={"target"}),
        "caller_b": _fi("caller_b", callees={"caller_a"}),
    }
    parsed = _mini_parsed(funcs)
    ctx = RealismToolContext(parsed=parsed, all_specs={})
    _, handlers = build_realism_tools(ctx)
    out = handlers["walk_call_chain"]({"fn_name": "target"})
    assert "chain" in out
    # Depth 1 has caller_a → target
    assert any(e["caller"] == "caller_a" for e in out["chain"][0]["edges"])
    # Depth 2 has caller_b → caller_a
    if len(out["chain"]) > 1:
        assert any(e["caller"] == "caller_b" for e in out["chain"][1]["edges"])


def test_walk_call_chain_empty_when_no_callers():
    """Vtable-only / dead code → empty chain + note."""
    funcs = {"orphan": _fi("orphan")}
    ctx = RealismToolContext(parsed=_mini_parsed(funcs), all_specs={})
    _, handlers = build_realism_tools(ctx)
    out = handlers["walk_call_chain"]({"fn_name": "orphan"})
    assert out["chain"] == []
    assert "no callers" in out["note"]


def test_walk_call_chain_includes_caller_pre_when_spec_exists():
    from bmc_agent.spec import Spec
    funcs = {
        "target": _fi("target"),
        "caller_a": _fi("caller_a", callees={"target"}),
    }
    parsed = _mini_parsed(funcs)
    specs = {
        "caller_a": Spec(function_name="caller_a",
                         precondition="!null(p)",
                         postcondition="true"),
    }
    ctx = RealismToolContext(parsed=parsed, all_specs=specs)
    _, handlers = build_realism_tools(ctx)
    out = handlers["walk_call_chain"]({"fn_name": "target"})
    edge = out["chain"][0]["edges"][0]
    assert edge["caller"] == "caller_a"
    assert edge["caller_pre"] == "!null(p)"


def test_walk_call_chain_marks_no_spec_yet():
    funcs = {
        "target": _fi("target"),
        "caller_a": _fi("caller_a", callees={"target"}),
    }
    ctx = RealismToolContext(parsed=_mini_parsed(funcs), all_specs={})
    _, handlers = build_realism_tools(ctx)
    out = handlers["walk_call_chain"]({"fn_name": "target"})
    assert out["chain"][0]["edges"][0]["caller_pre"] == "(no spec yet)"


def test_walk_call_chain_respects_max_depth():
    """Long chain — clipped to max_depth."""
    funcs = {f"f{i}": _fi(f"f{i}", callees={f"f{i-1}"}) for i in range(1, 10)}
    funcs["f0"] = _fi("f0")
    parsed = _mini_parsed(funcs)
    ctx = RealismToolContext(parsed=parsed, all_specs={})
    _, handlers = build_realism_tools(ctx)
    out = handlers["walk_call_chain"]({"fn_name": "f0", "max_depth": 2})
    assert len(out["chain"]) <= 2


def test_walk_call_chain_missing_fn_name_errors():
    ctx = RealismToolContext(parsed=_mini_parsed(), all_specs={})
    _, handlers = build_realism_tools(ctx)
    assert "error" in handlers["walk_call_chain"]({})


def test_walk_call_chain_avoids_revisit_cycles():
    """Mutual recursion: a → b → a. BFS must terminate."""
    funcs = {
        "a": _fi("a", callees={"b"}),
        "b": _fi("b", callees={"a"}),
    }
    parsed = _mini_parsed(funcs)
    ctx = RealismToolContext(parsed=parsed, all_specs={})
    _, handlers = build_realism_tools(ctx)
    # If this didn't terminate / dedup, the call would hang or repeat.
    out = handlers["walk_call_chain"]({"fn_name": "a", "max_depth": 5})
    assert isinstance(out["chain"], list)


# ---------- lookup_function ------------------------------------------------


def test_lookup_function_returns_body_and_signature():
    fi = _fi("foo", body="{ return 1; }",
             params=[("int", "x"), ("char *", "s")])
    ctx = RealismToolContext(parsed=_mini_parsed({"foo": fi}), all_specs={})
    _, handlers = build_realism_tools(ctx)
    out = handlers["lookup_function"]({"name": "foo"})
    assert out["body"] == "{ return 1; }"
    assert out["parameters"] == [
        {"type": "int", "name": "x"},
        {"type": "char *", "name": "s"},
    ]


def test_lookup_function_missing_errors():
    ctx = RealismToolContext(parsed=_mini_parsed(), all_specs={})
    _, handlers = build_realism_tools(ctx)
    assert "error" in handlers["lookup_function"]({"name": "ghost"})


def test_lookup_function_truncates_long_body():
    fi = _fi("big", body="{ " + "X" * 8000 + " }")
    ctx = RealismToolContext(parsed=_mini_parsed({"big": fi}), all_specs={})
    _, handlers = build_realism_tools(ctx)
    out = handlers["lookup_function"]({"name": "big"})
    assert out["body_truncated"] is True
    assert len(out["body"]) <= 4001


# ---------- lookup_callee_postcondition -----------------------------------


def test_lookup_callee_postcondition_returns_spec_fields():
    from bmc_agent.spec import Spec, SpecStatus
    s = Spec(
        function_name="cb", precondition="!null(p)",
        postcondition="result == 0 || result == -1",
        pre_validity="!null(p)", pre_protocol="",
        evidence={"!null(p)": ["caller_site_1"]},
        status=SpecStatus.GENERATED,
    )
    ctx = RealismToolContext(parsed=_mini_parsed(), all_specs={"cb": s})
    _, handlers = build_realism_tools(ctx)
    out = handlers["lookup_callee_postcondition"]({"name": "cb"})
    assert out["postcondition"] == "result == 0 || result == -1"
    assert out["precondition"] == "!null(p)"
    assert "!null(p)" in out["evidence_tags"]
    assert out["status"] == "generated"


def test_lookup_callee_postcondition_missing_returns_error():
    ctx = RealismToolContext(parsed=_mini_parsed(), all_specs={})
    _, handlers = build_realism_tools(ctx)
    out = handlers["lookup_callee_postcondition"]({"name": "ghost"})
    assert "error" in out
    assert "no spec" in out["error"]


# ---------- prompt addendum -----------------------------------------------


def test_addendum_mentions_all_tools():
    text = TOOL_USE_PROMPT_ADDENDUM
    for name in ("walk_call_chain", "lookup_function",
                 "lookup_callee_postcondition"):
        assert name in text


def test_addendum_calls_out_call_cap():
    assert "3 tool calls" in TOOL_USE_PROMPT_ADDENDUM
