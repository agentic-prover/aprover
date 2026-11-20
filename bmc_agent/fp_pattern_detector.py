"""
False-positive pattern detection (Phase 4 of autonomous mode).

Inspect a confirmed bug finding (CEx state + classification + realism
verdict) and check whether it matches a known FP pattern. Returns a
typed :class:`FpPattern` plus evidence the autonomous outer loop can use
to inject a session-local skepticism hint into the next round's realism
prompt.

The patterns are derived from the methodology insight in
``findings/methodology_insight_2026-05-22.md`` and the 12-bug
libarchive_rb.c result from the 2026-05-23 sweep (all confirmed but
all caller-contract slips):

* :data:`FpPattern.UNINIT_VTABLE` — the CEx assigns NULL to a function
  pointer that's a member of a ``*->ops->*`` / ``*->vtable->*``
  structure. In practice the container is initialized by a separate
  ``foo_init(handle, ops)`` call that the lite-mode harness skipped.
* :data:`FpPattern.UNINIT_CONTAINER` — every field of the param's
  struct backing is nondet/default and the witness depends on those
  fields being a particular value (NULL/zero). Strong signal of a
  container that's supposed to be initialized by the caller before
  use.
* :data:`FpPattern.UNREACHABLE_BRANCH` — the witness state requires
  a sentinel value (e.g. ``-1`` / ``MAX_INT``) that the caller-site
  guard explicitly rejects.

This module is *pure detection*. It returns the pattern + evidence;
the autonomous loop in cli.py decides what hint to inject.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class FpPattern(str, Enum):
    UNINIT_VTABLE = "uninit_vtable"
    """CEx requires a NULL function pointer to be invoked. Almost
    always the caller-contract slip where the container's ops/vtable
    field is unset because the harness didn't simulate the init call."""

    UNINIT_CONTAINER = "uninit_container"
    """CEx witness shows every container field at its default nondet
    value (NULL / 0). Likely a forgotten initialization on the harness
    side. Weaker signal than UNINIT_VTABLE — many real bugs also have
    nondet fields, but the *combination* of all-nondet + system-entry
    confidence flags this for follow-up."""

    UNREACHABLE_BRANCH = "unreachable_branch"
    """CEx requires a sentinel input value (often ``-1``, ``UINT_MAX``,
    very large size) that callers explicitly guard against. Pattern
    detection: combination of integer-overflow CEx + system-entry
    caller chain through a known length-validating wrapper."""

    UNRELATED_PAIRED_POINTERS = "unrelated_paired_pointers"
    """CEx has two parameters whose names suggest a pointer pair
    (``start``/``end``, ``begin``/``end``, ``first``/``last``,
    ``src``/``dst``, ``head``/``tail``) but their backing arrays in
    the witness are independent allocations. Every real caller passes
    pointers into the SAME buffer (caller-contract), so the unrelated-
    backing CEx is unreachable from any public API. Observed on
    libarchive's ``ismode(const char *start, const char *end, …)``
    family in the 2026-05-23 archive_acl calibration."""

    NO_PATTERN = "no_pattern"
    """No known FP pattern matched — finding looks like a candidate
    real bug or an unclassified FP class."""


@dataclass
class FpEvidence:
    """The detector's verdict for one bug finding."""

    pattern: FpPattern
    confidence: float
    """0.0 - 1.0 — heuristic certainty that this is the named FP
    pattern. Used by the realism-hint injector to weigh hints by
    frequency × confidence."""

    cited_fields: list[str] = field(default_factory=list)
    """The witness-state field names that triggered the match.
    Example: ``['compare_key', 'compare_nodes']`` for an UNINIT_VTABLE
    on archive_rb."""

    cited_functions: list[str] = field(default_factory=list)
    """The call-chain functions implicated in the FP. The hint
    injector uses these to phrase the skepticism rule concretely
    ("for callers of ``__archive_rb_tree_*``, the tree object is
    initialized via ``__archive_rb_tree_init`` first.")."""


# ---------------------------------------------------------------------------
# Witness-state walker
# ---------------------------------------------------------------------------


