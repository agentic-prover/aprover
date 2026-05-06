"""
Phase 1.5: Per-function CBMC flag selection [AGENTIC].

Lightweight LLM step between spec generation and BMC.  For each function the
LLM examines the signature and body and decides which optional CBMC checks are
semantically meaningful — enabling them only where they catch real bugs, not
everywhere (which would drown real findings in noise).

Currently selects:
  --unsigned-overflow-check  — unsigned integer overflow (allocation-size math,
                               network/filesystem length arithmetic).
  --signed-overflow-check    — signed integer overflow (index/offset arithmetic
                               on external data where wrap-around is exploitable).
  --conversion-check         — unsafe type conversions / truncation (wide→narrow
                               casts on packet fields, register values).
  --pointer-overflow-check   — pointer arithmetic overflow (buffer indexing,
                               stride-based address computation).

Design principle: agents propose, conventional tools dispose.  The LLM decides
which checks are meaningful; CBMC executes them soundly.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bmc_agent.logger import get_logger

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo

logger = get_logger("flag_selector")

_FLAG_SELECTION_PROMPT = """\
You are analyzing a C function to decide which optional CBMC verification \
flags are semantically meaningful for it.

FUNCTION: {name}
SIGNATURE: {signature}
BODY:
{body}

---
For each flag below decide true/false. Only enable a flag when it is \
semantically meaningful for THIS function — enabling flags everywhere creates \
noise that hides real bugs.

FLAG 1: --unsigned-overflow-check
Enable when the function:
- Multiplies two size/count/length values (nmemb*size, width*height, rows*cols).
- Computes an allocation size feeding into malloc/calloc/realloc/mmap.
- Does arithmetic on lengths/sizes from network packets, filesystem data, \
hardware registers, or user input.
Do NOT enable for plain loop counters or provably-bounded index increments.

FLAG 2: --signed-overflow-check
Enable when the function:
- Does signed arithmetic on values from external sources (packet fields, \
file offsets, ioctl parameters) where wrap-around would be exploitable.
- Computes array offsets or buffer positions using signed integers derived \
from untrusted input.
Do NOT enable for simple loop counters or comparisons with no downstream \
security consequence.

FLAG 3: --conversion-check
Enable when the function:
- Explicitly casts a wider integer type to a narrower one (uint32->uint16, \
int64->int32, long->int) on values from external sources.
- Truncates packet length fields, register values, or filesystem sizes \
when assigning to smaller types.
Do NOT enable when all casts are between same-width types or involve only \
internal constants.

FLAG 4: --pointer-overflow-check
Enable when the function:
- Computes buffer addresses via pointer arithmetic with externally-controlled \
offsets (base + offset, ptr + count*stride).
- Walks memory regions using pointer increments where the step size or count \
comes from external data.
Do NOT enable for simple array iteration with provably-bounded indices.

