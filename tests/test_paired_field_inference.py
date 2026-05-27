"""
Tests for the paired (count, array) field-invariant inference in
spec_generator_v2 — closes the archive_match_owner_excluded /
match_owner_id FP class triaged from postfix5.

When a function body indexes ``param->arr[…]`` AND reads
``param->count`` (with count-shaped name + array_ptr-typed field
in the same struct), the inferred PRE is
``param->count == 0 || param->arr != NULL`` — the implicit
invariant maintained by sibling constructors (add_owner_id pattern).
Constructors (functions that WRITE either field of the pair) are
intentionally excluded.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_func(name: str, params, body: str):
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name=name, return_type="int",
        parameters=params,
    )
    return FunctionInfo(
        name=name, signature=sig, body=body, callees=set(),
        source_file=f"/tmp/{name}.c",
    )


def _make_parsed(struct_definitions):
    """Build a minimal ParsedCFile stand-in for the inference. Only
    ``struct_definitions`` is read by the helper."""
    return SimpleNamespace(struct_definitions=struct_definitions)


# ---------------------------------------------------------------------------
# Canonical positive case (libarchive match_owner_id shape)
# ---------------------------------------------------------------------------

def test_infer_canonical_count_array_pattern():
    """``b = ids->count; while (...) ids->ids[m] ...`` — the canonical
    pattern. Returns (param, count_field, array_field)."""
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant
    body = """\
{
    size_t b, m, t;
    t = 0;
    b = ids->count;
    while (t < b) {
        m = (t + b) >> 1;
        if (ids->ids[m] == id)
            return 1;
        if (ids->ids[m] < id)
            t = m + 1;
        else
            b = m;
    }
    return 0;
}
"""
    func = _make_func(
        "match_owner_id",
        [("struct id_array *", "ids"), ("int64_t", "id")],
        body,
    )
    parsed = _make_parsed({
        "id_array": [
            ("size_t", "size"),
            ("size_t", "count"),
            ("int64_t *", "ids"),
        ],
    })
    result = _infer_paired_field_invariant(func, parsed)
    assert result == ("ids", "count", "ids")


# ---------------------------------------------------------------------------
# _spec_from_paired_field_invariant
# ---------------------------------------------------------------------------

def test_spec_from_paired_field_invariant_shape():
    from bmc_agent.spec_generator_v2 import _spec_from_paired_field_invariant
    from bmc_agent.spec import SpecStatus
    spec = _spec_from_paired_field_invariant(
        "match_owner_id", "ids", "count", "ids",
    )
    assert spec.function_name == "match_owner_id"
    assert spec.precondition == "ids->count == 0 || ids->ids != NULL"
    assert spec.status == SpecStatus.GENERATED
    assert any(
        "caller_contract:paired_field_invariant" in tags
        for tags in spec.evidence.values()
    )


# ---------------------------------------------------------------------------
# Negative cases — must NOT fire
# ---------------------------------------------------------------------------

def test_excludes_constructor_that_writes_count():
    """A function that writes to the count field is the constructor —
    excluded so we don't hide bugs in the invariant-maintenance code."""
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant
    body = """\
{
    if (ids->count + 1 >= ids->size) {
        ids->size *= 2;
        ids->ids = realloc(ids->ids, ids->size);
    }
    ids->ids[ids->count++] = id;
    return 0;
}
"""
    func = _make_func(
        "add_owner_id",
        [("struct id_array *", "ids"), ("int64_t", "id")],
        body,
    )
    parsed = _make_parsed({
        "id_array": [
            ("size_t", "size"),
            ("size_t", "count"),
            ("int64_t *", "ids"),
        ],
    })
    assert _infer_paired_field_invariant(func, parsed) is None


def test_excludes_constructor_that_writes_array():
    """Same exclusion when the function writes the array field
    (``ids->ids = …``) — constructor / mutator."""
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant
    body = """\
{
    void *p = realloc(ids->ids, ids->count * sizeof(int64_t));
    ids->ids = p;
    return ids->ids[0];
}
"""
    func = _make_func(
        "grow_id_array",
        [("struct id_array *", "ids")],
        body,
    )
    parsed = _make_parsed({
        "id_array": [
            ("size_t", "count"),
            ("int64_t *", "ids"),
        ],
    })
    assert _infer_paired_field_invariant(func, parsed) is None


def test_no_inference_when_no_indexing():
    """Body reads count but doesn't index any pointer-array field —
    no inference (could be a different invariant)."""
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant
    body = "{\n    return ids->count;\n}\n"
    func = _make_func(
        "get_count", [("struct id_array *", "ids")], body,
    )
    parsed = _make_parsed({
        "id_array": [("size_t", "count"), ("int64_t *", "ids")],
    })
    assert _infer_paired_field_invariant(func, parsed) is None


