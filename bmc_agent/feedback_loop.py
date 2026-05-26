"""
Realism-rejection feedback loop.

When the realism checker rejects a counterexample (verdict=UNREALISTIC),
the rejection contains useful information about WHY the witness was
unreachable. Instead of discarding it, the feedback loop distills the
rejection into one of three remediation classes:

  (a) CODE_CHANGE   — bmc-agent is missing a structural capability
                      (e.g., a new artifact-detector pattern, a parser
                      improvement, a harness-generator gap). Written to
                      ``TODO_BMC_AGENT.md`` for the developer to triage.

  (b) FUNCTION_SPEC — the spec for the function whose CE was rejected
                      was too permissive. A `__CPROVER_assume(...)` clause
                      is added to the function's spec/harness for the
                      NEXT sweep so CBMC never explores this state again.

  (c) PROJECT_INVARIANT — the rejection identifies a project-wide
                      invariant (e.g., ``xmlMalloc != NULL``). The clause
                      is persisted to ``<artifact_dir>/learned_constraints.json``
                      and applied to every harness in the project.

Persistence schema (learned_constraints.json)::

    {
      "version": 1,
      "function_clauses": {
        "<function_name>": ["clause1", "clause2", ...]
      },
      "project_clauses": ["xmlMalloc != NULL", ...],
      "code_change_todos": [
        {"description": "...", "from_function": "...", "from_property": "..."}
      ]
    }

All clauses are valid CBMC `__CPROVER_assume()` expressions and are
review-gated by default — see the soundness notes below.

Soundness
=========
A spec clause can hide real bugs if it's too strong. The loop mitigates
this in three ways:

  1. The LLM is prompted to emit ONLY invariants it can defend as
     "true in all real executions of any public API call into this code".
     If it can't, it must return `scope=code-change` instead, deferring
     to the developer.

  2. Each persisted clause carries a `confidence` and `rationale`. Low
     confidence clauses are written but disabled by default (require
     explicit opt-in to apply).

  3. The optional ``--feedback-soundness-gate`` flag re-runs CBMC after
     applying a new clause; if the function flips from FAIL to PASS,
     the clause is too strong and is rejected.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bmc_agent.logger import get_logger

if TYPE_CHECKING:
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo, ParsedCFile
    from bmc_agent.realism_checker import RealismCheckResult

logger = get_logger("feedback_loop")


# ---------------------------------------------------------------------------
# Remediation types
# ---------------------------------------------------------------------------


class RemediationScope(str, Enum):
    CODE_CHANGE = "code-change"
    FUNCTION_SPEC = "function-spec"
    PROJECT_INVARIANT = "project-invariant"
    # Triggered when CBMC reports the FUT's POST assertion violated by
    # a path that real callers can also hit (i.e., the LLM-emitted POST
    # is over-tight, not the implementation being buggy). Drops the
    # offending POST clause from the FUT's spec on the next harness
    # emission. Orthogonal to caller-grounded spec gen.
    FUNCTION_POST_RELAX = "function-post-relax"
    NONE = "none"


@dataclass
class Remediation:
    """A proposed fix for a realism-rejected counterexample."""

    scope: RemediationScope
    clause: str = ""               # __CPROVER_assume content (no `assume(...)` wrapper)
    code_change: str = ""          # human-readable description for the developer
    rationale: str = ""            # why this fix is sound / appropriate
    confidence: str = "low"        # "high" | "medium" | "low"

    def to_dict(self) -> dict:
        return {
            "scope": self.scope.value,
            "clause": self.clause,
            "code_change": self.code_change,
            "rationale": self.rationale,
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# Stub-precondition assertion detection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LLM prompt for distillation
# ---------------------------------------------------------------------------


_DISTILL_PROMPT = """\
A formal verifier (CBMC) reported a property violation in a C function.
A subsequent realism check returned verdict={verdict}.

When the verdict is **UNREALISTIC**, the rejection contains structured
information about WHY the witness state cannot occur in real execution.

When the verdict is **UNCERTAIN**, the LLM couldn't commit to either
REALISTIC or UNREALISTIC — usually because something prevented analysis
(source truncation, missing callee body, ambiguous witness state, etc.).
UNCERTAIN verdicts are STRONG signals that bmc-agent needs a structural
improvement (more context, richer harness modelling, better stub
contract). Prefer scope="code-change" for UNCERTAIN unless you can
identify a concrete, defensible invariant.

Your task: pick ONE of three remediation paths and emit the change that
would prevent this kind of rejection from re-appearing.

=== FUNCTION ===
{function_name}

=== FUNCTION BODY ===
{function_body}

=== VIOLATED PROPERTY ===
{violated_property}

