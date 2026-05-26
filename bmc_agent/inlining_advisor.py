"""LLM-driven inline-vs-stub decisions for callee functions.

The mechanical inlining rule in ``harness_generator._should_inline_callee``
is conservative: file-local static, ≤30 LoC, no loops, no malloc, no
recursion. It catches the obvious "tiny pure predicate" cases (jv_get_kind,
xmlIsBlank_ch, BUF_ERROR) but misses ones the LLM can spot:

  * 50-line predicates that the static-LoC bound rejects but are obviously
    inline-able (one switch over a tag, no side effects)
  * Getters with a single dereference whose stub spec is permissive and
    produces stub-disconnect FPs
  * Helpers that compute a derived field — the stub returns nondet but
    the real body has a one-line invariant

This module fires AFTER the static rule, on the callees the static rule
marked "stub." The LLM gets the callee bodies in a single batched call
and emits one bit per candidate. Wrong bit → either over-permissive stub
(spec system absorbs it) or CBMC timeout (visible failure). Bounded
LLM-as-configurator role, not LLM-as-author.

Default-off (Config.enable_inlining_advisor=False, opt-in via
--enable-inlining-advisor) so the existing pipeline is unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo, ParsedCFile


# Hard cap on candidate-body size in the prompt. A pathologically large
# candidate would be rejected by the LLM anyway; truncating keeps the
# prompt budget bounded.
MAX_CANDIDATE_BODY_CHARS = 2000

# Hard cap on the number of candidates per LLM call. If a function has
# more than this many stubbed callees, the rest are silently kept as
# stubs (the LoC cost of the prompt scales with N; we'd rather have
# a precise call on the most-promising candidates).
MAX_CANDIDATES_PER_CALL = 12


# ---------- data class -----------------------------------------------------


@dataclass
class InlineDecision:
    inline: bool
    reason: str = ""


# ---------- prompt ---------------------------------------------------------

_ADVISOR_PROMPT = """\
You are deciding whether to INLINE a callee's real implementation into a
CBMC harness or STUB it with a nondet contract.

Context: the mechanical rule (file-local static, ≤30 LoC, no loops, no
malloc, no recursion) already marked these callees as STUB. Your job is
to reconsider them. Promoting one to INLINE is the right call when:

  (a) it's a small predicate / getter / accessor whose body trivially
      constrains the return — the stub would return arbitrary nondet
      and trip "stub disconnect" FPs (e.g. `jv_get_kind` returning a
      tag, `BUF_ERROR(b)` returning a single bit, `archive_format_name`
      returning a struct field)
  (b) it's a 30-80 LoC helper with no loops, no allocations, and no
      recursion — the LoC bound was tripped but the body is still
      analytically simple
  (c) the body is essentially one switch / chain of comparisons over
      input values, with deterministic return

DO NOT inline when:

  (d) the body has loops (CBMC unwind cost multiplies)
  (e) the body does allocation, file I/O, or any side effect
  (f) the body calls into other functions that themselves would need
      inlining (transitive cost)
  (g) the body has any recursion (CBMC depth explosion)
  (h) the function is genuinely complex (>80 LoC, multiple state
      machines, nested control flow) — let the stub absorb it

=== CALLER CONTEXT ===
caller: {caller_name}
caller signature: {caller_signature}

=== CANDIDATE CALLEES (mechanical rule said STUB; reconsider each) ===
{candidates_block}

Respond with ONLY this JSON object — one entry per candidate, exact name:

{{
  "<callee_name>": {{"inline": true|false, "reason": "<one short sentence>"}},
  ...
}}

