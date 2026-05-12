"""
Tests for Phase 3 Realism Checker.

Covers:
1. RealismCheckResult dataclass and verdict enum
2. Prompt construction (correct fields populated, no missing keys)
3. LLM response parsing (all three verdicts, low-confidence guard)
4. Disabled-by-default behaviour (skipped when enable_realism_check=False)
5. Confidence downgrade: UNREALISTIC+high/medium → "unlikely" in BugReport
6. UNREALISTIC+low confidence does NOT downgrade BugReport
7. Integration smoke test via mocked LLM
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_C = REPO_ROOT / "examples" / "simple_driver.c"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(enable_realism_check: bool = True, enable_realism_thinking: bool = False):
    from bmc_agent.config import Config
    return Config(
        llm_api_key="test",
        enable_realism_check=enable_realism_check,
        enable_realism_thinking=enable_realism_thinking,
    )


def _make_counterexample(failing_property: str = "null-pointer-dereference.p"):
    from bmc_agent.cbmc import Counterexample
    return Counterexample(
        failing_property=failing_property,
        variable_assignments={"buf": "0x0 (NULL)", "len": "4"},
        trace=["main -> rb_write -> ..."],
    )


def _make_func_info():
    from bmc_agent.parser import parse_c_file
    parsed = parse_c_file(str(EXAMPLE_C))
    return parsed.get_function_info("rb_write")


def _make_validation_result(func_name: str = "rb_write"):
    from bmc_agent.cex_validator import CExOutcome, ValidationResult
    cex = _make_counterexample()
    result = ValidationResult(
        function_name=func_name,
        counterexample=cex,
        caller_path=["dev_write", func_name],
        system_entry_input=None,
        refinement_history=[],
        final_precondition=None,
        reasoning="CBMC confirmed reachability from dev_write.",
        outcome=CExOutcome.REAL_BUG,
    )
    return result


def _make_llm_response(verdict: str, confidence: str = "high", key_concern: str = "") -> str:
    # REALISTIC verdicts now require concrete REQ-1 (source-line guard
    # analysis) and REQ-2 (public-API call chain) evidence; without it,
    # the parser auto-downgrades to UNCERTAIN. Tests asserting REALISTIC
    # must supply both so they exercise the verdict path rather than the
    # downgrade path.
    payload = {
        "verdict": verdict,
        "reasoning": f"Step-by-step analysis for {verdict}.",
        "key_concern": key_concern or "concrete exploit scenario for test",
        "confidence": confidence,
    }
    if verdict.upper() == "REALISTIC":
        payload["source_line_guard"] = (
            "rb_write at line 14 dereferences rb->buf without any preceding "
            "if(rb==NULL) check; the function trusts its caller, and the "
            "violation point is the assignment rb->buf[rb->pos] = byte."
        )
        payload["public_api_call_chain"] = (
            "external_input → public_api_entry(buf, len) → "
            "rb_write(rb, buf, len) where buf == NULL and rb is uninitialised"
        )
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# 1. Basic types
# ---------------------------------------------------------------------------

def test_realism_verdict_values():
    from bmc_agent.realism_checker import RealismVerdict
    assert RealismVerdict.REALISTIC.value == "realistic"
    assert RealismVerdict.UNREALISTIC.value == "unrealistic"
    assert RealismVerdict.UNCERTAIN.value == "uncertain"


def test_realism_check_result_to_dict():
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    r = RealismCheckResult(
        verdict=RealismVerdict.UNREALISTIC,
        reasoning="test reason",
        key_concern="NULL pointer injected",
        llm_confidence="high",
    )
    d = r.to_dict()
    assert d["verdict"] == "unrealistic"
    assert d["key_concern"] == "NULL pointer injected"
    assert d["llm_confidence"] == "high"


# ---------------------------------------------------------------------------
# 2. Disabled-by-default
# ---------------------------------------------------------------------------

def test_realism_checker_skipped_when_disabled():
    """When enable_realism_check=False the result should be UNCERTAIN/skipped."""
    from bmc_agent.realism_checker import RealismChecker, RealismVerdict

    config = _make_config(enable_realism_check=False)
    mock_llm = MagicMock()
    checker = RealismChecker(config=config, llm=mock_llm)

    func = _make_func_info()
    assert func is not None

    from bmc_agent.parser import parse_c_file
    parsed = parse_c_file(str(EXAMPLE_C))
    from bmc_agent.spec import Spec
    spec = Spec(function_name="rb_write", precondition="true", postcondition="true")

    result = checker.check(
        func=func,
        counterexample=_make_counterexample(),
        validation_result=_make_validation_result(),
        parsed_file=parsed,
        all_funcs={},
        spec=spec,
    )

    assert result.verdict == RealismVerdict.UNCERTAIN
    assert "skipped" in result.reasoning.lower()
    mock_llm.complete.assert_not_called()


# ---------------------------------------------------------------------------
# 3. LLM response parsing
# ---------------------------------------------------------------------------

def test_parse_realistic_verdict():
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    r = _parse_result(_make_llm_response("REALISTIC", "high"), "rb_write")
    assert r.verdict == RealismVerdict.REALISTIC
    assert r.llm_confidence == "high"


def test_parse_unrealistic_verdict():
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    r = _parse_result(_make_llm_response("UNREALISTIC", "medium", "NULL never passed"), "rb_write")
    assert r.verdict == RealismVerdict.UNREALISTIC
    assert r.key_concern == "NULL never passed"


def test_parse_uncertain_verdict():
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    r = _parse_result(_make_llm_response("UNCERTAIN", "low"), "rb_write")
    assert r.verdict == RealismVerdict.UNCERTAIN


def test_realistic_downgraded_when_reasoning_says_artifact():
    """REALISTIC verdict with reasoning that admits it's a CBMC artifact
    is downgraded to UNREALISTIC.

    Regression: every "realistic" finding in this session's bounty runs
    (OpenSSL ASN1_STRING_type_new, curl curl_url_dup / curl_url_cleanup,
    parsedate datestring->checktz) had reasoning explicitly identifying
    the witness as a stub-returns-NULL or CBMC modelling artifact while
    the verdict field still said REALISTIC.
    """
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    payload = {
        "verdict": "REALISTIC",
        "reasoning": (
            "The counterexample shows Curl_ccalloc=NULL which is a CBMC "
            "modelling artifact; real curl initialises this via "
            "curl_global_init. The function's if(u) guard handles a NULL "
            "return correctly."
        ),
        "key_concern": "stub returns NULL but real allocator wouldn't",
        "confidence": "medium",
        "source_line_guard": (
            "if(u) at line 1234 protects the dereference path; only "
            "reachable when curlx_calloc returns NULL"
        ),
        "public_api_call_chain": "any application → curl_url_dup(in)",
    }
    r = _parse_result(json.dumps(payload), "curl_url_dup")
    assert r.verdict == RealismVerdict.UNREALISTIC
    assert "auto-downgraded" in r.key_concern.lower()


def test_realistic_downgraded_when_evidence_missing():
    """REALISTIC verdict without REQ-1 source-line-guard or REQ-2
    public-API call chain is downgraded to UNCERTAIN.

    Empirically, REALISTIC verdicts without these concrete evidence
    fields turned out to be LLM hand-waving in every bounty case this
    session.
    """
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    payload = {
        "verdict": "REALISTIC",
        "reasoning": "Could occur if attacker controls input.",
        "key_concern": "general concern, no specific exploit chain",
        "confidence": "low",
        "source_line_guard": "",
        "public_api_call_chain": "",
    }
    r = _parse_result(json.dumps(payload), "f")
    assert r.verdict == RealismVerdict.UNCERTAIN

    # Handwave phrases also count as missing evidence
    payload["source_line_guard"] = "this is hypothetical"
    payload["public_api_call_chain"] = "an attacker could pass NULL"
    r = _parse_result(json.dumps(payload), "f")
    assert r.verdict == RealismVerdict.UNCERTAIN


def test_parse_unknown_verdict_defaults_uncertain():
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    bad = json.dumps({"verdict": "MAYBE", "reasoning": "x", "key_concern": "", "confidence": "high"})
    r = _parse_result(bad, "rb_write")
    assert r.verdict == RealismVerdict.UNCERTAIN


def test_parse_malformed_json():
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    r = _parse_result("not json at all", "rb_write")
    assert r.verdict == RealismVerdict.UNCERTAIN


def test_parse_markdown_fenced_json():
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    fenced = "```json\n" + _make_llm_response("REALISTIC", "high") + "\n```"
    r = _parse_result(fenced, "rb_write")
    assert r.verdict == RealismVerdict.REALISTIC


# ---------------------------------------------------------------------------
# 4. Prompt construction
# ---------------------------------------------------------------------------

def test_prompt_contains_function_name():
    from bmc_agent.realism_checker import RealismChecker
    from bmc_agent.parser import parse_c_file

    config = _make_config()
    mock_llm = MagicMock()
    checker = RealismChecker(config=config, llm=mock_llm)

    func = _make_func_info()
    assert func is not None
    parsed = parse_c_file(str(EXAMPLE_C))
    vr = _make_validation_result()

    prompt = checker._build_prompt(
        func=func,
        counterexample=_make_counterexample(),
        validation_result=vr,
        parsed_file=parsed,
        all_funcs={},
    )

    assert "rb_write" in prompt
    assert "null-pointer" in prompt.lower() or "null" in prompt.lower()


def test_prompt_contains_call_chain():
    from bmc_agent.realism_checker import RealismChecker
    from bmc_agent.parser import parse_c_file

    config = _make_config()
    mock_llm = MagicMock()
    checker = RealismChecker(config=config, llm=mock_llm)

    func = _make_func_info()
    assert func is not None
    parsed = parse_c_file(str(EXAMPLE_C))

    # Build validation result with a two-step call chain
    vr = _make_validation_result()
    prompt = checker._build_prompt(
        func=func,
        counterexample=_make_counterexample(),
        validation_result=vr,
        parsed_file=parsed,
        all_funcs={},
    )
    assert "dev_write" in prompt


# ---------------------------------------------------------------------------
# 5. BugReport confidence downgrade
# ---------------------------------------------------------------------------

def test_bug_report_downgraded_to_unlikely_for_unrealistic_high():
    """UNREALISTIC + high LLM confidence → BugReport.confidence == 'unlikely'."""
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.parser import parse_c_file

    parsed = parse_c_file(str(EXAMPLE_C))
    func = parsed.get_function_info("rb_write")
    assert func is not None

    store = ArtifactStore("/tmp/test_realism_artifacts")
    reporter = BugReporter(store)
    vr = _make_validation_result()

    realism = RealismCheckResult(
        verdict=RealismVerdict.UNREALISTIC,
        reasoning="The buf parameter is always non-NULL in real callers.",
        key_concern="NULL pointer injected by harness",
        llm_confidence="high",
    )
    report = reporter.create_report(vr, func, realism_check=realism)
    assert report.confidence == "unlikely"
    assert report.realism_check is realism


def test_bug_report_not_downgraded_for_unrealistic_low_confidence():
    """UNREALISTIC + low LLM confidence should NOT downgrade the report."""
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.parser import parse_c_file

    parsed = parse_c_file(str(EXAMPLE_C))
    func = parsed.get_function_info("rb_write")
    assert func is not None

    store = ArtifactStore("/tmp/test_realism_artifacts2")
    reporter = BugReporter(store)
    vr = _make_validation_result()

    realism = RealismCheckResult(
        verdict=RealismVerdict.UNREALISTIC,
        reasoning="Hard to say.",
        key_concern="maybe NULL",
        llm_confidence="low",  # low confidence → no downgrade
    )
    report = reporter.create_report(vr, func, realism_check=realism)
    # Should keep the original tier (confirmed_bmc for this validation result)
    assert report.confidence != "unlikely"


def test_bug_report_unchanged_for_realistic_verdict():
    """REALISTIC verdict should not affect the confidence tier."""
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.parser import parse_c_file

    parsed = parse_c_file(str(EXAMPLE_C))
    func = parsed.get_function_info("rb_write")
    assert func is not None

    store = ArtifactStore("/tmp/test_realism_artifacts3")
    reporter = BugReporter(store)
    vr = _make_validation_result()

    realism = RealismCheckResult(
        verdict=RealismVerdict.REALISTIC,
        reasoning="Caller can pass NULL.",
        llm_confidence="high",
    )
    report = reporter.create_report(vr, func, realism_check=realism)
    assert report.confidence == "confirmed_bmc"


def test_bug_report_no_realism_check_unchanged():
    """Without a realism check the confidence tier is unchanged."""
    from bmc_agent.bug_reporter import BugReporter
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.parser import parse_c_file

    parsed = parse_c_file(str(EXAMPLE_C))
    func = parsed.get_function_info("rb_write")
    assert func is not None

    store = ArtifactStore("/tmp/test_realism_artifacts4")
    reporter = BugReporter(store)
    vr = _make_validation_result()

    report = reporter.create_report(vr, func, realism_check=None)
    assert report.confidence == "confirmed_bmc"
    assert report.realism_check is None


# ---------------------------------------------------------------------------
# 6. Integration smoke test with mocked LLM
# ---------------------------------------------------------------------------

def test_realism_checker_check_with_mocked_llm():
    """End-to-end check(): enabled, LLM returns REALISTIC."""
    from bmc_agent.realism_checker import RealismChecker, RealismVerdict
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec

    config = _make_config(enable_realism_check=True)
    mock_llm = MagicMock()
    mock_llm.complete.return_value = _make_llm_response("REALISTIC", "high")

    checker = RealismChecker(config=config, llm=mock_llm)
    func = _make_func_info()
    assert func is not None

    parsed = parse_c_file(str(EXAMPLE_C))
    spec = Spec(function_name="rb_write", precondition="valid(rb)", postcondition="true")

    result = checker.check(
        func=func,
        counterexample=_make_counterexample(),
        validation_result=_make_validation_result(),
        parsed_file=parsed,
        all_funcs={},
        spec=spec,
    )

    assert result.verdict == RealismVerdict.REALISTIC
    mock_llm.complete.assert_called_once()
    # Verify the system prompt includes DSL grammar content
    call_args = mock_llm.complete.call_args
    system_prompt = call_args[0][0]
    assert "requires" in system_prompt or "formal verification" in system_prompt.lower()


def test_realism_checker_llm_error_returns_uncertain():
    """LLM errors should produce UNCERTAIN, not raise."""
    from bmc_agent.realism_checker import RealismChecker, RealismVerdict
    from bmc_agent.llm import LLMError
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec

    config = _make_config(enable_realism_check=True)
    mock_llm = MagicMock()
    mock_llm.complete.side_effect = LLMError("rate limit")

    checker = RealismChecker(config=config, llm=mock_llm)
    func = _make_func_info()
    assert func is not None

    parsed = parse_c_file(str(EXAMPLE_C))
    spec = Spec(function_name="rb_write", precondition="true", postcondition="true")

    result = checker.check(
        func=func,
        counterexample=_make_counterexample(),
        validation_result=_make_validation_result(),
        parsed_file=parsed,
        all_funcs={},
        spec=spec,
    )

    assert result.verdict == RealismVerdict.UNCERTAIN
    assert "rate limit" in result.reasoning.lower()


# ---------------------------------------------------------------------------
# 7. REALISM_CHECK_PROMPT placeholder coverage
# ---------------------------------------------------------------------------

def test_realism_check_prompt_has_required_placeholders():
    """The prompt template should have all required format keys."""
    from bmc_agent.prompts import REALISM_CHECK_PROMPT

    required = {
        "{function_name}", "{function_signature}", "{function_body}",
        "{violated_property}", "{counterexample_state}",
        "{call_chain}", "{caller_context}",
        "{dynamic_result}", "{harness_code}",
        "{call_site_analysis}", "{global_context}",
    }
    for key in required:
        assert key in REALISM_CHECK_PROMPT, f"Missing placeholder {key} in REALISM_CHECK_PROMPT"


# ---------------------------------------------------------------------------
# 8. DynamicValidationResult harness_source field
# ---------------------------------------------------------------------------

def test_dynamic_validation_result_has_harness_source():
    from bmc_agent.dynamic_validator import DynamicOutcome, DynamicValidationResult

    r = DynamicValidationResult(
        outcome=DynamicOutcome.CONFIRMED,
        signal_name="SIGSEGV",
        harness_source="int main() { return 0; }",
    )
    assert r.harness_source == "int main() { return 0; }"
    d = r.to_dict()
    assert d["harness_source"] == "int main() { return 0; }"
