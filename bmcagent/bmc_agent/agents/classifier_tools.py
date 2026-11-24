"""Agentic classifier adjudicator (in-process tool-using).

The deterministic "Classifier downgrade" rule in cex_validator demotes a CBMC
REAL_BUG to UNRESOLVED whenever dynamic validation did NOT reproduce it on a
crash-class property ("model artifact"). That rule is blunt: a real bug whose
reproducer simply failed to construct the triggering input is demoted exactly
like a genuine model artifact.

This agent is consulted at that downgrade point (when enable_classifier_tools is
set). It may grep/read the real source to decide whether the counterexample is
GENUINELY unreachable (uphold the downgrade) or a real bug the reproducer just
missed (override -> keep REAL_BUG). Routing role: ``classifier``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.agents.code_investigation_tools import CodeToolsCallMixin

_CLASSIFIER_SYSTEM = (
    "You are a soundness adjudicator for a bounded model checker. A static "
    "analysis found a potential crash-class bug, but the dynamic reproducer did "
    "NOT trigger it. Decide, by reading the ACTUAL code, whether the finding is "
    "(a) a GENUINE reachable bug whose reproducer merely failed to construct the "
    "triggering input, or (b) a true model artifact / unreachable state. Use the "
    "grep_code / read_function / read_lines tools to inspect callers and guards. "
    "Be conservative: only KEEP when you can point to a realistic caller path "
    "that reaches the faulting state. Respond with a JSON object: "
    '{"verdict": "real" | "artifact", "reasoning": "<one paragraph citing code>"}'
)

_PROMPT = """Function: {fn}
Failing property: {prop}
Static reasoning: {reasoning}
Witness state:
{witness}

Investigate the code and decide: is this a genuine reachable bug (verdict=real)
or a model artifact / unreachable (verdict=artifact)? Return the JSON object."""


class ClassifierAdjudicatorAgent(CodeToolsCallMixin, BaseAgent[dict]):
    """Reads code to confirm/override the deterministic REAL_BUG->UNRESOLVED
    downgrade. Output: {"verdict": "real"|"artifact", "reasoning": str}."""

    name = "classifier"

    def __init__(self, config: "Any", llm: "Any") -> None:
        self.system_prompt = _CLASSIFIER_SYSTEM
        super().__init__(config, llm)

    def _llm_call_kwargs(self) -> dict:
        return {"max_tokens": 4096, "thinking": False}

    def build_prompt(self, *, fn: str, prop: str, reasoning: str,
                     witness: str = "", **_: Any) -> str:
        return _PROMPT.format(fn=fn, prop=prop,
                              reasoning=(reasoning or "(none)")[:1500],
                              witness=(witness or "(none)")[:1500])

    def parse(self, response: str) -> Optional[dict]:
        if not response:
            return None
        m = re.search(r"\{.*\}", response, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        v = str(obj.get("verdict", "")).strip().lower()
        if v not in ("real", "artifact"):
            return None
        return {"verdict": v, "reasoning": str(obj.get("reasoning", ""))[:1000]}

    def keeps_real_bug(self, **kwargs: Any) -> bool:
        """Convenience: True iff the adjudicator says the bug is real (override
        the downgrade). Best-effort: any failure => False (defer to the rule)."""
        res = self.run(**kwargs)
        return bool(res.ok and res.output and res.output.get("verdict") == "real")