Respond with ONLY valid JSON — no markdown, no extra text:
{{
  "unsigned_overflow_check": true | false,
  "signed_overflow_check": true | false,
  "conversion_check": true | false,
  "pointer_overflow_check": true | false,
  "reasoning": "<one concise sentence covering all enabled flags>"
}}
"""


@dataclass
class FlagSelection:
    """Per-function CBMC flag selections chosen by the LLM."""

    unsigned_overflow_check: bool = False
    signed_overflow_check: bool = False
    conversion_check: bool = False
    pointer_overflow_check: bool = False
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {
            "unsigned_overflow_check": self.unsigned_overflow_check,
            "signed_overflow_check": self.signed_overflow_check,
            "conversion_check": self.conversion_check,
            "pointer_overflow_check": self.pointer_overflow_check,
            "reasoning": self.reasoning,
        }

    def any_enabled(self) -> bool:
        return (
            self.unsigned_overflow_check
            or self.signed_overflow_check
            or self.conversion_check
            or self.pointer_overflow_check
        )

    def enabled_flags(self) -> list[str]:
        flags = []
        if self.unsigned_overflow_check:
            flags.append("--unsigned-overflow-check")
        if self.signed_overflow_check:
            flags.append("--signed-overflow-check")
        if self.conversion_check:
            flags.append("--conversion-check")
        if self.pointer_overflow_check:
            flags.append("--pointer-overflow-check")
        return flags


# Default when flag selection is disabled or the LLM fails.
_DEFAULT = FlagSelection(reasoning="default (flag selection skipped)")


class FlagSelector:
    """
    LLM agent that selects per-function CBMC flags before Phase 2.

    Parameters
    ----------
    config : Config
    llm    : LLMClient
    """

    def __init__(self, config: "Config", llm: "LLMClient") -> None:
        self.config = config
        self.llm = llm

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_all(
        self,
        funcs: "dict[str, FunctionInfo]",
    ) -> "dict[str, FlagSelection]":
        """
        Select flags for all functions in parallel.

        Returns a mapping function_name → FlagSelection.
        Falls back to the default (all off) for any function where the LLM
        call fails, so Phase 2 is never blocked.
        """
        if not getattr(self.config, "enable_flag_selection", False):
            return {name: _DEFAULT for name in funcs}

        results: dict[str, FlagSelection] = {}
        max_workers = min(len(funcs), self.config.batch_size, 8)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_name = {
                pool.submit(self._select_one, func): name
                for name, func in funcs.items()
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    logger.warning("Flag selection failed for '%s': %s — using defaults", name, exc)
                    results[name] = _DEFAULT

        enabled = [n for n, s in results.items() if s.any_enabled()]
        if enabled:
            logger.info(
                "Flag selection: extra flags enabled for %d/%d function(s): %s",
                len(enabled), len(funcs), ", ".join(sorted(enabled)),
            )
            for name in sorted(enabled):
                logger.debug(
                    "  %s: %s", name, ", ".join(results[name].enabled_flags()),
                )
        else:
            logger.debug("Flag selection: no functions selected for extra flags")

        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _select_one(self, func: "FunctionInfo") -> FlagSelection:
        from bmc_agent.llm import LLMError

        sig = func.signature
        params = ", ".join(f"{pt} {pn}".strip() for pt, pn in sig.parameters)
        signature_str = f"{sig.return_type} {sig.name}({params})"
        body = (func.body or "")[:1500]

        prompt = _FLAG_SELECTION_PROMPT.format(
            name=func.name,
            signature=signature_str,
            body=body,
        )

        try:
            raw = self.llm.complete(
                system_prompt="You are a formal verification expert. Respond with only valid JSON.",
                user_prompt=prompt,
                max_tokens=256,
                thinking=False,
            )
        except LLMError as exc:
            logger.warning("LLM flag selection call failed for '%s': %s", func.name, exc)
            return _DEFAULT

        return _parse_response(raw, func.name)


def _parse_response(raw: str, func_name: str) -> FlagSelection:
    text = raw.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        inner = [l for l in lines if not l.startswith("```")]
        text = "\n".join(inner).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Flag selection: could not parse JSON for '%s' — using defaults", func_name)
        return _DEFAULT

    uoc = bool(data.get("unsigned_overflow_check", False))
    soc = bool(data.get("signed_overflow_check", False))
    cc  = bool(data.get("conversion_check", False))
    poc = bool(data.get("pointer_overflow_check", False))
    reasoning = str(data.get("reasoning", "")).strip()

    sel = FlagSelection(
        unsigned_overflow_check=uoc,
        signed_overflow_check=soc,
        conversion_check=cc,
        pointer_overflow_check=poc,
        reasoning=reasoning,
    )
    if sel.any_enabled():
        logger.debug(
            "Flag selection '%s': %s — %s",
            func_name, ", ".join(sel.enabled_flags()), reasoning,
        )
    return sel
