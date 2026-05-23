"""
Universal preconditions for bmc-agent-lite.

Lite-mode strips the LLM-inferred precondition (every function gets
``pre=true``), which causes CBMC to find caller-contract slips —
violations that are syntactically legal but unreachable from any real
caller in practice (paired pointers from unrelated buffers, NULL
function-pointer ops tables, etc.). This module synthesises a small
set of *universally-true* preconditions from parameter names + types,
*without* asking an LLM, so the lite-mode harness gets meaningful
input constraints for the dominant FP classes.

Patterns covered today:

* **Paired pointers** — when two pointer parameters have canonical
  pair names (``start``/``end``, ``begin``/``end``, ``src``/``dst``,
  ``first``/``last``, ``head``/``tail``, ``low``/``high``,
  ``from``/``to``) the synthesised precondition emits ``a <= b``,
  which the existing ``_detect_paired_pointers`` in
  ``harness_generator.py`` picks up and uses to allocate a single
  shared backing buffer instead of two independent stack arrays.
  This is the textbook fix for the libarchive
  ``ismode(const char *start, const char *end, …)`` family of FPs
  (see 2026-05-23 calibration data).

Patterns reserved for follow-on:

* **Container ops/vtable non-null** — when a struct param has a
  recognisable ops/vtable field, emit ``param->ops != NULL``. Avoided
  in this commit because it requires parsed struct definitions and
  the FP class is already partially mitigated by Phase 4b's
  realism-prompt hints.
* **Length bounds** — ``len <= sizeof(buf)`` for paired (buf, len)
  parameters. The existing ``infer_array_param_bounds`` machinery
  already handles the common cases.

By design, universal contracts are **conservative**: they encode
properties every real caller maintains, so adding them as
preconditions doesn't mask any real bug a real caller could trigger.
The trade-off is that the contracts hide CBMC findings that would
require the property to be VIOLATED — which is exactly what we want
for the FP classes targeted here.

Off by default for non-lite modes (the LLM spec gen already produces
better preconditions per function). On by default in lite-mode via
``config.lite_with_contracts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from bmc_agent.parser import FunctionInfo


# Pairs of parameter names that real callers always derive from the
# same buffer (and where ``a <= b`` always holds). Each tuple is
# ordered: the first is the "left" pointer, the second is the "right"
# pointer; the synthesised precondition is ``<left> <= <right>``.
_PAIRED_POINTER_NAMES: tuple[tuple[str, str], ...] = (
    ("start", "end"),
    ("begin", "end"),
    ("first", "last"),
    ("head", "tail"),
    ("low", "high"),
    ("from", "to"),
    ("src", "dst"),
    ("source", "destination"),
)


def _is_pointer_type(c_type: str) -> bool:
    """Conservative pointer-type detector. Matches anything containing
    a ``*``, which captures ``char *``, ``const char *``, ``void *``,
    ``struct foo *``, ``foo **``, etc. — every form we care about for
    universal contracts.
    """
    return "*" in (c_type or "")


def derive_universal_precondition(func: "FunctionInfo") -> str:
    """Return a deterministic precondition string for *func*, built
    only from parameter names + types — no LLM, no parsed body.

    Returns ``"true"`` when no universal pattern matches, so the
    caller can use the result as a drop-in replacement for the
    lite-mode default. Multiple clauses are joined with ``&&``.

    Examples
    --------
    >>> derive_universal_precondition(fn_with_start_end_char_ptrs)
    'start <= end'

    >>> derive_universal_precondition(fn_with_no_paired_params)
    'true'
    """
    sig = getattr(func, "signature", None)
    if sig is None or not sig.parameters:
        return "true"

    pname_to_type: dict[str, str] = {}
    for ptype, pname in sig.parameters:
        if pname and ptype:
            pname_to_type[pname] = ptype

    clauses: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Pattern 1: paired pointers.
    for left, right in _PAIRED_POINTER_NAMES:
        if left in pname_to_type and right in pname_to_type:
            if not _is_pointer_type(pname_to_type[left]):
                continue
            if not _is_pointer_type(pname_to_type[right]):
                continue
            pair_key = tuple(sorted((left, right)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            clauses.append(f"{left} <= {right}")

    if not clauses:
        return "true"
    return " && ".join(clauses)


def derive_contract_summary(func: "FunctionInfo") -> dict[str, list[str]]:
    """Same as :func:`derive_universal_precondition` but returns a
    structured digest the autonomous-mode summary can log per round.
    """
    sig = getattr(func, "signature", None)
    pname_to_type: dict[str, str] = {}
    if sig and sig.parameters:
        for ptype, pname in sig.parameters:
            if pname and ptype:
                pname_to_type[pname] = ptype

    paired: list[str] = []
    for left, right in _PAIRED_POINTER_NAMES:
        if (
            left in pname_to_type
            and right in pname_to_type
            and _is_pointer_type(pname_to_type[left])
            and _is_pointer_type(pname_to_type[right])
        ):
            paired.append(f"{left} <= {right}")
    return {"paired_pointers": paired}


def known_param_pairs() -> Iterable[tuple[str, str]]:
    """Expose the pair table for tests / docs."""
    return iter(_PAIRED_POINTER_NAMES)
