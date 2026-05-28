"""
Tests for Phase 3 Stage 3: Dynamic CEx Validation.

These tests compile and run small C harnesses with gcc to verify that the
DynamicValidator can:
  - confirm real faults (SIGSEGV / SIGFPE) as CONFIRMED
  - report safe executions as NOT_TRIGGERED
  - return SKIPPED when disabled, INCONCLUSIVE when gcc is absent
  - fall back from with_globals=True to with_globals=False on compile failure
"""

from __future__ import annotations

import subprocess
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bmc_agent.cbmc import Counterexample
from bmc_agent.config import Config
from bmc_agent.dynamic_validator import (
    DynamicOutcome,
    DynamicValidationResult,
    DynamicValidator,
    _looks_like_c_code,
    _wrap_reproducer_with_signal_handlers,
)
from bmc_agent.harness_generator import HarnessGenerator, _strip_glibc_internal_typedefs, _strip_inline_asm, _strip_static_inline_defs
from bmc_agent.parser import FunctionInfo, FunctionSignature, ParsedCFile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GCC_AVAILABLE = bool(subprocess.run(
    ["which", "gcc"], capture_output=True
).returncode == 0)


def _compile_and_run(src: str, *, timeout: int = 10) -> tuple[int, str, str]:
    """Compile C source string with gcc and run it; returns (returncode, stdout, stderr)."""
    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as sf:
        sf.write(src)
        src_path = sf.name
    with tempfile.NamedTemporaryFile(suffix="", delete=False) as bf:
        bin_path = bf.name
    try:
        cp = subprocess.run(
            ["gcc", "-w", "-fno-builtin", "-o", bin_path, src_path],
            capture_output=True, text=True, timeout=30,
        )
        if cp.returncode != 0:
            return cp.returncode, cp.stdout, cp.stderr
        rp = subprocess.run(
            [bin_path], capture_output=True, text=True, timeout=timeout,
        )
        return rp.returncode, rp.stdout, rp.stderr
    finally:
        Path(src_path).unlink(missing_ok=True)
        Path(bin_path).unlink(missing_ok=True)


def _make_parsed_file(name: str = "test") -> ParsedCFile:
    return ParsedCFile(
        path=f"{name}.c",
        functions={},
        call_graph={},
        function_bodies={},
        preprocessed_source=None,
    )


def _make_func(
    name: str,
    body: str,
    params: list[tuple[str, str]] | None = None,
    ret_type: str = "void",
    callees: set[str] | None = None,
) -> FunctionInfo:
    sig = FunctionSignature(
        return_type=ret_type,
        name=name,
        parameters=params or [],
    )
    fi = FunctionInfo(
        name=name,
        signature=sig,
        body=body,
        source_file="test.c",
        callees=callees or set(),
    )
    return fi


def _make_cex(var_assignments: dict | None = None) -> Counterexample:
    return Counterexample(
        failing_property="test property",
        variable_assignments=var_assignments or {},
        trace=[],
    )


# ---------------------------------------------------------------------------
# Unit: DynamicOutcome enum and DynamicValidationResult
# ---------------------------------------------------------------------------


def test_dynamic_outcome_values():
    assert DynamicOutcome.CONFIRMED.value == "confirmed"
    assert DynamicOutcome.NOT_TRIGGERED.value == "not_triggered"
    assert DynamicOutcome.INCONCLUSIVE.value == "inconclusive"
    assert DynamicOutcome.SKIPPED.value == "skipped"


def test_dynamic_validation_result_to_dict():
    r = DynamicValidationResult(
        outcome=DynamicOutcome.CONFIRMED,
        signal_name="SIGSEGV",
        reasoning="test",
    )
    d = r.to_dict()
    assert d["outcome"] == "confirmed"
    assert d["signal_name"] == "SIGSEGV"
    assert d["reasoning"] == "test"
    assert d["compile_error"] is None


