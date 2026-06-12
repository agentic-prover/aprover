"""Tests for string-copy SOURCE detection (the FN dual of the (buf,len) fix)."""
from __future__ import annotations

from bmc_agent.parser import FunctionInfo, FunctionSignature
from bmc_agent.string_copy_sink import (
    detect_copy_sources,
    detect_copy_sinks,
    plan_copy_source_widening,
    copy_sink_unwind_floor,
    _resolve_dest_size,
    _source_root,
    _split_top_level_args,
    _balanced_args,
)


def _fn(body: str, params: list[tuple[str, str]]) -> FunctionInfo:
    return FunctionInfo(
        name="f",
        signature=FunctionSignature(name="f", return_type="void", parameters=params),
        body=body,
        callees=set(),
        source_file="t.c",
    )


# ---- low-level helpers -------------------------------------------------

def test_balanced_args_simple():
    body = "strcpy(dst, src);"
    i = body.index("(")
    assert _balanced_args(body, i) == "dst, src"


def test_balanced_args_nested():
    body = "strcpy(dst, (char*)foo(a, b));"
    i = body.index("(")
    assert _balanced_args(body, i) == "dst, (char*)foo(a, b)"


def test_split_top_level_args_respects_parens():
    assert _split_top_level_args("dst, foo(a, b)") == ["dst", " foo(a, b)"]


def test_source_root_bare():
    assert _source_root("src") == ("src", None)


def test_source_root_cast_and_field():
    assert _source_root("(char*)temp->data") == ("temp", "data")


def test_source_root_const_cast_field_dot():
    assert _source_root("(const char *)node.name") == ("node", "name")


def test_source_root_non_lvalue():
    assert _source_root('"literal"') == (None, None)


# ---- parameter sources -------------------------------------------------

def test_strcpy_param_source_detected():
    fn = _fn("void f(char *in) { char buf[16]; strcpy(buf, in); }",
             [("char *", "in")])
    params, fields = detect_copy_sources(fn)
    assert params == {"in"}
    assert fields == set()


def test_strcat_param_source_detected():
    fn = _fn("void f(char *in) { char b[8]; strcat(b, in); }",
             [("char *", "in")])
    params, _ = detect_copy_sources(fn)
    assert params == {"in"}


def test_dst_is_not_flagged_as_source():
    # The DESTINATION (arg0) must never be widened — only the source (arg1).
    fn = _fn("void f(char *dst, char *src) { strcpy(dst, src); }",
             [("char *", "dst"), ("char *", "src")])
    params, _ = detect_copy_sources(fn)
    assert params == {"src"}
    assert "dst" not in params


# ---- struct-field sources ---------------------------------------------

def test_field_source_detected():
    fn = _fn("void f(node_t *n) { char buf[16]; strcpy(buf, (char*)n->data); }",
             [("node_t *", "n")])
    params, fields = detect_copy_sources(fn)
    assert fields == {"data"}


def test_local_struct_field_source_still_flagged_by_name():
    # vfs_open_handle shape: source is a callee-return local's field. We can't
    # widen the local, but the field NAME is still surfaced (harmless, helps
    # when the same-named field IS a modeled param).
    fn = _fn(
        "void f(void) { node_t *t = lookup(p); char buf[256]; "
        "strcpy(buf, (char*)t->data); }",
        [],
    )
    _, fields = detect_copy_sources(fn)
    assert fields == {"data"}


# ---- negatives ---------------------------------------------------------

def test_strncpy_is_not_a_sink():
    fn = _fn("void f(char *in) { char b[8]; strncpy(b, in, 7); }",
             [("char *", "in")])
    params, fields = detect_copy_sources(fn)
    assert params == set()
    assert fields == set()


def test_no_copy_no_sources():
    fn = _fn("int f(char *in) { return in[0]; }", [("char *", "in")])
    assert detect_copy_sources(fn) == (set(), set())


# ---- destination-size resolution --------------------------------------

def test_resolve_dest_fixed_char_array():
    body = "void f(char *in){ char buf[16]; strcpy(buf, in); }"
    assert _resolve_dest_size(body, "buf") == 16


def test_resolve_dest_malloc_literal():
    body = "void f(char *in){ char *p = malloc(256); strcpy(p, in); }"
    assert _resolve_dest_size(body, "p") == 256


def test_resolve_dest_cast_malloc():
    body = "void f(char *in){ char *p = (char*)malloc(64); strcpy(p, in); }"
    assert _resolve_dest_size(body, "p") == 64


def test_resolve_dest_calloc_product():
    body = "void f(char *in){ char *p = calloc(8, 16); strcpy(p, in); }"
    assert _resolve_dest_size(body, "p") == 128


def test_resolve_dest_strlen_sized_is_unresolvable():
    # malloc(strlen(x)+1) is correctly sized -> must NOT resolve to a number
    # (so it falls back to the modest default cap and can't false-positive).
    body = "void f(char *in){ char *p = malloc(strlen(in)+1); strcpy(p, in); }"
    assert _resolve_dest_size(body, "p") is None


# ---- per-sink widening plan -------------------------------------------

def test_sink_carries_resolved_dest_size():
    fn = _fn("void f(char *in){ char buf[16]; strcpy(buf, in); }",
             [("char *", "in")])
    sinks = detect_copy_sinks(fn)
    assert len(sinks) == 1 and sinks[0].dest_size == 16


def test_plan_widens_param_to_dest_size():
    fn = _fn("void f(char *in){ char buf[16]; strcpy(buf, in); }",
             [("char *", "in")])
    pmax, fmax, floor = plan_copy_source_widening(fn, default_cap=32, ceiling=256)
    assert pmax == {"in": 16}
    assert floor == 18                       # 16 + 2


def test_plan_large_dest_capped_by_ceiling():
    fn = _fn("void f(char *in){ char *p = malloc(1024); strcpy(p, in); }",
             [("char *", "in")])
    pmax, _f, floor = plan_copy_source_widening(fn, default_cap=32, ceiling=256)
    assert pmax == {"in": 256}               # capped
    assert floor == 258


def test_plan_unresolvable_dest_uses_default_cap():
    fn = _fn("void f(char *in){ char *p = malloc(strlen(in)+1); strcpy(p, in); }",
             [("char *", "in")])
    pmax, _f, floor = plan_copy_source_widening(fn, default_cap=32, ceiling=256)
    assert pmax == {"in": 32}
    assert floor == 34


def test_plan_field_dest_size():
    fn = _fn("void f(node_t *n){ char buf[256]; strcpy(buf, (char*)n->data); }",
             [("node_t *", "n")])
    _p, fmax, floor = plan_copy_source_widening(fn, default_cap=32, ceiling=512)
    assert fmax == {"data": 256}
    assert floor == 258


# ---- unwind floor ------------------------------------------------------

def test_unwind_floor_when_sink_present():
    fn = _fn("void f(char *in) { char b[8]; strcpy(b, in); }",
             [("char *", "in")])
    assert copy_sink_unwind_floor(fn, 32) == 10      # dest 8 -> 8+2


def test_unwind_floor_zero_without_sink():
    fn = _fn("int f(char *in) { return in[0]; }", [("char *", "in")])
    assert copy_sink_unwind_floor(fn, 32) == 0


def test_unwind_floor_zero_when_disabled():
    fn = _fn("void f(char *in) { char b[8]; strcpy(b, in); }",
             [("char *", "in")])
    assert copy_sink_unwind_floor(fn, 0) == 0
