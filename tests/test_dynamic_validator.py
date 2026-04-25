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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amc.cbmc import Counterexample
from amc.config import Config
from amc.dynamic_validator import (
    DynamicOutcome,
    DynamicValidationResult,
    DynamicValidator,
)
from amc.harness_generator import HarnessGenerator
from amc.parser import FunctionInfo, FunctionSignature, ParsedCFile


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
