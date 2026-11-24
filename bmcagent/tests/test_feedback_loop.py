"""Tests for the realism-rejection feedback loop (bmc_agent/feedback_loop.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Remediation parsing
# ---------------------------------------------------------------------------


def test_parse_remediation_code_change():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    raw = json.dumps({
        "scope": "code-change",
        "code_change": "Add a self-ref struct field NULL-init in harness_generator.",
        "rationale": "The CEx walks comp->next which is nondet — this is a model artifact class.",
        "confidence": "high",
    })
    r = _parse_remediation(raw, "xmlPatternStreamable")
    assert r.scope == RemediationScope.CODE_CHANGE
    assert "self-ref" in r.code_change
    assert r.confidence == "high"


def test_parse_remediation_function_spec():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    raw = json.dumps({
        "scope": "function-spec",
        "clause": "comp != NULL && comp->next == NULL",
        "rationale": "All real callers pass a freshly-allocated comp with no chain attached.",
        "confidence": "medium",
    })
    r = _parse_remediation(raw, "xmlPatternStreamable")
    assert r.scope == RemediationScope.FUNCTION_SPEC
    assert r.clause == "comp != NULL && comp->next == NULL"


def test_parse_remediation_project_invariant():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    raw = json.dumps({
        "scope": "project-invariant",
        "clause": "xmlMalloc != NULL",
        "rationale": "xmlMalloc is set by library init before any public API.",
        "confidence": "high",
    })
    r = _parse_remediation(raw, "any_function")
    assert r.scope == RemediationScope.PROJECT_INVARIANT
    assert r.clause == "xmlMalloc != NULL"


def test_parse_remediation_handles_markdown_fence():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    raw = (
        "```json\n"
        + json.dumps({
            "scope": "none",
            "rationale": "Cannot safely propose anything.",
            "confidence": "low",
        })
        + "\n```\n"
    )
    r = _parse_remediation(raw, "f")
    assert r.scope == RemediationScope.NONE


def test_parse_remediation_handles_garbage():
    from bmc_agent.feedback_loop import _parse_remediation, RemediationScope
    r = _parse_remediation("not json at all", "f")
    assert r.scope == RemediationScope.NONE


# ---------------------------------------------------------------------------
# LearnedConstraintsStore
# ---------------------------------------------------------------------------


def test_store_starts_empty(tmp_path: Path):
    from bmc_agent.feedback_loop import LearnedConstraintsStore
    store = LearnedConstraintsStore(tmp_path)
    assert store.project_clauses() == []
    assert store.function_clauses("anything") == []
    assert store.summary()["project_clauses"] == 0


def test_store_records_project_invariant(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.PROJECT_INVARIANT,
        clause="xmlMalloc != NULL",
        rationale="lib init",
        confidence="high",
    )
    assert store.record("any_fn", r) is True
    assert "xmlMalloc != NULL" in store.project_clauses()
    # Re-recording is a no-op (idempotent)
    assert store.record("any_fn", r) is False


def test_store_records_function_clause(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.FUNCTION_SPEC,
        clause="comp != NULL",
        rationale="all callers check",
        confidence="medium",
    )
    store.record("xmlPatternStreamable", r)
    assert "comp != NULL" in store.function_clauses("xmlPatternStreamable")
    # Different function: not contaminated
    assert store.function_clauses("xmlOther") == []


def test_store_records_code_change_todo(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.CODE_CHANGE,
        code_change="Add an artifact pattern for chrooted-host filesystem stubs.",
        rationale="not encodable as an invariant",
        confidence="high",
    )
    store.record("xmlSomeFn", r, source_property="xmlSomeFn.pointer.1")
    summary = store.summary()
    assert summary["code_change_todos"] == 1


def test_store_persists_across_instances(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    s1 = LearnedConstraintsStore(tmp_path)
    s1.record("f", Remediation(
        scope=RemediationScope.PROJECT_INVARIANT,
        clause="xmlFree != NULL",
        confidence="high",
    ))
    # Reload from disk
    s2 = LearnedConstraintsStore(tmp_path)
    assert "xmlFree != NULL" in s2.project_clauses()


def test_store_auto_promotes_when_threshold_reached(tmp_path: Path):
    """When ≥PROMOTION_THRESHOLD functions independently learn the same
    clause, it auto-migrates from function_clauses to project_clauses.
    Uses a non-param-style ident (xmlGlobalParser) so the write-time
    gate (param-style block, see test_store_refuses_auto_promote_*)
    doesn't kick in — that gate has its own coverage."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    clause = "xmlGlobalParser != NULL"
    r = Remediation(scope=RemediationScope.FUNCTION_SPEC, clause=clause)
    store.record("fnA", r)
    store.record("fnB", r)
    # Not yet promoted (only 2 functions)
    assert clause not in store.project_clauses()
    assert clause in store.function_clauses("fnA")
    # 3rd function triggers promotion
    store.record("fnC", r)
    assert clause in store.project_clauses()
    # Per-function copies are retired
    assert clause not in store.function_clauses("fnA")
    assert clause not in store.function_clauses("fnB")
    assert clause not in store.function_clauses("fnC")


