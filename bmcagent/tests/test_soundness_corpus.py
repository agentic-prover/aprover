"""Soundness corpus — a standing regression guard on the confidence-tiering
logic (``BugReporter.create_report``), generalizing ``test_immunity_gate``.

It models the Phase-3 protected reals (vfs_open_handle, ip_handle, the
system-entry parser OOBs) and the known false-positive class (static
nondet-arg harness artifacts) as *synthetic* findings, then asserts the tiering
rules across enforcement ON/OFF:

  * a genuine real whose realism verdict is REALISTIC is never demoted;
  * a confirmed_dynamic real on the attack surface (public OR system-entry)
    keeps immunity when enforcement is OFF, even under an UNREALISTIC verdict;
  * a static internal helper with no system-entry path (the FP class) is
    demoted to 'unlikely' under an UNREALISTIC/high verdict.

SCOPE: this guards the *deterministic logic* — that a future change (a
default-flip, broadening enforcement, dropping the immunity check) can't
silently change who gets demoted. It does NOT verify the LLM's realism
*judgment* (that reals actually receive REALISTIC verdicts) — that is the
empirical gate, run via tools/check_soundness_gate.py over a real --agentic
sweep. The one boundary case below documents why that empirical gate matters.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bmc_agent.bug_reporter import BugReporter
from bmc_agent.cbmc import Counterexample
from bmc_agent.cex_validator import CExOutcome
from bmc_agent.dynamic_validator import DynamicOutcome, DynamicValidationResult
from bmc_agent.parser import FunctionInfo, FunctionSignature
from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict

_CONFIRMED_TIERS = {"confirmed_dynamic", "confirmed_system_entry", "confirmed_bmc"}


def _func(name: str, is_static: bool) -> FunctionInfo:
    sig = FunctionSignature(
        name=name,
        return_type="static void" if is_static else "void",
        parameters=[],
        is_static=is_static,
    )
    return FunctionInfo(name=name, signature=sig, body="{}", callees=[], source_file="x.c")


def _validation(name: str, system_entry_reached: bool):
    cex = Counterexample(
        failing_property=f"{name}.pointer_dereference.1",
        variable_assignments={"len": "4294967295"},
        trace=[],
    )
    return SimpleNamespace(
        function_name=name,
        counterexample=cex,
        dynamic_result=DynamicValidationResult(
            outcome=DynamicOutcome.CONFIRMED, signal_name="SIGSEGV"
        ),
        system_entry_reached=system_entry_reached,
        caller_path=["net_poll", name],
        reasoning="dynamic crash observed",
        outcome=CExOutcome.REAL_BUG,
        system_entry_input="",
    )


def _realism(verdict: RealismVerdict, conf: str = "high") -> RealismCheckResult:
    return RealismCheckResult(
        verdict=verdict,
        reasoning="synthetic",
        key_concern="synthetic",
        llm_confidence=conf,
    )


_REALISTIC = _realism(RealismVerdict.REALISTIC)
_UNREALISTIC = _realism(RealismVerdict.UNREALISTIC, "high")


def _tier(name, *, is_static, system_entry_reached, realism, enforce) -> str:
    reporter = BugReporter(store=None)
    reporter.enforce_realism_on_dynamic = enforce
    rep = reporter.create_report(
        _validation(name, system_entry_reached),
        _func(name, is_static),
        realism_check=realism,
    )
    return rep.confidence


# (id, is_static, system_entry_reached, realism, enforce, expected_kept_confirmed)
_REALS = [
    ("vfs_open_handle/public/REALISTIC/enforce-on",   False, False, _REALISTIC,   True),
    ("vfs_open_handle/public/REALISTIC/enforce-off",  False, False, _REALISTIC,   False),
    ("ip_handle/entry/REALISTIC/enforce-on",          False, True,  _REALISTIC,   True),
    ("parser_oob/entry/REALISTIC/enforce-on",         False, True,  _REALISTIC,   True),
    # Immunity invariant: confirmed_dynamic on the attack surface keeps immunity
    # when enforcement is OFF, even under an UNREALISTIC verdict.
    ("vfs_open_handle/public/UNREALISTIC/enforce-off", False, False, _UNREALISTIC, False),
]


@pytest.mark.parametrize("name,is_static,entry,realism,enforce", _REALS,
                         ids=[c[0] for c in _REALS])
def test_genuine_reals_are_never_demoted(name, is_static, entry, realism, enforce):
    tier = _tier(name, is_static=is_static, system_entry_reached=entry,
                 realism=realism, enforce=enforce)
    assert tier in _CONFIRMED_TIERS, f"{name}: real demoted to {tier!r}"


# (id, is_static, system_entry_reached, enforce)
_FPS = [
    ("static-nondet-helper/UNREALISTIC/enforce-on",  True, False, True),
    ("static-nondet-helper/UNREALISTIC/enforce-off", True, False, False),
]


@pytest.mark.parametrize("name,is_static,entry,enforce", _FPS,
                         ids=[c[0] for c in _FPS])
def test_known_fps_are_demoted(name, is_static, entry, enforce):
    # Static internal helper, no system-entry path, UNREALISTIC/high → 'unlikely'
    # (loses immunity regardless of enforcement; enforcement only matters for
    # the public/system-entry confirmed_dynamic case).
    tier = _tier(name, is_static=is_static, system_entry_reached=entry,
                 realism=_UNREALISTIC, enforce=enforce)
    assert tier == "unlikely", f"{name}: FP not demoted (tier={tier!r})"


def test_boundary_real_under_enforcement_defers_to_realism():
    """DOCUMENTED CONTRACT (not a 'good' outcome): under enforcement, a public
    real's confirmed_dynamic immunity is removed, so an UNREALISTIC/high verdict
    DOES demote it. The deterministic logic cannot tell a wrong verdict from a
    right one — which is exactly why the empirical gate
    (tools/check_soundness_gate.py over a real sweep) must confirm the LLM never
    mislabels a genuine real as UNREALISTIC. If this assertion ever flips, the
    enforcement contract changed and the empirical gate must be re-run."""
    tier = _tier("vfs_open_handle", is_static=False, system_entry_reached=False,
                 realism=_UNREALISTIC, enforce=True)
    assert tier == "unlikely"
