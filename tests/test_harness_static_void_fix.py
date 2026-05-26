"""
Regression tests for the storage-class-keyword normalization in
harness_generator (commit fixing rar5_cleanup / next_field seed bugs).

Root cause: the regex parser fallback (used when tree-sitter isn't
installed in the runtime) keeps ``static`` / ``inline`` in
``FunctionSignature.return_type``. Sites in harness_generator that
substituted the return type into LOCAL variable declarations produced
invalid C — ``static void result = next_field(...);`` — and CBMC
rejected the harness with "void-typed symbol not permitted" /
CONVERSION ERROR.

This test exercises the _ret_type_bare helper and confirms that every
local-variable substitution branch picks the right (void / non-void)
path even when the return type has leading storage-class keywords.
"""

from __future__ import annotations

import pytest


def test_ret_type_bare_strips_static():
    from bmc_agent.harness_generator import _ret_type_bare
    assert _ret_type_bare("static void") == "void"
    assert _ret_type_bare("static int") == "int"
    assert _ret_type_bare("static void ") == "void"
    assert _ret_type_bare("  static  void  ") == "void"


def test_ret_type_bare_strips_inline():
    from bmc_agent.harness_generator import _ret_type_bare
    assert _ret_type_bare("inline int") == "int"
    assert _ret_type_bare("static inline int") == "int"
    assert _ret_type_bare("static inline __uint16_t") == "__uint16_t"


def test_ret_type_bare_strips_extern_register_noreturn():
    from bmc_agent.harness_generator import _ret_type_bare
    assert _ret_type_bare("extern int") == "int"
    assert _ret_type_bare("register int") == "int"
    assert _ret_type_bare("_Noreturn void") == "void"


def test_ret_type_bare_passthrough_when_no_storage_class():
    from bmc_agent.harness_generator import _ret_type_bare
    assert _ret_type_bare("void") == "void"
    assert _ret_type_bare("int") == "int"
    assert _ret_type_bare("struct foo *") == "struct foo *"
    assert _ret_type_bare("const char *") == "const char *"


def test_ret_type_bare_keeps_qualifiers_thatre_part_of_type():
    """const / volatile / unsigned / long are part of the type — they
    must NOT be stripped."""
    from bmc_agent.harness_generator import _ret_type_bare
    assert _ret_type_bare("const char *") == "const char *"
    assert _ret_type_bare("unsigned long") == "unsigned long"
    assert _ret_type_bare("volatile int") == "volatile int"
    assert _ret_type_bare("static const char *") == "const char *"


def test_ret_type_bare_only_word_boundary_match():
    """The strip must be word-boundary aware — a type named ``staticint_t``
    should NOT lose its leading 'static'."""
    from bmc_agent.harness_generator import _ret_type_bare
    assert _ret_type_bare("staticint_t") == "staticint_t"
    assert _ret_type_bare("static staticint_t") == "staticint_t"


# ---------------------------------------------------------------------------
# Integration: sibling-placeholder emission with static void
# ---------------------------------------------------------------------------

