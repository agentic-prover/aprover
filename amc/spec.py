"""
Spec DSL: data classes for pre/postconditions, parser, and validator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SpecStatus(Enum):
    PENDING = "pending"
    GENERATED = "generated"
    VERIFIED = "verified"
    FAILED = "failed"
    REFINED = "refined"


@dataclass
class Spec:
    """
    Formal specification for a single C function.

    Attributes:
        function_name:   Name of the function being specified.
        precondition:    Natural language or DSL string for the precondition.
        postcondition:   Natural language or DSL string for the postcondition.
        callee_specs:    Expected specs for callees (used for compositional checking).
        loop_invariants: Loop invariants expressed as strings.
        status:          Current status of the spec in the verification pipeline.
    """

    function_name: str
    precondition: str
    postcondition: str
    callee_specs: dict[str, "Spec"] = field(default_factory=dict)
    loop_invariants: list[str] = field(default_factory=list)
    status: SpecStatus = SpecStatus.PENDING
    spec_disagreement: bool = False

    def to_dict(self, _seen: frozenset | None = None) -> dict:
        # Guard against circular callee_specs (e.g. mutually recursive fns).
        if _seen is None:
            _seen = frozenset()
        callee_dicts = {}
        for k, v in self.callee_specs.items():
            vid = id(v)
            if vid not in _seen:
                callee_dicts[k] = v.to_dict(_seen | {vid})
        return {
            "function_name": self.function_name,
            "precondition": self.precondition,
            "postcondition": self.postcondition,
            "callee_specs": callee_dicts,
            "loop_invariants": self.loop_invariants,
            "status": self.status.value,
            "spec_disagreement": self.spec_disagreement,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Spec":
        callee_specs = {
            k: cls.from_dict(v) for k, v in d.get("callee_specs", {}).items()
        }
        return cls(
            function_name=d["function_name"],
            precondition=d["precondition"],
            postcondition=d["postcondition"],
            callee_specs=callee_specs,
            loop_invariants=d.get("loop_invariants", []),
            status=SpecStatus(d.get("status", SpecStatus.PENDING.value)),
            spec_disagreement=d.get("spec_disagreement", False),
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_PRECOND_RE = re.compile(
    r"(?:precondition|pre|requires?)[:\s]+(.+?)(?=postcondition|post|ensures?|$)",
    re.IGNORECASE | re.DOTALL,
)
_POSTCOND_RE = re.compile(
    r"(?:postcondition|post|ensures?)[:\s]+(.+?)(?=precondition|pre|requires?|loop[_ ]invariant|$)",
    re.IGNORECASE | re.DOTALL,
)
_INVARIANT_RE = re.compile(
    r"(?:loop[_ ]invariant)[:\s]+(.+?)(?=precondition|postcondition|loop[_ ]invariant|$)",
    re.IGNORECASE | re.DOTALL,
)


def parse_spec(text: str) -> Optional[Spec]:
    """
    Parse LLM output into a Spec object.

    Tries JSON first; falls back to a simple regex-based heuristic parser.
    Returns None if parsing fails.
    """
    text = text.strip()
    if not text:
        return None

    # 1. Try JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "function_name" in data:
            return Spec.from_dict(data)
    except (json.JSONDecodeError, KeyError):
        pass

    # 2. Heuristic regex parse
    pre_match = _PRECOND_RE.search(text)
    post_match = _POSTCOND_RE.search(text)

    if not pre_match or not post_match:
        return None

    # Extract function name (first word after "function:" or first identifier line)
    fn_name = ""
    fn_match = re.search(r"function[:\s]+(\w+)", text, re.IGNORECASE)
    if fn_match:
        fn_name = fn_match.group(1)

    invariants: list[str] = []
    for m in _INVARIANT_RE.finditer(text):
        inv = m.group(1).strip()
        if inv:
            invariants.append(inv)

    return Spec(
        function_name=fn_name,
        precondition=pre_match.group(1).strip(),
        postcondition=post_match.group(1).strip(),
        loop_invariants=invariants,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_spec(spec: Spec) -> bool:
    """
    Basic validation: spec must have a non-empty function name,
    non-empty precondition, and non-empty postcondition.
    """
    if not spec.function_name or not spec.function_name.strip():
        return False
    if not spec.precondition or not spec.precondition.strip():
        return False
    if not spec.postcondition or not spec.postcondition.strip():
        return False
    return True


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def merge_specs(specs: list[Spec]) -> Spec:
    """
    Merge a list of specs (e.g. caller-expected specs for the same function).

    Strategy:
    - Precondition:  disjunction  (any of the callers' preconditions is enough)
    - Postcondition: conjunction  (all callers' postconditions must hold)
    - Loop invariants: union (deduplicated)
    - function_name: taken from the first spec
    - callee_specs: union (last writer wins per callee name)

    Raises ValueError if the list is empty.
    """
    if not specs:
        raise ValueError("merge_specs requires at least one Spec")
    if len(specs) == 1:
        return specs[0]

    fn_name = specs[0].function_name
    pres = [s.precondition for s in specs if s.precondition.strip()]
    posts = [s.postcondition for s in specs if s.postcondition.strip()]

    merged_pre = " OR ".join(f"({p})" for p in pres) if pres else ""
    merged_post = " AND ".join(f"({p})" for p in posts) if posts else ""

    invariants: list[str] = []
    seen: set[str] = set()
    for s in specs:
        for inv in s.loop_invariants:
            if inv not in seen:
                invariants.append(inv)
                seen.add(inv)

    merged_callee: dict[str, Spec] = {}
    for s in specs:
        merged_callee.update(s.callee_specs)

    return Spec(
        function_name=fn_name,
        precondition=merged_pre,
        postcondition=merged_post,
        callee_specs=merged_callee,
        loop_invariants=invariants,
        status=SpecStatus.PENDING,
    )
