"""
Phase 1.5: Per-function CBMC flag selection [AGENTIC].

Lightweight LLM step between spec generation and BMC.  For each function the
LLM examines the signature and body and decides which optional CBMC checks are
semantically meaningful — enabling them only where they catch real bugs, not
everywhere (which would drown real findings in noise).

Currently selects:
  --unsigned-overflow-check  — for functions where unsigned integer overflow has
                               security-relevant consequences (allocation-size
                               computation, size arithmetic on external inputs).

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
DECISION: Should --unsigned-overflow-check be enabled for this function?

Enable --unsigned-overflow-check when the function:
1. Multiplies two size/count/length parameters together \
(e.g. nmemb*size, width*height, count*stride, rows*cols*bpp).
2. Computes an allocation size that feeds into malloc/calloc/realloc/mmap.
3. Does arithmetic on values arriving from external sources: network packets, \
filesystem data, hardware registers, user/driver input.
4. Has an integer overflow that could cause a security-relevant outcome \
(under-allocation, buffer shorter than expected, wrap-around to a small value).

Do NOT enable --unsigned-overflow-check when:
1. Arithmetic is only index increments (i++, i += stride, loop counters) \
where no security-relevant downstream use occurs.
2. All integer operations are on values provably bounded by the program \
structure (e.g. small enum, fixed-size array index).
3. The function only reads, compares, or does bitwise operations — no \
multiplication or addition of user-influenced values.

Respond with ONLY valid JSON — no markdown, no extra text:
{{
  "unsigned_overflow_check": true | false,
  "reasoning": "<one concise sentence>"
}}
"""


@dataclass
class FlagSelection:
    """Per-function CBMC flag selections chosen by the LLM."""

    unsigned_overflow_check: bool = False
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {
            "unsigned_overflow_check": self.unsigned_overflow_check,
            "reasoning": self.reasoning,
        }


# Default when flag selection is disabled or the LLM fails.
_DEFAULT = FlagSelection(unsigned_overflow_check=False, reasoning="default (flag selection skipped)")


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

        enabled = [n for n, s in results.items() if s.unsigned_overflow_check]
        if enabled:
            logger.info(
                "Flag selection: --unsigned-overflow-check enabled for %d/%d function(s): %s",
                len(enabled), len(funcs), ", ".join(sorted(enabled)),
            )
        else:
            logger.debug("Flag selection: no functions selected for --unsigned-overflow-check")

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

    enabled = bool(data.get("unsigned_overflow_check", False))
    reasoning = str(data.get("reasoning", "")).strip()

    if enabled:
        logger.debug("Flag selection '%s': unsigned_overflow_check=True — %s", func_name, reasoning)

    return FlagSelection(unsigned_overflow_check=enabled, reasoning=reasoning)