Hard rule: if you're unsure, emit `"inline": false`. The default is STUB.
The LLM-write-the-harness path failed historically; LLM-pick-a-bit on a
mechanical scaffold is what we trust.
"""


# ---------- the advisor ----------------------------------------------------


class InliningAdvisor:
    """Per-function inlining advisor. Batches all candidates for one
    caller into a single LLM call."""

    def __init__(self, config: "Config", llm: "LLMClient") -> None:
        self.config = config
        self.llm = llm

    def decide(
        self,
        *,
        candidates: list[str],
        parsed_file: "ParsedCFile",
        caller_name: str,
    ) -> dict[str, InlineDecision]:
        """Return per-candidate inline-or-stub decisions.

        Falls back to all-stub (the safe default) when:
          * candidates is empty
          * no candidate body is available in parsed_file
          * the LLM call fails
          * the response doesn't parse

        Logs every promotion to inline so the operator can audit.
        """
        if not candidates:
            return {}
        # Trim to the top-MAX_CANDIDATES_PER_CALL by body size (smallest
        # first — small candidates are the most-likely inlining wins).
        sized = []
        for name in candidates:
            fi = parsed_file.get_function_info(name)
            if fi is None or not (fi.body or "").strip():
                continue
            sized.append((len(fi.body), name, fi))
        sized.sort(key=lambda x: x[0])
        chosen = sized[:MAX_CANDIDATES_PER_CALL]
        if not chosen:
            return {}

        caller_info = parsed_file.get_function_info(caller_name)
        caller_sig = (
            f"{caller_info.signature.return_type} {caller_name}(...)"
            if caller_info else f"{caller_name}(...)"
        )

        candidates_block_parts: list[str] = []
        for _sz, name, fi in chosen:
            sig = fi.signature
            params_str = ", ".join(f"{t} {n}" for t, n in sig.parameters) or "void"
            body = (fi.body or "")[:MAX_CANDIDATE_BODY_CHARS]
            candidates_block_parts.append(
                f"--- {name} ---\n"
                f"// signature: {sig.return_type} {name}({params_str})\n"
                f"{body}\n"
            )
        candidates_block = "\n".join(candidates_block_parts)

        prompt = _ADVISOR_PROMPT.format(
            caller_name=caller_name,
            caller_signature=caller_sig,
            candidates_block=candidates_block,
        )

        try:
            from bmc_agent.prompts import SPEC_SYSTEM_PROMPT
            raw = self.llm.complete(
                SPEC_SYSTEM_PROMPT, prompt,
                max_tokens=2048, thinking=False,
                role="refinement",
            )
        except Exception as exc:
            logger.warning(
                "inlining_advisor [%s]: LLM call failed (%r); defaulting all to stub",
                caller_name, exc,
            )
            return {name: InlineDecision(inline=False, reason="LLM call failed")
                    for _sz, name, _fi in chosen}

        decisions = _parse_advisor_response(raw, [n for _sz, n, _fi in chosen])
        for name, d in decisions.items():
            if d.inline:
                logger.info(
                    "inlining_advisor: promoted '%s' to INLINE (caller=%s): %s",
                    name, caller_name, d.reason,
                )
        return decisions


# ---------- response parsing ----------------------------------------------


def _parse_advisor_response(
    raw: str, candidates: list[str],
) -> dict[str, InlineDecision]:
    """Parse the LLM's JSON output. Robust to code fences + prose.

    Unknown candidate names in the response are silently ignored.
    Candidates missing from the response default to STUB (the safe
    default — the advisor's role is only to PROMOTE).
    """
    candidate_set = set(candidates)
    fallback = {name: InlineDecision(inline=False, reason="not in response")
                for name in candidates}
    if not raw:
        return fallback
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = re.sub(r"```", "", cleaned)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return fallback
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
                        return fallback
        else:
            return fallback

    if not isinstance(data, dict):
        return fallback

    out = dict(fallback)
    for name, payload in data.items():
        if name not in candidate_set or not isinstance(payload, dict):
            continue
        inline = bool(payload.get("inline", False))
        reason = str(payload.get("reason", "")).strip()
        out[name] = InlineDecision(inline=inline, reason=reason)
    return out
