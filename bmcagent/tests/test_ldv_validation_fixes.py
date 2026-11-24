"""Regression tests for the LDV validation fixes (Phase A).

Covers the three defects that made every LDV entry-point validation query fail
before any property could be judged:

1. the C parser registered GCC ``__attribute__`` annotations as functions, so a
   forward declaration ``void * __attribute__((__warn_unused_result__));`` was
   emitted and CBMC rejected the harness with ``syntax error before ';'``;
2. the feasibility harness emitted a wrapper ``main`` on top of the real ``main``
   body when the function under test was itself the entry point, producing
   "function body 'main' defined twice";
3. the feasibility harness prepended ``<stdlib.h>`` next to a self-contained
   kernel TU that already defines ``dev_t``, producing "type symbol 'dev_t'
   defined twice" (and sys/types.h dev_t is 64-bit, so it is not a substitute).

These are unit-level assertions on the generator/parser; the end-to-end solver
evidence lives in the run artifacts, not here.
"""

from __future__ import annotations

import json

from bmc_agent.cbmc import Counterexample, _extract_plain_counterexamples
from bmc_agent.config import Config
from bmc_agent.harness_generator import (
    HarnessGenerator,
    _type_decls_define_libc_types,
)
from bmc_agent.parser import _declarator_identifier, parse_c_file


# ---------------------------------------------------------------------------
# 1. Attribute-mangled declarators
# ---------------------------------------------------------------------------

def test_declarator_identifier_recovers_name_from_attributes():
    assert _declarator_identifier(
        "( __attribute__((__always_inline__)) get_current)"
    ) == "get_current"
    assert _declarator_identifier(
        "( __attribute__((__always_inline__)) kmalloc)"
    ) == "kmalloc"


def test_declarator_identifier_rejects_annotation_only():
    # The declaration ``void * __attribute__((__warn_unused_result__));`` was
    # being registered as a function named ``__attribute__``.
    assert _declarator_identifier("__attribute__") == ""
    assert _declarator_identifier("__attribute__((__warn_unused_result__))") == ""


def test_declarator_identifier_leaves_plain_names_alone():
    assert _declarator_identifier("mousedev_open") == "mousedev_open"
    assert _declarator_identifier("(PTR_ERR)") == "PTR_ERR"


def test_attribute_pseudo_function_not_registered(tmp_path):
    src = tmp_path / "attr.c"
    src.write_text(
        "__inline static void * __attribute__((__warn_unused_result__)) "
        "ERR_PTR(long error) { return (void *)error; }\n"
        "__inline static struct task_struct *( "
        "__attribute__((__always_inline__)) get_current)(void) { return 0; }\n"
        "int main(void) { return 0; }\n"
    )
    parsed = parse_c_file(str(src))
    assert "__attribute__" not in parsed.functions
    # The real name is recovered, so call sites can resolve the body.
    assert "get_current" in parsed.functions


# ---------------------------------------------------------------------------
# 2. Self-contained source detection (dev_t collision)
# ---------------------------------------------------------------------------

def test_libc_typedef_detection():
    assert _type_decls_define_libc_types(
        "typedef __u32 __kernel_dev_t;\ntypedef __kernel_dev_t dev_t;\n"
    )
    # div_t alone is not evidence of a self-contained KERNEL TU (glibc
    # userspace TUs define it too and still need <stdlib.h>).
    assert not _type_decls_define_libc_types("typedef struct { int v[2]; } div_t;\n")
    assert not _type_decls_define_libc_types("typedef unsigned int uint32_t;\n")


# ---------------------------------------------------------------------------
# 3. Feasibility harness for an entry-point function
# ---------------------------------------------------------------------------

