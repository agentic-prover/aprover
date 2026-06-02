"""Tests for the CBMC error classifier and auto-retry registry
(Phase 1 of autonomous mode).
"""

from __future__ import annotations

import json

from bmc_agent.cbmc_error_classifier import (
    CbmcErrorClass,
    CbmcErrorDiagnosis,
    classify,
)
from bmc_agent.auto_retry_registry import (
    RetryAction,
    plan_retry,
)


def _wrap(messages: list[dict]) -> dict:
    """Build a cbmc_result.json-shaped payload from a list of CBMC messages."""
    raw = json.dumps(messages)
    return {
        "result": {
            "error": "cbmc exited with code 6",
            "raw_output": raw,
            "verified": False,
        }
    }


def _err(text: str, line: str | None = None) -> dict:
    msg: dict = {"messageText": text, "messageType": "ERROR"}
    if line is not None:
        msg["sourceLocation"] = {"line": line}
    return msg


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_classify_parse_syntax_before_id():
    d = classify(_wrap([_err("syntax error before 'off64_t'", line="42")]))
    assert d.error_class == CbmcErrorClass.PARSE_SYNTAX_BEFORE_ID
    assert d.identifier == "off64_t"
    assert d.source_line == 42


def test_classify_parse_syntax_before_star():
    d = classify(_wrap([_err("syntax error before '*'", line="200")]))
    assert d.error_class == CbmcErrorClass.PARSE_SYNTAX_BEFORE_STAR
    assert d.identifier is None
    assert d.source_line == 200


def test_classify_parse_incomplete_type():
    d = classify(_wrap([_err("incomplete type not permitted here", line="2362")]))
    assert d.error_class == CbmcErrorClass.PARSE_INCOMPLETE_TYPE
    assert d.source_line == 2362


def test_classify_convert_type_redefinition():
    d = classify(_wrap([
        _err("type symbol 'register_t' defined twice:\\nOriginal: signed long int")
    ]))
    assert d.error_class == CbmcErrorClass.CONVERT_TYPE_REDEFINITION
    assert d.identifier == "register_t"


def test_classify_convert_body_redefinition_union():
    d = classify(_wrap([
        _err("redefinition of body of 'union pthread_attr_t'")
    ]))
    assert d.error_class == CbmcErrorClass.CONVERT_BODY_REDEFINITION
    assert d.identifier == "pthread_attr_t"
    assert d.aggregate_kind == "union"


def test_classify_convert_body_redefinition_struct():
    d = classify(_wrap([_err("redefinition of body of 'struct _IO_FILE'")]))
    assert d.error_class == CbmcErrorClass.CONVERT_BODY_REDEFINITION
    assert d.identifier == "_IO_FILE"
    assert d.aggregate_kind == "struct"


def test_classify_skips_parsing_summary_errors():
    """``PARSING ERROR`` / ``CONVERSION ERROR`` are summaries that appear
    after the concrete diagnostic — the classifier must not match on them.
    """
    d = classify(_wrap([
        _err("syntax error before 'wint_t'", line="219"),
        _err("PARSING ERROR"),
    ]))
    assert d.error_class == CbmcErrorClass.PARSE_SYNTAX_BEFORE_ID
    assert d.identifier == "wint_t"


def test_classify_unknown_error():
    d = classify(_wrap([_err("something completely unexpected and weird")]))
    assert d.error_class == CbmcErrorClass.UNKNOWN
    assert "something" in d.raw_message


def test_classify_no_error_returns_unknown():
    d = classify({"result": {"verified": True, "raw_output": "", "error": None}})
    assert d.error_class == CbmcErrorClass.UNKNOWN


def test_classify_oom_fast_path():
    d = classify({
        "result": {
            "error": "Out of memory: bad_alloc",
            "raw_output": "",
            "verified": None,
        }
    })
    assert d.error_class == CbmcErrorClass.OUT_OF_MEMORY


def test_classify_accepts_inner_or_outer_dict():
    """The classifier accepts either the outer ``cbmc_result.json`` dict
    (with ``saved_at``+``result``) or the inner ``result`` dict directly.
    """
    inner = {"error": "x", "raw_output": json.dumps([_err("syntax error before 'foo'")])}
    d1 = classify({"result": inner})
    d2 = classify(inner)
    assert d1.error_class == d2.error_class == CbmcErrorClass.PARSE_SYNTAX_BEFORE_ID


# ---------------------------------------------------------------------------
# Retry registry
# ---------------------------------------------------------------------------


def test_plan_retry_syntax_before_id_maps_to_typedef_strip():
    d = CbmcErrorDiagnosis(
        error_class=CbmcErrorClass.PARSE_SYNTAX_BEFORE_ID,
        identifier="off64_t",
    )
    p = plan_retry(d)
    assert p.action == RetryAction.ADD_TYPEDEF_TO_STRIP
    assert p.target == "off64_t"


def test_plan_retry_type_redefinition_maps_to_typedef_strip():
    d = CbmcErrorDiagnosis(
        error_class=CbmcErrorClass.CONVERT_TYPE_REDEFINITION,
        identifier="register_t",
    )
    p = plan_retry(d)
    assert p.action == RetryAction.ADD_TYPEDEF_TO_STRIP
    assert p.target == "register_t"


