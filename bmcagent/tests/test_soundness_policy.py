"""The soundness invariant: agentic judgments may only RE-TIER; only
deterministic verifiers / self-verifying witnesses may DELETE a finding."""
from bmc_agent.soundness_policy import (
    Action, Justification, may_delete, refiner_exclusion_action,
    resolve_action, witness_action,
)


def test_agentic_judgment_may_not_delete():
    assert not may_delete(Justification.AGENTIC_JUDGMENT)
    assert resolve_action(Justification.AGENTIC_JUDGMENT) is Action.RETIER


def test_deterministic_verifier_may_delete():
    assert may_delete(Justification.DETERMINISTIC_VERIFIER)
    assert resolve_action(Justification.DETERMINISTIC_VERIFIER) is Action.DELETE


def test_self_verifying_witness_may_delete():
    assert may_delete(Justification.SELF_VERIFYING_WITNESS)
    assert resolve_action(Justification.SELF_VERIFYING_WITNESS) is Action.DELETE


def test_every_justification_has_a_resolution():
    # No justification is left unhandled (guards against a new enum member
    # silently defaulting to DELETE).
    for j in Justification:
        assert resolve_action(j) in (Action.DELETE, Action.RETIER)
    # And the ONLY ones that may delete are the two non-agentic kinds.
    deleters = {j for j in Justification if resolve_action(j) is Action.DELETE}
    assert deleters == {
        Justification.DETERMINISTIC_VERIFIER,
        Justification.SELF_VERIFYING_WITNESS,
    }


def test_refiner_deletes_only_with_deterministic_caller_check():
    # CBMC excluded the CEx AND the clause is caller-checked -> safe to delete.
    assert refiner_exclusion_action(
        cbmc_excluded_cex=True,
        clause_caller_checked_deterministically=True,
    ) is Action.DELETE


def test_refiner_retiers_when_soundness_is_agentic_only():
    # CBMC excluded the CEx but the clause's caller-validity is only an LLM
    # judgment -> may NOT delete; re-tier instead (cannot silently hide a bug).
    assert refiner_exclusion_action(
        cbmc_excluded_cex=True,
        clause_caller_checked_deterministically=False,
    ) is Action.RETIER


def test_refiner_retiers_when_cbmc_did_not_exclude():
    assert refiner_exclusion_action(
        cbmc_excluded_cex=False,
        clause_caller_checked_deterministically=True,
    ) is Action.RETIER


def test_witness_deletes_only_on_exact_reproduction():
    assert witness_action(reproduced_exact_fault=True) is Action.DELETE
    assert witness_action(reproduced_exact_fault=False) is Action.RETIER