def test_feasibility_harness_for_main_has_single_main_definition(tmp_path):
    src = tmp_path / "entry.c"
    src.write_text(
        "typedef unsigned int __u32;\n"
        "typedef __u32 __kernel_dev_t;\n"
        "typedef __kernel_dev_t dev_t;\n"
        "struct device { dev_t dev; };\n"
        "static struct device g;\n"
        "void reach_error(void);\n"
        "void ldv_check_final_state(void);\n"
        "void main(void) {\n"
        "  if (g.dev) { reach_error(); }\n"
        "  ldv_check_final_state();\n"
        "}\n"
    )
    parsed = parse_c_file(str(src))
    func = parsed.get_function_info("main")
    # Explicit LDV mode: the single-main special case is gated on it.
    config = Config()
    config.preserve_ldv_property = True
    generator = HarnessGenerator(config=config)

    from bmc_agent.cbmc import Counterexample
    from bmc_agent.spec import Spec

    harness = generator.generate_feasibility_harness(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )

    # Exactly one *definition* of main (a forward declaration is fine).
    definitions = [
        line for line in harness.splitlines()
        if line.startswith("void main(void)") and not line.rstrip().endswith(";")
    ]
    assert len(definitions) == 1, harness[-400:]
    assert "No wrapper main" in harness
    # The kernel dev_t is kept; <stdlib.h> (which re-defines dev_t as 64-bit)
    # must not be pulled in.
    assert "typedef __kernel_dev_t dev_t;" in harness
    assert "#include <stdlib.h>" not in harness


# ---------------------------------------------------------------------------
# 4. Plain-text property verdicts (feasibility fallback path)
# ---------------------------------------------------------------------------

def test_plain_counterexamples_capture_failed_property_names():
    raw = (
        "[mousedev_notify_readers.precondition_instance.1] line 12 x: SUCCESS\n"
        "[reach_error.assertion.1] line 9169 assertion 0: FAILURE\n"
        "VERIFICATION FAILED\n"
    )
    cexes = _extract_plain_counterexamples(raw)
    assert [c.failing_property for c in cexes] == ["reach_error.assertion.1"]
    assert cexes[0].failure_location["line"] == "9169"


# ---------------------------------------------------------------------------
# 5. Strict evidence grading is opt-in
# ---------------------------------------------------------------------------

def test_strict_evidence_grading_defaults_off(monkeypatch):
    monkeypatch.delenv("BMC_AGENT_STRICT_EVIDENCE_GRADING", raising=False)
    assert Config.from_env().strict_evidence_grading is False
    monkeypatch.setenv("BMC_AGENT_STRICT_EVIDENCE_GRADING", "true")
    assert Config.from_env().strict_evidence_grading is True


# ---------------------------------------------------------------------------
# 6. Audit-driven regressions
# ---------------------------------------------------------------------------

def test_attribute_before_name_function_is_recovered(tmp_path):
    """``void * __attribute__((...)) ERR_PTR(long)`` keeps ERR_PTR.

    tree-sitter puts the real name in a sibling ``call_expression``; dropping it
    removed ERR_PTR/pack_hex_byte from parsed_file.functions, so specs and
    call-graph edges were silently lost even though the body still compiled.
    """
    src = tmp_path / "errptr.c"
    src.write_text(
        "void * __attribute__((__warn_unused_result__)) ERR_PTR(long error );\n"
        "void * __attribute__((__warn_unused_result__)) ERR_PTR(long error)"
        " { return (void *)error; }\n"
        "unsigned char pack_hex_byte(const char *p) { return 0; }\n"
        "int (*getfn(void))(int);\n"
        "int getfn(void) { return 0; }\n"
    )
    parsed = parse_c_file(str(src))
    assert "ERR_PTR" in parsed.functions
    assert parsed.functions["ERR_PTR"].return_type.strip() == "void *"
    assert parsed.functions["ERR_PTR"].parameters == [("long", "error")]
    assert "pack_hex_byte" in parsed.functions
    # Function returning a function pointer is still dropped (documented), but
    # its name must not leak as a bogus annotation-derived identifier.
    assert "" not in parsed.functions


def test_annotation_scan_respects_identifier_boundary():
    assert _declarator_identifier("my_asm(foo)") == ""
    assert _declarator_identifier("__asm__(\"nop\") handler") == "handler"


