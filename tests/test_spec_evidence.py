"""Tests for bmc_agent.spec_evidence — caller harvest, doc parse, universal seeding.

Synthetic mini-corpora only. No real LLM, no real CBMC.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bmc_agent.spec_evidence import (
    CallerEvidence,
    DocClause,
    EvidenceBundle,
    FieldAccessHint,
    SeedClause,
    extract_field_accesses,
    gather_evidence_bundle,
    harvest_address_taken_sites,
    harvest_callers,
    parse_doc_annotations,
    seed_from_universal_patterns,
)


def _write(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(src)
    return p


# ---------- harvest_callers --------------------------------------------------


def test_harvest_basic_call_sites(tmp_path):
    """A direct call site is picked up; declaration on its own line is not."""
    f = _write(tmp_path, "a.c", """
static int foo(int);
int caller(int x) {
    int r = foo(x);
    return r;
}
""")
    hits = harvest_callers("foo", [f], k=5)
    assert len(hits) == 1
    assert hits[0].line == 4   # the actual call line
    assert "foo(x)" in hits[0].call_line_text


def test_harvest_skips_forward_declaration_with_type_prefix(tmp_path):
    """`static int foo(int);` is a decl; do NOT harvest as a call."""
    f = _write(tmp_path, "a.c", """
static int foo(int);
extern void foo(int);
int caller(void) { return foo(1); }
""")
    hits = harvest_callers("foo", [f], k=5)
    assert len(hits) == 1
    assert "return foo(1)" in hits[0].call_line_text


def test_harvest_skips_function_definition_kr_style(tmp_path):
    """K&R-style def with type on previous line is NOT a call site."""
    f = _write(tmp_path, "a.c", """
static int
foo(int x)
{
    return x + 1;
}

int caller(int y) { return foo(y); }
""")
    hits = harvest_callers("foo", [f], k=5)
    # Only the caller's call, not the K&R-style definition.
    assert len(hits) == 1
    assert "caller" in hits[0].call_line_text or "return foo(y)" in hits[0].call_line_text


def test_harvest_distinct_files_preferred_over_repeat(tmp_path):
    """Round-robin selection: 1 per file before 2 from any single file."""
    a = _write(tmp_path, "a.c", "int x(void){return foo(1);}\nint y(void){return foo(2);}\nint z(void){return foo(3);}")
    b = _write(tmp_path, "b.c", "int q(void){return foo(9);}")
    hits = harvest_callers("foo", [a, b], k=2)
    assert len(hits) == 2
    files = {h.file for h in hits}
    assert len(files) == 2  # one from a.c, one from b.c


def test_harvest_non_test_path_preferred(tmp_path):
    """When file priorities tie, non-test files win over test files."""
    real = _write(tmp_path, "real.c", "int caller(void){return foo(1);}")
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    test_file = _write(test_dir, "test_foo.c", "int t(void){return foo(2);}")
    hits = harvest_callers("foo", [real, test_file], k=1)
    assert len(hits) == 1
    assert "real.c" in hits[0].file


def test_harvest_respects_k_limit(tmp_path):
    f = _write(tmp_path, "a.c", "\n".join(
        f"int c{i}(void){{return foo({i});}}" for i in range(10)
    ))
    hits = harvest_callers("foo", [f], k=3)
    assert len(hits) == 3


def test_harvest_no_match_returns_empty(tmp_path):
    f = _write(tmp_path, "a.c", "int caller(void){return bar(1);}")
    assert harvest_callers("nonexistent_fn", [f]) == []
    assert harvest_callers("", [f]) == []


def test_harvest_skips_string_literals(tmp_path):
    """A function name inside `"..."` is not a real call."""
    f = _write(tmp_path, "a.c", '''
int caller(void) {
    const char *s = "foo(123)";
    return foo(1);
}
''')
    hits = harvest_callers("foo", [f], k=5)
    assert len(hits) == 1
    assert "return foo(1)" in hits[0].call_line_text


def test_harvest_skips_block_comments(tmp_path):
    """A function name inside /* ... */ is not a real call."""
    f = _write(tmp_path, "a.c", """
/* This calls foo(99) but only in documentation */
int caller(void){return foo(1);}
""")
    hits = harvest_callers("foo", [f], k=5)
    assert len(hits) == 1
    assert "return foo(1)" in hits[0].call_line_text


def test_harvest_context_includes_surrounding_lines(tmp_path):
    f = _write(tmp_path, "a.c", "\n".join([f"int line{i}(void);" for i in range(20)] + [
        "int caller(void){return foo(1);}",
        "int after1(void);",
        "int after2(void);",
    ]))
    hits = harvest_callers("foo", [f], k=1, context_radius=3)
    assert len(hits) == 1
    # ±3 lines means up to 7 lines of context.
    assert len(hits[0].context_lines) <= 7
    assert any("caller" in ln for ln in hits[0].context_lines)


# ---------- harvest_address_taken_sites --------------------------------------


def test_address_taken_finds_vtable_registration(tmp_path):
    """The canonical libarchive vtable-dispatch case."""
    f = _write(tmp_path, "a.c", """