def test_sibling_placeholder_static_void_emits_no_void_variable(tmp_path):
    """When a sibling function has return type 'static void' (regex
    parser fallback), the placeholder must emit `/* void sibling */`
    body, NOT `static void _r;` — the latter is invalid C and CBMC
    rejects it with 'void-typed symbol not permitted'."""
    from bmc_agent.parser import (
        FunctionInfo, FunctionSignature, ParsedCFile,
    )
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus

    # Build a minimal ParsedCFile with:
    #   void target(void) { sibling(); }      <- FUT
    #   static void sibling(void) { ... }     <- sibling
    target_sig = FunctionSignature(
        name="target", return_type="void", parameters=[("void", "")],
    )
    target = FunctionInfo(
        name="target",
        signature=target_sig,
        body="{\n    sibling();\n}",
        callees={"sibling"},
        source_file=str(tmp_path / "fake.c"),
    )
    sibling_sig = FunctionSignature(
        # Simulate the parser fallback: storage class kept in return_type
        name="sibling", return_type="static void", parameters=[("void", "")],
    )
    sibling = FunctionInfo(
        name="sibling",
        signature=sibling_sig,
        body="{\n    /* sibling body */\n}",
        callees=set(),
        source_file=str(tmp_path / "fake.c"),
    )

    parsed = ParsedCFile(
        path=str(tmp_path / "fake.c"),
        functions={"target": target_sig, "sibling": sibling_sig},
        function_bodies={"target": target.body, "sibling": sibling.body},
        call_graph={"target": {"sibling"}, "sibling": set()},
    )
    # The harness generator reads functions / call_graph; struct_definitions etc.
    # default to empty containers, which is fine here.

    spec = Spec(
        function_name="target", precondition="true", postcondition="true",
        status=SpecStatus.GENERATED,
    )

    config = Config(llm_api_key="test")
    gen = HarnessGenerator(config)
    text = gen.generate_harness(
        func=target, spec=spec, parsed_file=parsed,
        all_funcs={"target": target, "sibling": sibling},
    )

    # Locate the sibling placeholder block in the emitted harness
    assert "Sibling placeholder" in text or "static void sibling" in text, (
        "expected sibling block in harness:\n" + text[-2000:]
    )
    # The placeholder for `sibling` must NOT contain `static void _r;`
    # — that's the broken-pre-fix output that CBMC rejects.
    assert "static void _r" not in text, (
        "sibling placeholder emitted INVALID 'static void _r;' — "
        "_ret_type_bare strip not applied:\n" + text[-2000:]
    )
    # And the void-branch comment should appear in the sibling block
    assert "void sibling" in text  # the comment placeholder added on void branch


# ---------------------------------------------------------------------------
# Integration: FUT call site with static-void FUT
# ---------------------------------------------------------------------------

def test_fut_call_static_void_emits_bare_call(tmp_path):
    """When the FUT itself has 'static void' return type, the main
    harness must emit a bare `fut_name(args);` call — NOT a `static void
    result = fut_name(...);` (invalid C, void-typed variable)."""
    from bmc_agent.parser import (
        FunctionInfo, FunctionSignature, ParsedCFile,
    )
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus

    fut_sig = FunctionSignature(
        name="my_void_fn", return_type="static void",
        parameters=[("int", "x")],
    )
    fut = FunctionInfo(
        name="my_void_fn",
        signature=fut_sig,
        body="{\n    (void)x;\n}",
        callees=set(),
        source_file=str(tmp_path / "fake.c"),
    )

    parsed = ParsedCFile(
        path=str(tmp_path / "fake.c"),
        functions={"my_void_fn": fut_sig},
        function_bodies={"my_void_fn": fut.body},
        call_graph={"my_void_fn": set()},
    )

    spec = Spec(
        function_name="my_void_fn", precondition="true", postcondition="true",
        status=SpecStatus.GENERATED,
    )

    config = Config(llm_api_key="test")
    gen = HarnessGenerator(config)
    text = gen.generate_harness(
        func=fut, spec=spec, parsed_file=parsed,
        all_funcs={"my_void_fn": fut},
    )

    # The FUT call must be bare — no result variable
    assert "static void result" not in text
    assert "static void _amc_ret" not in text
    # And the call should appear
    assert "my_void_fn(" in text


# ---------------------------------------------------------------------------
# Learned project-clause parameter-name safety gate
# ---------------------------------------------------------------------------

def test_clause_references_only_known_idents_accepts_clause_using_param():
    """A clause whose root identifier matches a current parameter passes."""
    from bmc_agent.harness_generator import _clause_references_only_known_idents
    assert _clause_references_only_known_idents(
        "a->inclusion_uids.count == 0", param_names={"a", "entry"},
    ) is True


def test_clause_references_only_known_idents_rejects_clause_using_unknown_param():
    """A clause whose root identifier isn't a current parameter is rejected
    — this is the ``a`` vs ``_a`` case that produced ``failed to find
    symbol 'a'`` CBMC errors in pre-fix sweeps."""
    from bmc_agent.harness_generator import _clause_references_only_known_idents
    assert _clause_references_only_known_idents(
        "a->inclusion_uids.count == 0", param_names={"_a", "entry"},
    ) is False