def test_store_does_not_double_promote(tmp_path: Path):
    """Re-recording the same clause for additional functions after
    promotion shouldn't duplicate the project entry. Uses
    ``xmlGlobalSize > 0`` — a non-param-style ident so the write-time
    gate lets the auto-promotion through."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    store = LearnedConstraintsStore(tmp_path)
    r = Remediation(
        scope=RemediationScope.FUNCTION_SPEC, clause="xmlGlobalSize > 0",
    )
    for fn in ("a", "b", "c", "d", "e"):
        store.record(fn, r)
    assert store.project_clauses().count("xmlGlobalSize > 0") == 1


def test_store_ignores_unknown_schema_version(tmp_path: Path):
    from bmc_agent.feedback_loop import LearnedConstraintsStore
    f = tmp_path / "learned_constraints.json"
    f.write_text(json.dumps({"version": 99, "project_clauses": ["bogus"]}))
    store = LearnedConstraintsStore(tmp_path)
    assert store.project_clauses() == []


# ---------------------------------------------------------------------------
# Harness applies learned clauses when feedback-loop enabled
# ---------------------------------------------------------------------------


def test_harness_emit_learned_clauses_disabled_returns_empty(tmp_path: Path):
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config
    config = Config()
    config.artifact_dir = str(tmp_path)
    # feedback loop OFF — nothing emitted even if store has entries
    assert _emit_learned_clauses(config, "any_fn", "project") == []


def test_harness_emit_learned_clauses_project(tmp_path: Path):
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    # Seed the store
    store = LearnedConstraintsStore(tmp_path)
    store.record("any", Remediation(
        scope=RemediationScope.PROJECT_INVARIANT,
        clause="xmlMalloc != NULL",
        confidence="high",
    ))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    out = _emit_learned_clauses(config, "x", "project")
    assert "__CPROVER_assume(xmlMalloc != NULL);" in out


def test_harness_emit_learned_clauses_project_skips_bare_ident_collision(tmp_path: Path):
    """Regression: postfix8 sweep (2026-05-27) had 701 CBMC parse failures
    because the project_clauses store held ``acl != NULL`` — distilled
    from archive_acl.c functions — and the filter only checked struct-
    deref roots (``X->``), so the bare-identifier clause leaked into
    every harness, including rar5_cleanup whose params don't include
    ``acl``. CBMC reported ``failed to find symbol 'acl'`` and the whole
    TU died at parse. The filter must also skip param-style bare-ident
    references that don't resolve in the current function's scope.
    """
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    # The write-time gate (test_store_refuses_to_promote_*) prevents
    # these clauses from entering project_clauses via the normal record()
    # API now — so simulate a legacy / on-disk-contaminated store by
    # writing the bad data directly to the JSON file. This emulates the
    # postfix8 sweep artifact that triggered this regression and verifies
    # the read-time filter still defends against pre-fix data.
    bad_store = {
        "version": LearnedConstraintsStore.SCHEMA_VERSION,
        "function_clauses": {},
        "project_clauses": [
            "acl != NULL",
            "!(acl != NULL && (acl->acl_types & ARCHIVE_ENTRY_ACL_TYPE_NFS4))",
        ],
        "code_change_todos": [],
    }
    (tmp_path / LearnedConstraintsStore.FILENAME).write_text(json.dumps(bad_store))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    # rar5_cleanup's only param is ``a`` (struct archive_read *). Both
    # ``acl``-rooted clauses must be filtered out at emission time.
    out_other = _emit_learned_clauses(
        config, "rar5_cleanup", "project", param_names={"a"},
    )
    assert out_other == [], (
        f"both acl-rooted clauses should be filtered for rar5_cleanup, got {out_other}"
    )

    # For an archive_acl function (param ``acl``), the bare-ident
    # clause should survive emission. The dereference-form clause has
    # ``acl->acl_types`` so its root resolves too.
    out_self = _emit_learned_clauses(
        config, "archive_acl_clear", "project", param_names={"acl"},
    )
    assert "__CPROVER_assume(acl != NULL);" in out_self


def test_harness_emit_learned_clauses_project_skips_long_struct_deref_root(tmp_path: Path):
    """Regression: postfix9b sweep (2026-05-28) — rar5.c failed because
    a learned project clause referenced ``iso9660`` (a 7-char param
    name distilled from iso9660.c functions) as a struct-deref root:

        iso9660 == NULL || iso9660->use_files == NULL || ...

    The old gate's ``_is_param_style_ident`` heuristic only flagged
    identifiers as param-style when len ≤ 4 OR _-prefixed. ``iso9660``
    is 7 chars, no underscore prefix — it slipped through. CBMC then
    rejected all 101 rar5 harnesses with ``failed to find symbol
    'iso9660'``.

    Fix: struct-deref roots (``X->field``) are always treated as
    locals — by construction you can't dereference a macro. So we
    drop the length filter for struct-deref roots specifically.
    Bare-identifier checks (``X != NULL``) keep the length heuristic
    because long bare names CAN be global macros.
    """
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    # Simulate a legacy / on-disk-contaminated store with the exact
    # rar5 problem clause.
    bad_store = {
        "version": LearnedConstraintsStore.SCHEMA_VERSION,
        "function_clauses": {},
        "project_clauses": [
            # The actual postfix9b problem clause:
            "iso9660 == NULL || iso9660->use_files == NULL "
            "|| iso9660->use_files->utf16be_name == NULL",
            # A safe sibling that should survive:
            "archive_match != NULL",  # bare-ident long-name; assumed global-like
        ],
        "code_change_todos": [],
    }
    (tmp_path / LearnedConstraintsStore.FILENAME).write_text(json.dumps(bad_store))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    # rar5 functions have params like ``rar``, ``a``, ``arg`` — never
    # ``iso9660``. The iso9660-rooted clause must be dropped.
    out = _emit_learned_clauses(
        config, "rar5_cleanup", "project",
        param_names={"a", "rar"},
    )
    assert all("iso9660" not in c for c in out), (
        f"iso9660-rooted clause must be dropped for rar5 function, got {out}"
    )


def test_harness_emit_learned_clauses_project_skips_fn_call_arity_mismatch(tmp_path: Path):
    """Regression: postfix9 sweep (2026-05-27 night) had every CBMC
    verification on cab/cpio/iso9660/rar5 die with CONVERSION ERROR
    because the project_clauses store held a clause calling
    ``archive_mstring_get_mbs(NULL, NULL)`` — a 2-arg call distilled
    from a TU where the function had a 2-arg stub — emitted into
    cab/cpio/iso9660/rar5 harnesses where ``archive_mstring_get_mbs``
    is declared with 3 args per ``archive_string.h``. CBMC reported
    "wrong number of function arguments: expected 3, but got 2" and
    the entire TU's harness died at type-check.

    Project-scope clauses must NOT contain calls to project-defined
    functions because the declared signature can vary across TUs.
    Only CBMC builtins (``__CPROVER_*``), reserved C keywords
    (``sizeof``), and fixed-signature stdlib calls are exempted.
    """
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    bad_store = {
        "version": LearnedConstraintsStore.SCHEMA_VERSION,
        "function_clauses": {},
        "project_clauses": [
            # The actual postfix9 problem clause:
            "archive_mstring_get_mbs(NULL, NULL) == NULL || "
            "((uintptr_t)archive_mstring_get_mbs(NULL, NULL) >= 4096)",
            # A safe sibling that should survive:
            "p == NULL || __CPROVER_r_ok(p, 8)",
            # Stdlib calls should also survive:
            "len <= strlen(input)",
        ],
        "code_change_todos": [],
    }
    (tmp_path / LearnedConstraintsStore.FILENAME).write_text(json.dumps(bad_store))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    # Any function should drop the archive_mstring_get_mbs clause but
    # keep the __CPROVER_r_ok one. param_names={"p"} so the param-name
    # gate doesn't drop the safe ones for unrelated reasons.
    out = _emit_learned_clauses(
        config, "some_fn", "project", param_names={"p", "input", "len"},
    )
    assert all("archive_mstring_get_mbs" not in c for c in out), (
        f"archive_mstring_get_mbs clause must be dropped, got {out}"
    )
    assert any("__CPROVER_r_ok" in c for c in out), (
        f"__CPROVER_r_ok clause should survive (CBMC builtin), got {out}"
    )
    assert any("strlen" in c for c in out), (
        f"strlen clause should survive (fixed-signature stdlib), got {out}"
    )


def test_store_refuses_to_promote_function_local_clause(tmp_path: Path):
    """Write-time gate (companion to ed48fb9 read-time filter). When the
    LLM emits scope=project-invariant with a clause that references a
    param-style identifier (``acl != NULL`` from the archive_acl.c sweep),
    the store should refuse to add it to project_clauses and instead
    record it as function-spec for the source function — so other
    functions' harnesses don't get contaminated."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )

    store = LearnedConstraintsStore(tmp_path)
    # The exact clause that caused 701 CBMC parse failures in postfix8.
    store.record("archive_acl_clear", Remediation(
        scope=RemediationScope.PROJECT_INVARIANT,
        clause="acl != NULL",
        confidence="high",
    ))

    assert store.project_clauses() == [], (
        "function-local-style clause must not enter project_clauses"
    )
    assert "acl != NULL" in store.function_clauses("archive_acl_clear"), (
        "demoted clause must survive as function-spec on the source function"
    )