static int cmp_key_mbs(const void *, const void *);
static int cmp_node_mbs(const void *, const void *);
static const struct ops vtable = {
    cmp_node_mbs, cmp_key_mbs
};
""")
    hits = harvest_address_taken_sites("cmp_key_mbs", [f])
    assert len(hits) >= 1
    # The hit should be on the vtable line, not the decl.
    assert any("cmp_node_mbs" in h.call_line_text for h in hits)


def test_address_taken_skips_calls(tmp_path):
    """`foo(...)` should NOT register as address-taken (paren follows)."""
    f = _write(tmp_path, "a.c", "int caller(void){return foo(1);}")
    hits = harvest_address_taken_sites("foo", [f])
    assert hits == []


# ---------- parse_doc_annotations --------------------------------------------


def _make_parsed_with_def(text: str, fn_name: str):
    """Build a minimal ParsedCFile-like object good enough for parse_doc_annotations."""
    class _P:
        path = "/tmp/_x.c"
        function_definitions = {}
        preprocessed_source = text
    p = _P()
    # function_definitions value: the literal def text — parser will search for it.
    start = text.find(fn_name + "(")
    if start < 0:
        return p
    # Walk back to start of return type / decl line.
    line_start = text.rfind("\n", 0, start) + 1
    end = text.find("}", start)
    if end >= 0:
        p.function_definitions[fn_name] = text[line_start:end + 1]
    return p


def test_doc_parse_extracts_doxygen_block():
    src = """
/**
 * \\brief Resolve a thing.
 * \\param p must be non-NULL.
 * \\param n must be > 0.
 * \\returns 0 on success, -1 on error.
 */
int resolve(int *p, int n)
{
    return 0;
}
"""
    p = _make_parsed_with_def(src, "resolve")
    clauses = parse_doc_annotations(p, "resolve")
    kinds = [c.annotation_type for c in clauses]
    assert "brief" in kinds
    assert kinds.count("param") == 2
    assert "returns" in kinds
    # The param annotation should capture the param name.
    pms = [c for c in clauses if c.annotation_type == "param"]
    assert {c.param_name for c in pms} == {"p", "n"}


def test_doc_parse_supports_at_style_annotations():
    src = """
/**
 * @param x must be positive.
 * @return non-negative.
 */
int fn(int x){return x;}
"""
    p = _make_parsed_with_def(src, "fn")
    clauses = parse_doc_annotations(p, "fn")
    kinds = {c.annotation_type for c in clauses}
    assert "param" in kinds
    assert "returns" in kinds  # @return normalised to "returns"


def test_doc_parse_no_doc_block_returns_empty():
    src = """
int fn(int x){return x;}
"""
    p = _make_parsed_with_def(src, "fn")
    assert parse_doc_annotations(p, "fn") == []


def test_doc_parse_intervening_code_blocks_doc_attribution():
    """A doc comment followed by code-then-fn should NOT attribute to fn."""
    src = """
/** \\brief Misattributed. */
int unrelated_decl;
int fn(int x){return x;}
"""
    p = _make_parsed_with_def(src, "fn")
    assert parse_doc_annotations(p, "fn") == []


# ---------- seed_from_universal_patterns ------------------------------------


def _func_info(name: str, params: list[tuple[str, str]], rtype: str = "int"):
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(name=name, return_type=rtype, parameters=params)
    return FunctionInfo(name=name, signature=sig, body="", callees=set(), source_file="")


def test_seed_paired_pointers():
    fi = _func_info("scan", [("const char *", "start"), ("const char *", "end")])
    seeds = seed_from_universal_patterns(fi)
    clauses = {s.clause for s in seeds}
    # universal_contracts emits a paired_pointers clause for start/end.
    assert any("start" in c and "end" in c for c in clauses)
    # Tag should reflect the pattern category.
    assert all(s.pattern_name for s in seeds)


def test_seed_no_pattern_match_returns_empty():
    fi = _func_info("trivial", [("int", "x")])
    assert seed_from_universal_patterns(fi) == []


# ---------- gather_evidence_bundle -----------------------------------------


def test_gather_falls_back_to_address_taken_when_no_callers(tmp_path):
    """When direct-caller harvest is empty, address-taken sites fill in."""
    f = _write(tmp_path, "a.c", """
static int callback(const void *, const void *);
static const struct ops v = { callback };