def test_no_inference_when_struct_body_not_visible():
    """Without struct_definitions for the tag we can't validate the
    pair — skip rather than over-infer."""
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant
    body = """\
{
    b = ids->count;
    return ids->ids[0];
}
"""
    func = _make_func(
        "fn", [("struct opaque *", "ids")], body,
    )
    parsed = _make_parsed({})   # no struct bodies
    assert _infer_paired_field_invariant(func, parsed) is None


def test_no_inference_when_count_field_name_doesnt_match():
    """Without a count-shaped field name (count/len/size/num),
    the field-read is ambiguous — skip."""
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant
    body = """\
{
    b = ids->limit;
    return ids->data[0];
}
"""
    func = _make_func(
        "fn", [("struct foo *", "ids")], body,
    )
    parsed = _make_parsed({
        "foo": [("size_t", "limit"), ("int *", "data")],
    })
    # "limit" doesn't match count/len/size/num — should skip.
    assert _infer_paired_field_invariant(func, parsed) is None


def test_no_inference_when_array_field_isnt_pointer():
    """The array field has to be pointer-typed. A scalar field that
    happens to be indexed (rare, but possible via cast) doesn't
    establish a pointer-non-null invariant."""
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant
    body = """\
{
    b = ids->count;
    return ids->data[0];
}
"""
    func = _make_func("fn", [("struct foo *", "ids")], body)
    parsed = _make_parsed({
        "foo": [("size_t", "count"), ("int", "data")],  # data NOT pointer
    })
    assert _infer_paired_field_invariant(func, parsed) is None


def test_no_inference_when_param_not_struct_pointer():
    """Only struct-pointer parameters get the inference (the invariant
    is about a pointer field of a struct)."""
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant
    body = "{\n    b = ids->count;\n    return ids->ids[0];\n}\n"
    func = _make_func(
        "fn", [("int *", "ids")], body,   # plain int*, not struct*
    )
    parsed = _make_parsed({})
    assert _infer_paired_field_invariant(func, parsed) is None


# ---------------------------------------------------------------------------
# Helper: _looks_like_count_name
# ---------------------------------------------------------------------------

def test_looks_like_count_name_recognises_common_tokens():
    from bmc_agent.spec_generator_v2 import _looks_like_count_name
    assert _looks_like_count_name("count") is True
    assert _looks_like_count_name("len") is True
    assert _looks_like_count_name("size") is True
    assert _looks_like_count_name("num") is True
    assert _looks_like_count_name("nitems") is True
    assert _looks_like_count_name("entry_count") is True
    assert _looks_like_count_name("Count") is True   # case-insensitive
    assert _looks_like_count_name("CAPACITY") is False   # different concept
    assert _looks_like_count_name("limit") is False
    assert _looks_like_count_name("data") is False
    assert _looks_like_count_name("ids") is False


# ---------------------------------------------------------------------------
# Integration through the orchestrator boundary
# ---------------------------------------------------------------------------

def test_libarchive_match_owner_id_via_real_source():
    """End-to-end on real libarchive source — requires tree-sitter
    to populate struct_definitions correctly. Skipped under pytest's
    sandbox env where tree-sitter isn't always present (regex
    fallback doesn't populate struct bodies)."""
    from pathlib import Path
    if not Path("/tmp/libarchive_bench/libarchive/libarchive/archive_match.c").exists():
        pytest.skip("libarchive corpus not present")
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_c  # noqa: F401
    except ImportError:
        pytest.skip("tree-sitter not installed in pytest env")
    from bmc_agent.config import Config
    from bmc_agent.parser import parse_c_file
    from bmc_agent.preprocessor import preprocess
    from bmc_agent.spec_generator_v2 import _infer_paired_field_invariant

    cfg = Config()
    cfg.include_dirs = [
        "/tmp/libarchive_bench/libarchive/build",
        "/tmp/libarchive_bench/libarchive/libarchive",
    ]
    cfg.cbmc_defines = ["HAVE_CONFIG_H"]
    src = Path("/tmp/libarchive_bench/libarchive/libarchive/archive_match.c")
    expanded = preprocess(src, include_dirs=cfg.include_dirs, defines=cfg.cbmc_defines)
    parsed = parse_c_file(src, source_text=expanded)
    # Accessor: invariant inferred
    accessor = parsed.get_function_info("match_owner_id")
    assert accessor is not None
    assert _infer_paired_field_invariant(accessor, parsed) == ("ids", "count", "ids")
    # Constructor: excluded
    ctor = parsed.get_function_info("add_owner_id")
    assert ctor is not None
    assert _infer_paired_field_invariant(ctor, parsed) is None
