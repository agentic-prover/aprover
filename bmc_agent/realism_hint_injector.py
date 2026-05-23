"""
Realism-prompt hint injection (Phase 4b of autonomous mode).

After an autonomous round completes, scan every confirmed-real bug
finding through the FP-pattern detector. When a pattern recurs N or
more times across the round's findings, generate a constrained
skepticism hint paragraph that the next round's realism checker
prepends to its system prompt. The intent is to converge on stronger
filtering for FP classes that the static realism prompt already
struggles with — caller-contract slips being the canonical case.

Why constrained? Free-form prompt edits can erode the realism
checker's hard decision rules (REQ-1 source-line guard, REQ-2
public-API chain, etc.). The injector only adds *additional skepticism
context* — it never replaces or overrides the existing decision logic.
The hint paragraph format is:

    ADDITIONAL SKEPTICISM CONTEXT (learned from prior rounds):
    <one paragraph per detected pattern, max 3>

Each hint is keyed on a stable :class:`FpPattern` value, so the same
hint never accumulates twice across rounds.

The injector is *pure*: it reads the artifact tree, computes hints,
writes them to ``<output>/learned_realism_hints.md`` and returns the
text. Wiring into the realism checker is done by callers reading
``config.realism_extra_skepticism`` and passing it into the prompt
template.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bmc_agent.fp_pattern_detector import FpEvidence, FpPattern, scan_artifact_tree


# Minimum number of distinct findings matching the same pattern before
# we promote it to a hint. Below this threshold, the pattern is noise.
_DEFAULT_PATTERN_THRESHOLD = 3


# Hint templates per pattern. Each one is a constrained paragraph: it
# names the pattern + the typical witness + a concrete instruction the
# realism checker can act on. No imperative override of the existing
# decision rules — only added context.
_HINT_TEMPLATES: dict[FpPattern, str] = {
    FpPattern.UNINIT_VTABLE: (
        "Pattern observed in {count} prior finding(s) this sweep: the "
        "counterexample witness sets a function pointer in a "
        "container's ops/vtable field to NULL (typical fields: "
        "{fields}). In practice, real callers always initialise that "
        "container via a dedicated init/setup call before invoking "
        "the function under test (typical init wrappers observed: "
        "{functions}). When the witness depends on such a NULL "
        "function pointer being invoked, treat it as a caller-"
        "contract slip and prefer UNREALISTIC unless the call-site "
        "analysis demonstrates a path that genuinely bypasses the "
        "init call."
    ),
    FpPattern.UNINIT_CONTAINER: (
        "Pattern observed in {count} prior finding(s) this sweep: the "
        "counterexample witness has every user field of the entry "
        "parameter at its default nondet/zero value, indicating an "
        "uninitialised container. Typical fields involved: {fields}. "
        "When the function under test is a utility helper for an "
        "opaque-container type and the witness depends on a "
        "completely zeroed container, the violation is usually "
        "unreachable from a real public-API entry point — every real "
        "caller will have populated at least the type-tag / sentinel "
        "fields. Lean UNCERTAIN before voting REALISTIC."
    ),
    FpPattern.UNRELATED_PAIRED_POINTERS: (
        "Pattern observed in {count} prior finding(s) this sweep: the "
        "function takes a canonical paired-pointer signature ("
        "{fields} — typical pairs: start/end, begin/end, src/dst, "
        "first/last). The counterexample witness places the two "
        "pointers in UNRELATED backing buffers, but every real caller "
        "passes pointers into the SAME buffer (start ≤ end, both "
        "indexing one allocation). When the violation depends on the "
        "two pointers being from different objects, it's a harness "
        "artifact, not a reachable bug. Prefer UNREALISTIC unless the "
        "caller-site analysis shows a path that genuinely passes "
        "unrelated buffers (extremely rare in real C code)."
    ),
}


@dataclass
class HintBundle:
    """The injector's output for one round."""

    text: str = ""
    """Markdown-formatted hint paragraphs ready to be prepended to the
    realism system prompt. Empty when no pattern crossed the
    threshold."""

    patterns_observed: dict = field(default_factory=dict)
    """Map of ``FpPattern.value`` → count, for telemetry."""

    examples: dict = field(default_factory=dict)
    """Map of ``FpPattern.value`` → up to N (function_name, cited_fields)
    examples, used to render concrete instances in the hint text."""


def collect_hints(
    artifact_root: str | Path,
    threshold: int = _DEFAULT_PATTERN_THRESHOLD,
) -> HintBundle:
    """Scan the round's bug-report artifacts and produce a hint bundle.

    Patterns appearing fewer than ``threshold`` times are dropped to
    avoid promoting noise. Returns an empty bundle when no pattern
    qualifies.
    """
    scan = scan_artifact_tree(artifact_root)
    counts = {p.value: n for p, n in scan["counts"].items() if n > 0}
    examples = {p.value: scan["examples"][p] for p in scan["examples"]}

    qualifying: list[tuple[FpPattern, int]] = []
    for pat in FpPattern:
        n = scan["counts"].get(pat, 0)
        if n >= threshold and pat in _HINT_TEMPLATES:
            qualifying.append((pat, n))

    if not qualifying:
        return HintBundle(patterns_observed=counts, examples=examples)

    # Render at most 3 hints — too many erodes the prompt's signal-to-
    # noise ratio. Sort by frequency descending so the most common
    # pattern leads.
    qualifying.sort(key=lambda kv: kv[1], reverse=True)
    paragraphs: list[str] = []
    for pat, n in qualifying[:3]:
        template = _HINT_TEMPLATES[pat]
        # Aggregate field + function examples for the template slots.
        ex = scan["examples"][pat]
        fields = sorted({f for _, fields in ex for f in (fields or [])})[:6]
        functions = sorted({fn for fn, _ in ex})[:6]
        paragraphs.append(
            "* " + template.format(
                count=n,
                fields=", ".join(fields) or "(see artifact)",
                functions=", ".join(functions) or "(see artifact)",
            )
        )

    text = (
        "ADDITIONAL SKEPTICISM CONTEXT (learned from prior rounds):\n\n"
        + "\n\n".join(paragraphs)
        + "\n"
    )
    return HintBundle(text=text, patterns_observed=counts, examples=examples)


def persist_hints(
    bundle: HintBundle, output_root: str | Path, round_idx: int
) -> Path:
    """Write the bundle's hint text to ``<output>/learned_realism_hints.md``.

    Each round's hints overwrite the file — we want the *latest*
    aggregated view (since patterns can rise or fall), not an append-
    log. The autonomous round summary already retains the per-round
    counts for audit.
    """
    out = Path(output_root) / "learned_realism_hints.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = f"<!-- generated after autonomous round {round_idx + 1} -->\n\n"
    out.write_text(header + (bundle.text or "(no patterns crossed threshold this round)\n"))
    return out


def realism_extra_skepticism(bundle: HintBundle) -> Optional[str]:
    """Return the hint text suitable for assignment to
    ``config.realism_extra_skepticism``. ``None`` when the bundle is
    empty so callers can leave the field unset.
    """
    return bundle.text or None