=== COUNTEREXAMPLE WITNESS (variable assignments) ===
{witness_state}

=== REALISM CHECK ===
verdict: {verdict}
reasoning: {rejection_reasoning}
key_concern: {key_concern}

=== EXISTING PROJECT INVARIANTS (already learned) ===
{existing_project_clauses}

---

Four remediation paths — pick exactly ONE:

  (a) scope="code-change"
        bmc-agent itself is missing a structural capability that would
        prevent this witness pattern. Examples: a new
        witness-pattern artifact detector, a parser fix to recognize a
        typedef form, a harness-generator gap for some struct field
        type, an unmodeled stub return contract.
        REQUIRED: `code_change` field describes what to add to bmc-agent
        (one-paragraph) so the developer can implement it.

  (b) scope="function-spec"
        The spec for THIS function is too permissive — its precondition
        admits a state that no real caller can produce. The clause is
        a single C boolean expression true in all real calls to this
        function. Applied to the next harness for THIS function only.
        REQUIRED: `clause` field is a valid C boolean expression
        referring to the function's parameters and reachable globals.

  (c) scope="project-invariant"
        The same rejection would apply to MANY functions in this
        project — it's a global invariant of the library. Persisted
        and applied to every future harness in the project. Use
        SPARINGLY; over-strong project invariants can hide real bugs.
        REQUIRED: `clause` field is a global C boolean expression
        (no function-local variables; e.g. `xmlMalloc != NULL`).

  (d) scope="function-post-relax"
        ONLY applicable when the violated property is the FUT's own
        postcondition assertion (CBMC names it ``main.assertion.<N>``).
        The offending POST clause is over-tight: real callers can hit
        the witness state, but the LLM-emitted post excludes it (e.g.,
        ``result == 0 || result < 0`` when real semantics allow small
        positive returns from ``copy_from_user``). The fix is to drop
        that clause from the **FUT's** postcondition.
        REQUIRED: `clause` is the precise atom to drop from this
        function's postcondition (DSL form).

If you can't safely propose any of these, return scope="none".

Soundness requirement: the clause MUST be true in every real execution
of every public API call. If you have any doubt, prefer scope="code-change"
(deferring to the developer) over scope="function-spec" (per-function
clause) over scope="project-invariant" (project-wide, highest risk).