static int callback(const void *a, const void *b) {
    return 0;
}
""")
    fi = _func_info("callback", [("const void *", "a"), ("const void *", "b")])

    class _P:
        path = str(f)
        function_definitions = {"callback": ""}
        preprocessed_source = None
        struct_definitions = {}
        functions = {"callback": fi.signature}
        call_graph = {"callback": set()}
    bundle = gather_evidence_bundle(fi, _P(), [f], k_callers=3)
    assert bundle.callers == []           # no direct calls
    assert len(bundle.address_taken_sites) >= 1  # vtable registration found


def test_bundle_is_empty_reports_correctly(tmp_path):
    fi = _func_info("orphan", [("int", "x")])

    class _P:
        path = "/tmp/empty.c"
        function_definitions = {"orphan": ""}
        preprocessed_source = "int orphan(int x){return x;}"
        struct_definitions = {}
        functions = {"orphan": fi.signature}
        call_graph = {"orphan": set()}
    bundle = gather_evidence_bundle(fi, _P(), [], k_callers=5)
    assert bundle.is_empty()


# ---------- extract_field_accesses (v2.1) -----------------------------------


def test_field_extract_basic_param_field():
    body = """{
    struct match_list *m = p->inclusion_list;
    return m->count;
}"""
    hits = extract_field_accesses(body, ["p"])
    paths = {h.path for h in hits}
    assert "p->inclusion_list" in paths
    # m is a local, not a param → m->count should NOT be a hint
    assert "m->count" not in paths


def test_field_extract_multi_hop_emits_all_prefixes():
    body = """{
    return p->head->next->value;
}"""
    hits = extract_field_accesses(body, ["p"])
    paths = {h.path for h in hits}
    assert "p->head" in paths
    assert "p->head->next" in paths
    assert "p->head->next->value" in paths


def test_field_extract_cast_alias_recognized():
    """`a = (struct X *)_a;` should treat a->field as _a->field."""
    body = """{
    struct archive_match *a = (struct archive_match *)_a;
    free(a->inclusion_unames);
    free(a->exclusions);
}"""
    hits = extract_field_accesses(body, ["_a"])
    paths = {h.path for h in hits}
    assert "a->inclusion_unames" in paths
    assert "a->exclusions" in paths


def test_field_extract_skips_non_param_locals():
    body = """{
    struct foo *local = malloc(sizeof(*local));
    local->field = 1;
    return p->other;
}"""
    hits = extract_field_accesses(body, ["p"])
    paths = {h.path for h in hits}
    # local is malloc'd here, not a param alias — should not emit
    assert "local->field" not in paths
    assert "p->other" in paths


def test_field_extract_guard_detection_simple():
    body = """{
    if (p->field == NULL)
        return -1;
    return p->field->value;
}"""
    hits = extract_field_accesses(body, ["p"])
    field_hit = next(h for h in hits if h.path == "p->field" and h.line_offset >= 3)
    # The deref on line 4 should be marked guarded by the if-check on line 2.
    assert field_hit.guarded is True


def test_field_extract_guard_via_truthy_check():
    body = """{
    if (p->field)
        do_something(p->field->next);
}"""
    hits = extract_field_accesses(body, ["p"])
    # The p->field->next access happens AFTER the truthy check; should be guarded.
    field_next = [h for h in hits if h.path == "p->field->next"]
    assert field_next
    assert field_next[0].guarded is True


def test_field_extract_no_guard_when_no_check():
    body = """{
    return p->field->value;
}"""
    hits = extract_field_accesses(body, ["p"])
    assert all(h.guarded is False for h in hits)


def test_field_extract_strips_comments():
    body = """{
    /* p->commented_out should be ignored */
    // p->line_commented either
    return p->real_field;
}"""
    hits = extract_field_accesses(body, ["p"])
    paths = {h.path for h in hits}
    assert "p->real_field" in paths
    assert "p->commented_out" not in paths
    assert "p->line_commented" not in paths


def test_field_extract_empty_body_empty_params():
    assert extract_field_accesses("", ["p"]) == []
    assert extract_field_accesses("{ return p->x; }", []) == []
    assert extract_field_accesses("{ return p->x; }", ["q"]) == []  # not a param


def test_field_extract_dedups_within_function():
    """Same (path, line) shouldn't appear twice even if text matches twice."""
    body = """{
    if (p->x == p->x)  /* tautology — should still extract only once for this line */
        return 0;
}"""
    hits = extract_field_accesses(body, ["p"])
    # Two textual matches of p->x on the same line; should be deduped to 1.
    on_line = [h for h in hits if h.path == "p->x" and h.line_offset == 1]
    assert len(on_line) == 1


def test_field_extract_bundle_includes_field_accesses():
    """gather_evidence_bundle should populate bundle.field_accesses."""
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(name="fn", return_type="int",
                            parameters=[("struct foo *", "p")])
    fi = FunctionInfo(
        name="fn", signature=sig,
        body="{ return p->field; }",
        callees=set(), source_file="",
    )
    class _P:
        path = "/tmp/_x.c"
        function_definitions = {}
        preprocessed_source = None
        struct_definitions = {}
        functions = {"fn": sig}
        call_graph = {"fn": set()}
    bundle = gather_evidence_bundle(fi, _P(), [], k_callers=3)
    assert len(bundle.field_accesses) == 1
    assert bundle.field_accesses[0].path == "p->field"
    assert bundle.is_empty() is False  # field_accesses count as evidence
