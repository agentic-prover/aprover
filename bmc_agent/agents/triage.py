"""``TriageAgent`` — independent second-opinion verdict on an UNRESOLVED CEx.

When the pipeline classifies a counterexample as ``UNRESOLVED`` (the
caller-chain trace + dyn-val signals don't agree, the spec-refiner
declined to over-tighten, the realism LLM had mixed signals), a human
still has to decide REAL_BUG vs FP. This agent automates that triage
step: given ALL available evidence (the CEx witness, the function
source, the call chain, the dyn-val result, the realism verdict, the
LLM-generated public-API reproducer), it produces a structured verdict
the human can spot-check or trust.

The agent is INTENTIONALLY independent of the rest of the pipeline:

  * It does NOT see the per-oracle verdicts the pipeline produced —
    instead it gets the raw signals and re-evaluates from scratch.
  * It uses its OWN routing role (``triage``) so you can dial a
    stronger model for this task without inflating other budgets.
  * It is post-hoc by default — runs over classification.json files
    on disk, not in-pipeline. Wiring into the pipeline as a Phase 3e
    after the spec-refiner gives up is a follow-up.

Returns a ``TriageVerdict`` dataclass with:
  * ``verdict``: one of REAL_BUG / LIKELY_FP / NEEDS_HUMAN
  * ``confidence``: low / medium / high
  * ``reasoning``: structured analysis (cited evidence)
  * ``fp_class`` (optional): which known FP shape this matches
    (caller-contract-slip, harness-over-permissive-pointer, …)

Routing: ``BMC_AGENT_LLM_TRIAGE_*`` env vars; falls back to default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient


class TriageVerdict(str, Enum):
    REAL_BUG = "real_bug"
    LIKELY_FP = "likely_fp"
    LATENT = "latent"
    NEEDS_HUMAN = "needs_human"


@dataclass
class TriageResult:
    verdict: TriageVerdict
    confidence: str  # low / medium / high
    reasoning: str
    fp_class: Optional[str] = None
    raw_response: str = ""


_SYSTEM_PROMPT = (
    "You are an experienced C reviewer auditing OSS code to confirm "
    "whether bug-finding tool reports correspond to real defects. You "
    "will be shown a CBMC counterexample, the function it fires on, "
    "the call chain, and supporting evidence. Your ONLY job: code-"
    "review the source to determine whether there is a real defect "
    "that fires through realistic in-tree call paths.\n\n"
    "Your purpose is CONFIRMATION — gain confidence the bug is real, "
    "realistic, and reachable. NOT exploitation: do not construct "
    "attack scenarios, do not write PoC exploits, do not describe how "
    "to weaponize the defect. Treat this exactly like reviewing a "
    "patch on a PR: is the code wrong, and can a normal in-tree "
    "caller actually reach the wrong state?\n\n"
    "Approach: read every function in the call chain, audit each "
    "write against any size calculation that allocates the buffer, "
    "look for missing branches in size accumulators, off-by-ones, "
    "missing NULL checks, type-conversion truncations, integer "
    "overflows. The CBMC counterexample is a POINTER to where the "
    "bug might be — not the bug itself. Real bugs frequently live N "
    "frames upstream from the function the CEx fires on (in a size "
    "calculator that under-counts, in a wrapper that forgets to NUL-"
    "terminate, in a caller that doesn't honor a precondition the "
    "callee assumes).\n\n"
    "CRITICAL: do NOT bias toward false-positive. The CBMC harness "
    "may be over-permissive AND the underlying code may still have a "
    "real defect. The questions are independent:\n"
    "  * Is the CEx witness reproducible AS-IS through a real call "
    "path? (Often no — harnesses are over-permissive.)\n"
    "  * Is there a real defect in the code reachable through SOME "
    "in-tree call sequence the CEx pointed you toward?\n"
    "    (Often yes — that's why the CEx fired.)\n"
    "The second question is the one you must answer. Do NOT use the "
    "answer to the first question as evidence for the second.\n\n"
    "When auditing a size calculator (``*_text_len``, ``*_compute_size``, "
    "``*_bytes_needed``, etc.) against its corresponding writer, build "
    "a mental TABLE: enumerate every write path in the writer (every "
    "``strcpy``, ``*p++``, ``append_*`` call, etc.), and for each one, "
    "locate the matching ``length += N`` (or equivalent) in the "
    "calculator. If any write has no matching budget — REAL BUG. If "
    "any write is conditional on a flag/branch the calculator doesn't "
    "also check — REAL BUG.\n\n"
    "STUB-DISCONNECT CHECK (mandatory whenever a callee is stubbed): if the "
    "CEx\u2019s failing state depends on a STUBBED callee\u2019s return value, "
    "you MUST read the real callee\u2019s definition and decide whether the real "
    "callee could actually produce that value. If the stub is MORE PERMISSIVE than "
    "the real callee (e.g. it returns a non-NULL pointer for a size the real bounded "
    "allocator would reject, or a value that violates the real callee\u2019s "
    "guaranteed contract), the witness is NOT reproducible as-is \u2014 do NOT return "
    "REAL_BUG on that witness. Then ask the independent question: is there still a "
    "real underlying defect reachable by SOME (possibly future) caller? -> LATENT if "
    "yes, LIKELY_FP if no.\n\n"
    "Four verdicts, no thumbnail:\n"
    "  * REAL_BUG  — a specific source-level defect that an IN-TREE call path can "
    "reach AS-IS (a real caller establishes the triggering input).\n"
    "  * LATENT    — a real source-level defect EXISTS, but the witness is NOT "
    "reachable through an in-tree call path as-is: it needs an input/precondition NO "
    "current in-tree caller establishes (a future/adversarial-caller risk), OR the "
    "specific witness is a stub-disconnect yet the underlying defect is genuine. Use "
    "LATENT (not REAL_BUG) when the defect is real but only a non-existent/violating "
    "caller reaches it; use LATENT (not LIKELY_FP) whenever a real defect REMAINS "
    "after you strip the spurious witness.\n"
    "  * LIKELY_FP — NO real source-level defect: the witness violates a UNIVERSAL "
    "invariant no real execution breaks (pure harness/stub artifact), AND you found "
    "no genuine defect upstream. 'The harness is over-permissive' is necessary but "
    "NOT sufficient — you must also confirm there is no real defect.\n"
    "  * NEEDS_HUMAN — the audit is incomplete or genuinely ambiguous after reading "
    "every relevant function.\n\n"
    "Respond with ONLY valid JSON, no markdown fences, no commentary:\n"
    "{\n"
    '  "verdict": "real_bug" | "latent" | "likely_fp" | "needs_human",\n'
    '  "confidence": "low" | "medium" | "high",\n'
    '  "fp_class": "<short tag for the FP pattern OR null>",\n'
    '  "reasoning": "<5-10 sentences. If REAL_BUG: quote the buggy line, '
    "explain the precondition violation and what in-tree caller reaches "
    "it. If LIKELY_FP: list what you audited and why each was safe. "
    'CITE SOURCE LINES — file:line when possible.>"\n'
    "}"
)


class TriageAgent(BaseAgent[TriageResult]):
    """Returns a ``TriageResult`` for an UNRESOLVED (or low-confidence
    REAL_BUG) CEx. Independent of the pipeline's oracles — re-evaluates
    from raw evidence.

    Routing: ``BMC_AGENT_LLM_TRIAGE_*`` env vars.
    """

    name = "triage"
    system_prompt = _SYSTEM_PROMPT

    def _llm_call_kwargs(self) -> dict:
        # Triage benefits from extended thinking — the LLM has to
        # weigh several streams of evidence. 6000-token budget mirrors
        # the realism check's deep-reasoning mode.
        return {"max_tokens": 6000, "thinking": False}

    def build_prompt(
        self,
        *,
        function_name: str,
        function_source: str,
        cbmc_property: str,
        harness_source: str,
        witness_text: str,
        caller_path: list,
        dyn_outcome: Optional[str],
        dyn_reasoning: Optional[str],
        reproducer_source: Optional[str],
        realism_verdict: Optional[str],
        realism_reasoning: Optional[str],
        pipeline_reasoning: str,
        sys_entry_reached: bool,
        **_: Any,
    ) -> str:
        lines: list[str] = []
        lines.append(f"=== FUNCTION: {function_name} ===")
        lines.append(f"=== CBMC PROPERTY VIOLATED: {cbmc_property} ===")
        lines.append("")
        lines.append("--- function source ---")
        lines.append("```c")
        lines.append((function_source or "(source not available)")[:4000])
        lines.append("```")
        lines.append("")
        lines.append("--- CBMC harness ---")
        lines.append("```c")
        lines.append((harness_source or "(harness not available)")[:3000])
        lines.append("```")
        lines.append("")
        lines.append("--- counterexample witness (function-relevant) ---")
        lines.append("```")
        lines.append((witness_text or "(no witness)")[:2500])
        lines.append("```")
        lines.append("")
        lines.append(f"--- static caller chain ---")
        lines.append(f"  {' → '.join(caller_path) if caller_path else '(no caller path)'}")
        lines.append(f"  system_entry_reached: {sys_entry_reached}")
        lines.append("")
        lines.append(f"--- dynamic validation ---")
        lines.append(f"  outcome: {dyn_outcome or 'no-dyn'}")
        if dyn_reasoning:
            lines.append(f"  reasoning: {dyn_reasoning[:400]}")
        if reproducer_source:
            lines.append("  reproducer (LLM-generated public-API harness):")
            lines.append("```c")
            lines.append(reproducer_source[:2500])
            lines.append("```")
        lines.append("")
        if realism_verdict:
            lines.append(f"--- realism LLM verdict ---")
            lines.append(f"  verdict: {realism_verdict}")
            if realism_reasoning:
                lines.append(f"  reasoning: {realism_reasoning[:600]}")
            lines.append("")
        lines.append("--- pipeline's classification reasoning ---")
        lines.append(f"  {(pipeline_reasoning or '(none)')[:1000]}")
        lines.append("")
        lines.append(
            "Given the above, deliver your INDEPENDENT verdict per "
            "the JSON schema in the system prompt."
        )
        return "\n".join(lines)

    def parse(self, response: str) -> Optional[TriageResult]:
        import json
        import re
        text = (response or "").strip()
        if not text:
            return None
        # Try in order:
        #   1. Whole response is a JSON object → parse directly
        #   2. A ```json … ``` fenced block anywhere in the response →
        #      parse the contents (handles agents that prose first, JSON last)
        #   3. Any ``` … ``` fenced block whose contents parse as JSON
        #   4. The LAST balanced {...} block in the text — important
        #      because reasoning prose often contains C-code blocks
        #      with embedded ``{`` braces that fool a naive
        #      ``re.search(r'\{.*\}', text, DOTALL)`` (greedy match
        #      from the first ``{`` in a code sample to the closing
        #      ``}`` of the real JSON).
        candidates: list[str] = []

        # 1. whole response
        candidates.append(text)

        # 2. ```json fenced block (best-of any number)
        for m in re.finditer(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL,
        ):
            candidates.append(m.group(1))

        # 4. all balanced {...} blocks via depth tracking
        def _balanced_blocks(s: str) -> list[str]:
            blocks: list[str] = []
            i = 0
            n = len(s)
            while i < n:
                if s[i] != "{":
                    i += 1
                    continue
                depth = 0
                j = i
                in_str = False
                escape = False
                while j < n:
                    c = s[j]
                    if escape:
                        escape = False
                    elif in_str:
                        if c == "\\":
                            escape = True
                        elif c == '"':
                            in_str = False
                    else:
                        if c == '"':
                            in_str = True
                        elif c == "{":
                            depth += 1
                        elif c == "}":
                            depth -= 1
                            if depth == 0:
                                blocks.append(s[i:j + 1])
                                break
                    j += 1
                i = j + 1
            return blocks

        # Search LAST-FIRST so a verdict at the end of the response
        # wins over example/sample JSON earlier in the prose.
        for blk in reversed(_balanced_blocks(text)):
            candidates.append(blk)

        data = None
        for cand in candidates:
            try:
                parsed = json.loads(cand.strip())
            except Exception:
                continue
            if isinstance(parsed, dict) and "verdict" in parsed:
                data = parsed
                break
        if data is None:
            return None
        verdict_str = (data.get("verdict") or "").lower().strip()
        try:
            verdict = TriageVerdict(verdict_str)
        except Exception:
            return None
        confidence = (data.get("confidence") or "low").lower().strip()
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
        fp_class = data.get("fp_class")
        if fp_class in (None, "", "null"):
            fp_class = None
        return TriageResult(
            verdict=verdict,
            confidence=confidence,
            reasoning=str(data.get("reasoning", "")),
            fp_class=fp_class,
            raw_response=response,
        )