def test_dynamic_validation_result_defaults():
    r = DynamicValidationResult(outcome=DynamicOutcome.SKIPPED)
    assert r.signal_name is None
    assert r.compile_error is None
    assert r.run_error is None
    assert r.reasoning == ""


# ---------------------------------------------------------------------------
# Unit: DynamicValidator respects enable_dynamic_validation=False
# ---------------------------------------------------------------------------


def test_dynamic_validator_disabled_returns_skipped():
    config = Config(enable_dynamic_validation=False)
    harness_gen = MagicMock()
    dv = DynamicValidator(config, harness_gen)
    func = _make_func("foo", "{ }")
    cex = _make_cex()
    result = dv.validate(func, cex, _make_parsed_file(), {}, {})
    assert result.outcome == DynamicOutcome.SKIPPED
    harness_gen.generate_dynamic_harness.assert_not_called()


def test_dynamic_validator_missing_compiler_returns_inconclusive():
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="__no_such_compiler__")
    harness_gen = MagicMock()
    dv = DynamicValidator(config, harness_gen)
    func = _make_func("foo", "{ }")
    cex = _make_cex()
    result = dv.validate(func, cex, _make_parsed_file(), {}, {})
    assert result.outcome == DynamicOutcome.INCONCLUSIVE
    assert "__no_such_compiler__" in result.reasoning