def test_store_refuses_auto_promote_function_local_clause(tmp_path: Path):
    """Auto-promotion path (3+ functions independently learning the
    same clause). Sibling functions in the same module often share
    parameter names — that's a naming convention coincidence, not a
    project-wide truth. The auto-promotion must skip param-style
    clauses even when the threshold is hit."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )

    store = LearnedConstraintsStore(tmp_path)
    # 3 archive_acl functions all learn ``acl != NULL`` as function-spec.
    for fn in ("archive_acl_clear", "archive_acl_add_entry",
               "archive_acl_from_text_w"):
        store.record(fn, Remediation(
            scope=RemediationScope.FUNCTION_SPEC,
            clause="acl != NULL",
            confidence="high",
        ))

    assert store.project_clauses() == [], (
        "auto-promotion threshold met but param-style clause must still "
        "be blocked from project_clauses"
    )
    # The clause stays as function-spec on each owner.
    for fn in ("archive_acl_clear", "archive_acl_add_entry",
               "archive_acl_from_text_w"):
        assert "acl != NULL" in store.function_clauses(fn), (
            f"{fn} should still own the function-spec clause"
        )


def test_store_cleanup_contaminated_project_clauses(tmp_path: Path):
    """Legacy stores from before the write-time gate (2f19a1c) may have
    contaminated clauses in project_clauses. The cleanup utility scans
    and removes them; genuine project clauses (long names like
    xmlMalloc, ARCHIVE_OK-style macros) survive."""
    import json
    from bmc_agent.feedback_loop import LearnedConstraintsStore

    bad_store = {
        "version": LearnedConstraintsStore.SCHEMA_VERSION,
        "function_clauses": {},
        "project_clauses": [
            "acl != NULL",                          # contaminated (param-style)
            "!(a != NULL && a->t == 1)",            # contaminated (param-style root)
            "xmlMalloc != NULL",                    # genuine (long ident)
            "g_init != NULL",                       # genuine (≥5 chars, no _ prefix)
        ],
        "code_change_todos": [],
    }
    (tmp_path / LearnedConstraintsStore.FILENAME).write_text(json.dumps(bad_store))

    store = LearnedConstraintsStore(tmp_path)
    removed = store.cleanup_contaminated_project_clauses()
    assert removed == 2
    remaining = store.project_clauses()
    assert "acl != NULL" not in remaining
    assert "!(a != NULL && a->t == 1)" not in remaining
    assert "xmlMalloc != NULL" in remaining
    assert "g_init != NULL" in remaining


def test_store_still_promotes_genuine_project_clause(tmp_path: Path):
    """Sanity check: a long-name clause (xmlMalloc != NULL) is the
    canonical project-wide invariant and must still auto-promote."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )

    store = LearnedConstraintsStore(tmp_path)
    for fn in ("xmlFoo", "xmlBar", "xmlBaz"):
        store.record(fn, Remediation(
            scope=RemediationScope.FUNCTION_SPEC,
            clause="xmlMalloc != NULL",
            confidence="high",
        ))

    assert "xmlMalloc != NULL" in store.project_clauses(), (
        "genuine project invariant must still auto-promote"
    )
    # Auto-promotion retires per-function copies.
    for fn in ("xmlFoo", "xmlBar", "xmlBaz"):
        assert "xmlMalloc != NULL" not in store.function_clauses(fn)


