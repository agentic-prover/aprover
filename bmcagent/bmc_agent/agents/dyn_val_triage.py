"""``DynValTriageAgent`` — input-realism triage on a CONFIRMED dyn-val result.

Step B of the dyn-val triage layer (Step A is the fault_site checkpoint
in dynamic_validator.py; Step C is the iterative regen loop). When the
dynamic harness fires a signal AND the fault site is inside the FUT
(or unknown — i.e., not already filtered by Step A), this agent looks
at the harness + witness + run output + function source and decides
whether the signal corresponds to a real-bug-shaped state or a
harness-artifact (unrealistic input the LLM-generated harness allowed
but no in-tree caller would produce).

Three verdicts:

  * ``real_bug_shaped`` — the witness inputs are consistent with what
    in-tree callers can produce; the signal is a real-bug signal.
    Keep CONFIRMED.
  * ``harness_artifact`` — the witness requires inputs no in-tree
    caller produces (e.g., NULL pointer through a parameter the
    callers check non-NULL; out-of-range flag value; garbage stub
    return). The signal is a harness artifact. Reclassify.
  * ``unbounded_input`` — specifically: the harness lets a length /
    size / count parameter take values arbitrarily larger than any
    real allocation. Real callers bound these. Reclassify.

Plus the catch-all ``uncertain`` for when the agent can't decide.

Routing: ``BMC_AGENT_LLM_DYNVAL_TRIAGE_*`` env vars; falls back to
the default triage role chain, then the global default.

The agent is **opt-in** via the ``BMC_AGENT_DYNVAL_INPUT_TRIAGE``
environment variable so existing pipelines incur no extra LLM cost
unless explicitly enabled. When enabled, each CONFIRMED dyn-val
outcome with ``fault_site != "in_setup"`` costs one extra LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient


class DynValTriageVerdict(str, Enum):
    REAL_BUG_SHAPED   = "real_bug_shaped"
    HARNESS_ARTIFACT  = "harness_artifact"
    UNBOUNDED_INPUT   = "unbounded_input"
    UNCERTAIN         = "uncertain"


@dataclass
class DynValTriageResult:
    verdict: DynValTriageVerdict
    confidence: str  # "low" / "medium" / "high"
    reasoning: str
    # Optional: a short tag for which class of artifact was detected
    # (e.g. "calloc-zero-init-violation", "caller-checks-nonnull",
    # "length-exceeds-buffer"). Used by Step C to choose the regen
    # prompt's emphasis.
    artifact_class: Optional[str] = None
    raw_response: str = ""


_SYSTEM_PROMPT = (
    "You are a C-language expert auditing whether a dynamic-validation "
    "signal corresponds to a real bug or a harness-instrumentation "
    "artifact.\n\n"
    "Context: BMC-Agent's dynamic validator compiled a small C harness "
    "from a counterexample witness and ran it under GCC. The harness "
    "fired a signal (SIGSEGV / SIGABRT / etc.). Your job is to decide "
    "whether the signal is a REAL-BUG signal (the function-under-test's "
    "defect actually fires) or a HARNESS-ARTIFACT signal (the witness "
    "input state is not reachable from any in-tree caller — the bug "
    "exists only in the harness's nondet input space).\n\n"
    "Common harness-artifact shapes to look for:\n"
    "  * Calloc-init violation: harness lets a struct field be garbage "
    "    but the in-tree allocator (calloc / explicit zero-init) makes "
    "    it always zero.\n"
    "  * Caller-precondition slip: harness lets a parameter be NULL / "
    "    out-of-range, but every in-tree caller checks for that and "
    "    early-returns.\n"
    "  * Unbounded input: harness lets a length / size / count "
    "    parameter exceed the accompanying buffer's actual size, but "
    "    in-tree callers always set length = strlen(buf) or similar.\n"
    "  * Stub-modeling artifact: a stubbed callee returns NULL / "
    "    garbage; the harness lets that propagate into the FUT, but "
    "    the real callee never returns those values.\n"
    "  * Type-confusion / wrong-magic: harness lets an opaque handle "
    "    have a wrong magic field; in-tree callers never produce "
    "    type-mismatched handles.\n\n"
    "If you find one of these patterns, vote HARNESS_ARTIFACT or "
    "UNBOUNDED_INPUT (the more specific tag when applicable). Otherwise "
    "vote REAL_BUG_SHAPED. Use UNCERTAIN only when the evidence is "
    "genuinely ambiguous.\n\n"
    "Respond with ONLY valid JSON, no markdown, no commentary:\n"
    "{\n"
    '  "verdict": "real_bug_shaped" | "harness_artifact" | '
    '"unbounded_input" | "uncertain",\n'
    '  "confidence": "low" | "medium" | "high",\n'
    '  "artifact_class": "<short tag, or null when verdict='
    'real_bug_shaped>",\n'
    '  "reasoning": "<3-6 sentences. Cite the specific witness value '
    "or harness construct that triggered your verdict. If "
    'HARNESS_ARTIFACT, name the in-tree invariant the witness violates.>"\n'
    "}"
)


_USER_PROMPT_TEMPLATE = (
    "=== FUNCTION UNDER TEST ===\n"
    "Name: {func_name}\n"
    "Signal that fired: {signal_name}\n"
    "Step A fault_site verdict: {fault_site}\n"
    "\n"
    "--- FUT source ---\n"
    "```c\n"
    "{func_source}\n"
    "```\n"
    "\n"
    "--- Dynamic harness (compiled, ran, fired the signal) ---\n"
    "```c\n"
    "{harness}\n"
    "```\n"
    "\n"
    "--- Counterexample witness (CBMC variable assignments) ---\n"
    "```\n"
    "{witness}\n"
    "```\n"
    "\n"
    "--- Runtime output (stdout from the harness run) ---\n"
    "```\n"
    "{run_output}\n"
    "```\n"
    "\n"
    "Audit the witness values against the FUT's in-tree caller "
    "invariants. Deliver your verdict per the JSON schema in the "
    "system prompt."
)


class DynValTriageAgent(BaseAgent[DynValTriageResult]):
    """Input-realism triage agent for CONFIRMED dyn-val outcomes.

    Routing: ``BMC_AGENT_LLM_DYNVAL_TRIAGE_*`` env vars.
    """

    name = "dynval_triage"
    system_prompt = _SYSTEM_PROMPT

    def _llm_call_kwargs(self) -> dict:
        # Smaller token budget than the realism check — this is a
        # focused yes/no audit, not a full re-classification with
        # call-chain walking.
        return {"max_tokens": 2000, "thinking": False}

    def build_prompt(
        self,
        *,
        func_name: str,
        func_source: str,
        harness: str,
        witness: str,
        run_output: str,
        signal_name: str,
        fault_site: str,
        **_: Any,
    ) -> str:
        return _USER_PROMPT_TEMPLATE.format(
            func_name=func_name,
            func_source=(func_source or "(source not available)")[:3000],
            harness=(harness or "(harness not available)")[:3000],
            witness=(witness or "(no witness)")[:1500],
            run_output=(run_output or "(no run output)")[:1000],
            signal_name=signal_name or "unknown",
            fault_site=fault_site or "unknown",
        )

    def parse(self, response: str) -> Optional[DynValTriageResult]:
        import json
        import re
        text = (response or "").strip()
        if not text:
            return None
        # Strip code-fence if present.
        if text.startswith("```"):
            inner: list[str] = []
            in_fence = False
            for line in text.splitlines():
                if line.startswith("```"):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    inner.append(line)
            text = "\n".join(inner).strip()
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m is None:
                return None
            try:
                data = json.loads(m.group(0))
            except Exception:
                return None
        if not isinstance(data, dict):
            return None
        verdict_str = (data.get("verdict") or "").lower().strip()
        try:
            verdict = DynValTriageVerdict(verdict_str)
        except Exception:
            return None
        confidence = (data.get("confidence") or "low").lower().strip()
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
        artifact_class = data.get("artifact_class")
        if artifact_class in (None, "", "null"):
            artifact_class = None
        return DynValTriageResult(
            verdict=verdict,
            confidence=confidence,
            reasoning=str(data.get("reasoning", "")),
            artifact_class=artifact_class,
            raw_response=response,
        )