_FN_POINTER_PAT = re.compile(
    # CBMC formats function-pointer variables as ``signed int (*)(...) name = NULL``
    # in the variable_assignments dict. The KEY is the variable name; the
    # VALUE is the function-pointer type string.
    r'^(?:const\s+)?(?:un)?signed\s+\w+\s*\(\s*\*\s*\)\s*\(.*\)$'
)


def _is_null_function_pointer(field_name: str, value: str) -> bool:
    """True if ``value`` represents a NULL function pointer.

    CBMC writes function-pointer NULLs in several forms — match any of:
      * ``((signed int (*)(struct X *, const void *))NULL)``
      * ``NULL``  (in the context of a known fn-pointer field name)
      * ``((<sig>)NULL)`` for any sig
    """
    v = value.strip()
    if v == "NULL":
        # Only treat bare NULL as a fn-pointer if the field name strongly
        # suggests it (compare_*, *_fn, *_cb, *_callback, *->ops->*).
        return _field_name_suggests_function_pointer(field_name)
    # Strip outer parens.
    if v.startswith("((") and v.endswith(")NULL)"):
        inside = v[2:-len(")NULL)")]
        # Detect a function-pointer type signature.
        if "(" in inside and "*" in inside:
            return True
    return False


def _field_name_suggests_function_pointer(name: str) -> bool:
    """Heuristic: a field name strongly suggesting a function-pointer
    callback. Matches camelCase and snake_case forms.
    """
    name_l = name.lower()
    return (
        name_l.startswith("compare_") or name_l.endswith("_fn") or
        name_l.endswith("_cb") or name_l.endswith("_callback") or
        name_l in {"ctor", "dtor", "init_fn", "hook", "free_fn",
                   "compare", "compare_nodes", "compare_key",
                   "alloc_fn", "release_fn"} or
        "->ops->" in name_l or "->vtable->" in name_l
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_pattern(
    bug_report: dict,
    classification: Optional[dict] = None,
) -> FpEvidence:
    """Inspect a bug-report dict (the on-disk ``bug_report.json``) and
    classify the likely FP pattern.

    Pure function. Optionally accepts a ``classification`` dict (the
    ``classification.json`` companion) for the witness state when the
    bug report doesn't carry it inline.
    """
    # The bug_report.json may be stored at two nesting levels:
    #   (a) outer ``{saved_at, report: {function_name, counterexample, …}}``
    #   (b) inner ``{function_name, counterexample, …}`` directly.
    # Accept either.
    if "report" in bug_report and isinstance(bug_report["report"], dict):
        report = bug_report["report"]
    else:
        report = bug_report

    # Extract the variable_assignments dict from wherever it lives.
    cex: dict = {}
    state = report.get("state") or {}
    if isinstance(state, dict) and state:
        cex = state
    if not cex:
        ce = report.get("counterexample") or {}
        if isinstance(ce, dict):
            cex = ce.get("variable_assignments") or {}
    if not cex and classification:
        ce_outer = classification.get("classification") or {}
        ce_cls = ce_outer.get("counterexample") or {}
        cex = ce_cls.get("variable_assignments") or {}

    call_chain: list[str] = report.get("call_chain") or []
    if not call_chain and classification:
        cls_inner = classification.get("classification") or {}
        call_chain = cls_inner.get("caller_path") or []

    # Pattern 1: uninit vtable — any field name + value that looks
    # like a NULL function pointer.
    null_fn_fields = [
        k for k, v in cex.items()
        if isinstance(v, str) and _is_null_function_pointer(k, v)
    ]
    if null_fn_fields:
        return FpEvidence(
            pattern=FpPattern.UNINIT_VTABLE,
            confidence=0.9 if len(null_fn_fields) >= 2 else 0.7,
            cited_fields=null_fn_fields,
            cited_functions=call_chain,
        )

    # Pattern 2: uninit container — all non-CPROVER fields nondet/NULL.
    user_fields = {
        k: v for k, v in cex.items()
        if isinstance(v, str)
        and not k.startswith("__CPROVER")
        and not k.startswith("return_value_")
        and not k.startswith("_") and not k.endswith("_buf")
    }
    if user_fields and len(user_fields) >= 3:
        nondet_count = sum(
            1 for v in user_fields.values()
            if v.strip() in ("NULL", "0", "0u", "0ul", "0l")
            or "{'name': 'unknown'}" in v
        )
        if nondet_count >= len(user_fields) * 0.8:
            return FpEvidence(
                pattern=FpPattern.UNINIT_CONTAINER,
                confidence=0.6,
                cited_fields=list(user_fields.keys())[:6],
                cited_functions=call_chain,
            )

    # Pattern 3: unrelated paired pointers.
    paired = _detect_paired_pointers(cex)
    if paired:
        return FpEvidence(
            pattern=FpPattern.UNRELATED_PAIRED_POINTERS,
            confidence=0.7,
            cited_fields=paired,
            cited_functions=call_chain,
        )

    return FpEvidence(
        pattern=FpPattern.NO_PATTERN,
        confidence=0.0,
        cited_fields=[],
        cited_functions=call_chain,
    )


# Pairs of parameter names that the harness almost certainly mis-models
# when given independent nondet backings. Each entry: (a, b) such that
# real callers always pass pointers into the SAME buffer.
_PAIRED_POINTER_NAMES: frozenset[tuple[str, str]] = frozenset({
    ("start", "end"),
    ("begin", "end"),
    ("first", "last"),
    ("src", "dst"),
    ("source", "destination"),
    ("head", "tail"),
    ("low", "high"),
    ("from", "to"),
})


def _detect_paired_pointers(cex: dict) -> list[str]:
    """Look for canonical paired-pointer parameter names where the
    witness state shows independent backings (``_<a>_buf`` and
    ``_<b>_buf`` distinct arrays). Returns the field-name list if a
    pair is detected, empty list otherwise.

    Pattern fingerprint in CBMC's variable_assignments:
      ``start = _start_buf!0@1``
      ``end   = _end_buf!0@1``  (different backing → unrelated)

    Real callers would have:
      ``start = _shared_buf!0@1``
      ``end   = _shared_buf!N@1`` (offset into same backing).
    """
    if not cex:
        return []
    for a, b in _PAIRED_POINTER_NAMES:
        if a in cex and b in cex:
            va = str(cex[a])
            vb = str(cex[b])
            if "!" not in va or "!" not in vb:
                continue
            # Pointer base = everything before the ``!``.
            base_a = va.split("!", 1)[0].strip()
            base_b = vb.split("!", 1)[0].strip()
            if base_a and base_b and base_a != base_b:
                return [a, b]
    return []


def detect_pattern_from_paths(
    bug_report_path: str | Path,
    classification_path: Optional[str | Path] = None,
) -> FpEvidence:
    """Convenience: load both JSON files and detect."""
    with open(bug_report_path) as f:
        bug = json.load(f)
    classification = None
    if classification_path:
        try:
            with open(classification_path) as f:
                classification = json.load(f)
        except FileNotFoundError:
            pass
    return detect_pattern(bug, classification)


def scan_artifact_tree(artifact_root: str | Path) -> dict[FpPattern, int]:
    """Walk every ``bug_report.json`` under *artifact_root* and tally
    detected FP patterns. Used by the autonomous outer loop to decide
    which hints are worth injecting next round.
    """
    root = Path(artifact_root)
    counts: dict[FpPattern, int] = {p: 0 for p in FpPattern}
    examples: dict[FpPattern, list[tuple[str, list[str]]]] = {p: [] for p in FpPattern}
    for br in root.rglob("bug_report.json"):
        try:
            with br.open() as f:
                bug = json.load(f)
            if not bug:
                continue
            # Outer-or-inner shape.
            top = bug.get("report") if isinstance(bug.get("report"), dict) else bug
            if not (top.get("function_name") or top.get("violated_property")):
                continue
            cls_path = br.parent / "classification.json"
            classification = None
            if cls_path.exists():
                try:
                    with cls_path.open() as f:
                        classification = json.load(f)
                except Exception:
                    classification = None
            ev = detect_pattern(bug, classification)
            counts[ev.pattern] += 1
            fn = (top.get("function_name") or br.parent.name)
            if len(examples[ev.pattern]) < 5:
                examples[ev.pattern].append((fn, ev.cited_fields))
        except Exception:
            continue
    return {"counts": counts, "examples": examples}
