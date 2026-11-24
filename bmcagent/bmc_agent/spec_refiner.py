"""Realism-feedback-driven in-sweep spec refinement.

When the realism checker rejects a CEx with verdict=UNREALISTIC and a
concrete ``key_concern`` (not just "looks artificial"), that rejection
identifies exactly which precondition the spec is missing. The refiner
takes that signal, asks the LLM for the precise clause that would
exclude the CEx, and produces a refined precondition.

Distinct from cex_validator's existing refinement loop, which triggers
on **caller-reachability failure** (CBMC-formal). This module triggers
on **realism rejection** (LLM-judged). They're complementary signals
producing the same artifact (a refined precondition string) that flows
through the same Phase 3c re-verification machinery.

Soundness guard (the methodology-trap defense): a refined spec is
ACCEPTED only when re-verification confirms (a) the targeted CEx is
gone AND (b) no previously-REALISTIC CEx silently dropped. The second
half catches the failure mode where over-tight refinement masks a real
bug while satisfying the "CEx count dropped" surface check.

Bounded: K=3 refinement iterations per function, then we stop and let
the caller-reachability path or human review handle it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from bmc_agent.spec import Spec, SpecStatus

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo
    from bmc_agent.realism_checker import RealismCheckResult


# ---------- defaults ---------------------------------------------------------

MAX_REFINE_ITERATIONS = 3

# Realism key_concerns that are too vague to drive a targeted refinement.
# We require a concrete signal, not a hand-wave.
_VAGUE_KEY_CONCERN_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"^(looks?\s+artificial|seems?\s+impossible|not\s+plausible)\s*$",
               re.IGNORECASE),
    re.compile(r"^\(?cannot determine\)?$", re.IGNORECASE),
]


def _is_actionable_key_concern(key_concern: str) -> bool:
    """Decide whether the realism rejection's key_concern is concrete
    enough to drive a refinement. Empty / hand-wave concerns aren't.
    """
    if not key_concern:
        return False
    for rx in _VAGUE_KEY_CONCERN_PATTERNS:
        if rx.match(key_concern):
            return False
    # Heuristic: a useful key_concern names a specific variable, field,
    # function, or constraint. Require at least one identifier-shaped token.
    return bool(re.search(r"\b[A-Za-z_]\w{2,}\b", key_concern))


# ---------- the refinement prompt -------------------------------------------

_REFINE_PROMPT = """\
A CBMC counterexample for function `{fn_name}` was rejected by the
realism checker as UNREALISTIC. The realism check identified a specific
reason the counterexample's input state cannot occur in real execution.

Your task: emit the precise PRE clause to ADD to the function's spec
that would exclude this counterexample from CBMC's exploration.

=== FUNCTION SIGNATURE ===
{fn_signature}

=== FUNCTION BODY (first 4000 chars) ===
```c
{fn_body}
```

=== CURRENT SPEC ===
pre_validity:  {pre_validity}
pre_protocol:  {pre_protocol}
postcondition: {postcondition}

=== REJECTED COUNTEREXAMPLE ===
failing_property: {failing_property}
witness state:
{witness_state}

=== REALISM CHECK ===
verdict:     UNREALISTIC
key_concern: {key_concern}
reasoning:   {realism_reasoning}

---

HARD RULES (your output is rejected if any are violated):

  1. Emit ONE NEW PRE CLAUSE that, AND'd with the current spec, would
     have prevented this CEx from being produced. Not "tighten somehow"
     — name a specific predicate that excludes the witness state.

  2. The clause MUST be true in EVERY real execution. If the realism
     check's key_concern is wrong (e.g., it claimed a field is always
     non-NULL but the constructor can leave it NULL on error paths),
     emit scope="cannot-refine" with the discrepancy.

  3. DO NOT propose clauses that contradict the function's body. If
     the body itself checks `p->field == NULL` and handles that case,
     the body has admitted that case; do NOT add `!null(p->field)` to
     the PRE — the CEx is genuinely reachable via that path.

  4. Use the DSL primitives (!null, valid, valid_range, valid_string,
     in_bounds, no_overflow). Avoid prose clauses.

  5. Prefer the WEAKEST clause that excludes the CEx. If the witness
     shows `len = 65536` and the realism says "real callers pass len < 256",
     emit `len <= 255` rather than `len <= 16`.