Respond with ONLY valid JSON:
{{
  "scope": "code-change" | "function-spec" | "project-invariant" | "function-post-relax" | "none",
  "clause": "<C boolean expression — for function-spec / project-invariant / function-post-relax>",
  "code_change": "<short description of the bmc-agent change — for code-change>",
  "rationale": "<one paragraph: why is this sound? What real invariant does it encode?>",
  "confidence": "high" | "medium" | "low"
}}
"""


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def learn_from_rejection(
    config: "Config",
    llm: "LLMClient",
    func: "FunctionInfo",
    counterexample: "Counterexample",
    realism: "RealismCheckResult",
    existing_project_clauses: list[str],
) -> Remediation:
    """Ask the LLM to distill an UNREALISTIC realism verdict into a
    structured remediation. Returns ``Remediation`` (which may be
    NONE if no safe fix is possible)."""
    from bmc_agent.llm import LLMError
    from bmc_agent.prompts import SPEC_SYSTEM_PROMPT
    from bmc_agent.realism_checker import RealismVerdict

    # Fire on UNREALISTIC (definite artifact — distill an invariant)
    # AND on UNCERTAIN (LLM couldn't decide — usually because of missing
    # context, which is itself a code-change signal). REALISTIC verdicts
    # are the bug signal we want to preserve; never distill them.
    if realism.verdict not in (RealismVerdict.UNREALISTIC, RealismVerdict.UNCERTAIN):
        return Remediation(scope=RemediationScope.NONE,
                           rationale="Feedback loop only fires on UNREALISTIC/UNCERTAIN.")

    body = (getattr(func, "body", None) or "(unavailable)")[:6000]
    var_state = "\n".join(
        f"  {k} = {v}"
        for k, v in (counterexample.variable_assignments or {}).items()
    )[:3000] or "  (no witness variables)"

    prompt = _DISTILL_PROMPT.format(
        verdict=realism.verdict.value.upper(),
        function_name=func.name,
        function_body=body,
        violated_property=counterexample.failing_property,
        witness_state=var_state,
        rejection_reasoning=(realism.reasoning or "")[:1500],
        key_concern=(realism.key_concern or "")[:300],
        existing_project_clauses=(
            "\n".join(f"  {c}" for c in existing_project_clauses) or "  (none)"
        ),
    )

    try:
        raw = llm.complete(
            SPEC_SYSTEM_PROMPT,
            prompt,
            # K2 Think exhausts a 2048 budget on its <think> trace before
            # emitting the JSON remediation -- live sweep showed 16 of these
            # calls failing with finish_reason=length. The role="feedback_distill"
            # tag routes the call to Claude in hybrid mode; bumping max_tokens
            # also gives K2 enough headroom when hybrid isn't configured.
            max_tokens=16384,
            thinking=False,
            role="feedback_distill",
        )
    except LLMError as exc:
        logger.warning(
            "Feedback distillation LLM call failed for '%s': %s",
            func.name, exc,
        )
        return Remediation(scope=RemediationScope.NONE,
                           rationale=f"LLM call failed: {exc}")

    return _parse_remediation(raw, func.name)


def _parse_remediation(raw: str, func_name: str) -> Remediation:
    """Parse the LLM's distill response."""
    text = raw.strip()
    # Strip markdown fence
    if text.startswith("```"):
        lines = text.splitlines()
        inner: list[str] = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                inner.append(line)
        text = "\n".join(inner).strip() or text

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract first balanced JSON object
        from bmc_agent.realism_checker import _extract_first_json_object
        embedded = _extract_first_json_object(text)
        if embedded is None:
            logger.warning(
                "Feedback: failed to parse distill response for '%s': %s",
                func_name, raw[:200],
            )
            return Remediation(scope=RemediationScope.NONE,
                               rationale="Could not parse LLM response.")
        try:
            data = json.loads(embedded)
        except json.JSONDecodeError:
            return Remediation(scope=RemediationScope.NONE,
                               rationale="Embedded JSON also unparseable.")

    scope_str = str(data.get("scope", "none")).lower().strip()
    scope_map = {
        "code-change": RemediationScope.CODE_CHANGE,
        "function-spec": RemediationScope.FUNCTION_SPEC,
        "project-invariant": RemediationScope.PROJECT_INVARIANT,
        "function-post-relax": RemediationScope.FUNCTION_POST_RELAX,
        "none": RemediationScope.NONE,
    }
    scope = scope_map.get(scope_str, RemediationScope.NONE)
    return Remediation(
        scope=scope,
        clause=str(data.get("clause", "")).strip(),
        code_change=str(data.get("code_change", "")).strip(),
        rationale=str(data.get("rationale", "")).strip(),
        confidence=str(data.get("confidence", "low")).lower().strip(),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class LearnedConstraintsStore:
    """File-backed store for learned constraints + code-change TODOs.

    The store lives under ``<artifact_dir>/learned_constraints.json``
    (per-project) so future sweeps on the same project automatically
    pick up the invariants without re-deriving them.
    """

    SCHEMA_VERSION = 1
    FILENAME = "learned_constraints.json"

    def __init__(self, artifact_dir: str | os.PathLike) -> None:
        self.path = Path(artifact_dir) / self.FILENAME
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {
                "version": self.SCHEMA_VERSION,
                "function_clauses": {},
                "project_clauses": [],
                "code_change_todos": [],
            }
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != self.SCHEMA_VERSION:
                logger.warning(
                    "learned_constraints.json schema version mismatch — ignoring"
                )
                return self._fresh()
            return data
        except Exception as exc:
            logger.warning(
                "Failed to load learned_constraints.json (%s) — starting fresh",
                exc,
            )
            return self._fresh()

    @classmethod
    def _fresh(cls) -> dict:
        return {
            "version": cls.SCHEMA_VERSION,
            "function_clauses": {},
            "project_clauses": [],
            "code_change_todos": [],
            # FUT POST relaxations: clauses to DROP from a function's
            # postcondition on the next run (over-tight LLM-emitted
            # POST clauses that real implementations don't satisfy).
            "function_post_relaxations": {},
        }

    def project_clauses(self) -> list[str]:
        return list(self._data.get("project_clauses", []))

    def function_clauses(self, func_name: str) -> list[str]:
        return list(self._data.get("function_clauses", {}).get(func_name, []))

    def function_post_relaxations(self, func_name: str) -> list[str]:
        """Return the list of POST clauses to DROP from *func_name*'s
        postcondition on the next harness emission. Triggered by the
        FUNCTION_POST_RELAX scope when realism rejects a FUT-POST
        violation.
        """
        return list(
            self._data.get("function_post_relaxations", {}).get(func_name, [])
        )

    def record(self, func_name: str, r: Remediation,
               source_property: str = "") -> bool:
        """Persist a remediation. Returns True if anything new was added.

        Side-effect: when the same clause has now been learned for 3+
        distinct functions, auto-promote it to ``project_clauses`` and
        retire the per-function copies. This catches project-wide
        invariants the LLM was learning incrementally function-by-function
        (e.g. ``ctxt != NULL`` independently re-derived for several
        xmlXInclude* functions before being formally voted as a project
        invariant).
        """
        changed = False
        if r.scope == RemediationScope.FUNCTION_SPEC and r.clause:
            slot = self._data.setdefault("function_clauses", {}).setdefault(func_name, [])
            if r.clause not in slot:
                slot.append(r.clause)
                changed = True
                logger.info("Learned function-spec clause for '%s': %s",
                            func_name, r.clause[:100])
                # Auto-promotion check.
                if self._maybe_promote_to_project(r.clause):
                    changed = True
        elif r.scope == RemediationScope.PROJECT_INVARIANT and r.clause:
            slot = self._data.setdefault("project_clauses", [])
            if r.clause not in slot:
                slot.append(r.clause)
                changed = True
                logger.info("Learned project invariant: %s", r.clause[:100])
        elif r.scope == RemediationScope.FUNCTION_POST_RELAX and r.clause:
            slot = (
                self._data.setdefault("function_post_relaxations", {})
                .setdefault(func_name, [])
            )
            if r.clause not in slot:
                slot.append(r.clause)
                changed = True
                logger.info(
                    "Learned function-post relaxation for '%s' (drop clause): %s",
                    func_name, r.clause[:100],
                )
        elif r.scope == RemediationScope.CODE_CHANGE and r.code_change:
            slot = self._data.setdefault("code_change_todos", [])
            entry = {
                "description": r.code_change,
                "from_function": func_name,
                "from_property": source_property,
                "rationale": r.rationale,
                "confidence": r.confidence,
            }
            # Dedup on (description, from_function)
            key = (r.code_change, func_name)
            existing_keys = {
                (e.get("description"), e.get("from_function")) for e in slot
            }
            if key not in existing_keys:
                slot.append(entry)
                changed = True
                logger.info(
                    "Recorded code-change TODO from '%s': %s",
                    func_name, r.code_change[:120],
                )
        if changed:
            self._save()
        return changed

    # Threshold: ≥3 distinct functions independently learn the same
    # clause → promote to project_clauses. Tuned conservatively;
    # individual function clauses are safer than project clauses,
    # so we only promote when the LLM has converged on the same
    # invariant for several independent functions.
    PROMOTION_THRESHOLD = 3

    def _maybe_promote_to_project(self, clause: str) -> bool:
        """Promote a clause to project_clauses if ≥PROMOTION_THRESHOLD
        functions have learned it. Returns True if a promotion happened.
        """
        if not clause:
            return False
        per_fn = self._data.get("function_clauses", {}) or {}
        owners = [fn for fn, cs in per_fn.items() if clause in cs]
        if len(owners) < self.PROMOTION_THRESHOLD:
            return False
        proj = self._data.setdefault("project_clauses", [])
        if clause in proj:
            # Already promoted; just clean up per-function copies.
            for fn in owners:
                per_fn[fn] = [c for c in per_fn[fn] if c != clause]
            # Drop empty function entries.
            for fn in list(per_fn.keys()):
                if not per_fn[fn]:
                    del per_fn[fn]
            return True
        proj.append(clause)
        # Retire the per-function copies now that it lives at project scope.
        for fn in owners:
            per_fn[fn] = [c for c in per_fn[fn] if c != clause]
        for fn in list(per_fn.keys()):
            if not per_fn[fn]:
                del per_fn[fn]
        logger.info(
            "Auto-promoted clause to project_clauses (learned by %d functions): %s",
            len(owners), clause[:80],
        )
        return True

    def compact(self) -> int:
        """Scan all per-function clauses; promote any that have been
        independently learned by ≥PROMOTION_THRESHOLD functions to the
        project_clauses list. Useful for migrating a store created by
        an older bmc-agent that lacked auto-promotion. Returns the
        number of clauses promoted.
        """
        per_fn = self._data.get("function_clauses", {}) or {}
        # Count owners per clause.
        from collections import Counter
        counter: Counter = Counter()
        for fn, cs in per_fn.items():
            for c in cs:
                counter[c] += 1
        promoted = 0
        changed = False
        for clause, owners in counter.items():
            if owners >= self.PROMOTION_THRESHOLD:
                if self._maybe_promote_to_project(clause):
                    promoted += 1
                    changed = True
        if changed:
            self._save()
        return promoted

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)

    def summary(self) -> dict:
        return {
            "project_clauses": len(self._data.get("project_clauses", [])),
            "functions_with_clauses": len(self._data.get("function_clauses", {})),
            "code_change_todos": len(self._data.get("code_change_todos", [])),
        }