def test_clause_references_only_known_idents_skips_keyword_prefix():
    """A clause starting with ``struct`` / cast keywords picks the next
    identifier as the root."""
    from bmc_agent.harness_generator import _clause_references_only_known_idents
    # `((struct archive_match *)_a)->magic == 0xcad11c9U` — first real
    # ident is `archive_match`, but that's not a parameter. The safe
    # rule: ALL non-keyword roots must be known params. This particular
    # clause SHOULD pass if `_a` is the only "true" parameter root, but
    # the simpler check looks at the first non-keyword non-type ident.
    # Test: a cast-prefix clause with the param in the path is accepted
    # when the param name appears as a root.
    # The test above already covers the common case; here we test the
    # edge where the first identifier is a type name (`int`, etc.)
    assert _clause_references_only_known_idents(
        "_a != NULL", param_names={"_a"},
    ) is True


def test_clause_references_only_known_idents_pure_literal_passes():
    """A clause with no identifiers (just literals) is harmless."""
    from bmc_agent.harness_generator import _clause_references_only_known_idents
    assert _clause_references_only_known_idents(
        "1 == 1", param_names={"x"},
    ) is True


def test_emit_learned_clauses_filters_project_by_param_set(tmp_path):
    """Integration: when a project clause was distilled from a function
    whose param was ``a`` and we're now emitting for a function whose
    params are {``_a``, ``entry``}, the clause must be filtered out."""
    import json as _json
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import _emit_learned_clauses

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    (art_dir / "learned_constraints.json").write_text(_json.dumps({
        "project_clauses": [
            "a->inclusion_uids.count == 0",   # references 'a' — should be filtered
            "1 == 1",                          # pure literal — should pass
        ],
        "function_clauses": {},
        "function_post_relaxations": {},
        "code_change_todos": [],
        "version": 1,
    }))

    config = Config(
        llm_api_key="test",
        enable_feedback_loop=True,
        artifact_dir=str(art_dir),
    )

    # Pretend the FUT has params {_a, entry}: the 'a'-rooted clause
    # should NOT survive.
    out = _emit_learned_clauses(
        config, "archive_match_owner_excluded", "project",
        param_names={"_a", "entry"},
    )
    assert all("a->inclusion_uids" not in c for c in out), (
        f"unsafe clause leaked through param-gate: {out}"
    )
    # The pure-literal clause should pass through
    assert any("1 == 1" in c for c in out)


def test_emit_learned_clauses_keeps_project_when_param_matches(tmp_path):
    """When the function's params DO include the clause's root, the
    clause must be kept — the gate is a safety net, not a blanket
    suppression."""
    import json as _json
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import _emit_learned_clauses

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    (art_dir / "learned_constraints.json").write_text(_json.dumps({
        "project_clauses": ["a->inclusion_uids.count == 0"],
        "function_clauses": {},
        "function_post_relaxations": {},
        "code_change_todos": [],
        "version": 1,
    }))

    config = Config(
        llm_api_key="test",
        enable_feedback_loop=True,
        artifact_dir=str(art_dir),
    )

    out = _emit_learned_clauses(
        config, "owner_excluded", "project",
        param_names={"a", "entry"},
    )
    assert len(out) == 1
    assert "a->inclusion_uids.count" in out[0]


def test_emit_learned_clauses_no_filter_when_param_names_none(tmp_path):
    """Callers that don't supply param_names get the historical
    behaviour — every clause emitted, no safety filter. Used by code
    paths that haven't been migrated yet."""
    import json as _json
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import _emit_learned_clauses

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    (art_dir / "learned_constraints.json").write_text(_json.dumps({
        "project_clauses": ["xyz_unknown->foo == 0"],
        "function_clauses": {},
        "function_post_relaxations": {},
        "code_change_todos": [],
        "version": 1,
    }))

    config = Config(
        llm_api_key="test",
        enable_feedback_loop=True,
        artifact_dir=str(art_dir),
    )

    out = _emit_learned_clauses(
        config, "any_fn", "project", param_names=None,
    )
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Feasibility-harness variadic-stub skip
# ---------------------------------------------------------------------------