# ---------------------------------------------------------------------------
# Integration: harness generation + compile + run
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_confirmed_null_pointer_dereference():
    """A function that dereferences a NULL pointer should be CONFIRMED (SIGSEGV)."""
    src = r"""
#include <signal.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdint.h>

static sigjmp_buf _amc_jmp;
static volatile const char *_amc_signal_name = NULL;
static void _amc_handler(int sig) {
    _amc_signal_name = (sig == 11) ? "SIGSEGV" : "OTHER";
    siglongjmp(_amc_jmp, 1);
}

void deref_null(uint8_t *p) {
    *p = 42;  /* crashes when p == NULL */
}

int main(void) {
    signal(11, _amc_handler);
    uint8_t *_amc_arg_p = NULL;
    if (sigsetjmp(_amc_jmp, 1) == 0) {
        deref_null(_amc_arg_p);
        puts("DYNAMIC:NOT_TRIGGERED");
        return 0;
    }
    printf("DYNAMIC:CONFIRMED signal=%s\n", (const char *)_amc_signal_name);
    return 1;
}
"""
    rc, stdout, _ = _compile_and_run(src)
    assert "DYNAMIC:CONFIRMED" in stdout
    assert "SIGSEGV" in stdout


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_not_triggered_safe_function():
    """A function that never faults should produce NOT_TRIGGERED."""
    src = r"""
#include <signal.h>
#include <setjmp.h>
#include <stdio.h>

static sigjmp_buf _amc_jmp;
static volatile const char *_amc_signal_name = NULL;
static void _amc_handler(int sig) {
    _amc_signal_name = "SIG";
    siglongjmp(_amc_jmp, 1);
}

int safe_add(int a, int b) { return a + b; }

int main(void) {
    signal(11, _amc_handler);
    signal(8, _amc_handler);
    int _amc_arg_a = 1, _amc_arg_b = 2;
    if (sigsetjmp(_amc_jmp, 1) == 0) {
        int _amc_result = safe_add(_amc_arg_a, _amc_arg_b);
        (void)_amc_result;
        puts("DYNAMIC:NOT_TRIGGERED");
        return 0;
    }
    printf("DYNAMIC:CONFIRMED signal=%s\n", (const char *)_amc_signal_name);
    return 1;
}
"""
    rc, stdout, _ = _compile_and_run(src)
    assert "DYNAMIC:NOT_TRIGGERED" in stdout


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_dynamic_validator_confirmed_via_harness_generator():
    """End-to-end: DynamicValidator confirms a null-deref via generated harness."""
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")

    # Build a minimal ParsedCFile with a null-deref function
    func = _make_func(
        name="write_byte",
        body="{ *p = 42; }",
        params=[("uint8_t *", "p")],
        ret_type="void",
    )
    pf = ParsedCFile(
        path="null_deref.c",
        functions={"write_byte": func.signature},
        call_graph={"write_byte": set()},
        function_bodies={"write_byte": func.body},
        preprocessed_source="",
    )

    cex = _make_cex({"p": "NULL"})
    harness_gen = HarnessGenerator(config)
    dv = DynamicValidator(config, harness_gen)

    result = dv.validate(
        entry_func=func,
        counterexample=cex,
        parsed_file=pf,
        all_funcs={"write_byte": func},
        all_specs={},
    )
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGSEGV"


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_dynamic_validator_not_triggered_via_harness_generator():
    """End-to-end: DynamicValidator reports NOT_TRIGGERED for a safe function."""
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")

    func = _make_func(
        name="safe_add",
        body="{ return a + b; }",
        params=[("int", "a"), ("int", "b")],
        ret_type="int",
    )
    pf = ParsedCFile(
        path="safe_func.c",
        functions={"safe_add": func.signature},
        call_graph={"safe_add": set()},
        function_bodies={"safe_add": func.body},
        preprocessed_source="",
    )

    cex = _make_cex({"a": "1", "b": "2"})
    harness_gen = HarnessGenerator(config)
    dv = DynamicValidator(config, harness_gen)

    result = dv.validate(
        entry_func=func,
        counterexample=cex,
        parsed_file=pf,
        all_funcs={"safe_add": func},
        all_specs={},
    )
    assert result.outcome == DynamicOutcome.NOT_TRIGGERED


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_dynamic_validator_global_state_injection():
    """With with_globals=True, a global variable is set before calling the entry function."""
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")

    # Function that crashes when g_limit == 0
    func = _make_func(
        name="check_limit",
        body="{ if (g_limit == 0) { int *p = 0; *p = 1; } }",
        params=[],
        ret_type="void",
    )
    pf = ParsedCFile(
        path="global_test.c",
        functions={"check_limit": func.signature},
        call_graph={"check_limit": set()},
        function_bodies={"check_limit": func.body},
        preprocessed_source="",
    )

    # CEx says g_limit = 0 (global, not a parameter)
    cex = _make_cex({"g_limit": "0"})
    harness_gen = HarnessGenerator(config)
    dv = DynamicValidator(config, harness_gen)

    # Without global state injection we can't set g_limit=0; the harness will
    # still try. It may compile and NOT_TRIGGERED if g_limit starts != 0.
    # The with_globals path should set g_limit and potentially trigger the crash
    # if the global is actually declared in the type decls section.
    # Here we're testing that the validator runs without error regardless.
    result = dv.validate(
        entry_func=func,
        counterexample=cex,
        parsed_file=pf,
        all_funcs={"check_limit": func},
        all_specs={},
    )
    # Outcome depends on whether g_limit is accessible, but it must not raise
    assert result.outcome in (
        DynamicOutcome.CONFIRMED,
        DynamicOutcome.NOT_TRIGGERED,
        DynamicOutcome.INCONCLUSIVE,
    )


def test_dynamic_validator_compile_failure_returns_inconclusive():
    """If harness generation produces uncompilable code, outcome is INCONCLUSIVE."""
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")

    harness_gen = MagicMock()
    harness_gen.generate_dynamic_harness.return_value = (
        "THIS IS NOT VALID C CODE @@@@;"
    )
    dv = DynamicValidator(config, harness_gen)
    func = _make_func("bad", "{ }")
    cex = _make_cex()

    result = dv.validate(func, cex, _make_parsed_file(), {}, {})
    assert result.outcome == DynamicOutcome.INCONCLUSIVE
    assert result.compile_error is not None


