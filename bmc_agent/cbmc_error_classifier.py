"""
CBMC error classification (Phase 1 of autonomous mode).

Reads ``cbmc_result.json`` raw output and classifies the first ERROR
message into a finite taxonomy. The taxonomy drives the auto-retry
registry in :mod:`bmc_agent.auto_retry_registry`, which proposes a
runtime fix (force-opaque a parameter, strip an extra typedef, etc.)
that can be applied without modifying bmc-agent source code.

Two-stage retry loop (in pipeline.py after Phase 2):

    function CBMC-errors → classify → retry plan → regen harness →
    re-run CBMC → if still errored, classify again → registry returns
    NO_ACTION → give up and report.

The classifier never alters program state. All it does is parse the
CBMC raw output and return a :class:`CbmcErrorDiagnosis`. The retry
registry decides what to do with the diagnosis.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CbmcErrorClass(str, Enum):
    """Finite taxonomy of CBMC failure modes the auto-retry layer can act on.

    Only the classes flagged "actionable" below are reachable by the
    registry's recovery actions. UNKNOWN / OOM / TIMEOUT are bucketed
    separately so the pipeline can decide whether to skip, retry with a
    smaller unwind, or give up.
    """

    # Parse-time errors (CBMC's C frontend rejected the harness).
    PARSE_UNDEFINED_TYPEDEF = "parse_undefined_typedef"  # actionable
    PARSE_INCOMPLETE_TYPE = "parse_incomplete_type"      # actionable
    PARSE_SYNTAX_BEFORE_STAR = "parse_syntax_before_star"  # actionable (typically same root cause)
    PARSE_SYNTAX_BEFORE_ID = "parse_syntax_before_id"      # actionable

    # Convert-time errors (parse OK, type-check / symbol-table reject).
    CONVERT_TYPE_REDEFINITION = "convert_type_redefinition"  # actionable
    CONVERT_BODY_REDEFINITION = "convert_body_redefinition"  # actionable
    CONVERT_UNDEFINED_IDENTIFIER = "convert_undefined_identifier"  # actionable

    # Resource limits / unclassified.
    OUT_OF_MEMORY = "out_of_memory"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class CbmcErrorDiagnosis:
    """The classifier's verdict for a single failed CBMC run."""

    error_class: CbmcErrorClass
    """The taxonomy class the error belongs to."""

    identifier: Optional[str] = None
    """The offending typedef / struct tag / identifier, when extractable.
    For ``CONVERT_BODY_REDEFINITION`` this is the struct/union tag.
    For ``PARSE_UNDEFINED_TYPEDEF`` / ``CONVERT_UNDEFINED_IDENTIFIER`` this
    is the missing identifier. For syntax-before patterns it's the token
    that triggered the syntax error.
    """

    aggregate_kind: Optional[str] = None
    """For BODY_REDEFINITION: 'struct' or 'union'. None otherwise."""

    source_line: Optional[int] = None
    """Line in the harness where the error fired, if reported by CBMC."""

    raw_message: str = ""
    """The first CBMC ERROR-level messageText, verbatim. Useful for
    logs and for debugging when ``error_class == UNKNOWN``.
    """

    extras: dict[str, Any] = field(default_factory=dict)
    """Free-form bag for future class-specific data."""

    @property
    def actionable(self) -> bool:
        """True iff the registry has at least one recovery action wired
        up for this class. (UNKNOWN/OOM/TIMEOUT are never actionable.)
        """
        return self.error_class not in (
            CbmcErrorClass.OUT_OF_MEMORY,
            CbmcErrorClass.TIMEOUT,
            CbmcErrorClass.UNKNOWN,
        )


# ---------------------------------------------------------------------------
# Pattern table
# ---------------------------------------------------------------------------
#
# Patterns are matched in order; first match wins. The "extractor" returns
# the (identifier, aggregate_kind) tuple to populate the diagnosis.
#
# These patterns are validated against CBMC 5.95.1 outputs from the
# libarchive sweep (4829 failures across 124 files). New patterns added
# only when an UNKNOWN bucket on a real sweep proves we missed one.

_PARSE_INCOMPLETE_TYPE = re.compile(r"incomplete type not permitted here")
_PARSE_SYNTAX_BEFORE_QUOTED_ID = re.compile(r"syntax error before '(\w+)'")
_PARSE_SYNTAX_BEFORE_STAR = re.compile(r"syntax error before '\*'")
# CBMC's "defined twice" message format. Captures the colliding symbol name.
_CONVERT_TYPE_REDEFINITION = re.compile(
    r"type symbol '(\w+)' defined twice"
)
# CBMC body-redefinition format: literal newlines are stored as ``\n`` in
# the JSON-encoded raw output, so the regex sees a backslash + "n". Allow
# both forms to keep the matcher robust across CBMC versions.
_CONVERT_BODY_REDEFINITION = re.compile(
    r"redefinition of body of '(struct|union) (\w+)'"
)
_CONVERT_UNDEFINED_IDENTIFIER = re.compile(
    r"undefined identifier '(\w+)'|unknown type name '(\w+)'"
)

_OOM_HINTS = (
    "Out of memory", "std::bad_alloc", "Cannot allocate memory",
)
_TIMEOUT_HINTS = ("Timed out", "timeout",)