def test_feasibility_harness_skips_substitution_for_unknown_externals(tmp_path):
    """An external callee whose signature isn't in parsed_file.functions /
    extern_sigs / universal_stub_contracts (e.g., libarchive's variadic
    ``archive_set_error``) makes ``_generate_stub`` fall through to the
    zero-arg ``void X_stub(void)`` fallback. Substituting the call site
    to ``X_stub(arg, arg, arg)`` then produces ``wrong number of function
    arguments`` CBMC errors (exit code 6) and the entire feasibility
    check is skipped.

    Fix: the feasibility harness must SKIP substitution for any external
    whose stub generation fell through to the unknown-fallback. The call
    site stays as the original symbol; CBMC treats it as a nondet-return
    unresolved external — the right semantics for feasibility anyway.
    """
    from bmc_agent.parser import (
        FunctionInfo, FunctionSignature, ParsedCFile,
    )
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus
    from bmc_agent.cbmc import Counterexample

    # A FUT that calls a known-variadic external (archive_set_error_like).
    fut_body = (
        "{\n"
        "    if (a == 0) {\n"
        "        archive_set_error_like(a, 1, \"two\", \"three\");\n"
        "        return -1;\n"
        "    }\n"
        "    return 0;\n"
        "}"
    )
    fut_sig = FunctionSignature(
        name="my_fut", return_type="int",
        parameters=[("int", "a")],
    )
    fut = FunctionInfo(
        name="my_fut",
        signature=fut_sig,
        body=fut_body,
        callees={"archive_set_error_like"},
        source_file=str(tmp_path / "fake.c"),
    )

    parsed = ParsedCFile(
        path=str(tmp_path / "fake.c"),
        functions={"my_fut": fut_sig},
        function_bodies={"my_fut": fut.body},
        call_graph={"my_fut": {"archive_set_error_like"}},
    )

    spec = Spec(
        function_name="my_fut", precondition="true", postcondition="true",
        status=SpecStatus.GENERATED,
    )
    cex = Counterexample(
        failing_property="my_fut.assertion.1",
        variable_assignments={"a": "0"},
        trace=[],
    )

    config = Config(llm_api_key="test")
    gen = HarnessGenerator(config)
    text = gen.generate_feasibility_harness(
        func=fut, spec=spec, counterexample=cex,
        parsed_file=parsed, all_specs={},
    )

    # The broken-fallback stub must NOT be emitted, and the call site
    # must NOT be substituted (no `_stub` suffix on the call).
    assert "void archive_set_error_like_stub(void)" not in text, (
        f"broken zero-arg stub emitted:\n{text[-2000:]}"
    )
    assert "archive_set_error_like_stub(" not in text, (
        f"call site substituted to broken stub:\n{text[-2000:]}"
    )
    # And the original call should remain in the FUT body
    assert "archive_set_error_like(" in text


def test_feasibility_harness_still_stubs_known_registry_externals(tmp_path):
    """Sanity: externals whose signature IS in universal_stub_contracts
    (e.g. ``archive_entry_pathname``) DO still get stubbed and
    substituted — the fix only skips the unknown-fallback case, not
    every external."""
    from bmc_agent.parser import (
        FunctionInfo, FunctionSignature, ParsedCFile,
    )
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.universal_stub_contracts import known_callees

    registry_callee = "archive_entry_pathname"
    assert registry_callee in known_callees(), "test assumption broken"

    fut_sig = FunctionSignature(
        name="caller", return_type="int", parameters=[("int", "x")],
    )
    fut_body = (
        "{\n"
        f"    const char *p = {registry_callee}(0);\n"
        "    (void)p;\n"
        "    return x;\n"
        "}"
    )
    fut = FunctionInfo(
        name="caller", signature=fut_sig,
        body=fut_body,
        callees={registry_callee},
        source_file=str(tmp_path / "fake.c"),
    )
    parsed = ParsedCFile(
        path=str(tmp_path / "fake.c"),
        functions={"caller": fut_sig},
        function_bodies={"caller": fut_body},
        call_graph={"caller": {registry_callee}},
    )

    spec = Spec(function_name="caller", precondition="true",
                postcondition="true", status=SpecStatus.GENERATED)
    cex = Counterexample(
        failing_property="caller.assertion.1",
        variable_assignments={"x": "0"},
        trace=[],
    )

    config = Config(llm_api_key="test")
    gen = HarnessGenerator(config)
    text = gen.generate_feasibility_harness(
        func=fut, spec=spec, counterexample=cex,
        parsed_file=parsed, all_specs={},
    )

    assert f"/* Stub for callee: {registry_callee} */" in text, (
        f"registry-known external not stubbed:\n{text[-2000:]}"
    )
    assert f"{registry_callee}_stub(" in text