def test_self_contained_gate_requires_kernel_marker():
    """glibc/userspace TUs also define dev_t and still need <stdlib.h>."""
    kernel = "typedef __u32 __kernel_dev_t;\ntypedef __kernel_dev_t dev_t;\n"
    glibc = "typedef unsigned long __dev_t;\ntypedef __dev_t dev_t;\n"
    assert _type_decls_define_libc_types(kernel, "kernel TU") is True
    # Same dev_t typedef, but a glibc TU (identified by _IO_FILE): must not fire.
    assert _type_decls_define_libc_types(glibc, "struct _IO_FILE;") is False
    # Trailing attribute and multi-declarator forms must still be detected.
    assert _type_decls_define_libc_types(
        "typedef __u32 __kernel_dev_t;\n"
        "typedef __kernel_dev_t dev_t __attribute__((__aligned__(8)));\n", "k") is True
    assert _type_decls_define_libc_types(
        "typedef __u32 __kernel_dev_t;\ntypedef __kernel_dev_t dev_t, *pdev_t;\n", "k") is True


def _feas_fixture(tmp_path, source: str, ldv: bool = True):
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator

    path = tmp_path / "unit.c"
    path.write_text(source)
    parsed = parse_c_file(str(path))
    func = parsed.get_function_info("main")
    config = Config()
    # Explicit LDV mode: the strict target-identity rule and the solver fallback
    # are confined to it.
    config.preserve_ldv_property = ldv
    validator = CExValidator(config, None, None, HarnessGenerator(config=config))  # type: ignore[arg-type]
    return validator, func, parsed, config


def _run_feas(monkeypatch, validator, func, parsed, failing: str, target: "str | None" = None):
    import shutil as _shutil
    from bmc_agent import cex_validator as cv
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.spec import Spec

    monkeypatch.setattr(_shutil, "which", lambda _p: True)
    monkeypatch.setattr(
        cv, "run_cbmc",
        lambda **_kw: CBMCResult(verified=False, counterexamples=[
            Counterexample(failing_property=failing)]),
    )
    return validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(
            failing_property=failing if target is None else target),
        parsed_file=parsed,
    )


SIMPLE_MAIN = (
    "void reach_error(void);\n"
    "static int helper(int x) { return x + 1; }\n"
    "void main(void) { if (helper(1)) { reach_error(); } }\n"
)


def test_other_property_failure_is_not_confirmation(tmp_path, monkeypatch):
    """A different failed assertion must not confirm the target, and must not
    set _feas_errored (which suppresses dynamic validation)."""
    validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN)
    result = _run_feas(monkeypatch, validator, func, parsed,
                       "poll_wait.pointer_dereference.1",
                       target="reach_error.assertion.1")
    assert result is None
    assert validator._feas_errored is False
    assert validator._feas_evidence["verdict"] == "inconclusive"
    assert validator._feas_evidence["target_property"] == "reach_error.assertion.1"


def test_missing_target_identity_is_not_confirmation(tmp_path, monkeypatch):
    for failing in ("", "unknown"):
        validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN)
        result = _run_feas(monkeypatch, validator, func, parsed, failing)
        assert result is None, failing
        assert validator._feas_evidence["verdict"] == "inconclusive", failing


def test_target_property_failure_confirms(tmp_path, monkeypatch):
    validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN)
    result = _run_feas(monkeypatch, validator, func, parsed,
                       "reach_error.assertion.1")
    # Same name as the target -> confirmed (the mock reports exactly that one).
    assert result is None or result is True


def test_early_return_populates_evidence(tmp_path):
    validator, func, parsed, config = _feas_fixture(tmp_path, SIMPLE_MAIN)
    config.cbmc_path = "definitely-not-a-real-cbmc-binary"
    validator._feas_evidence = {"stale": True}
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.spec import Spec

    result = validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert result is None
    assert validator._feas_evidence.get("verdict") == "error"
    assert "stale" not in validator._feas_evidence


