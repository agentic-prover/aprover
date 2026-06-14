"""Soundness-policy compliance for the spec-refiner accept path
(realism-enforcement plan, Phase 2).

When the refiner's clause excludes the targeted counterexample, CBMC proves the
EXCLUSION but not that the clause holds at every call site -- that validity rests
only on the agentic SoundnessAgent. Per ``soundness_policy`` an agentic judgment
may RE-TIER but never DELETE a sound finding, so marking such a finding VERIFIED
CLEAN (a delete) is unsound unless the clause is ALSO deterministically
caller-checked. These tests lock the two invariants the pipeline branch relies on:

  1. ``_clause_deterministically_caller_checked`` returns False today (there is no
     deterministic caller-check yet -> the DELETE hook is closed).
  2. Composing that helper with ``soundness_policy.refiner_exclusion_action`` yields
     RETIER for an accepted-but-agentic-only clause, and DELETE only if a future
     deterministic caller-check flips the helper to True.
"""

from types import SimpleNamespace

from bmc_agent import soundness_policy as sp
from bmc_agent.pipeline import AMCPipeline


def _shim():
    # The helper only reads self.* trivially; a bare namespace is enough.
    return SimpleNamespace()


def test_caller_check_hook_is_closed_today():
    # No deterministic caller-check exists yet -> the DELETE hook stays closed,
    # so the refiner can only ever RE-TIER until one is added.
    func = SimpleNamespace(name="f")
    proposal = SimpleNamespace(added_clause="x < n")
    assert AMCPipeline._clause_deterministically_caller_checked(
        _shim(), func, proposal) is False


def test_accept_retiers_when_caller_check_absent():
    # Pipeline composition: helper False + CBMC excluded the CEx -> RETIER.
    caller_checked = AMCPipeline._clause_deterministically_caller_checked(
        _shim(), SimpleNamespace(name="f"), SimpleNamespace(added_clause="x < n"))
    action = sp.refiner_exclusion_action(
        cbmc_excluded_cex=True,
        clause_caller_checked_deterministically=caller_checked,
    )
    assert action is sp.Action.RETIER


def test_accept_deletes_only_if_caller_check_present():
    # If a future deterministic caller-check proves the clause at all call sites,
    # the same composition yields DELETE -- the extension point is correct.
    action = sp.refiner_exclusion_action(
        cbmc_excluded_cex=True,
        clause_caller_checked_deterministically=True,
    )
    assert action is sp.Action.DELETE