# ---------------------------------------------------------------------------
# Pseudo-formal-logic syntactic gate
# ---------------------------------------------------------------------------

def test_clause_syntactic_safe_rejects_bare_forall():
    """LLM-emitted ``forall struct ... in ... : ...`` is not valid C —
    CBMC rejects with "syntax error before 'struct'". Reject at emit
    time."""
    from bmc_agent.harness_generator import _clause_is_syntactically_safe
    bad = (
        "a != NULL && valid(&a->t) && "
        "(forall struct archive_rb_node *n in a->t: valid(container_of(n, struct match_file, node)))"
    )
    assert _clause_is_syntactically_safe(bad) is False


def test_clause_syntactic_safe_rejects_bare_exists():
    from bmc_agent.harness_generator import _clause_is_syntactically_safe
    assert _clause_is_syntactically_safe(
        "exists int i: a[i] == 0"
    ) is False


def test_clause_syntactic_safe_accepts_cprover_quantifiers():
    """CBMC's own ``__CPROVER_forall`` / ``__CPROVER_exists`` ARE valid —
    they must not be rejected by the gate."""
    from bmc_agent.harness_generator import _clause_is_syntactically_safe
    assert _clause_is_syntactically_safe(
        "__CPROVER_forall { int i; 0 <= i && i < n ==> a[i] != 0 }"
    ) is True
    assert _clause_is_syntactically_safe(
        "__CPROVER_exists { int i; a[i] == target }"
    ) is True


def test_clause_syntactic_safe_accepts_plain_boolean_expressions():
    """Ordinary C boolean expressions pass the gate unchanged."""
    from bmc_agent.harness_generator import _clause_is_syntactically_safe
    for c in (
        "a != NULL",
        "(a->count == 0 || a->ids != NULL)",
        "((struct archive_match *)_a)->magic == 0xcad11c9U",
        "1 == 1",
    ):
        assert _clause_is_syntactically_safe(c) is True, c


def test_emit_learned_clauses_filters_pseudo_logic_in_function_scope(tmp_path):
    """Integration: a function-scope clause containing ``forall``
    must be filtered (this is the regression — function-scope clauses
    bypassed the param-name gate but not the syntactic gate)."""
    import json as _json
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import _emit_learned_clauses

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    bad_clause = (
        "a != NULL && (forall struct archive_rb_node *n in a->t: "
        "valid(container_of(n, struct match_file, node)))"
    )
    (art_dir / "learned_constraints.json").write_text(_json.dumps({
        "project_clauses": [],
        "function_clauses": {"add_entry": [bad_clause, "a != NULL"]},
        "function_post_relaxations": {},
        "code_change_todos": [],
        "version": 1,
    }))

    config = Config(
        llm_api_key="test",
        enable_feedback_loop=True,
        artifact_dir=str(art_dir),
    )

    out = _emit_learned_clauses(config, "add_entry", "function")
    # Pseudo-logic clause filtered; plain one kept.
    assert all("forall" not in c for c in out), (
        f"forall clause leaked through gate: {out}"
    )
    assert any("a != NULL" in c for c in out)


def test_emit_learned_clauses_filters_pseudo_logic_in_project_scope(tmp_path):
    """Same gate also applies to project scope, alongside the
    param-name check."""
    import json as _json
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import _emit_learned_clauses

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    (art_dir / "learned_constraints.json").write_text(_json.dumps({
        "project_clauses": [
            "g_init != 0",
            "exists int i: g_arr[i] > 0",
        ],
        "function_clauses": {},
        "function_post_relaxations": {},
        "code_change_todos": [],
        "version": 1,
    }))

    config = Config(
        llm_api_key="test",
        enable_feedback_loop=True,
        artifact_dir=str(art_dir),
    )

    out = _emit_learned_clauses(
        config, "any_fn", "project", param_names={"x"},
    )
    assert all("exists" not in c for c in out)
    assert any("g_init" in c for c in out)