def test_harness_emit_learned_clauses_project_keeps_global_bare_ident(tmp_path: Path):
    """Long lowercase identifiers (xmlMalloc, archive_match_globals) are
    treated as globals — they must survive the bare-ident filter even
    when not in the current function's param set."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    store = LearnedConstraintsStore(tmp_path)
    store.record("xmlFoo", Remediation(
        scope=RemediationScope.PROJECT_INVARIANT,
        clause="xmlMalloc != NULL",
        confidence="high",
    ))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    # Calling function has a totally different parameter set; xmlMalloc
    # should still be emitted because it's clearly a global, not a param.
    out = _emit_learned_clauses(
        config, "rar5_cleanup", "project", param_names={"a"},
    )
    assert "__CPROVER_assume(xmlMalloc != NULL);" in out


def test_harness_emit_learned_clauses_project_keeps_macro_constants(tmp_path: Path):
    """ALL_CAPS and UpperCamel_WITH_UNDERSCORE identifiers (macros,
    enum constants like SIZE_MAX or ARCHIVE_OK) must pass through the
    bare-ident filter when the surrounding param-style ident matches
    the function's params. Uses the legacy bypass because the write-
    time gate (more conservative; runs without per-function context)
    would refuse to promote a clause containing a param-style ident."""
    import json
    from bmc_agent.feedback_loop import LearnedConstraintsStore
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    bad_store = {
        "version": LearnedConstraintsStore.SCHEMA_VERSION,
        "function_clauses": {},
        "project_clauses": ["size < SIZE_MAX"],
        "code_change_todos": [],
    }
    (tmp_path / LearnedConstraintsStore.FILENAME).write_text(json.dumps(bad_store))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    out = _emit_learned_clauses(
        config, "acl_new_entry", "project", param_names={"size"},
    )
    assert "__CPROVER_assume(size < SIZE_MAX);" in out


def test_harness_emit_learned_clauses_function_scoped(tmp_path: Path):
    """function clauses must be returned ONLY for the matching function."""
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.harness_generator import _emit_learned_clauses
    from bmc_agent.config import Config

    store = LearnedConstraintsStore(tmp_path)
    store.record("xmlFoo", Remediation(
        scope=RemediationScope.FUNCTION_SPEC,
        clause="x != NULL",
        confidence="medium",
    ))

    config = Config()
    config.artifact_dir = str(tmp_path)
    config.enable_feedback_loop = True

    assert _emit_learned_clauses(config, "xmlFoo", "function") == [
        "__CPROVER_assume(x != NULL);"
    ]
    assert _emit_learned_clauses(config, "xmlOther", "function") == []


# ---------------------------------------------------------------------------
# Feedback loop: flag_selection threading (bug 2 regression)
# ---------------------------------------------------------------------------


def test_feedback_iterate_threads_flag_selection_through(tmp_path: Path):
    """The iter-1 CBMC re-run must receive the same Phase-1.5
    flag_selection that iter-0 used. Without this, --unsigned-overflow-check
    and friends get dropped on the re-run and CBMC silently 'verifies
    clean' the property the bug was on.

    Regression: VibeOS memory.c malloc.overflow.1 was being suppressed
    this way — iter-0 CBMC with --unsigned-overflow-check found the
    overflow, iter-1 dropped the flag and "verified clean".
    """
    from unittest.mock import MagicMock
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.feedback_loop import Remediation, RemediationScope
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.flag_selector import FlagSelection
    from bmc_agent.config import Config

    config = Config()
    config.enable_realism_check = True
    config.enable_feedback_loop = True
    config.feedback_max_iters = 1

    pipeline = AMCPipeline.__new__(AMCPipeline)
    pipeline.config = config
    pipeline.llm = MagicMock()
    pipeline.cex_validator = MagicMock()
    pipeline.realism_checker = MagicMock()
    pipeline.reporter = MagicMock()

    # The iter-0 flag selection stashed on self by Phase 1.5.
    pipeline._flag_selections = {
        "malloc": FlagSelection(unsigned_overflow_check=True, reasoning="size math"),
    }

    # Capture the flag_selection arg the feedback loop's re-run passes.
    captured = {}
    fake_verdict = MagicMock()
    fake_verdict.verified = True
    fake_verdict.counterexamples = []
    def fake_check(func, spec, parsed, driver_name, all_funcs=None, flag_selection=None):
        captured["flag_selection"] = flag_selection
        return fake_verdict
    pipeline.bmc_engine = MagicMock()
    pipeline.bmc_engine.check_function.side_effect = fake_check

    # _feedback_record's LLM distillation: skip the real LLM, return a clause.
    pipeline._feedback_record = MagicMock(return_value=Remediation(
        scope=RemediationScope.FUNCTION_SPEC,
        clause="size <= (SIZE_MAX / 2)",
        confidence="high",
    ))

    # Inputs for _feedback_iterate.
    validation = MagicMock()
    validation.counterexample = MagicMock()
    validation.counterexample.failing_property = "malloc.overflow.1"
    validation.counterexample.failure_location = {"line": "10"}
    realism = RealismCheckResult(
        verdict=RealismVerdict.UNREALISTIC,
        reasoning="bounded heap, never SIZE_MAX",
        key_concern="overflow on alignment math",
        llm_confidence="high",
    )
    func = MagicMock(); func.name = "malloc"; func.body = "..."
    from bmc_agent.spec import Spec
    spec = Spec(function_name="malloc", precondition="size >= 0", postcondition="true")

    pipeline._feedback_iterate(
        validation, realism, func, spec, MagicMock(),
        all_funcs={}, driver_name="d", all_specs={},
    )

    # CRITICAL: the flag_selection from Phase 1.5 must have been passed through.
    assert captured.get("flag_selection") is not None
    assert captured["flag_selection"].unsigned_overflow_check is True


def test_pipeline_clean_proof_helpers():
    """``_all_applied_clauses`` and ``_flag_summary`` give the log line
    explicit information about what was assumed and what was checked."""
    from bmc_agent.pipeline import _all_applied_clauses, _flag_summary
    from bmc_agent.feedback_loop import (
        LearnedConstraintsStore, Remediation, RemediationScope,
    )
    from bmc_agent.flag_selector import FlagSelection
    from bmc_agent.config import Config
    from bmc_agent.spec import Spec
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = LearnedConstraintsStore(td)
        # Project + function clauses
        store.record("malloc", Remediation(
            scope=RemediationScope.FUNCTION_SPEC,
            clause="size <= (SIZE_MAX / 2)",
            confidence="high",
        ))
        store.record("anyone", Remediation(
            scope=RemediationScope.PROJECT_INVARIANT,
            clause="heap_start != 0",
            confidence="high",
        ))
        cfg = Config()
        cfg.enable_feedback_loop = True
        cfg.artifact_dir = td
        spec = Spec(function_name="malloc", precondition="size >= 0", postcondition="true")
        clauses = _all_applied_clauses(cfg, "malloc", spec)
        # Order: project, function, spec-pre
        assert clauses == ["heap_start != 0", "size <= (SIZE_MAX / 2)", "size >= 0"]

    # Trivial preconditions ("true", "1") get filtered out so the log
    # doesn't say "verified clean under {true}".
    cfg2 = Config()
    cfg2.enable_feedback_loop = False
    spec2 = Spec(function_name="f", precondition="true", postcondition="true")
    assert _all_applied_clauses(cfg2, "f", spec2) == []

    # Flag summary
    assert _flag_summary(None) == "default (pointer-check, bounds-check)"
    assert _flag_summary(FlagSelection()) == "default (pointer-check, bounds-check)"
    s = _flag_summary(FlagSelection(unsigned_overflow_check=True, pointer_overflow_check=True))
    assert "unsigned-overflow-check" in s
    assert "pointer-overflow-check" in s