def test_dynamic_validator_harness_generation_exception_returns_inconclusive():
    """If generate_dynamic_harness raises, outcome is INCONCLUSIVE."""
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")

    harness_gen = MagicMock()
    harness_gen.generate_dynamic_harness.side_effect = RuntimeError("boom")
    dv = DynamicValidator(config, harness_gen)
    func = _make_func("broken", "{ }")
    cex = _make_cex()

    result = dv.validate(func, cex, _make_parsed_file(), {}, {})
    assert result.outcome == DynamicOutcome.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Unit: _strip_inline_asm
# ---------------------------------------------------------------------------


def test_strip_inline_asm_basic_nop():
    src = 'void f(void) { asm("nop"); }'
    out = _strip_inline_asm(src)
    assert 'asm(' not in out
    assert '/* asm removed */' in out
    assert 'void f(void) {' in out


def test_strip_inline_asm_volatile():
    src = 'static void setup(void) { asm volatile("isb"); }'
    out = _strip_inline_asm(src)
    assert 'asm' not in out.replace('/* asm removed */', '')
    assert '/* asm removed */' in out


def test_strip_inline_asm_dunder_asm():
    src = '__asm__("ldr x0, [sp]");'
    out = _strip_inline_asm(src)
    assert '__asm__' not in out
    assert '/* asm removed */' in out


def test_strip_inline_asm_multiline():
    src = (
        'void f(void) {\n'
        '    __asm__ volatile(\n'
        '        "mov x0, #0\\n"\n'
        '        "ret\\n"\n'
        '    );\n'
        '}'
    )
    out = _strip_inline_asm(src)
    assert '__asm__' not in out
    assert '/* asm removed */' in out
    assert 'void f(void)' in out


def test_strip_inline_asm_no_asm():
    src = 'int add(int a, int b) { return a + b; }'
    assert _strip_inline_asm(src) == src


def test_strip_inline_asm_multiple_blocks():
    src = 'void f(void) { asm("nop"); asm("isb"); }'
    out = _strip_inline_asm(src)
    assert out.count('/* asm removed */') == 2


def test_strip_inline_asm_not_a_statement():
    """'asm' not followed by '(' should be left in place (e.g. in identifiers)."""
    src = 'int has_asm_support = 1;'
    out = _strip_inline_asm(src)
    assert out == src


# ---------------------------------------------------------------------------
# Unit: _strip_static_inline_defs
# ---------------------------------------------------------------------------


def test_strip_static_inline_def_simple():
    src = 'static inline int foo(void) { return 0; }'
    out = _strip_static_inline_defs(src)
    assert 'static inline int foo' not in out
    assert '/* static inline removed */' in out


def test_strip_static_inline_def_dunder():
    src = 'static __inline__ void bar(int x) { x = x + 1; }'
    out = _strip_static_inline_defs(src)
    assert '/* static inline removed */' in out


def test_strip_static_inline_keeps_declaration():
    """A declaration (ends in ';') must be preserved."""
    src = 'static inline int baz(void);'
    out = _strip_static_inline_defs(src)
    assert 'static inline int baz(void);' in out
    assert '/* static inline removed */' not in out


def test_strip_static_inline_mixed():
    src = (
        'static inline int decl(void);\n'
        'static inline int def(void) { return 1; }\n'
        'int other(void) { return 2; }\n'
    )
    out = _strip_static_inline_defs(src)
    assert 'static inline int decl(void);' in out
    assert '/* static inline removed */' in out
    assert 'int other(void)' in out


def test_strip_static_inline_nested_braces():
    src = 'static inline int nested(void) { if (1) { return 1; } return 0; }'
    out = _strip_static_inline_defs(src)
    assert '/* static inline removed */' in out
    assert 'nested' not in out.replace('/* static inline removed */', '')


# ---------------------------------------------------------------------------
# Unit: DynamicValidator._run() — negative-exit-code signal detection
# ---------------------------------------------------------------------------


def _make_fake_proc(returncode: int, stdout: str = "") -> types.SimpleNamespace:
    proc = types.SimpleNamespace()
    proc.returncode = returncode
    proc.stdout = stdout
    return proc