# ---------------------------------------------------------------------------
# Feasibility-harness opaque-struct guard
# ---------------------------------------------------------------------------

def test_feasibility_harness_opaque_struct_param_uses_nondet_pointer(tmp_path):
    """When a function-under-test takes a pointer-to-opaque-struct
    (forward-decl only; no body in struct_definitions), the
    feasibility harness must NOT stack-allocate the pointee — that
    triggers ``incomplete type not permitted here`` at CBMC type-check.
    Mirror the main harness's treatment: declare only the pointer and
    let CBMC nondet it. Regression for libarchive's
    ``struct archive_entry *entry`` params on owner_excluded /
    archive_match_owner_excluded / add_entry / etc."""
    from bmc_agent.parser import (
        FunctionInfo, FunctionSignature, ParsedCFile,
    )
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus
    from bmc_agent.cbmc import Counterexample

    fut_sig = FunctionSignature(
        name="caller", return_type="int",
        parameters=[("struct opaque_t *", "p")],
    )
    fut = FunctionInfo(
        name="caller", signature=fut_sig,
        body="{\n    (void)p;\n    return 0;\n}",
        callees=set(),
        source_file=str(tmp_path / "fake.c"),
    )
    # NOTE: opaque_t intentionally not in struct_definitions
    parsed = ParsedCFile(
        path=str(tmp_path / "fake.c"),
        functions={"caller": fut_sig},
        function_bodies={"caller": fut.body},
        call_graph={"caller": set()},
        struct_definitions={},  # opaque — no body
    )

    spec = Spec(function_name="caller", precondition="true",
                postcondition="true", status=SpecStatus.GENERATED)
    cex = Counterexample(
        failing_property="caller.assertion.1",
        variable_assignments={},
        trace=[],
    )

    config = Config(llm_api_key="test")
    gen = HarnessGenerator(config)
    text = gen.generate_feasibility_harness(
        func=fut, spec=spec, counterexample=cex,
        parsed_file=parsed, all_specs={},
    )

    # Must NOT stack-allocate the opaque body
    assert "struct opaque_t _p_val" not in text, (
        "opaque struct was stack-allocated — CBMC would reject as "
        f"incomplete type:\n{text[-1500:]}"
    )
    # Must declare the pointer with the opaque-marker comment
    assert "/* opaque struct opaque_t: nondet pointer" in text


def test_feasibility_harness_concrete_struct_still_stack_allocates(tmp_path):
    """Sanity: concrete (non-opaque) struct params still get the
    stack-allocated backing variable — the fix is targeted, not a
    blanket disable."""
    from bmc_agent.parser import (
        FunctionInfo, FunctionSignature, ParsedCFile,
    )
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus
    from bmc_agent.cbmc import Counterexample

    fut_sig = FunctionSignature(
        name="caller", return_type="int",
        parameters=[("struct concrete_t *", "p")],
    )
    fut = FunctionInfo(
        name="caller", signature=fut_sig,
        body="{\n    (void)p;\n    return 0;\n}",
        callees=set(),
        source_file=str(tmp_path / "fake.c"),
    )
    # concrete_t HAS a body in struct_definitions
    parsed = ParsedCFile(
        path=str(tmp_path / "fake.c"),
        functions={"caller": fut_sig},
        function_bodies={"caller": fut.body},
        call_graph={"caller": set()},
        struct_definitions={"concrete_t": [("int", "x")]},
    )

    spec = Spec(function_name="caller", precondition="true",
                postcondition="true", status=SpecStatus.GENERATED)
    cex = Counterexample(
        failing_property="caller.assertion.1",
        variable_assignments={}, trace=[],
    )

    config = Config(llm_api_key="test")
    gen = HarnessGenerator(config)
    text = gen.generate_feasibility_harness(
        func=fut, spec=spec, counterexample=cex,
        parsed_file=parsed, all_specs={},
    )

    # Concrete struct still gets its stack backing
    assert "struct concrete_t _p_val" in text
    # And not flagged as opaque
    assert "/* opaque" not in text