def classify(cbmc_result: dict) -> CbmcErrorDiagnosis:
    """Inspect a parsed ``cbmc_result.json`` payload and produce a diagnosis.

    Accepts the *outer* dict (the one ``ArtifactStore.save_cbmc_result``
    writes, with keys ``saved_at`` and ``result``) OR the inner
    ``result`` dict directly. Robust to both — callers in different
    parts of the codebase pass different layers.
    """
    inner = cbmc_result.get("result") if "result" in cbmc_result else cbmc_result
    if not isinstance(inner, dict):
        return CbmcErrorDiagnosis(
            error_class=CbmcErrorClass.UNKNOWN,
            raw_message="cbmc_result has no usable inner dict",
        )

    err_field = inner.get("error") or ""
    raw_output = inner.get("raw_output") or ""

    if not err_field and inner.get("verified") is not None:
        # No error → no diagnosis (caller should not have called us).
        return CbmcErrorDiagnosis(
            error_class=CbmcErrorClass.UNKNOWN,
            raw_message="cbmc_result has no error",
        )

    # Resource-limit fast paths.
    for hint in _OOM_HINTS:
        if hint in err_field or hint in raw_output:
            return CbmcErrorDiagnosis(
                error_class=CbmcErrorClass.OUT_OF_MEMORY,
                raw_message=err_field or hint,
            )
    for hint in _TIMEOUT_HINTS:
        if hint.lower() in err_field.lower():
            return CbmcErrorDiagnosis(
                error_class=CbmcErrorClass.TIMEOUT,
                raw_message=err_field,
            )

    # Find the first ERROR-level message in the raw output.
    messages = _extract_messages(raw_output)
    error_msgs = [m for m in messages if "error" in m.get("type", "").lower()]
    if not error_msgs:
        return CbmcErrorDiagnosis(
            error_class=CbmcErrorClass.UNKNOWN,
            raw_message=err_field or "no ERROR-level message in raw_output",
        )

    # Walk error messages in order; first concrete pattern wins.
    for em in error_msgs:
        text = em.get("text", "")
        source_line = em.get("line")
        # Skip the bare "PARSING ERROR" / "CONVERSION ERROR" summaries
        # that CBMC emits after the specific diagnostic.
        if text in ("PARSING ERROR", "CONVERSION ERROR"):
            continue

        m = _CONVERT_BODY_REDEFINITION.search(text)
        if m:
            return CbmcErrorDiagnosis(
                error_class=CbmcErrorClass.CONVERT_BODY_REDEFINITION,
                identifier=m.group(2),
                aggregate_kind=m.group(1),
                source_line=source_line,
                raw_message=text,
            )
        m = _CONVERT_TYPE_REDEFINITION.search(text)
        if m:
            return CbmcErrorDiagnosis(
                error_class=CbmcErrorClass.CONVERT_TYPE_REDEFINITION,
                identifier=m.group(1),
                source_line=source_line,
                raw_message=text,
            )
        m = _CONVERT_UNDEFINED_IDENTIFIER.search(text)
        if m:
            return CbmcErrorDiagnosis(
                error_class=CbmcErrorClass.CONVERT_UNDEFINED_IDENTIFIER,
                identifier=m.group(1) or m.group(2),
                source_line=source_line,
                raw_message=text,
            )
        if _PARSE_INCOMPLETE_TYPE.search(text):
            # Identifier not present in the message itself; the caller
            # may extract it from the surrounding harness line via
            # the ``source_line`` hint.
            return CbmcErrorDiagnosis(
                error_class=CbmcErrorClass.PARSE_INCOMPLETE_TYPE,
                source_line=source_line,
                raw_message=text,
            )
        m = _PARSE_SYNTAX_BEFORE_QUOTED_ID.search(text)
        if m:
            return CbmcErrorDiagnosis(
                error_class=CbmcErrorClass.PARSE_SYNTAX_BEFORE_ID,
                identifier=m.group(1),
                source_line=source_line,
                raw_message=text,
            )
        if _PARSE_SYNTAX_BEFORE_STAR.search(text):
            return CbmcErrorDiagnosis(
                error_class=CbmcErrorClass.PARSE_SYNTAX_BEFORE_STAR,
                source_line=source_line,
                raw_message=text,
            )

    # No concrete pattern matched any error message — bucket as unknown
    # but preserve the first error text so a human can triage.
    first_text = error_msgs[0].get("text", "")
    return CbmcErrorDiagnosis(
        error_class=CbmcErrorClass.UNKNOWN,
        raw_message=first_text or err_field,
    )


def classify_path(path: str) -> CbmcErrorDiagnosis:
    """Convenience: load a ``cbmc_result.json`` from disk and classify it."""
    with open(path) as f:
        return classify(json.load(f))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# CBMC's raw_output is a JSON-encoded *string* containing a JSON array.
# Parse that out so we can iterate over messages structurally instead of
# regexing the whole blob. Fall back to text-scan if it doesn't parse.

def _extract_messages(raw_output: str) -> list[dict[str, Any]]:
    if not raw_output:
        return []
    # Try the structured path first.
    try:
        arr = json.loads(raw_output)
    except json.JSONDecodeError:
        arr = None
    if isinstance(arr, list):
        out: list[dict[str, Any]] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            text = item.get("messageText", "") or ""
            mtype = item.get("messageType", "") or ""
            loc = item.get("sourceLocation") or {}
            line: Optional[int] = None
            if isinstance(loc, dict):
                lstr = loc.get("line")
                if isinstance(lstr, str) and lstr.isdigit():
                    line = int(lstr)
                elif isinstance(lstr, int):
                    line = lstr
            out.append({"text": text, "type": mtype, "line": line})
        return out

    # Unstructured fallback: split on "messageText" / "messageType"
    # boundaries — best-effort only.
    text_msgs: list[dict[str, Any]] = []
    for m in re.finditer(
        r'"messageText"\s*:\s*"((?:[^"\\]|\\.)*)"[^{]*?"messageType"\s*:\s*"(\w+)"',
        raw_output,
        re.DOTALL,
    ):
        text_msgs.append({"text": m.group(1), "type": m.group(2), "line": None})
    return text_msgs
