"""``SoundnessAgent`` — caller-grounded soundness gate on a refinement clause.

The spec refiner proposes a precondition clause to silence a counterexample
(e.g. ``start < end`` to exclude an empty-range read). Excluding the cex is
the easy part — derivable from the function body alone. The hard, FP-critical
question is whether that clause is actually *guaranteed by every caller*, or
whether AND-ing it into the precondition merely **assumes the bug away** and
hides a reachable path.

This agent answers that question. Given the function, the proposed clause, and
the rejected cex, it decides:

  * ``SOUND``   — the clause holds at every call site; the refinement is a
                  legitimate FP suppression.
  * ``UNSOUND`` — at least one caller can violate the clause; applying it would
                  mask a reachable path (a real-bug lead, not a FP).
  * ``UNKNOWN`` — could not determine (no caller visibility / unsure).

The agent earns its keep with an *agentic* backend (claude-code with Read/Grep
over the source tree): it actually reads the callers. A text-only backend will
usually return ``UNKNOWN`` here — which the gate treats as "do not block", so
the behaviour gracefully degrades to the pre-gate pipeline. Routing role is
``refinement`` so it follows the same per-role LLM selection as the refiner
(``BMC_AGENT_LLM_REFINEMENT_*`` / ``--specs-via-claude-code``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from bmc_agent.agents.base import BaseAgent


@dataclass
class SoundnessVerdict:
    """Verdict on whether a proposed refinement clause is caller-guaranteed."""

    verdict: str               # "SOUND" | "UNSOUND" | "UNKNOWN"
    rationale: str = ""
    implicated_caller: str = ""  # caller (file:line / name) that can violate it

    @property
    def is_unsound(self) -> bool:
        return self.verdict == "UNSOUND"

    @property
    def is_sound(self) -> bool:
        return self.verdict == "SOUND"


_SYSTEM_PROMPT = (
    "You are a C memory-safety auditor performing a SOUNDNESS check on a "
    "proposed function precondition. You decide whether a precondition clause "
    "is guaranteed by every caller of the function, or whether adopting it "
    "would hide a reachable execution path. Be decisive and evidence-based. "
    "If you have file-reading tools, USE them to find and read the actual call "
    "sites before answering; never guess a caller you have not seen. If you "
    "cannot see the callers, answer UNKNOWN rather than inventing them."
)

_PROMPT = """\
A verifier is trying to silence a counterexample in this C function by ADDING a
precondition clause. Your job: decide whether that clause is actually GUARANTEED
by all callers, or whether adding it would MASK a reachable path (turning a real
bug invisible).

FUNCTION: {fn_name}
SOURCE FILE: {source_file}

PROPOSED PRECONDITION CLAUSE TO ADD:
    {clause}

The counterexample this clause is meant to exclude:
    failing property : {failing_property}
    description      : {description}
    witness state    :
{witness}

FUNCTION BODY (the operation that faults is inside here):
{fn_body}

TASK:
1. Find EVERY call site of `{fn_name}` in the codebase. If you have Read/Grep/Glob
   tools, use them now — read each caller. Do not rely on memory or guess file
   names; cite the call sites you actually read (file:line).
2. For each caller, determine whether the proposed clause `{clause}` is
   guaranteed to hold for the arguments it passes.
3. Decide:
   - SOUND   : the clause holds at EVERY call site → the refinement is a
               legitimate false-positive suppression.
   - UNSOUND : at least one caller can pass arguments violating the clause →
               adopting it would mask a reachable path. Name that caller.
   - UNKNOWN : you cannot see/find the callers, or genuinely cannot decide.

Reply with ONLY a JSON object:
{{"verdict": "SOUND" | "UNSOUND" | "UNKNOWN",
  "implicated_caller": "<file:line or function name of a caller that can violate the clause, else empty>",
  "rationale": "<one or two sentences citing the specific call sites you examined>"}}
"""


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        inner, in_fence = [], False
        for line in t.splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                inner.append(line)
        t = "\n".join(inner).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m is None:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


class SoundnessAgent(BaseAgent[SoundnessVerdict]):
    """Caller-grounded soundness check on a refinement clause.

    Shares the ``refinement`` routing role so it follows the same per-role LLM
    selection (and agentic claude-code config) as the refiner it gates.
    """

    name = "refinement"
    system_prompt = _SYSTEM_PROMPT
    max_retries = 1

    def _llm_call_kwargs(self) -> dict:
        return {"max_tokens": 1500, "thinking": False}

    def build_prompt(
        self,
        *,
        func_info: Any,
        proposed_clause: str,
        rejected_cex: Any,
        **_: Any,
    ) -> str:
        witness = "\n".join(
            f"      {k} = {v}"
            for k, v in (getattr(rejected_cex, "variable_assignments", {}) or {}).items()
        )[:1500] or "      (no witness state)"
        return _PROMPT.format(
            fn_name=func_info.name,
            source_file=getattr(func_info, "source_file", "") or "(unknown)",
            clause=proposed_clause,
            failing_property=getattr(rejected_cex, "failing_property", "") or "(unknown)",
            description=getattr(rejected_cex, "description", "") or "(none)",
            witness=witness,
            fn_body=(getattr(func_info, "body", "") or "(unavailable)")[:4000],
        )

    def parse(self, response: str) -> Optional[SoundnessVerdict]:
        data = _extract_json(response)
        if not data:
            return None
        verdict = str(data.get("verdict", "")).strip().upper()
        if verdict not in ("SOUND", "UNSOUND", "UNKNOWN"):
            return None
        return SoundnessVerdict(
            verdict=verdict,
            rationale=str(data.get("rationale", ""))[:600],
            implicated_caller=str(data.get("implicated_caller", ""))[:200],
        )
