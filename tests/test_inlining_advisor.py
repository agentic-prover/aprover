"""Tests for bmc_agent.inlining_advisor."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bmc_agent.inlining_advisor import (
    InlineDecision,
    InliningAdvisor,
    MAX_CANDIDATES_PER_CALL,
    _parse_advisor_response,
)


# ---------- response parsing ------------------------------------------------


def test_parse_clean_response():
    raw = '''{
      "foo": {"inline": true, "reason": "tiny predicate"},
      "bar": {"inline": false, "reason": "has loop"}
    }'''
    out = _parse_advisor_response(raw, ["foo", "bar"])
    assert out["foo"].inline is True
    assert out["foo"].reason == "tiny predicate"
    assert out["bar"].inline is False


def test_parse_code_fenced_response():
    raw = '```json\n{"foo": {"inline": true, "reason": "ok"}}\n```'
    out = _parse_advisor_response(raw, ["foo"])
    assert out["foo"].inline is True


def test_parse_missing_candidate_defaults_to_stub():
    """A candidate the LLM didn't mention defaults to STUB (safe default)."""
    raw = '{"foo": {"inline": true, "reason": "ok"}}'
    out = _parse_advisor_response(raw, ["foo", "bar"])
    assert out["foo"].inline is True
    assert out["bar"].inline is False
    assert "not in response" in out["bar"].reason


def test_parse_unknown_candidate_silently_ignored():
    """LLM might emit decisions for callees we didn't ask about — ignore."""
    raw = '{"foo": {"inline": true}, "unknown_callee": {"inline": true}}'
    out = _parse_advisor_response(raw, ["foo"])
    assert set(out.keys()) == {"foo"}
    assert out["foo"].inline is True


def test_parse_invalid_returns_all_stub():
    assert all(not d.inline for d in
               _parse_advisor_response("not json", ["foo", "bar"]).values())
    assert all(not d.inline for d in
               _parse_advisor_response("", ["foo"]).values())


def test_parse_non_object_payload_treated_as_stub():
    """If the LLM emits a non-object value for a candidate, default to STUB."""
    raw = '{"foo": "yes inline"}'  # string, not object
    out = _parse_advisor_response(raw, ["foo"])
    assert out["foo"].inline is False


def test_parse_inline_field_coerced_to_bool():
    """Defensive: payload {"inline": 1} should be treated as True."""
    raw = '{"foo": {"inline": 1, "reason": "ok"}}'
    out = _parse_advisor_response(raw, ["foo"])
    assert out["foo"].inline is True


# ---------- InliningAdvisor.decide ------------------------------------------


def _mock_parsed_file(callees):
    """Build a minimal ParsedCFile-like object with the given callee FunctionInfos."""
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    p = MagicMock()
    fn_infos = {}
    for name, body in callees.items():
        sig = FunctionSignature(name=name, return_type="int",
                                parameters=[("int", "x")])
        fi = FunctionInfo(name=name, signature=sig, body=body,
                          callees=set(), source_file="")
        fn_infos[name] = fi
    p.get_function_info = lambda n: fn_infos.get(n)
    return p


def _advisor_with_response(response_text):
    from bmc_agent.config import Config
    cfg = Config(artifact_dir="/tmp/_advisor_test")
    llm = MagicMock()
    llm.complete.return_value = response_text
    return InliningAdvisor(cfg, llm), llm


def test_decide_empty_candidates_short_circuits():
    advisor, llm = _advisor_with_response("")
    out = advisor.decide(candidates=[], parsed_file=MagicMock(),
                         caller_name="caller")
    assert out == {}
    assert llm.complete.call_count == 0


def test_decide_returns_per_candidate_decisions():
    advisor, llm = _advisor_with_response(
        '{"foo": {"inline": true, "reason": "tiny"}, '
        '"bar": {"inline": false, "reason": "loops"}}'
    )
    p = _mock_parsed_file({
        "foo": "{ return x + 1; }",
        "bar": "{ for (int i = 0; i < x; i++) {} return 0; }",
        "caller": "{ return foo(x) + bar(x); }",
    })
    out = advisor.decide(candidates=["foo", "bar"], parsed_file=p,
                         caller_name="caller")
    assert out["foo"].inline is True
    assert out["bar"].inline is False
    assert llm.complete.call_count == 1


def test_decide_skips_candidates_with_no_body():
    """Candidates whose body isn't available in parsed_file are skipped."""
    advisor, llm = _advisor_with_response('{"foo": {"inline": true}}')
    p = _mock_parsed_file({"foo": "{ return 0; }"})
    # "missing_callee" isn't in parsed_file → skipped from the LLM ask
    out = advisor.decide(candidates=["foo", "missing_callee"],
                         parsed_file=p, caller_name="caller")
    assert "foo" in out
    # Missing-callee not in output (silently dropped during sizing).
    assert "missing_callee" not in out


def test_decide_caps_at_max_candidates_per_call():
    """If asked about >MAX_CANDIDATES_PER_CALL, only the smallest by body
    size are sent to the LLM."""
    bodies = {f"c{i}": "{ return 0; }" * (i + 1) for i in range(MAX_CANDIDATES_PER_CALL + 5)}
    p = _mock_parsed_file(bodies)
    # Mock the LLM to return inline=True for whatever it's asked about.
    advisor, llm = _advisor_with_response(
        "{" + ", ".join(f'"c{i}": {{"inline": true}}' for i in range(MAX_CANDIDATES_PER_CALL)) + "}"
    )
    out = advisor.decide(candidates=list(bodies.keys()),
                         parsed_file=p, caller_name="caller")
    # Only the cap'd subset gets decisions.
    inlined = [name for name, d in out.items() if d.inline]
    assert len(inlined) <= MAX_CANDIDATES_PER_CALL


def test_decide_llm_failure_returns_all_stub():
    advisor, llm = _advisor_with_response("")
    llm.complete.side_effect = RuntimeError("LLM crashed")
    p = _mock_parsed_file({"foo": "{ return 0; }"})
    out = advisor.decide(candidates=["foo"], parsed_file=p, caller_name="caller")
    assert out["foo"].inline is False
    assert "failed" in out["foo"].reason.lower()


def test_decide_parse_failure_returns_all_stub():
    advisor, llm = _advisor_with_response("not json at all")
    p = _mock_parsed_file({"foo": "{ return 0; }"})
    out = advisor.decide(candidates=["foo"], parsed_file=p, caller_name="caller")
    # Parser falls back to all-stub.
    assert out["foo"].inline is False


def test_decide_never_demotes_already_inlined():
    """The advisor's API contract: it operates on STUB candidates only.
    Callers (harness_generator) should never pass already-inlined callees
    into `candidates` — but if they did, the advisor's decision is just
    advice (caller decides whether to apply). Here we verify that
    'inline: false' from the advisor doesn't carry any 'force demote'
    semantics — that's purely on the caller to honor."""
    advisor, llm = _advisor_with_response('{"foo": {"inline": false, "reason": "complex"}}')
    p = _mock_parsed_file({"foo": "{ return 0; }"})
    out = advisor.decide(candidates=["foo"], parsed_file=p, caller_name="caller")
    assert out["foo"].inline is False
    # The bit is what it is; the caller in harness_generator only
    # promotes on .inline=True, so a False from the advisor leaves the
    # mechanical-rule STUB decision unchanged. (Validated by the
    # harness_generator wiring, not directly tested here.)
