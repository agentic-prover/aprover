"""Shared helpers for pulling a JSON object out of an LLM response.

Reasoning models (K2 Think etc.) and chat models alike often wrap the JSON
they were asked for in a ``` fence or prepend a sentence of prose. These
helpers extract the first top-level ``{...}`` object robustly so call sites
don't each re-implement the same fence/brace-balancing logic.
"""

from __future__ import annotations

import json
import re
from typing import Optional

# Matches the outermost {...} span (greedy, across newlines).
_JSON_BLOCK_RX = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> Optional[dict]:
    """Pull the first top-level JSON object out of ``text``.

    Robust to ``` fences and stray prose. Returns ``None`` on parse failure.
    """
    if not text:
        return None
    # Strip code-fence markers first.
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```", "", cleaned)
    m = _JSON_BLOCK_RX.search(cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        # Tightening pass: walk forward to the first balanced { ... } block.
        candidate = m.group(0)
        depth = 0
        start = candidate.find("{")
        for i, ch in enumerate(candidate[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None
