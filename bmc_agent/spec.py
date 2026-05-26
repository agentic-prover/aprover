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
    # PRE split: validity = caller's obligation (asserted at call sites);
    # protocol = higher-level invariant (assumed for callee verification).
    # Both default to "" — when empty, classify_precondition() splits the
    # flat ``precondition`` field on demand. See plan_validity_protocol_split.
    pre_validity: str = ""
    pre_protocol: str = ""
    # Provenance: maps each clause text → list of evidence tags that
    # support it. Tag conventions used by spec_generator_v2:
    #   "body:L<line>"        — derived from reading the function body
    #   "caller_site_<idx>"   — derived from observing call site #idx
    #   "header_comment"      — extracted from doxygen/header annotation
    #   "signature_pattern"   — derived from universal_contracts patterns
    #   "canonical_contract"  — from universal_stub_contracts registry
    #   "external_boundary"   — boundary function; spec is trivial by design
    # Empty dict for v1-generated specs (back-compat). Consumed by the
    # feedback loop to drop low-trust clauses preferentially when a
    # spec-derived constraint produces a spurious counterexample.
    evidence: dict[str, list[str]] = field(default_factory=dict)

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
            "pre_validity": self.pre_validity,
            "pre_protocol": self.pre_protocol,
            "evidence": self.evidence,
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
            pre_validity=d.get("pre_validity", ""),
            pre_protocol=d.get("pre_protocol", ""),
            evidence=d.get("evidence", {}),
        )

    def evidence_for(self, clause: str) -> list[str]:
        """Return the evidence tags for ``clause``, or [] if untagged.

        Lookup is exact-text; callers should pass clause strings as
        they appear in pre_validity / pre_protocol / postcondition.
        """
        return self.evidence.get(clause, [])

    def clause_trust_score(self, clause: str) -> int:
        """Rough trust score (higher = more trusted).

        Used by feedback_loop to decide which clause to drop first when
        a spec-derived constraint produces a spurious counterexample.
        Scoring:

          +3 canonical_contract           (hand-curated, authoritative)
          +2 caller_site_*                (independent evidence)
          +2 header_comment               (author-stated intent)
          +1 signature_pattern            (universal pattern, no LLM)
          +0 body:*                       (impl-only, contamination risk)
          -1 (no evidence tags)           (unsupported guess)

        When dropping clauses, drop lowest-scored first.
        """
        tags = self.evidence_for(clause)
        if not tags:
            return -1
        score = 0
        for tag in tags:
            if tag == "canonical_contract":
                score = max(score, 3)
            elif tag.startswith("caller_site_") or tag == "header_comment":
                score = max(score, 2)
            elif tag == "signature_pattern":
                score = max(score, 1)
        return score

    def split_precondition(self) -> tuple[str, str]:
        """Return ``(pre_validity, pre_protocol)``.

        Uses the structured fields when both are populated (or when
        either is non-empty — indicating the LLM/parser supplied a
        split). Falls back to ``classify_precondition`` on the flat
        ``precondition`` otherwise. An empty ``precondition`` yields
        ``("", "")``.
        """
        if self.pre_validity or self.pre_protocol:
            return self.pre_validity, self.pre_protocol
        if not self.precondition.strip():
            return "", ""
        return classify_precondition(self.precondition)


# ---------------------------------------------------------------------------
# Validity / protocol clause classifier
# ---------------------------------------------------------------------------
#
# Splits a flat PRE clause-by-clause into:
#   - validity  : caller's obligation — memory-safety primitives the
#                 callee body literally requires: valid(), valid_range(),
#                 in_bounds(), !null(), no_overflow(), owns(),
#                 valid_string(), valid_user_pointer(), and bare
#                 comparisons of pointer/index/size-shaped values.
#   - protocol  : caller cooperation invariants the callee body assumes
#                 but cannot enforce: locked(), npid_is_attached(), state
#                 equalities on initialised objects, ref-count predicates,
#                 etc.
#
# Default policy when in doubt: classify as **validity**. Asserting too
# much surfaces as new FPs (visible, fixable); assuming too much hides
# bugs (the failure mode we are explicitly fixing — see
# findings/methodology_insight_2026-05-22.md).

_VALIDITY_HEAD_PATTERNS = (
    r"\bvalid\s*\(",
    r"\bvalid_range\s*\(",
    r"\bvalid_string\s*\(",
    r"\bvalid_user_pointer\s*\(",
    r"\bin_bounds\s*\(",
    r"\bno_overflow\s*\(",
    r"\bowns\s*\(",
    r"\bnull\s*\(",
    r"\b__CPROVER_r_ok\s*\(",
    r"\b__CPROVER_w_ok\s*\(",
    r"\b__CPROVER_rw_ok\s*\(",
    r"\bsizeof\s*\(",
)

_PROTOCOL_HEAD_PATTERNS = (
    r"\blocked\s*\(",
    r"\bnpid_is_attached\s*\(",
    r"\biminor\s*\(",
)

# Tokens whose presence inside a comparison clause hints at protocol
# state rather than raw memory-safety bounds. The intent is intentionally
# loose — these names show up in object-state predicates, not in
# pointer/size bookkeeping.
_PROTOCOL_NAME_HINTS = (
    "initialized",
    "initialised",
    "state",
    "ready",
    "ref_count",
    "refcount",
    "open",
    "closed",
    "active",
    "attached",
    "registered",
    "mounted",
    "kind",
)

_VALIDITY_HEAD_RE = re.compile("|".join(_VALIDITY_HEAD_PATTERNS))
_PROTOCOL_HEAD_RE = re.compile("|".join(_PROTOCOL_HEAD_PATTERNS))