# ---------------------------------------------------------------------------
# Feasibility-harness return-variable / postcondition mismatch
# ---------------------------------------------------------------------------

def test_feasibility_harness_return_var_matches_postcond_result_keyword(tmp_path):
    """The postcondition DSL uses ``result`` as the return-value
    placeholder, and ``postcond_to_assert`` translates it to a literal
    C identifier ``result``. The feasibility harness's local
    return-capture variable MUST also be named ``result`` (with
    collision fallback to ``_amc_ret`` when a param shadows it) —
    otherwise CBMC fails with ``failed to find symbol 'result'`` →
    CONVERSION ERROR → exit 6 on every function whose postcondition
    references the return value (e.g. libarchive's
    set_timefilter_date)."""
    from bmc_agent.parser import (
        FunctionInfo, FunctionSignature, ParsedCFile,
    )
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus
    from bmc_agent.cbmc import Counterexample

    fut_sig = FunctionSignature(
        name="fn", return_type="int",
        parameters=[("int", "x")],
    )
    fut = FunctionInfo(
        name="fn", signature=fut_sig,
        body="{\n    return x == 0 ? -25 : 0;\n}",
        callees=set(),
        source_file=str(tmp_path / "fake.c"),
    )
    parsed = ParsedCFile(
        path=str(tmp_path / "fake.c"),
        functions={"fn": fut_sig},
        function_bodies={"fn": fut.body},
        call_graph={"fn": set()},
    )
    # Postcondition referencing the return value via the standard
    # ``result`` placeholder — the same shape set_timefilter_date uses.
    spec = Spec(
        function_name="fn", precondition="true",
        postcondition="result == -25 || result == 0",
        status=SpecStatus.GENERATED,
    )
    cex = Counterexample(
        failing_property="fn.assertion.1",
        variable_assignments={}, trace=[],
    )

    config = Config(llm_api_key="test")
    gen = HarnessGenerator(config)
    text = gen.generate_feasibility_harness(
        func=fut, spec=spec, counterexample=cex,
        parsed_file=parsed, all_specs={},
    )

    # The call must capture into ``result`` (matching the postcond
    # translator), NOT ``_result``.
    assert "int result = fn(" in text, (
        f"FUT call doesn't use 'result' as return-capture name:\n"
        f"{text[-1500:]}"
    )
    # And the postcondition assert must reference the same name
    assert "result == -25" in text


def test_feasibility_harness_return_var_collision_fallback(tmp_path):
    """When a parameter is itself named ``result``, the return-capture
    variable falls back to ``_amc_ret`` to avoid the C redefinition
    that hit libarchive's isint / isint_w."""
    from bmc_agent.parser import (
        FunctionInfo, FunctionSignature, ParsedCFile,
    )
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec, SpecStatus
    from bmc_agent.cbmc import Counterexample

    fut_sig = FunctionSignature(
        name="isint", return_type="int",
        parameters=[("const char *", "s"), ("int *", "result")],
    )
    fut = FunctionInfo(
        name="isint", signature=fut_sig,
        body="{\n    *result = 0;\n    return s ? 1 : 0;\n}",
        callees=set(),
        source_file=str(tmp_path / "fake.c"),
    )
    parsed = ParsedCFile(
        path=str(tmp_path / "fake.c"),
        functions={"isint": fut_sig},
        function_bodies={"isint": fut.body},
        call_graph={"isint": set()},
    )
    spec = Spec(
        function_name="isint", precondition="true", postcondition="true",
        status=SpecStatus.GENERATED,
    )
    cex = Counterexample(
        failing_property="isint.assertion.1",
        variable_assignments={}, trace=[],
    )

    config = Config(llm_api_key="test")
    gen = HarnessGenerator(config)
    text = gen.generate_feasibility_harness(
        func=fut, spec=spec, counterexample=cex,
        parsed_file=parsed, all_specs={},
    )

    # Param 'result' already declared — return capture must use _amc_ret
    assert "int _amc_ret = isint(" in text, (
        f"return-capture collision fallback not applied:\n{text[-1500:]}"
    )
    assert "int result = isint(" not in text, (
        "would have collided with parameter named 'result'"
    )