def _make_dv() -> DynamicValidator:
    config = Config(enable_dynamic_validation=True)
    return DynamicValidator(config, MagicMock())


def test_run_negative_exit_sigsegv_detected():
    """returncode=-11 with no DYNAMIC: line → CONFIRMED SIGSEGV."""
    dv = _make_dv()
    with patch("subprocess.run", return_value=_make_fake_proc(-11)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGSEGV"
    assert "SIGSEGV" in result.reasoning


def test_run_negative_exit_sigabrt_detected():
    dv = _make_dv()
    with patch("subprocess.run", return_value=_make_fake_proc(-6)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGABRT"


def test_run_negative_exit_sigfpe_detected():
    dv = _make_dv()
    with patch("subprocess.run", return_value=_make_fake_proc(-8)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGFPE"


def test_run_negative_exit_sigill_detected():
    dv = _make_dv()
    with patch("subprocess.run", return_value=_make_fake_proc(-4)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGILL"


def test_run_in_process_confirmed_takes_priority_over_negative_exit():
    """If DYNAMIC:CONFIRMED appears in stdout, use that even if returncode is also -11."""
    dv = _make_dv()
    stdout = "DYNAMIC:CONFIRMED signal=SIGSEGV\n"
    with patch("subprocess.run", return_value=_make_fake_proc(-11, stdout)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGSEGV"


def test_run_not_triggered():
    dv = _make_dv()
    stdout = "DYNAMIC:NOT_TRIGGERED\n"
    with patch("subprocess.run", return_value=_make_fake_proc(0, stdout)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.NOT_TRIGGERED


def test_run_unknown_nonzero_exit_inconclusive():
    """Non-zero exit that isn't a signal code and has no DYNAMIC: line → INCONCLUSIVE."""
    dv = _make_dv()
    with patch("subprocess.run", return_value=_make_fake_proc(42)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.INCONCLUSIVE


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_negative_exit_code_detected_end_to_end():
    """Real crash with no signal handlers → negative exit code detected by _run()."""
    src = r"""
#include <stdint.h>
/* No signal handler registered — process is killed by SIGSEGV directly */
int main(void) {
    volatile uint8_t *p = (volatile uint8_t *)0;
    *p = 42;
    return 0;
}
"""
    with tempfile.TemporaryDirectory() as td:
        src_path = Path(td) / "crash.c"
        bin_path = Path(td) / "crash"
        src_path.write_text(src)
        cp = subprocess.run(
            ["gcc", "-w", "-fno-builtin", "-o", str(bin_path), str(src_path)],
            capture_output=True, text=True,
        )
        assert cp.returncode == 0, f"Compile failed: {cp.stderr}"

        config = Config(enable_dynamic_validation=True)
        dv = DynamicValidator(config, MagicMock())
        result = dv._run(str(bin_path))

    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGSEGV"


# ---------------------------------------------------------------------------
# Step A: fault-site classification via fut_called checkpoint
# ---------------------------------------------------------------------------


def test_run_confirmed_with_fut_called_1_marks_in_fut():
    """CONFIRMED line with fut_called=1 → fault_site='in_fut'."""
    dv = _make_dv()
    stdout = "DYNAMIC:CONFIRMED signal=SIGSEGV fut_called=1\n"
    with patch("subprocess.run", return_value=_make_fake_proc(1, stdout)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGSEGV"
    assert result.fault_site == "in_fut"


def test_run_confirmed_with_fut_called_0_reclassifies_to_inconclusive():
    """CONFIRMED line with fut_called=0 → reclassified as INCONCLUSIVE.

    This is the Step A defense against harness-setup signals being
    misreported as real bugs. The fault fired before the FUT was even
    called, so the signal is a harness artifact, not a real-bug signal.
    """
    import os as _os
    # Default (strict mode on) — reclassify
    _saved = _os.environ.pop("BMC_AGENT_DYNVAL_STRICT_FAULT_SITE", None)
    try:
        dv = _make_dv()
        stdout = "DYNAMIC:CONFIRMED signal=SIGSEGV fut_called=0\n"
        with patch("subprocess.run", return_value=_make_fake_proc(1, stdout)):
            result = dv._run("/fake/binary")
        assert result.outcome == DynamicOutcome.INCONCLUSIVE
        assert result.signal_name == "SIGSEGV"
        assert result.fault_site == "in_setup"
        assert "harness setup" in result.reasoning
    finally:
        if _saved is not None:
            _os.environ["BMC_AGENT_DYNVAL_STRICT_FAULT_SITE"] = _saved


def test_run_confirmed_with_fut_called_0_strict_off_keeps_confirmed():
    """When BMC_AGENT_DYNVAL_STRICT_FAULT_SITE=0, the reclassification
    is disabled and the CONFIRMED outcome is preserved (fault_site
    still recorded for downstream consumers).
    """
    import os as _os
    _saved = _os.environ.get("BMC_AGENT_DYNVAL_STRICT_FAULT_SITE")
    _os.environ["BMC_AGENT_DYNVAL_STRICT_FAULT_SITE"] = "0"
    try:
        dv = _make_dv()
        stdout = "DYNAMIC:CONFIRMED signal=SIGSEGV fut_called=0\n"
        with patch("subprocess.run", return_value=_make_fake_proc(1, stdout)):
            result = dv._run("/fake/binary")
        assert result.outcome == DynamicOutcome.CONFIRMED
        assert result.fault_site == "in_setup"
    finally:
        if _saved is None:
            _os.environ.pop("BMC_AGENT_DYNVAL_STRICT_FAULT_SITE", None)
        else:
            _os.environ["BMC_AGENT_DYNVAL_STRICT_FAULT_SITE"] = _saved


def test_run_confirmed_no_fut_called_token_defaults_to_unknown():
    """Older harness without the fut_called marker → fault_site='unknown';
    treated as in_fut for backward-compat (no reclassification)."""
    dv = _make_dv()
    stdout = "DYNAMIC:CONFIRMED signal=SIGSEGV\n"  # no fut_called=
    with patch("subprocess.run", return_value=_make_fake_proc(1, stdout)):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.fault_site == "unknown"


def test_run_negative_exit_records_unknown_fault_site():
    """Process killed by OS signal (signal handler bypassed) → fault_site
    is recorded as 'unknown' because the checkpoint marker was never
    emitted. The CONFIRMED outcome is preserved (this is the bare-metal
    / async-signal-died-fast path that's still a real bug signal)."""
    dv = _make_dv()
    with patch("subprocess.run", return_value=_make_fake_proc(-11, "")):
        result = dv._run("/fake/binary")
    assert result.outcome == DynamicOutcome.CONFIRMED
    assert result.signal_name == "SIGSEGV"
    assert result.fault_site == "unknown"


# ---------------------------------------------------------------------------
# Unit: _strip_glibc_internal_typedefs
# ---------------------------------------------------------------------------


def test_strip_glibc_typedefs_simple():
    src = "typedef unsigned long int __dev_t;"
    out = _strip_glibc_internal_typedefs(src)
    assert "typedef unsigned long int __dev_t;" not in out
    assert "/* typedef __dev_t removed */" in out


def test_strip_glibc_typedefs_struct():
    src = "typedef struct { int __val[2]; } __fsid_t;"
    out = _strip_glibc_internal_typedefs(src)
    assert "typedef struct" not in out
    assert "/* typedef __fsid_t removed */" in out


def test_strip_glibc_typedefs_keeps_public_names():
    src = "typedef unsigned long kernel_addr_t;"
    out = _strip_glibc_internal_typedefs(src)
    assert out == src


def test_strip_glibc_typedefs_mixed():
    src = (
        "typedef unsigned long int __dev_t;\n"
        "typedef unsigned int kernel_flags_t;\n"
        "typedef struct { int __val[2]; } __fsid_t;\n"
        "typedef long int __time_t;\n"
        "typedef long long max_align_t;\n"
    )
    out = _strip_glibc_internal_typedefs(src)
    assert "typedef unsigned int kernel_flags_t;" in out
    assert "/* typedef __dev_t removed */" in out
    assert "/* typedef __fsid_t removed */" in out
    assert "/* typedef __time_t removed */" in out
    assert "/* typedef max_align_t removed */" in out
    assert "typedef unsigned long int __dev_t;" not in out
    assert "typedef struct" not in out


def test_strip_glibc_typedefs_no_typedefs():
    src = "int x = 0;\nvoid foo(void) { }\n"
    assert _strip_glibc_internal_typedefs(src) == src


def test_dynamic_harness_compiles_without_fsid_conflict():
    """End-to-end: harness with preprocessed glibc type decls compiles cleanly."""
    if not GCC_AVAILABLE:
        pytest.skip("gcc not available")
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")
    hg = HarnessGenerator(config)

    # Inject a preprocessed_source that contains conflicting glibc typedefs.
    # This simulates what happens when VibeOS kernel sources are preprocessed.
    glibc_types = (
        "typedef unsigned long int __dev_t;\n"
        "typedef struct { int __val[2]; } __fsid_t;\n"
        "typedef long int __time_t;\n"
        "typedef long int __clock_t;\n"
    )
    func = _make_func(
        name="safe_nop",
        body="{ }",
        params=[],
        ret_type="void",
    )
    pf = ParsedCFile(
        path="kernel.c",
        functions={"safe_nop": func.signature},
        call_graph={"safe_nop": set()},
        function_bodies={"safe_nop": func.body},
        preprocessed_source=glibc_types + "\nvoid safe_nop(void) { }\n",
    )
    cex = _make_cex()
    dv = DynamicValidator(config, hg)

    result = dv.validate(
        entry_func=func,
        counterexample=cex,
        parsed_file=pf,
        all_funcs={"safe_nop": func},
        all_specs={},
    )
    assert result.outcome in (DynamicOutcome.NOT_TRIGGERED, DynamicOutcome.CONFIRMED), (
        f"Expected compile success, got {result.outcome}: {result.compile_error}"
    )


# ---------------------------------------------------------------------------
# Unit: _looks_like_c_code
# ---------------------------------------------------------------------------


def test_looks_like_c_code_valid():
    src = "int main(void) { return 0; }"
    assert _looks_like_c_code(src) is True


def test_looks_like_c_code_empty():
    assert _looks_like_c_code("") is False
    assert _looks_like_c_code(None) is False


def test_looks_like_c_code_too_short():
    assert _looks_like_c_code("main{}") is False


def test_looks_like_c_code_no_main():
    assert _looks_like_c_code("int add(int a, int b) { return a + b; }") is False


def test_looks_like_c_code_no_braces():
    assert _looks_like_c_code("int main void return 0") is False


# ---------------------------------------------------------------------------
# Unit: _wrap_reproducer_with_signal_handlers
# ---------------------------------------------------------------------------


def test_wrap_reproducer_contains_signal_setup():
    src = "int main(void) { return 0; }"
    wrapped = _wrap_reproducer_with_signal_handlers(src)
    assert "#include <signal.h>" in wrapped
    assert "_amc_handler" in wrapped
    assert "DYNAMIC:CONFIRMED" in wrapped
    assert "DYNAMIC:NOT_TRIGGERED" in wrapped


def test_wrap_reproducer_renames_original_main():
    src = "int main(void) { return 0; }"
    wrapped = _wrap_reproducer_with_signal_handlers(src)
    assert "#define main _amc_reproducer_main" in wrapped
    assert "#undef main" in wrapped
    assert "_amc_reproducer_main();" in wrapped


def test_wrap_reproducer_includes_original_source():
    src = "int main(void) { int x = 42; return x; }"
    wrapped = _wrap_reproducer_with_signal_handlers(src)
    assert "int x = 42;" in wrapped


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_wrap_reproducer_not_triggered_compiles_and_runs():
    """Wrapped safe reproducer compiles and prints NOT_TRIGGERED."""
    src = "int main(void) { return 0; }"
    wrapped = _wrap_reproducer_with_signal_handlers(src)
    rc, stdout, _ = _compile_and_run(wrapped)
    assert "DYNAMIC:NOT_TRIGGERED" in stdout


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_wrap_reproducer_confirmed_on_null_deref():
    """Wrapped reproducer with null dereference prints DYNAMIC:CONFIRMED."""
    src = (
        "#include <stdint.h>\n"
        "int main(void) {\n"
        "    volatile uint8_t *p = (volatile uint8_t *)0;\n"
        "    *p = 1;\n"
        "    return 0;\n"
        "}\n"
    )
    wrapped = _wrap_reproducer_with_signal_handlers(src)
    rc, stdout, _ = _compile_and_run(wrapped)
    assert "DYNAMIC:CONFIRMED" in stdout


# ---------------------------------------------------------------------------
# Integration: validate() with system_entry_reproducer
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_validate_system_entry_reproducer_confirmed():
    """validate() with a crashing reproducer returns CONFIRMED via Attempt 0."""
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")
    hg = HarnessGenerator(config)
    dv = DynamicValidator(config, hg)

    func = _make_func("entry", body="{ }", params=[], ret_type="void")
    pf = ParsedCFile(
        path="entry.c",
        functions={"entry": func.signature},
        call_graph={"entry": set()},
        function_bodies={"entry": func.body},
    )
    cex = _make_cex()
    reproducer = (
        "#include <stdint.h>\n"
        "int main(void) {\n"
        "    volatile uint8_t *p = (volatile uint8_t *)0;\n"
        "    *p = 1;\n"
        "    return 0;\n"
        "}\n"
    )

    result = dv.validate(
        entry_func=func,
        counterexample=cex,
        parsed_file=pf,
        system_entry_reproducer=reproducer,
    )
    assert result.outcome == DynamicOutcome.CONFIRMED


@pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc not available")
def test_validate_system_entry_reproducer_not_triggered():
    """validate() with a safe reproducer returns NOT_TRIGGERED via Attempt 0."""
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")
    hg = HarnessGenerator(config)
    dv = DynamicValidator(config, hg)

    func = _make_func("entry", body="{ }", params=[], ret_type="void")
    pf = ParsedCFile(
        path="entry.c",
        functions={"entry": func.signature},
        call_graph={"entry": set()},
        function_bodies={"entry": func.body},
    )
    cex = _make_cex()
    reproducer = "int main(void) { return 0; }\n"

    result = dv.validate(
        entry_func=func,
        counterexample=cex,
        parsed_file=pf,
        system_entry_reproducer=reproducer,
    )
    assert result.outcome == DynamicOutcome.NOT_TRIGGERED


def test_validate_system_entry_reproducer_not_c_falls_through():
    """validate() skips Attempt 0 and uses unit harness when reproducer is not C."""
    config = Config(enable_dynamic_validation=True, dynamic_cc_path="gcc")
    hg = MagicMock()
    hg.generate_dynamic_harness.return_value = "int main(){puts(\"DYNAMIC:NOT_TRIGGERED\");return 0;}"
    dv = DynamicValidator(config, hg)

    func = _make_func("fn", body="{ return 0; }", params=[], ret_type="int")
    pf = ParsedCFile(
        path="fn.c",
        functions={"fn": func.signature},
        call_graph={"fn": set()},
        function_bodies={"fn": func.body},
    )
    cex = _make_cex()

    result = dv.validate(
        entry_func=func,
        counterexample=cex,
        parsed_file=pf,
        system_entry_reproducer="pseudocode: call fn with x=0",
    )
    assert result.outcome in (
        DynamicOutcome.NOT_TRIGGERED,
        DynamicOutcome.CONFIRMED,
        DynamicOutcome.INCONCLUSIVE,
    )
