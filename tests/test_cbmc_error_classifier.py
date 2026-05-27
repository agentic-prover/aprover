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


def test_plan_retry_timeout_maps_to_bump_timeout():
    """CBMC wall-clock timeouts get a BUMP_TIMEOUT plan — the flag-
    selection LLM agent's initial budget is sometimes underestimated
    (e.g. archive_acl_to_text_w gets 120s default but actually needs
    240s+). Without the bump, the verdict is silently dropped at
    Phase 3 — any real bug in that function would be missed."""
    d = CbmcErrorDiagnosis(error_class=CbmcErrorClass.TIMEOUT)
    p = plan_retry(d)
    assert p.action == RetryAction.BUMP_TIMEOUT
    # No target on this action (the per-function bump happens in the
    # pipeline's per-function retry loop, not via a shared session
    # mutation).
    assert p.target is None
    # Reason includes the budget-doubling rationale so the
    # auto_retries.json audit log is self-explanatory.
    assert "timeout" in p.reason.lower()
    assert "doubl" in p.reason.lower()


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