def test_plan_retry_body_redefinition_maps_to_struct_strip():
    d = CbmcErrorDiagnosis(
        error_class=CbmcErrorClass.CONVERT_BODY_REDEFINITION,
        identifier="pthread_attr_t",
        aggregate_kind="union",
    )
    p = plan_retry(d)
    assert p.action == RetryAction.ADD_STRUCT_TO_STRIP
    assert p.target == "pthread_attr_t"
    assert p.extras.get("aggregate_kind") == "union"


def test_plan_retry_incomplete_type_with_tag_maps_to_force_opaque():
    d = CbmcErrorDiagnosis(
        error_class=CbmcErrorClass.PARSE_INCOMPLETE_TYPE,
        extras={"incomplete_tag": "archive_string_conv"},
    )
    p = plan_retry(d)
    assert p.action == RetryAction.FORCE_OPAQUE_PARAM
    assert p.target == "archive_string_conv"


def test_plan_retry_incomplete_type_without_tag_is_no_action():
    d = CbmcErrorDiagnosis(error_class=CbmcErrorClass.PARSE_INCOMPLETE_TYPE)
    p = plan_retry(d)
    assert p.action == RetryAction.NO_ACTION


def test_plan_retry_undefined_identifier_maps_to_typedef_strip():
    d = CbmcErrorDiagnosis(
        error_class=CbmcErrorClass.CONVERT_UNDEFINED_IDENTIFIER,
        identifier="__FILE",
    )
    p = plan_retry(d)
    assert p.action == RetryAction.ADD_TYPEDEF_TO_STRIP
    assert p.target == "__FILE"


def test_plan_retry_unknown_is_no_action():
    d = CbmcErrorDiagnosis(error_class=CbmcErrorClass.UNKNOWN)
    assert plan_retry(d).action == RetryAction.NO_ACTION


def test_plan_retry_timeout_maps_to_stub_callee_as_primary():
    """CBMC wall-clock timeouts: PRIMARY recovery is STUB_CALLEE
    (replace a heavy inlined callee's body with a nondet stub —
    cuts state space rather than buying more time). The pipeline
    falls back to BUMP_TIMEOUT when no callee is available to stub.

    Why STUB_CALLEE first: in ``--real-libc`` mode the harness
    ``#include``s the whole preprocessed source so callees are
    inlined by default. The state-space explosion that triggers the
    timeout is usually dominated by ONE inlined callee (recursive
    parsers, state machines), so stubbing it cuts CBMC's work
    proportionally — bumping timeout would just give the same
    explosion more wall-clock to chew through."""
    d = CbmcErrorDiagnosis(error_class=CbmcErrorClass.TIMEOUT)
    p = plan_retry(d)
    assert p.action == RetryAction.STUB_CALLEE
    # No target on this action — the pipeline picks the callee from
    # ``funcs[fn_name].callees`` since plan_retry doesn't have call-
    # graph access.
    assert p.target is None
    assert "stub" in p.reason.lower()
    assert "callee" in p.reason.lower()


def test_plan_retry_oom_remains_no_action():
    """OOM is still NO_ACTION (no automated workaround) — guard against
    the BUMP_TIMEOUT change accidentally widening the actionable set."""
    d = CbmcErrorDiagnosis(error_class=CbmcErrorClass.OUT_OF_MEMORY)
    assert plan_retry(d).action == RetryAction.NO_ACTION


def test_plan_retry_oom_is_no_action():
    d = CbmcErrorDiagnosis(error_class=CbmcErrorClass.OUT_OF_MEMORY)
    assert plan_retry(d).action == RetryAction.NO_ACTION


def test_actionable_flag():
    """``diagnosis.actionable`` is False for OOM/TIMEOUT/UNKNOWN, True
    for every parse/convert class.
    """
    for cls in CbmcErrorClass:
        d = CbmcErrorDiagnosis(error_class=cls)
        if cls in (CbmcErrorClass.OUT_OF_MEMORY, CbmcErrorClass.TIMEOUT, CbmcErrorClass.UNKNOWN):
            assert not d.actionable
        else:
            assert d.actionable


# --- vacuity guard: 0 VCCs must never be reported as verified ----------------

def test_vacuity_guard_zero_vccs_not_verified():
    """A 'VERIFICATION SUCCESSFUL' with 0 verification conditions means CBMC
    analysed nothing (e.g. the function under test had no body / wasn't linked).
    It must be demoted from verified=True to an INVALID result — else it's a
    soundness false-negative (silently passes an unexamined function)."""
    from bmc_agent.cbmc import _parse_cbmc_output
    vacuous = ('[{"messageText":"**** WARNING: no body for function add"},'
               '{"messageText":"Generated 0 VCC(s), 0 remaining after simplification"},'
               '{"result":[]},{"messageText":"VERIFICATION SUCCESSFUL"},'
               '{"cProverStatus":"success"}]')
    r = _parse_cbmc_output(vacuous, "", 0)
    assert r.verified is False
    assert r.error and "vacuous" in r.error.lower()


def test_vacuity_guard_nonzero_vccs_still_verified():
    """A genuine success (>=1 VCC, no counterexamples) stays verified=True."""
    from bmc_agent.cbmc import _parse_cbmc_output
    real = ('[{"messageText":"Generated 5 VCC(s), 5 remaining after simplification"},'
            '{"result":[]},{"messageText":"VERIFICATION SUCCESSFUL"}]')
    r = _parse_cbmc_output(real, "", 0)
    assert r.verified is True and r.error is None
