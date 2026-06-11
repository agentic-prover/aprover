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
* **bump the per-function CBMC timeout** — when CBMC times out, retry
  with double the wall-clock budget (capped at 600s). The flag-
  selection LLM agent picks an initial timeout per function but can
  underestimate; the auto-retry path lets the verifier earn more
  budget without re-running the LLM.

* **NO_ACTION** — for OOM, UNKNOWN, or actionable class with no
  identifier extracted.

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

    STUB_CALLEE = "stub_callee"
    """Replace a heavy inlined callee's body with a nondet stub and
    re-run. Primary action for ``CbmcErrorClass.TIMEOUT`` in
    ``--real-libc`` mode: the harness ``#include``s the whole
    preprocessed source so CBMC inlines callees by default, and the
    state-space explosion that causes the timeout is usually dominated
    by one or two heavy callees (recursive parsers, state machines).
    Stubbing them cuts the explored state space dramatically while
    leaving the function-under-test's own logic intact.

    Target is the CALLEE NAME picked by the pipeline's auto-retry loop
    (which has access to ``funcs[fn_name].callees`` to apply a
    heuristic — typically the longest local callee body). Applied by
    appending to ``config.session_stub_functions``; ``_generate_real_libc``
    consults the set and post-processes the included source via
    ``_replace_function_bodies_with_stubs`` to a fresh tmp file.

    Tradeoff: stubbing a callee can hide a bug that lives INSIDE it.
    Only used as a recovery from TIMEOUT, where the alternative is
    silently dropping the verdict.
    """

    BUMP_TIMEOUT = "bump_timeout"
    """Double the per-function CBMC timeout (capped at 600s) and re-run.

    The flag-selection LLM agent's initial picks are correct most of
    the time but sometimes underestimate — large parser / state-machine
    functions (e.g. archive_acl_from_text_l, archive_acl_to_text_w on
    the libarchive corpus) get the global 120s default but actually
    need 240-600s. Without this action, TIMEOUT errors are silently
    dropped at Phase 3's
    ``if verdict.error and not verdict.counterexamples: continue``
    gate — any real bug in those functions is missed.

    Applied per function (target = fn_name). Each retry round doubles
    the budget. Bounded by ``auto_retry_max_rounds`` (default 2 →
    120s, 240s, 480s) and the 600s cap (anything higher should be
    fixed by splitting the harness, not by sitting longer in CBMC).
    """

    NO_ACTION = "no_action"
    """The classifier returned a class with no hand-coded recovery
    (OOM, UNKNOWN, or actionable class with no identifier)."""


# Bounds for explosion-recovery unwind reduction (see plan_unwind_reduction).
_MIN_UNWIND_ON_REDUCE = 4
_UNWIND_REDUCE_CAP = 8


def plan_unwind_reduction(
    cur_unwind: int, *, threshold: int = 16, enabled: bool = True
) -> Optional[int]:
    """Return a REDUCED unwind bound for an explosion-class CBMC TIMEOUT, or
    ``None`` to keep the current bound (and let the caller bump the timeout
    instead — a low unwind that times out is more likely a near-miss than a
    deep-loop explosion).

    A high unwind that times out means the formula exploded from deep loop
    unrolling; more time won't help, but a smaller bound makes it tractable.
    Halve the bound, capped to a tractable level (``_UNWIND_REDUCE_CAP``) and
    floored at ``_MIN_UNWIND_ON_REDUCE``.

    SOUNDNESS is the caller's responsibility: this is safe ONLY while CBMC runs
    with ``--unwinding-assertions`` on, so that a loop able to exceed the
    reduced bound FAILS the unwinding assertion (routed to the refiner/spurious
    path, never reported clean) instead of being silently assumed to terminate.
    """
    if not enabled or cur_unwind < int(threshold):
        return None
    new_unwind = max(_MIN_UNWIND_ON_REDUCE, min(cur_unwind // 2, _UNWIND_REDUCE_CAP))
    return new_unwind if new_unwind < cur_unwind else None


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

    # --- Actionable: CBMC wall-clock timeout — stub heavy callee, then bump ---
    # Primary recovery is STUB_CALLEE: the state-space explosion that
    # causes the timeout is almost always dominated by one or two
    # inlined callees (the harness #include's the whole preprocessed
    # source in --real-libc mode). The pipeline auto-retry loop picks
    # the callee from ``funcs[fn_name].callees`` using a heuristic
    # (typically the longest local callee body), since plan_retry
    # itself doesn't have access to call-graph info.
    #
    # If the pipeline can't apply STUB_CALLEE (e.g., the function has
    # no local callees, or all candidates are already stubbed), it
    # falls back to BUMP_TIMEOUT.
    if cls == CbmcErrorClass.TIMEOUT:
        return RetryPlan(
            action=RetryAction.STUB_CALLEE,
            reason=(
                "CBMC wall-clock timeout — primary recovery is to stub "
                "a heavy inlined callee (cuts state space). Pipeline "
                "picks the callee from call-graph info; falls back to "
                "BUMP_TIMEOUT when no callee candidate exists."
            ),
        )

    # --- Non-actionable: OOM, UNKNOWN ---
    return RetryPlan(
        action=RetryAction.NO_ACTION,
        reason=f"no recovery action wired for class {cls.value}",
    )