def test_no_local_callees_verdict_is_distinct(tmp_path, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _p: True)
    validator, func, parsed, _ = _feas_fixture(
        tmp_path, "void reach_error(void);\nvoid main(void) { reach_error(); }\n")
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.spec import Spec

    result = validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert result is None
    assert validator._feas_evidence.get("verdict") == "skipped_no_local_callees"


def test_non_ldv_mode_keeps_legacy_feasibility_semantics(tmp_path, monkeypatch):
    """Other profiles must not be changed by the LDV strictness.

    Legacy contract: any violation with real callee bodies -> feasible, even if
    the reported property is not the one we asked about.
    """
    validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN, ldv=False)
    result = _run_feas(monkeypatch, validator, func, parsed,
                       "poll_wait.pointer_dereference.1",
                       target="reach_error.assertion.1")
    assert result is True
    assert validator._feas_evidence["verdict"] == "feasible"


def test_non_ldv_mode_does_not_retry_cbmc_crash(tmp_path, monkeypatch):
    """The text-mode solver fallback is LDV-only; other profiles keep one attempt."""
    import shutil as _shutil
    from bmc_agent import cex_validator as cv
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.spec import Spec

    validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN, ldv=False)
    monkeypatch.setattr(_shutil, "which", lambda _p: True)
    calls = []

    def fake_run_cbmc(**kwargs):
        calls.append(kwargs)
        return CBMCResult(verified=False,
                          error="cbmc exited with code 134; Invariant check failed")

    monkeypatch.setattr(cv, "run_cbmc", fake_run_cbmc)
    from bmc_agent.cbmc import Counterexample
    result = validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert result is None
    assert len(calls) == 1          # no fallback attempt
    assert validator._feas_evidence["verdict"] == "error"


def test_ldv_mode_retries_cbmc_crash_once(tmp_path, monkeypatch):
    import shutil as _shutil
    from bmc_agent import cex_validator as cv
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.spec import Spec

    validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN, ldv=True)
    monkeypatch.setattr(_shutil, "which", lambda _p: True)
    calls = []

    def fake_run_cbmc(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return CBMCResult(verified=False,
                              error="cbmc exited with code 134; Invariant check failed")
        return CBMCResult(verified=False, counterexamples=[
            Counterexample(failing_property="reach_error.assertion.1")])

    monkeypatch.setattr(cv, "run_cbmc", fake_run_cbmc)
    result = validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert result is True
    assert len(calls) == 2
    assert calls[1]["plain_output"] is True
    assert calls[1]["use_refine"] is False


def test_ldv_mode_non_crash_error_makes_one_call(tmp_path, monkeypatch):
    import shutil as _shutil
    from bmc_agent import cex_validator as cv
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.spec import Spec

    validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN, ldv=True)
    monkeypatch.setattr(_shutil, "which", lambda _p: True)
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return CBMCResult(verified=False, error="cbmc exited with code 6; PARSING ERROR")

    monkeypatch.setattr(cv, "run_cbmc", fake)
    result = validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert result is None
    assert len(calls) == 1                     # no retry for a non-crash error
    assert validator._feas_evidence["verdict"] == "error"


def test_ldv_mode_crash_twice_does_not_loop(tmp_path, monkeypatch):
    import shutil as _shutil
    from bmc_agent import cex_validator as cv
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.spec import Spec

    validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN, ldv=True)
    monkeypatch.setattr(_shutil, "which", lambda _p: True)
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return CBMCResult(verified=False,
                          error="Invariant check failed (code 134)")

    monkeypatch.setattr(cv, "run_cbmc", fake)
    result = validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert result is None
    assert len(calls) == 2                     # exactly one retry, then stop
    assert validator._feas_evidence["verdict"] == "error"


