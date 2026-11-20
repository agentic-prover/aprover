"""Tests for the copy-source RETURN stub (callee-return-field variant of the
string-copy FN fix): a stubbed callee that returns a struct pointer whose char*
field is strcpy'd into a fixed buffer in the caller gets a malloc-backed return
with that field widened, so the overflow is reachable."""
from __future__ import annotations

from bmc_agent.harness_generator import _emit_copy_source_return_stub, _copy_field_plan
from bmc_agent.parser import FunctionInfo, FunctionSignature

SD = {"vfs_node_t": [("char *", "name"), ("char *", "data"), ("int", "type")]}


def test_emits_malloc_backed_widened_return():
    out = _emit_copy_source_return_stub("vfs_lookup", "vfs_node_t *", SD, {"data": 256})
    assert out is not None
    body = "\n".join(out)
    # malloc-backed (NOT static — static zero-inits to an empty string)
    assert "malloc(sizeof(vfs_node_t))" in body
    assert "static" not in body
    # the copy-source field widened to 256 + NUL terminator at a nondet position
    assert "malloc((unsigned int)256 + 1)" in body
    assert "<= (unsigned int)256" in body
    assert "result->data =" in body
    # NULL path preserved (malloc may return NULL); no explicit non-null assume
    assert "result = malloc" in body


def test_none_when_no_field_matches():
    assert _emit_copy_source_return_stub("vfs_lookup", "vfs_node_t *", SD, {"path": 256}) is None


def test_none_for_non_struct_return():
    assert _emit_copy_source_return_stub("f", "int", SD, {"data": 256}) is None


def test_none_for_double_pointer_return():
    assert _emit_copy_source_return_stub("f", "vfs_node_t **", SD, {"data": 256}) is None


def test_none_when_field_is_not_char_ptr():
    sd = {"S": [("int", "data")]}      # 'data' exists but isn't char*
    assert _emit_copy_source_return_stub("f", "S *", sd, {"data": 256}) is None


def test_none_without_struct_defs():
    assert _emit_copy_source_return_stub("f", "vfs_node_t *", None, {"data": 256}) is None


def test_widen_width_follows_plan():
    # field widened to whatever the caller plan says (dest-coupled upstream)
    out = _emit_copy_source_return_stub("look", "vfs_node_t *", SD, {"data": 64})
    assert "malloc((unsigned int)64 + 1)" in "\n".join(out)


# ---- _copy_field_plan wiring -------------------------------------------

class _Cfg:
    enable_string_copy_source_modeling = True
    string_copy_source_max_len = 32
    string_copy_source_max_dest = 256


def _fn(body):
    return FunctionInfo(
        name="vfs_open_handle",
        signature=FunctionSignature(name="vfs_open_handle", return_type="void",
                                    parameters=[("const char *", "path")]),
        body=body, callees={"vfs_lookup"}, source_file="vfs.c",
    )


def test_copy_field_plan_surfaces_callee_return_field():
    # vfs_open_handle shape: strcpy(path_copy[256], temp->data)
    fn = _fn("void vfs_open_handle(const char *path){ "
             "vfs_node_t *temp = vfs_lookup(path); "
             "char *path_copy = malloc(256); strcpy(path_copy, (char*)temp->data); }")
    plan = _copy_field_plan(_Cfg(), fn)
    assert plan.get("data") == 256          # dest-coupled to malloc(256)


def test_copy_field_plan_empty_when_disabled():
    class Off(_Cfg):
        enable_string_copy_source_modeling = False
    fn = _fn("void f(const char*p){ vfs_node_t*t=vfs_lookup(p); char b[8]; strcpy(b,(char*)t->data); }")
    assert _copy_field_plan(Off(), fn) == {}
