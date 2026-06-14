"""Soundness policy: which justifications may DELETE a finding vs only RE-TIER it.

Determinism and soundness are ORTHOGONAL. A check can be deterministic-and-unsound
(a repeatably-wrong heuristic) or non-deterministic-and-sound-in-effect (an LLM whose
mistakes can only cost precision). What soundness actually requires is one concrete
property: *no real bug is ever silently removed from the report.*

A finding's exclusion is a NARROWING of the reported set, and narrowing is asymmetric:
a wrong "safe to exclude" HIDES a real bug (a silent false negative = soundness loss),
while a wrong "keep it" only adds noise (precision loss). So every removal must rest on a
justification that cannot wrongly hide a bug:

  - DETERMINISTIC_VERIFIER : a formal/deterministic check proves the exclusion. E.g. CBMC
                             re-verifies clean under a precondition clause that is ALSO shown
                             to hold at every call site by a deterministic caller-check.
                             -> may DELETE.
  - SELF_VERIFYING_WITNESS : the artifact is its own proof and is checked deterministically.
                             E.g. a reproducer that COMPILES and REPRODUCES the exact fault
                             (matching the CBMC property, via the real public API), or a
                             materialized-harness re-run. Generation may be non-deterministic;
                             the compile+run is deterministic ground truth, so it cannot falsely
                             accept. -> may DELETE.
  - AGENTIC_JUDGMENT       : any LLM verdict (realism, reachability, spec-soundness). It is
                             non-deterministic and can be confidently wrong. -> may only RE-TIER
                             (lower confidence); MUST NEVER be the sole justification to remove.

This module is the single source of truth for that rule. Call ``resolve_action`` (or the
convenience helpers) at any decision point that might drop a finding, and act on the returned
``Action``. Keeping the rule here -- rather than re-deriving it at each call site -- is what makes
the invariant auditable.
"""
from __future__ import annotations

import enum


class Justification(enum.Enum):
    """What is being relied on to exclude / down-weight a finding."""
    DETERMINISTIC_VERIFIER = "deterministic_verifier"
    SELF_VERIFYING_WITNESS = "self_verifying_witness"
    AGENTIC_JUDGMENT = "agentic_judgment"


class Action(enum.Enum):
    DELETE = "delete"   # remove the finding from the report
    RETIER = "retier"   # keep the finding, lower its confidence tier


# The justifications strong enough to remove a sound finding.
_MAY_DELETE = frozenset({
    Justification.DETERMINISTIC_VERIFIER,
    Justification.SELF_VERIFYING_WITNESS,
})


def may_delete(justification: Justification) -> bool:
    """True iff this justification is allowed to DELETE a finding."""
    return justification in _MAY_DELETE


def resolve_action(justification: Justification) -> Action:
    """Map a justification to the strongest action it is permitted to take.

    Agentic (non-deterministic) judgments are demoted to RE-TIER; only
    deterministic verifiers and self-verifying witnesses may DELETE.
    """
    return Action.DELETE if may_delete(justification) else Action.RETIER


def refiner_exclusion_action(
    *,
    cbmc_excluded_cex: bool,
    clause_caller_checked_deterministically: bool,
) -> Action:
    """Action for a spec-refiner that excluded a counterexample by ADDing a
    precondition clause.

    CBMC re-verification proves the clause EXCLUDES the CEx, but NOT that the
    clause holds at every call site -- that validity is what a (non-deterministic)
    SoundnessAgent judges. So a removal is sound only when BOTH the CBMC exclusion
    holds AND the clause is checked at all callers deterministically. Otherwise the
    exclusion rests on an agentic soundness judgment and may only RE-TIER.
    """
    if cbmc_excluded_cex and clause_caller_checked_deterministically:
        return Action.DELETE
    return Action.RETIER


def witness_action(*, reproduced_exact_fault: bool) -> Action:
    """Action for a self-verifying witness (reproducer / materialized re-run).

    DELETE only when the witness deterministically reproduced the EXACT fault
    (caller is responsible for the public-API + matching-property guards before
    calling this). A non-reproducing witness proves nothing -> RE-TIER.
    """
    return Action.DELETE if reproduced_exact_fault else Action.RETIER
