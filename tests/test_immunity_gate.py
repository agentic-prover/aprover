"""Phase-1 internal-helper immunity gate (bug_reporter.create_report).

A confirmed_dynamic runtime crash is immune to realism downgrade ONLY when it is
plausibly attacker-reachable. For a ``static`` (internal-linkage) helper whose
crash was never traced to a system entry, the fault was produced by the
unit-level harness feeding nondet args no real caller passes (e.g. a panic-screen
drawing helper handed base_y=INT_MAX). Immunity must NOT shield those from
realism. Public functions and any crash with a traced system-entry path keep full
immunity, so genuine confirmed_dynamic bugs (e.g. vfs_open_handle) are unaffected.
"""
from types import SimpleNamespace

from bmc_agent.bug_reporter import BugReporter
from bmc_agent.cbmc import Counterexample
from bmc_agent.dynamic_validator import DynamicOutcome, DynamicValidationResult
from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
from bmc_agent.parser import FunctionInfo, FunctionSignature
from bmc_agent.cex_validator import CExOutcome


def _func(name, is_static):
    sig = FunctionSignature(name=name, return_type="static void" if is_static else "void",
                            parameters=[], is_static=is_static)
    return FunctionInfo(name=name, signature=sig, body="{}", callees=[], source_file="x.c")


def _validation(name, system_entry_reached):
    cex = Counterexample(failing_property=f"{name}.pointer_arithmetic.5",
                         variable_assignments={"base_y": "2147483647"}, trace=[])
    return SimpleNamespace(
        function_name=name, counterexample=cex,
        dynamic_result=DynamicValidationResult(outcome=DynamicOutcome.CONFIRMED, signal_name="SIGSEGV"),
        system_entry_reached=system_entry_reached,
        caller_path=["handle_sync_exception", name],
        reasoning="dynamic crash observed",
        outcome=CExOutcome.REAL_BUG, system_entry_input="",
    )


_UNREALISTIC = RealismCheckResult(
    verdict=RealismVerdict.UNREALISTIC, reasoning="base_y=INT_MAX, no real caller passes it",
    key_concern="UNREALISTIC: nondet draw coordinate", llm_confidence="high")


def _confidence(name, is_static, system_entry_reached, realism=_UNREALISTIC):
    rep = BugReporter(store=None).create_report(
        _validation(name, system_entry_reached), _func(name, is_static), realism_check=realism)
    return rep.confidence


def test_static_internal_helper_loses_immunity():
    # FP: static helper, no entry path, realism unrealistic -> downgraded.
    assert _confidence("wsod_draw_text", is_static=True, system_entry_reached=False) == "unlikely"


def test_public_function_keeps_immunity():
    # REAL: public attack-surface fn (vfs_open_handle) stays confirmed despite unrealistic realism.
    assert _confidence("vfs_open_handle", is_static=False, system_entry_reached=False) == "confirmed_dynamic"


def test_static_but_entry_reachable_keeps_immunity():
    # REAL: static fn whose crash IS traced to a system entry stays confirmed.
    assert _confidence("reachable_static", is_static=True, system_entry_reached=True) == "confirmed_dynamic"


def test_static_helper_with_realistic_verdict_unaffected():
    # Gate only removes the *shield*; if realism says realistic, the bug still stands.
    realistic = RealismCheckResult(verdict=RealismVerdict.REALISTIC, reasoning="r",
                                   key_concern="k", llm_confidence="high")
    assert _confidence("wsod_draw_text", is_static=True, system_entry_reached=False,
                       realism=realistic) == "confirmed_dynamic"
