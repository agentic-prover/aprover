"""
Auto-retry registry (Phase 1 of autonomous mode).

Given a :class:`~bmc_agent.cbmc_error_classifier.CbmcErrorDiagnosis`,
return a :class:`RetryPlan` describing a *runtime* fix the pipeline can
apply without modifying bmc-agent source code. The fix is one of:

* **add a typedef name to a session-local strip set** — the
  harness generator consults this set alongside ``_SYSTEM_TYPEDEF_NAMES``
  so the next harness regen drops the offending typedef and (via the
  cascade-strip) every forward declaration referencing it.
* **add a struct/union name to a session-local strip set** — same
  pattern, but for body-redefinition errors against CBMC's libc model.
* **force a parameter to be treated as opaque** — when CBMC reports
  ``incomplete type not permitted here`` for a struct that's only
  forward-declared in the harness TU, regen with that struct's
  parameters as nondet pointers.
* **NO_ACTION** — for OOM, TIMEOUT, or UNKNOWN where the registry has
  no automated workaround.

The registry is *deterministic* and hand-coded. No LLM in this layer.
That's the whole point of Phase 1: high-confidence recoveries for the
finite taxonomy of known CBMC failure modes. Phase 3 will add an LLM
agent that proposes patches for the UNKNOWN bucket, but only behind
heavy safety gates.

Empirical taxonomy was derived from the 2026-05-23 libarchive sweep
(4829 CBMC failures across 124 files):

    4657  parse_syntax_before_id    e.g. off64_t / fpos64_t / btowc
     154  parse_syntax_before_star  (decls referencing __sighandler_t, ...)
      18  convert_type_redefinition e.g. register_t
       0  convert_body_redefinition (would have been pthread_attr_t, ...)
       0  parse_incomplete_type     (would have been opaque struct params)

Many of those layers are now fixed structurally in
``harness_generator.py`` (commits ``c44d498``, ``655cf1f``). The
registry handles the residual cases — codebases that surface a NEW
typedef / NEW struct / NEW opaque param that the static
``_SYSTEM_TYPEDEF_NAMES`` and ``_GLIBC_KNOWN_STRUCTS`` sets don't yet
cover. Each registry hit becomes a candidate for promotion into the
static sets after a human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from bmc_agent.cbmc_error_classifier import CbmcErrorClass, CbmcErrorDiagnosis


class RetryAction(str, Enum):
    """The set of *runtime* recovery actions the pipeline can apply.

    Every action results in a harness regen + a re-run of CBMC for the
    affected function. Bounded retry (default 2 attempts per function)
    is enforced by the caller.
    """

    ADD_TYPEDEF_TO_STRIP = "add_typedef_to_strip"
    """Append ``target`` to ``config.session_strip_typedefs``.
    The next harness regen will strip the typedef body and (via the
    existing cascade rule) every forward declaration referencing it."""

    ADD_STRUCT_TO_STRIP = "add_struct_to_strip"
    """Append ``target`` to ``config.session_strip_structs``.
    The next harness regen will strip the struct/union body and leave
    a forward declaration in place — CBMC's built-in libc model fills
    in any matching definition."""

    FORCE_OPAQUE_PARAM = "force_opaque_param"
    """Treat parameters of struct type ``target`` as opaque (nondet
    pointer, no stack-allocated backing). Resolves
    ``incomplete type not permitted here`` for opaque-handle params
    whose body lives in another TU."""

    NO_ACTION = "no_action"
    """The classifier returned a class with no hand-coded recovery
    (OOM, TIMEOUT, UNKNOWN, or actionable class with no identifier)."""


@dataclass(frozen=True)
class RetryPlan:
    """What the pipeline should do to recover from a CBMC error."""

    action: RetryAction
    target: Optional[str] = None
    """The identifier the action acts on (typedef name, struct tag).
    ``None`` for ``NO_ACTION``."""

    reason: str = ""
    """Human-readable explanation of why this plan was chosen. Logged
    to ``auto_retries.json`` for audit / promotion review."""

    extras: dict[str, str] = field(default_factory=dict)
    """Class-specific data the action may need (e.g. aggregate_kind
    for ADD_STRUCT_TO_STRIP)."""


def plan_retry(diag: CbmcErrorDiagnosis) -> RetryPlan:
    """Map a diagnosis to a retry plan.

    Pure function; never raises. Returns ``RetryPlan(NO_ACTION, …)``
    rather than ``None`` so callers can always destructure the result.
    """
    cls = diag.error_class
    tag = diag.identifier

    # --- Actionable: convert-time type redefinition ---
    if cls == CbmcErrorClass.CONVERT_TYPE_REDEFINITION and tag:
        return RetryPlan(
            action=RetryAction.ADD_TYPEDEF_TO_STRIP,
            target=tag,
            reason=f"typedef '{tag}' collides with CBMC built-in libc; strip the harness's variant",
        )

    # --- Actionable: convert-time body redefinition (struct/union) ---
    if cls == CbmcErrorClass.CONVERT_BODY_REDEFINITION and tag:
        return RetryPlan(
            action=RetryAction.ADD_STRUCT_TO_STRIP,
            target=tag,
            reason=f"{diag.aggregate_kind or 'struct'} '{tag}' body redefines CBMC built-in; strip body to forward decl",
            extras={"aggregate_kind": diag.aggregate_kind or "struct"},
        )

    # --- Actionable: parse-time incomplete type ---
    # The classifier couldn't extract the tag (CBMC's message doesn't
    # name it), so caller must look it up from the harness source line.
    # If they pass the tag in via ``diag.extras["incomplete_tag"]`` we
    # use it; otherwise NO_ACTION.
    if cls == CbmcErrorClass.PARSE_INCOMPLETE_TYPE:
        ext_tag = diag.extras.get("incomplete_tag") if diag.extras else None
        if ext_tag:
            return RetryPlan(
                action=RetryAction.FORCE_OPAQUE_PARAM,
                target=ext_tag,
                reason=f"struct '{ext_tag}' body not visible in harness TU; emit nondet pointer for params",
            )
        return RetryPlan(
            action=RetryAction.NO_ACTION,
            reason="incomplete-type error but tag couldn't be extracted from message; caller should retry with extras['incomplete_tag']",
        )

    # --- Actionable: parse-time syntax error before an identifier ---
    # The identifier is the token CBMC choked on. In every observed
    # case in the libarchive sweep, it was a glibc-internal typedef
    # that needed stripping (e.g. ``off64_t``, ``btowc``, ``fpos64_t``).
    # Add it to the session strip set so the next regen drops it (and
    # cascade-strips every forward decl that references it).
    if cls == CbmcErrorClass.PARSE_SYNTAX_BEFORE_ID and tag:
        return RetryPlan(
            action=RetryAction.ADD_TYPEDEF_TO_STRIP,
            target=tag,
            reason=f"'{tag}' is referenced but not declared in the harness TU; treat as a stripped typedef so cascade rule removes its forward decls",
        )

    # --- Actionable: convert-time undefined identifier ---
    # Same recovery as the parse-time version.
    if cls == CbmcErrorClass.CONVERT_UNDEFINED_IDENTIFIER and tag:
        return RetryPlan(
            action=RetryAction.ADD_TYPEDEF_TO_STRIP,
            target=tag,
            reason=f"identifier '{tag}' is referenced but undefined in the converted TU; strip its forward decls",
        )

    # --- ``syntax error before '*'`` ---
    # No identifier in the message itself; this pattern almost always
    # means the *previous* token was a stripped typedef. The pipeline
    # caller is responsible for extracting that token from the harness
    # line and passing it via ``diag.extras["prev_token"]``. Without it,
    # NO_ACTION.
    if cls == CbmcErrorClass.PARSE_SYNTAX_BEFORE_STAR:
        prev = diag.extras.get("prev_token") if diag.extras else None
        if prev:
            return RetryPlan(
                action=RetryAction.ADD_TYPEDEF_TO_STRIP,
                target=prev,
                reason=f"'{prev}' immediately precedes the '*' CBMC choked on; treat as a stripped typedef",
            )
        return RetryPlan(
            action=RetryAction.NO_ACTION,
            reason="syntax-before-* without a prev_token hint; caller should resolve and retry",
        )

    # --- Non-actionable: OOM, TIMEOUT, UNKNOWN ---
    return RetryPlan(
        action=RetryAction.NO_ACTION,
        reason=f"no recovery action wired for class {cls.value}",
    )