Respond with ONLY this JSON object:

{{
  "scope": "refine" | "cannot-refine",
  "added_clause": "<DSL clause to AND into pre_validity>",
  "evidence_tag": "realism:{fn_name}:{property_class}",
  "discrepancy": "<only when scope=cannot-refine: why the key_concern doesn't actually justify a refinement>",
  "rationale": "<one sentence: which part of the witness state this clause excludes>"
}}
"""


# ---------- the refiner -----------------------------------------------------


@dataclass
class RefinementProposal:
    """Output of one refinement attempt — a clause to add OR a decline."""

    scope: str                # "refine" | "cannot-refine"
    added_clause: str = ""
    evidence_tag: str = ""
    discrepancy: str = ""
    rationale: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.scope == "refine" and bool(self.added_clause.strip())


class SpecRefiner:
    """LLM-driven spec refiner triggered by realism rejection.

    Construction is cheap — instantiate once per pipeline and call
    ``propose_refinement`` per (function, rejected-CEx, realism-result)
    triple. Caller is responsible for re-running BMC and applying the
    acceptance check (see ``check_refinement_acceptance``).
    """

    def __init__(self, config: "Config", llm: "LLMClient") -> None:
        self.config = config
        self.llm = llm

    def propose_refinement(
        self,
        *,
        func_info: "FunctionInfo",
        current_spec: Spec,
        rejected_cex: "Counterexample",
        realism: "RealismCheckResult",
    ) -> Optional[RefinementProposal]:
        """Ask the LLM for the precise clause that would exclude the
        rejected CEx. Returns None when the realism key_concern isn't
        concrete enough to drive a refinement (gate-keeping at the
        trigger; we don't burn an LLM call on vague rejections).

        v2: delegates to ``RefinementAgent`` (C2 step 3). This method
        retains the public signature and owns the gating policy
        (verdict + actionable key_concern); the agent owns the prompt
        + parse + LLM-call cycle.
        """
        from bmc_agent.realism_checker import RealismVerdict
        if realism.verdict != RealismVerdict.UNREALISTIC:
            return None
        if not _is_actionable_key_concern(realism.key_concern):
            logger.debug(
                "spec_refiner [%s]: key_concern not actionable — skipping",
                func_info.name,
            )
            return None

        # Lazy import to avoid the agents package importing
        # spec_refiner back at module load time.
        if getattr(self.config, "enable_refinement_tools", False):
            from bmc_agent.agents.refinement_tools import RefinementWithToolsAgent
            agent = RefinementWithToolsAgent(config=self.config, llm=self.llm)
        else:
            from bmc_agent.agents.refinement import RefinementAgent
            agent = RefinementAgent(config=self.config, llm=self.llm)
        result = agent.run(
            func_info=func_info,
            current_spec=current_spec,
            rejected_cex=rejected_cex,
            realism=realism,
        )
        if not result.ok:
            logger.warning(
                "spec_refiner [%s]: agent failed: %s",
                func_info.name, (result.error or "")[:200],
            )
            return None
        return result.output

    def apply_refinement_to_spec(
        self,
        *,
        spec: Spec,
        proposal: RefinementProposal,
    ) -> Spec:
        """Return a new Spec with the proposed clause AND'd into
        pre_validity (and the flat precondition for back-compat consumers).
        The original spec is not mutated.
        """
        if not proposal.is_actionable:
            return spec
        clause = proposal.added_clause.strip()
        new_pre_validity = (
            (spec.pre_validity + " && " + clause).strip(" &")
            if spec.pre_validity.strip() and spec.pre_validity.strip() != "true"
            else clause
        )
        parts = [p for p in (new_pre_validity, spec.pre_protocol) if p.strip()]
        new_precondition = " && ".join(parts) if parts else "true"
        new_evidence = dict(spec.evidence)
        new_evidence[clause] = [
            proposal.evidence_tag or f"realism:{spec.function_name}"
        ]
        return Spec(
            function_name=spec.function_name,
            precondition=new_precondition,
            postcondition=spec.postcondition,
            callee_specs=dict(spec.callee_specs),
            loop_invariants=list(spec.loop_invariants),
            status=SpecStatus.REFINED,
            spec_disagreement=spec.spec_disagreement,
            pre_validity=new_pre_validity,
            pre_protocol=spec.pre_protocol,
            evidence=new_evidence,
        )


# ---------- response parsing ------------------------------------------------


def _parse_refinement_response(raw: str, fn_name: str) -> Optional[RefinementProposal]:
    """Parse the LLM's JSON refinement output. Robust to code fences
    and stray prose. Returns None on parse failure.
    """
    if not raw:
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = re.sub(r"```", "", cleaned)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        logger.warning("spec_refiner [%s]: no JSON block in response", fn_name)
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        depth = 0
        start = m.group(0).find("{")
        for i, ch in enumerate(m.group(0)[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(m.group(0)[start : i + 1])
                        break
                    except json.JSONDecodeError:
                        return None
        else:
            return None

    if not isinstance(data, dict):
        return None
    scope = str(data.get("scope", "")).strip().lower()
    if scope not in ("refine", "cannot-refine"):
        scope = "cannot-refine"
    return RefinementProposal(
        scope=scope,
        added_clause=str(data.get("added_clause", "")).strip(),
        evidence_tag=str(data.get("evidence_tag", "")).strip(),
        discrepancy=str(data.get("discrepancy", "")).strip(),
        rationale=str(data.get("rationale", "")).strip(),
    )


# ---------- acceptance check (the methodology-trap defense) -----------------


@dataclass
class AcceptanceResult:
    """Outcome of comparing pre-refinement and post-refinement CEx sets."""

    accepted: bool
    targeted_cex_gone: bool       # the rejected CEx isn't in the new CEx set
    realistic_preserved: bool     # every prior-REALISTIC CEx still appears
    dropped_realistic: list[str] = None   # property names of preserved realistic CExs
    reason: str = ""

    def __post_init__(self):
        if self.dropped_realistic is None:
            self.dropped_realistic = []


def check_refinement_acceptance(
    *,
    targeted_failing_property: str,
    previously_realistic_properties: set[str],
    new_failing_properties: set[str],
) -> AcceptanceResult:
    """Compare pre- and post-refinement CEx sets and decide whether to
    accept the refined spec.

    Acceptance criteria (BOTH must hold):
      (a) The targeted CEx is gone (its failing_property no longer
          appears in the new CEx set).
      (b) Every previously-REALISTIC CEx's failing_property still
          appears in the new CEx set. If a realistic CEx silently
          disappeared, the refinement masked a potential bug — REJECT.

    The "same failing_property string" heuristic is conservative: it
    rejects refinements that change property indices even if the
    underlying bug is unchanged. That's the right bias for soundness.
    """
    targeted_gone = targeted_failing_property not in new_failing_properties
    dropped = sorted(
        previously_realistic_properties - new_failing_properties
    )
    realistic_preserved = not dropped

    accepted = targeted_gone and realistic_preserved
    if accepted:
        reason = "targeted CEx eliminated; all previously-realistic CExs preserved"
    elif not targeted_gone:
        reason = f"targeted CEx '{targeted_failing_property}' still present"
    else:
        reason = (
            f"refinement masked {len(dropped)} previously-realistic CEx(s): "
            + ", ".join(dropped[:3])
        )

    return AcceptanceResult(
        accepted=accepted,
        targeted_cex_gone=targeted_gone,
        realistic_preserved=realistic_preserved,
        dropped_realistic=dropped,
        reason=reason,
    )