def test_ldv_mode_retry_with_other_property_is_inconclusive(tmp_path, monkeypatch):
    import shutil as _shutil
    from bmc_agent import cex_validator as cv
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.spec import Spec

    validator, func, parsed, _ = _feas_fixture(tmp_path, SIMPLE_MAIN, ldv=True)
    monkeypatch.setattr(_shutil, "which", lambda _p: True)
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return CBMCResult(verified=False, error="Invariant check failed")
        return CBMCResult(verified=False, counterexamples=[
            Counterexample(failing_property="poll_wait.pointer_dereference.1")])

    monkeypatch.setattr(cv, "run_cbmc", fake)
    result = validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert result is None
    assert len(calls) == 2
    assert validator._feas_evidence["verdict"] == "inconclusive"


def test_internal_crash_predicate_is_shared():
    from bmc_agent.cbmc import is_cbmc_internal_crash

    assert is_cbmc_internal_crash("--- begin invariant violation report ---")
    assert is_cbmc_internal_crash("Invariant check failed")
    assert is_cbmc_internal_crash("cbmc exited with code 134")
    assert not is_cbmc_internal_crash("cbmc exited with code 6; PARSING ERROR")
    assert not is_cbmc_internal_crash("")


def test_definition_present_in_decls_detects_duplicate_bodies():
    from bmc_agent.harness_generator import _definition_present_in_decls

    source = (
        "static int p54u_upload_firmware_3887(struct ieee80211_hw *dev )\n"
        "{ return 0; }\n"
        "static int other(struct x *x);\n"
    )
    # Full definition -> would be emitted twice.
    assert _definition_present_in_decls("p54u_upload_firmware_3887", source)
    # Forward declaration only -> inlining is required and safe.
    assert not _definition_present_in_decls("other", source)
    assert not _definition_present_in_decls("missing", source)
    assert not _definition_present_in_decls("", source)


def test_main_feasibility_has_single_definition_in_both_modes(tmp_path):
    """The single-main case must be unconditional.

    Gating it on the LDV flag was tried and reverted: with the flag off, the
    emitted ``func_def`` already defines main and the appended wrapper defines it
    again, so every non-LDV profile's query became "function body 'main' defined
    twice".
    """
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.spec import Spec

    src = tmp_path / "entry.c"
    src.write_text(
        "void reach_error(void);\n"
        "static int helper(int x) { return x + 1; }\n"
        "void main(void) { if (helper(1)) { reach_error(); } }\n"
    )
    parsed = parse_c_file(str(src))
    func = parsed.get_function_info("main")
    spec = Spec(function_name="main", precondition="true", postcondition="true")
    cex = Counterexample(failing_property="reach_error.assertion.1")

    for flag in (True, False):
        config = Config()
        config.preserve_ldv_property = flag
        harness = HarnessGenerator(config=config).generate_feasibility_harness(
            func=func, spec=spec, counterexample=cex, parsed_file=parsed)
        lines = harness.splitlines()
        definitions = sum(1 for i, line in enumerate(lines[:-1])
                          if line.startswith("void main(void)") and lines[i + 1].startswith("{"))
        assert definitions == 1, (flag, harness[-300:])
        assert "No wrapper main" in harness


def test_summary_reports_solver_errors_instead_of_no_bugs(tmp_path):
    """A crashed solver query must not be summarised as "No bugs found"."""
    import json as _json
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bug_reporter import BugReporter

    base = tmp_path / "artifacts"
    (base / "agdrv" / "main" / "bug_reports").mkdir(parents=True)
    (base / "agdrv" / "main" / "bug_reports" / "reach_error.assertion.1.json").write_text(
        _json.dumps({"report": {"function_name": "main",
                                "error": "--- begin invariant violation report ---"}}))
    reporter = BugReporter(ArtifactStore(base))
    summary = reporter.generate_summary("agdrv")
    assert "No bugs found" not in summary
    assert "solver/tool error" in summary
    assert "invariant violation" in summary

    # A clean run still says "No bugs found".
    clean = tmp_path / "clean"
    (clean / "agdrv" / "main").mkdir(parents=True)
    assert BugReporter(ArtifactStore(clean)).generate_summary("agdrv") == \
        "No bugs found for driver 'agdrv'."


