"""
Cross-file confirmed_system_entry integration test.

Uses the examples/cross_file_demo pair and real CBMC to demonstrate that:

  libmath.c   : apply_op(op_fn fn, int x) { return fn(x); }  ← null-ptr bug
  main.c      : void system_entry(op_fn fn, int x) { apply_op(fn, x); }

When AMC finds a counterexample for apply_op (fn=NULL → pointer dereference):
  - apply_op has no callers in libmath.c
  - system_entry in main.c calls apply_op and has no callers anywhere
  - CExValidator runs a CBMC reachability harness against system_entry
  - Confirms reachable → propagates to system entry → confirmed_system_entry

A simpler arithmetic variant (divide / kernel_main) is also tested for
_propagate_upward in isolation.

Requires cbmc on PATH.  Skipped otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
CROSS_FILE_DEMO = REPO_ROOT / "examples" / "cross_file_demo"

CBMC_REQUIRED = pytest.mark.skipif(
    __import__("shutil").which("cbmc") is None,
    reason="cbmc not on PATH",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LIB_C_SRC = """\
#include <stdint.h>

int divide(int x, int y) {
    return x / y;
}
"""

ENTRY_C_SRC = """\
#include <stdint.h>

extern int divide(int x, int y);

void kernel_main(void) {
    int y;
    int result = divide(1, y);
    (void)result;
}
"""


def _write_divide_files(tmp_path: Path) -> tuple[Path, Path]:
    lib = tmp_path / "lib.c"
    entry = tmp_path / "entry.c"
    lib.write_text(LIB_C_SRC)
    entry.write_text(ENTRY_C_SRC)
    return lib, entry


def _make_config(tmp_path: Path):
    from bmc_agent.config import Config
    return Config(
        artifact_dir=str(tmp_path / "artifacts"),
        cbmc_path="cbmc",
        cbmc_unwind=4,
        cbmc_timeout=60,
        llm_api_key="fake-key",
        max_refinement_iters=1,
    )


def _make_spec(name: str, pre: str = "true", post: str = "true"):
    from bmc_agent.spec import Spec, SpecStatus
    return Spec(
        function_name=name,
        precondition=pre,
        postcondition=post,
        status=SpecStatus.GENERATED,
    )


def _make_store(tmp_path: Path):
    from bmc_agent.artifacts import ArtifactStore
    return ArtifactStore(str(tmp_path / "artifacts"))


def _stub_reproducer_llm() -> MagicMock:
    """Return a mock LLM that only answers reproducer-generation requests."""
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "reproducer_code": "void test() { /* reproduced */ }",
        "explanation": "System entry can call with CEx inputs.",
        "concrete_values": {},
    })
    return llm


# ---------------------------------------------------------------------------
# Test 1: cross_file_demo example — apply_op / system_entry
# ---------------------------------------------------------------------------

@CBMC_REQUIRED
def test_cross_file_confirmed_system_entry_apply_op(tmp_path: Path):
    """
    Real-CBMC cross-file test using examples/cross_file_demo.

    apply_op (libmath.c) has no in-file callers.
    system_entry (main.c) calls apply_op and has no callers.

    A synthetic CEx for apply_op (fn=NULL) should be classified as
    confirmed_system_entry because CBMC can confirm system_entry reaches
    apply_op and system_entry itself is a true entry point.
    """
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import CExOutcome, CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    libmath_path = CROSS_FILE_DEMO / "libmath.c"
    main_path = CROSS_FILE_DEMO / "main.c"
    assert libmath_path.exists(), f"Missing {libmath_path}"
    assert main_path.exists(), f"Missing {main_path}"

    libmath_parsed = parse_c_file(str(libmath_path))
    main_parsed = parse_c_file(str(main_path))

    assert "apply_op" in libmath_parsed.functions
    assert "system_entry" in main_parsed.functions
    assert "apply_op" in main_parsed.call_graph.get("system_entry", set()), \
        "parser must detect that system_entry calls apply_op"

    apply_op_fi = libmath_parsed.get_function_info("apply_op")
    system_entry_fi = main_parsed.get_function_info("system_entry")
    assert apply_op_fi is not None
    assert system_entry_fi is not None

    lib_all_funcs = {
        n: libmath_parsed.get_function_info(n)
        for n in libmath_parsed.functions
        if libmath_parsed.get_function_info(n) is not None
    }
    all_specs = {
        "apply_op": _make_spec("apply_op"),
        "system_entry": _make_spec("system_entry"),
    }

    # Synthetic CEx: fn=NULL → null function-pointer dereference
    cex = Counterexample(
        failing_property="pointer-dereference",
        variable_assignments={"fn": "0"},
        trace=["apply_op(NULL, x)"],
    )

    cross_file_callers: set[str] = {"apply_op"}
    cross_file_caller_contexts = {
        "apply_op": [(system_entry_fi, main_parsed)],
    }

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, _stub_reproducer_llm(), store, harness_gen)

    result = validator.validate(
        func=apply_op_fi,
        spec=_make_spec("apply_op"),
        counterexample=cex,
        all_funcs=lib_all_funcs,
        all_specs=all_specs,
        parsed_file=libmath_parsed,
        driver_name="cross_file_demo",
        cross_file_callers=cross_file_callers,
        cross_file_caller_contexts=cross_file_caller_contexts,
    )

    assert result.outcome == CExOutcome.REAL_BUG, (
        f"Expected REAL_BUG, got {result.outcome}. Reasoning: {result.reasoning}"
    )
    assert result.system_entry_reached is True, (
        f"Expected system_entry_reached=True, got False. Reasoning: {result.reasoning}"
    )
    assert "system_entry" in result.caller_path, (
        f"system_entry not in call chain: {result.caller_path}"
    )
    assert "apply_op" in result.caller_path

    reporter = BugReporter(store)
    report = reporter.create_report(result, apply_op_fi)
    assert report.confidence == "confirmed_system_entry", (
        f"Expected confirmed_system_entry, got {report.confidence}"
    )

    print(f"\n[PASS] cross_file_demo apply_op → confirmed_system_entry")
    print(f"  Call chain : {result.caller_path}")
    print(f"  Confidence : {report.confidence}")
    print(f"  Reasoning  : {result.reasoning}")


# ---------------------------------------------------------------------------
# Test 2: divide / kernel_main — _propagate_upward in isolation
# ---------------------------------------------------------------------------

@CBMC_REQUIRED
def test_propagate_upward_crosses_file_boundary(tmp_path: Path):
    """
    _propagate_upward with real CBMC: divide (lib.c) has no in-file callers,
    but kernel_main (entry.c) calls divide and has no callers → system entry.
    """
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    lib_path, entry_path = _write_divide_files(tmp_path)
    lib_parsed = parse_c_file(str(lib_path))
    entry_parsed = parse_c_file(str(entry_path))

    divide_fi = lib_parsed.get_function_info("divide")
    kernel_main_fi = entry_parsed.get_function_info("kernel_main")
    lib_all_funcs = {
        n: lib_parsed.get_function_info(n)
        for n in lib_parsed.functions
        if lib_parsed.get_function_info(n) is not None
    }
    all_specs = {
        "divide": _make_spec("divide"),
        "kernel_main": _make_spec("kernel_main"),
    }
    cex = Counterexample(
        failing_property="division-by-zero",
        variable_assignments={"y": "0"},
        trace=[],
    )

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    validator = CExValidator(config, _stub_reproducer_llm(), store, HarnessGenerator(config))

    reachable, chain = validator._propagate_upward(
        func_name="divide",
        counterexample=cex,
        all_funcs=lib_all_funcs,
        all_specs=all_specs,
        parsed_file=lib_parsed,
        driver_name="cross_file_demo",
        cross_file_callers={"divide"},
        cross_file_caller_contexts={"divide": [(kernel_main_fi, entry_parsed)]},
    )

    assert reachable is True, f"Expected system entry reachable, got False. Chain: {chain}"
    assert "kernel_main" in chain, f"Expected kernel_main in chain: {chain}"
    print(f"\n[PASS] _propagate_upward crosses file boundary: {chain}")


# ---------------------------------------------------------------------------
# Test 3: divide / kernel_main — full validate() path
# ---------------------------------------------------------------------------

@CBMC_REQUIRED
def test_cross_file_confirmed_system_entry_divide(tmp_path: Path):
    """
    Full validate() call for divide/kernel_main pair with real CBMC.
    """
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import CExOutcome, CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file

    lib_path, entry_path = _write_divide_files(tmp_path)
    lib_parsed = parse_c_file(str(lib_path))
    entry_parsed = parse_c_file(str(entry_path))

    divide_fi = lib_parsed.get_function_info("divide")
    kernel_main_fi = entry_parsed.get_function_info("kernel_main")
    lib_all_funcs = {
        n: lib_parsed.get_function_info(n)
        for n in lib_parsed.functions
        if lib_parsed.get_function_info(n) is not None
    }
    all_specs = {
        "divide": _make_spec("divide"),
        "kernel_main": _make_spec("kernel_main"),
    }
    cex = Counterexample(
        failing_property="division-by-zero",
        variable_assignments={"y": "0"},
        trace=["divide(1, 0)"],
    )

    config = _make_config(tmp_path)
    store = _make_store(tmp_path)
    harness_gen = HarnessGenerator(config)
    validator = CExValidator(config, _stub_reproducer_llm(), store, harness_gen)

    result = validator.validate(
        func=divide_fi,
        spec=_make_spec("divide"),
        counterexample=cex,
        all_funcs=lib_all_funcs,
        all_specs=all_specs,
        parsed_file=lib_parsed,
        driver_name="cross_file_demo",
        cross_file_callers={"divide"},
        cross_file_caller_contexts={"divide": [(kernel_main_fi, entry_parsed)]},
    )

    assert result.outcome == CExOutcome.REAL_BUG
    assert result.system_entry_reached is True, (
        f"system_entry_reached=False. Reasoning: {result.reasoning}"
    )
    assert "kernel_main" in result.caller_path

    reporter = BugReporter(store)
    report = reporter.create_report(result, divide_fi)
    assert report.confidence == "confirmed_system_entry"

    print(f"\n[PASS] divide/kernel_main → confirmed_system_entry")
    print(f"  Call chain : {result.caller_path}")
    print(f"  Confidence : {report.confidence}")