def _split_top_level_and(text: str) -> list[str]:
    """Split *text* on top-level ``&&`` / ``AND`` / ``and`` connectors,
    respecting parens and brackets so we don't tear apart sub-expressions
    like ``f(a, b && c)``.
    """
    parts: list[str] = []
    depth = 0
    i = 0
    last = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            # "&&"
            if ch == "&" and i + 1 < n and text[i + 1] == "&":
                parts.append(text[last:i].strip())
                i += 2
                last = i
                continue
            # " AND " / " and " (case-insensitive word match)
            if ch in (" ", "\t", "\n"):
                rest = text[i:i + 5].lower()
                if rest.startswith(" and "):
                    parts.append(text[last:i].strip())
                    i += 5
                    last = i
                    continue
        i += 1
    tail = text[last:].strip()
    if tail:
        parts.append(tail)
    # Strip a single layer of matching outer parens off each clause.
    cleaned: list[str] = []
    for p in parts:
        p = p.strip().rstrip(",").strip()
        while p.startswith("(") and p.endswith(")"):
            inner = p[1:-1]
            depth = 0
            balanced = True
            for ch in inner:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if depth < 0:
                    balanced = False
                    break
            if balanced and depth == 0:
                p = inner.strip()
            else:
                break
        if p:
            cleaned.append(p)
    return cleaned


def _classify_clause(clause: str) -> str:
    """Return ``"validity"`` or ``"protocol"`` for a single clause.

    Conservative default: ``"validity"`` when unsure. Asserting a
    spurious clause as caller obligation surfaces as a new (debuggable)
    FP; assuming a clause that should be discharged hides bugs.
    """
    c = clause.strip()
    if not c:
        return "validity"
    # 1. Negation: classify by the head predicate underneath.
    if c.startswith("!"):
        return _classify_clause(c[1:].lstrip())
    # 2. Strong-protocol heads.
    if _PROTOCOL_HEAD_RE.search(c):
        return "protocol"
    # 3. Strong-validity heads.
    if _VALIDITY_HEAD_RE.search(c):
        return "validity"
    # 4. Bare comparisons: hint via field/identifier names. Anything
    #    smelling like a state machine, lifecycle flag, or membership
    #    predicate goes to protocol; otherwise validity.
    low = c.lower()
    for hint in _PROTOCOL_NAME_HINTS:
        # Match as a sub-token (word-ish boundary) to avoid catching
        # things like ``start_addr`` matching ``state``.
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(hint)}(?![A-Za-z0-9_])", low):
            return "protocol"
    # 5. Default: validity.
    return "validity"


def drop_clauses(precondition: str, drop: list[str]) -> str:
    """Return *precondition* with each clause in *drop* removed.

    Used by harness_generator to apply persisted
    ``callee_relaxations`` (from the feedback loop) before
    translating a callee's PRE into asserts/assumes.

    Matching is whitespace-insensitive on the full clause string;
    we don't try to canonicalise C-expression syntax beyond
    that. A drop entry that doesn't match any clause is a no-op
    rather than an error — over time the relaxation list can
    accumulate entries that no longer apply because the LLM
    re-generated a structurally different spec.
    """
    if not precondition or not drop:
        return precondition

    def _norm(s: str) -> str:
        # Strip matching outer parens (any number of layers) the same
        # way ``_split_top_level_and`` does. Without this, a drop entry
        # ``(x == 0)`` won't match a clause emitted as ``x == 0`` — and
        # vice versa. The whitespace strip then makes the match insens-
        # itive to formatting differences.
        s = s.strip()
        while s.startswith("(") and s.endswith(")"):
            inner = s[1:-1]
            depth = 0
            balanced = True
            for ch in inner:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if depth < 0:
                    balanced = False
                    break
            if balanced and depth == 0:
                s = inner.strip()
            else:
                break
        return re.sub(r"\s+", "", s)

    drop_norms = {_norm(c) for c in drop if c.strip()}
    if not drop_norms:
        return precondition
    # Strip leading "requires" / "pre" so we operate on the clause list.
    prefix_m = re.match(
        r"^(requires?|precondition\s*:?|pre\s*:?)\s+",
        precondition.strip(),
        flags=re.IGNORECASE,
    )
    body = precondition.strip()
    prefix = ""
    if prefix_m:
        prefix = prefix_m.group(0)
        body = body[prefix_m.end():]
    clauses = _split_top_level_and(body)
    kept = [c for c in clauses if _norm(c) not in drop_norms]
    if not kept:
        return ""
    return prefix + " && ".join(
        f"({c})" if (" && " in c or " || " in c) else c for c in kept
    )


def classify_precondition(precondition: str) -> tuple[str, str]:
    """Split *precondition* into ``(validity_text, protocol_text)``.

    Both returned strings use ``&&`` as the connector so they parse back
    through ``precond_to_assume`` / ``precond_to_assert`` unchanged.
    Returns ``("", "")`` for an empty / trivially-true precondition.
    """
    s = (precondition or "").strip()
    if not s or s.lower() in ("true", "1"):
        return "", ""
    # Strip a leading ``requires`` / ``pre`` / ``precondition:`` keyword
    # so the classifier sees just the clause list.
    s = re.sub(
        r"^(requires?|precondition\s*:?|pre\s*:?)\s+",
        "",
        s,
        flags=re.IGNORECASE,
    )
    clauses = _split_top_level_and(s)
    if not clauses:
        return "", ""
    validity: list[str] = []
    protocol: list[str] = []
    for c in clauses:
        target = _classify_clause(c)
        (validity if target == "validity" else protocol).append(c)
    join = lambda parts: " && ".join(f"({p})" if (" && " in p or " || " in p) else p for p in parts)
    return join(validity), join(protocol)


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