def test_feasibility_skips_body_already_in_declarations(tmp_path):
    """Microtek regression: the feasibility harness re-emitted a retained body.

    When excision is skipped the definition stays in the declaration section; the
    feasibility emitter must then skip its own copy or CBMC sees
    "function body 'mts_usb_probe' defined twice".
    """
    src = tmp_path / "unit.c"
    src.write_text(
        "static int kept(int x)\n{ return x + 1; }\n"
        "void main(void) { if (kept(1)) { } }\n"
    )
    parsed = parse_c_file(str(src))
    func = parsed.get_function_info("main")
    from bmc_agent.spec import Spec
    generator = HarnessGenerator(config=Config())
    harness = generator.generate_feasibility_harness(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    lines = harness.splitlines()
    definitions = sum(1 for i, line in enumerate(lines[:-1])
                      if "kept(" in line and line.rstrip().endswith(")")
                      and lines[i + 1].startswith("{"))
    assert definitions <= 1


def test_solver_errors_are_scoped_to_the_driver(tmp_path):
    """A nonexistent driver must not inherit another driver's errors."""
    import json as _json
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bug_reporter import BugReporter

    base = tmp_path / "artifacts"
    (base / "otherdrv" / "main" / "bug_reports").mkdir(parents=True)
    (base / "otherdrv" / "main" / "bug_reports" / "p.json").write_text(
        _json.dumps({"report": {"function_name": "f", "error": "invariant violation"}}))
    reporter = BugReporter(ArtifactStore(base))
    assert reporter._solver_errors("otherdrv")
    assert reporter._solver_errors("nosuchdriver") == []
    assert "No bugs found" in reporter.generate_summary("nosuchdriver")


def test_solver_errors_tolerate_malformed_artifacts(tmp_path):
    """Malformed or non-UTF8 artifacts must not crash the summary."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bug_reporter import BugReporter

    base = tmp_path / "artifacts"
    d = base / "agdrv" / "main" / "bug_reports"
    d.mkdir(parents=True)
    (d / "string_payload.json").write_text('{"report": "not-an-object"}')
    (d / "binary.json").write_bytes(b"\xff\xfe\x00broken")
    reporter = BugReporter(ArtifactStore(base))
    assert reporter._solver_errors("agdrv") == []
    assert reporter.generate_summary("agdrv") == "No bugs found for driver 'agdrv'."


def test_body_match_is_anchored_to_its_own_signature():
    """An identical body under another name must not suppress real inlining."""
    from bmc_agent.harness_generator import _definition_present_in_decls

    same_body_other_name = "int other(void)\n{ return 42; }\n"
    assert not _definition_present_in_decls("mine", same_body_other_name, "{ return 42; }")
    own = "static int mine(void)\n{ return 42; }\n"
    assert _definition_present_in_decls("mine", own, "{ return 42; }")


def test_body_binds_to_its_own_definition_not_a_same_name_prototype():
    """Confirmed counterexample: a prototype of `foo` must not bind another
    function's identical body to `foo` and suppress inlining of `foo`."""
    from bmc_agent.harness_generator import _definition_present_in_decls

    text = "int foo(void);\nint bar(void)\n{ return 42; }\n"
    body = "{ return 42; }"
    assert _definition_present_in_decls("foo", text, body) is False
    assert _definition_present_in_decls("bar", text, body) is True


def test_body_binding_survives_a_long_parameter_list():
    """The >400-character failure mode is a long PARAMETER LIST, not a long
    prefix: the name sits far from the body's opening brace."""
    from bmc_agent.harness_generator import _definition_present_in_decls

    params = ", ".join("int p%d" % i for i in range(120))
    assert len(params) > 400
    text = "static int target(%s)\n{ return 7; }\n" % params
    assert _definition_present_in_decls("target", text, "{ return 7; }")


def test_harness_generation_failure_is_persisted_and_summarised(tmp_path, monkeypatch):
    """Trigger the PRODUCTION harness-generation exception path.

    The failure must leave a real artifact and must not be summarised as
    "No bugs found". The test drives BMCEngine.check_function with a generator
    that raises, rather than hand-writing a report file.
    """
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.config import Config
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec

    src = tmp_path / "unit.c"
    src.write_text(
        "void reach_error(void);\n"
        "static int helper(int x) { return x + 1; }\n"
        "void main(void) { if (helper(1)) { reach_error(); } }\n"
    )
    parsed = parse_c_file(str(src))
    func = parsed.get_function_info("main")
    store = ArtifactStore(tmp_path / "artifacts")
    config = Config()
    engine = BMCEngine(config, store)

    def boom(*_args, **_kwargs):
        raise RuntimeError("harness emitter exploded")

    monkeypatch.setattr(HarnessGenerator, "generate_harness", boom)

    verdict = engine.check_function(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        parsed_file=parsed,
        driver_name="agdrv",
    )

    assert verdict.verified is False
    assert "Harness generation failed" in (verdict.error or "")
    report_path = tmp_path / "artifacts" / "agdrv" / "main" / "bug_report.json"
    assert report_path.is_file(), "the failure verdict was not persisted"
    saved = json.loads(report_path.read_text())
    saved_body = saved.get("report", saved)          # artifacts nest the verdict
    assert "Harness generation failed" in str(saved_body.get("error") or "")

    summary = BugReporter(store).generate_summary("agdrv")
    assert "No bugs found" not in summary
    assert "solver/tool error" in summary
    assert "harness emitter exploded" in summary


def test_unresolved_candidates_are_summarised(tmp_path):
    import json as _json
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bug_reporter import BugReporter

    base = tmp_path / "artifacts" / "agdrv" / "main"
    (base / "classifications").mkdir(parents=True)
    (base / "classifications" / "reach_error.assertion.1.json").write_text(_json.dumps({
        "classification": {"function_name": "main", "outcome": "unresolved",
                           "reasoning": "feasibility did not confirm the target"}}))
    summary = BugReporter(ArtifactStore(tmp_path / "artifacts")).generate_summary("agdrv")
    assert "No bugs found" not in summary
    assert "UNRESOLVED" in summary
    assert "reach_error.assertion.1" in summary


def test_non_report_json_with_error_is_not_a_solver_error(tmp_path):
    import json as _json
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bug_reporter import BugReporter

    d = tmp_path / "artifacts" / "agdrv" / "main" / "bug_reports"
    d.mkdir(parents=True)
    (d / "unrelated.json").write_text(_json.dumps({"error": "some unrelated failure"}))
    reporter = BugReporter(ArtifactStore(tmp_path / "artifacts"))
    assert reporter._solver_errors("agdrv") == []
    assert reporter.generate_summary("agdrv") == "No bugs found for driver 'agdrv'."


def test_feasibility_inherits_effective_unwind_in_ldv_mode(tmp_path, monkeypatch):
    """megaraid_mm: the main query ran at unwind 2 (agent flag) while the
    feasibility recheck fell back to the global default 1 and reported
    "verified". In LDV mode the recheck must use the effective unwind."""
    import shutil as _shutil
    from bmc_agent import cex_validator as cv
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.spec import Spec

    validator, func, parsed, config = _feas_fixture(tmp_path, SIMPLE_MAIN, ldv=True)
    config.cbmc_unwind = 1
    config.effective_unwind_by_function = {"main": 2}
    monkeypatch.setattr(_shutil, "which", lambda _p: True)

    seen = []

    def fake(**kwargs):
        seen.append(kwargs.get("unwind"))
        return CBMCResult(verified=False, counterexamples=[
            Counterexample(failing_property="reach_error.assertion.1")])

    monkeypatch.setattr(cv, "run_cbmc", fake)
    result = validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert result is True
    assert seen == [2], seen


def test_feasibility_keeps_global_unwind_outside_ldv(tmp_path, monkeypatch):
    """Other profiles keep the previous behaviour (global default)."""
    import shutil as _shutil
    from bmc_agent import cex_validator as cv
    from bmc_agent.cbmc import CBMCResult, Counterexample
    from bmc_agent.spec import Spec

    validator, func, parsed, config = _feas_fixture(tmp_path, SIMPLE_MAIN, ldv=False)
    config.cbmc_unwind = 1
    config.effective_unwind_by_function = {"main": 2}
    monkeypatch.setattr(_shutil, "which", lambda _p: True)

    seen = []

    def fake(**kwargs):
        seen.append(kwargs.get("unwind"))
        return CBMCResult(verified=False, counterexamples=[
            Counterexample(failing_property="reach_error.assertion.1")])

    monkeypatch.setattr(cv, "run_cbmc", fake)
    validator._check_cex_feasibility(
        func=func,
        spec=Spec(function_name="main", precondition="true", postcondition="true"),
        counterexample=Counterexample(failing_property="reach_error.assertion.1"),
        parsed_file=parsed,
    )
    assert seen == [1], seen


def test_engine_records_effective_unwind_for_ldv_only(tmp_path, monkeypatch):
    """The engine half: check_function records the unwind it actually used, and
    only in LDV mode. The solver is stubbed so no CBMC runs."""
    from bmc_agent import bmc_engine as be
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.spec import Spec

    src = tmp_path / "unit.c"
    src.write_text(SIMPLE_MAIN)
    parsed = parse_c_file(str(src))
    func = parsed.get_function_info("main")
    spec = Spec(function_name="main", precondition="true", postcondition="true")

    class Selector:
        unwind_override = 3
        timeout_override = None
        def any_enabled(self): return True
        def enabled_flags(self): return ["--unwind 3"]

    seen = []

    def fake_run_cbmc(**kw):
        seen.append(kw.get("unwind"))
        return CBMCResult(verified=True, counterexamples=[])

    monkeypatch.setattr(be, "run_cbmc", fake_run_cbmc)

    for ldv, expected in ((True, {"main": 3}), (False, {})):
        seen.clear()
        config = Config()
        config.preserve_ldv_property = ldv
        engine = be.BMCEngine(config, ArtifactStore(tmp_path / f"art_{ldv}"))
        engine.check_function(func=func, spec=spec, parsed_file=parsed,
                              driver_name="agdrv", all_funcs=parsed.functions,
                              flag_selection=Selector())
        assert config.effective_unwind_by_function == expected, (ldv, config.effective_unwind_by_function)
        # the recorded value must be the one the solver actually received
        assert seen, "the stubbed solver was never called"
        if expected:
            assert seen == [3], seen


def test_engine_records_the_floor_adjusted_unwind(tmp_path, monkeypatch):
    """Recording must happen AFTER the SV-COMP floor, not before it."""
    from bmc_agent import bmc_engine as be
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.cbmc import CBMCResult
    from bmc_agent.spec import Spec

    src = tmp_path / "unit.c"
    src.write_text(SIMPLE_MAIN)
    parsed = parse_c_file(str(src))
    func = parsed.get_function_info("main")

    class Selector:
        unwind_override = 2
        timeout_override = None
        def any_enabled(self): return True
        def enabled_flags(self): return ["--unwind 2"]

    seen = []

    def fake_run_cbmc(**kw):
        seen.append(kw.get("unwind"))
        return CBMCResult(verified=True, counterexamples=[])

    monkeypatch.setattr(be, "run_cbmc", fake_run_cbmc)
    monkeypatch.setenv("SVCOMP_PROP", "unreach")
    monkeypatch.setenv("SVCOMP_UNWIND", "64")
    monkeypatch.setenv("SVCOMP_TIMEOUT", "0")
    monkeypatch.delenv("BMC_UNWIND_CAP", raising=False)

    config = Config()
    config.preserve_ldv_property = True
    engine = be.BMCEngine(config, ArtifactStore(tmp_path / "art_floor"))
    engine.check_function(func=func, spec=Spec(function_name="main",
                                               precondition="true",
                                               postcondition="true"),
                          parsed_file=parsed, driver_name="agdrv",
                          all_funcs=parsed.functions, flag_selection=Selector())
    assert seen == [64], seen
    assert config.effective_unwind_by_function == {"main": 64}
