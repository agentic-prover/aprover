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


def test_pipeline_skips_realism_when_dynamic_not_triggered_on_crash_property():
    """LEGACY (now opt-in) skip path: with ``realism_authoritative=False``
    Pipeline._make_report takes the shortcut and returns UNREALISTIC
    directly when (a) dynamic validation reported NOT_TRIGGERED AND
    (b) the failing property is a crash-class (NULL deref, OOB, etc.).

    A real bug here would crash at runtime, so NOT_TRIGGERED proves the
    witness is a model artifact (stub returns, aliasing, …).  Kills the
    bsearch / calloc-stub FP class without an LLM call.

    NOTE: this dyn-val veto is DISABLED in the production default
    (``realism_authoritative=True``) because it was killing real bugs
    the reproducer merely couldn't synthesize a test for (read_be64 OOB,
    2026-06). The test below pins the now-default authoritative behavior.
    """
    from unittest.mock import MagicMock
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.dynamic_validator import DynamicValidationResult, DynamicOutcome
    from bmc_agent.realism_checker import RealismVerdict
    from bmc_agent.config import Config

    config = Config()
    config.enable_realism_check = True
    config.enable_dynamic_validation = True
    config.realism_authoritative = False  # exercise the legacy (opt-in) veto path

    pipeline = AMCPipeline.__new__(AMCPipeline)
    pipeline.config = config
    pipeline.llm = MagicMock()
    pipeline.realism_checker = MagicMock()
    pipeline.realism_checker.check_with_tools_if_enabled = MagicMock(
        side_effect=AssertionError("realism LLM should not be called on crash-class NOT_TRIGGERED")
    )
    pipeline.reporter = MagicMock()
    pipeline.reporter.create_report = MagicMock(side_effect=lambda v, f, realism_check: realism_check)

    validation = MagicMock()
    validation.counterexample = MagicMock()
    validation.counterexample.failing_property = "test_fn.pointer_dereference.1"
    validation.dynamic_result = DynamicValidationResult(
        outcome=DynamicOutcome.NOT_TRIGGERED,
        signal_name=None,
        reasoning="harness ran cleanly, no SIGSEGV/SIGABRT",
    )

    func = MagicMock()
    func.name = "test_fn"

    realism = pipeline._make_report(
        validation=validation, func=func, spec=MagicMock(),
        parsed=MagicMock(), all_funcs={}, driver_name="d",
    )
    assert realism is not None
    assert realism.verdict == RealismVerdict.UNREALISTIC
    pipeline.realism_checker.check_with_tools_if_enabled.assert_not_called()


def test_pipeline_calls_realism_when_authoritative_even_if_not_triggered():
    """Production default (``realism_authoritative=True``): a crash-class
    NOT_TRIGGERED must NOT short-circuit to UNREALISTIC. Realism is the
    sole authority on real-vs-FP; the reproducer failing to synthesize a
    triggering test is not evidence of a false positive (read_be64 OOB,
    2026-06). The pipeline MUST call the realism LLM, and its verdict
    (here REALISTIC) stands."""
    from unittest.mock import MagicMock
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.dynamic_validator import DynamicValidationResult, DynamicOutcome
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.config import Config

    config = Config()
    config.enable_realism_check = True
    config.enable_dynamic_validation = True
    config.enable_feedback_loop = False
    assert config.realism_authoritative is True  # production default

    pipeline = AMCPipeline.__new__(AMCPipeline)
    pipeline.config = config
    pipeline.llm = MagicMock()
    pipeline.realism_checker = MagicMock()
    pipeline.realism_checker.check_with_tools_if_enabled = MagicMock(return_value=RealismCheckResult(
        verdict=RealismVerdict.REALISTIC,
        reasoning="attacker-controlled OOB read; reproducer just couldn't synthesize a trigger",
        key_concern="out-of-bounds read",
        llm_confidence="high",
    ))
    pipeline.reporter = MagicMock()
    pipeline.reporter.create_report = MagicMock(side_effect=lambda v, f, realism_check: realism_check)

    validation = MagicMock()
    validation.counterexample = MagicMock()
    validation.counterexample.failing_property = "read_be64.pointer_dereference.1"  # crash-class
    validation.dynamic_result = DynamicValidationResult(
        outcome=DynamicOutcome.NOT_TRIGGERED,
        signal_name=None,
        reasoning="reproducer compiled+ran but did not synthesize a triggering input",
    )

    func = MagicMock()
    func.name = "read_be64"

    realism = pipeline._make_report(
        validation=validation, func=func, spec=MagicMock(),
        parsed=MagicMock(), all_funcs={}, driver_name="d",
    )
    assert realism is not None
    assert realism.verdict == RealismVerdict.REALISTIC  # realism decides, not the dyn-val veto
    pipeline.realism_checker.check_with_tools_if_enabled.assert_called_once()


def test_pipeline_does_not_skip_realism_on_silent_ub_property():
    """When the property is a SILENT UB class (overflow, conversion,
    pointer_arithmetic), dynamic NOT_TRIGGERED is uninformative — the
    runtime wraps silently even when the bug is real.  The pipeline
    MUST call the realism LLM rather than auto-marking UNREALISTIC.

    Regression observed on VibeOS memory.c: malloc.overflow.1 (the
    May-7 BUG-19) was being absorbed by the feedback loop after the
    pipeline shortcut wrongly classified it as an artifact.
    """
    from unittest.mock import MagicMock
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.dynamic_validator import DynamicValidationResult, DynamicOutcome
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.config import Config

    config = Config()
    config.enable_realism_check = True
    config.enable_dynamic_validation = True
    # Feedback loop OFF so we test the realism path directly.
    config.enable_feedback_loop = False

    pipeline = AMCPipeline.__new__(AMCPipeline)
    pipeline.config = config
    pipeline.llm = MagicMock()
    pipeline.realism_checker = MagicMock()
    pipeline.realism_checker.check_with_tools_if_enabled = MagicMock(return_value=RealismCheckResult(
        verdict=RealismVerdict.REALISTIC,
        reasoning="overflow on attacker-controlled size",
        key_concern="unsigned overflow",
        llm_confidence="high",
    ))
    pipeline.reporter = MagicMock()
    pipeline.reporter.create_report = MagicMock(side_effect=lambda v, f, realism_check: realism_check)

    validation = MagicMock()
    validation.counterexample = MagicMock()
    validation.counterexample.failing_property = "malloc.overflow.1"  # silent UB
    validation.dynamic_result = DynamicValidationResult(
        outcome=DynamicOutcome.NOT_TRIGGERED,
        signal_name=None,
        reasoning="harness ran without GCC catching the overflow",
    )

    func = MagicMock()
    func.name = "malloc"

    realism = pipeline._make_report(
        validation=validation, func=func, spec=MagicMock(),
        parsed=MagicMock(), all_funcs={}, driver_name="d",
    )
    # Realism LLM MUST have been called on the silent-UB property.
    pipeline.realism_checker.check_with_tools_if_enabled.assert_called_once()
    # And the LLM's REALISTIC verdict propagates.
    assert realism is not None
    assert realism.verdict == RealismVerdict.REALISTIC


def test_pipeline_runs_realism_when_dynamic_confirmed():
    """When dynamic validation CONFIRMS the fault, the realism LLM still
    runs (to assess realistic exploitability and call-chain feasibility)."""
    from unittest.mock import MagicMock
    from bmc_agent.pipeline import AMCPipeline
    from bmc_agent.dynamic_validator import DynamicValidationResult, DynamicOutcome
    from bmc_agent.realism_checker import RealismCheckResult, RealismVerdict
    from bmc_agent.config import Config

    config = Config()
    config.enable_realism_check = True
    config.enable_dynamic_validation = True

    pipeline = AMCPipeline.__new__(AMCPipeline)
    pipeline.config = config
    pipeline.llm = MagicMock()
    expected = RealismCheckResult(verdict=RealismVerdict.REALISTIC, reasoning="x")
    pipeline.realism_checker = MagicMock()
    pipeline.realism_checker.check_with_tools_if_enabled = MagicMock(return_value=expected)
    pipeline.reporter = MagicMock()
    pipeline.reporter.create_report = MagicMock(side_effect=lambda v, f, realism_check: realism_check)

    validation = MagicMock()
    validation.counterexample = MagicMock()
    validation.dynamic_result = DynamicValidationResult(
        outcome=DynamicOutcome.CONFIRMED, signal_name="SIGSEGV",
    )
    func = MagicMock(); func.name = "test_fn"

    realism = pipeline._make_report(
        validation=validation, func=func, spec=MagicMock(),
        parsed=MagicMock(), all_funcs={}, driver_name="d",
    )
    assert realism is expected
    pipeline.realism_checker.check_with_tools_if_enabled.assert_called_once()


@pytest.mark.skip(
    reason=(
        "Auto-downgrade based on phrase-matching reasoning was intentionally "
        "removed at realism_checker.py:2610-2616 — the rationale (in the "
        "code) is that phrase-matching adds bias rather than precision. The "
        "test asserts the OLD behaviour. Kept for reference in case the "
        "feature is ever re-enabled with stronger evidence."
    ),
)
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


@pytest.mark.skip(
    reason=(
        "Auto-downgrade based on missing REQ-1/REQ-2 evidence fields was "
        "intentionally removed at realism_checker.py:2610-2616 (same "
        "rationale as test_realistic_downgraded_when_reasoning_says_artifact "
        "— evidence-field templating was replaced by a freer prompt that "
        "doesn't carry the REQ fields). Kept for reference."
    ),
)
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


def test_parse_truncated_json_recovers_correct_verdict():
    """Regression for the libxml2 xmlAddEntity false-positive.

    The LLM returned a markdown-fenced JSON whose ``verdict`` field was
    UNCERTAIN, but the response was truncated by max_tokens, leaving the
    JSON un-parseable. The bare-keyword fallback used to trip on the word
    "realistic" appearing inside the reasoning prose and labelled the
    finding REALISTIC. Verify the new parser honours the JSON-key
    ``"verdict": "UNCERTAIN"`` even in the truncated case.
    """
    from bmc_agent.realism_checker import _parse_result, RealismVerdict
    truncated = (
        '```json\n'
        '{\n'
        '  "verdict": "UNCERTAIN",\n'
        '  "reasoning": "Q1 — Could the violation TYPE occur? '
        'Yes, this is a realistic null-pointer dereference. '
        'Q2 — Is the specific witness reachable? Uncertain — '
        'the CBMC trace has inconsistencies. The underlying '
        'concern is independe'
    )
    r = _parse_result(truncated, "xmlAddEntity")
    assert r.verdict == RealismVerdict.UNCERTAIN, (
        f"truncated JSON with verdict=UNCERTAIN must not be parsed as "
        f"{r.verdict.value}"
    )


def test_recover_prose_anchor_verdict():
    """``Verdict: REALISTIC`` anchor must beat keyword counts in the prose."""
    from bmc_agent.realism_checker import _recover_verdict_from_prose, RealismVerdict
    text = (
        "After analysis, the reasoning suggests both UNCERTAIN risk and "
        "REALISTIC class.\n\nFinal Verdict: UNREALISTIC because the guard "
        "exists at line 736."
    )
    assert _recover_verdict_from_prose(text) == RealismVerdict.UNREALISTIC


def test_recover_prose_hedge_defaults_uncertain():
    """When multiple verdict words appear without an anchor, default UNCERTAIN."""
    from bmc_agent.realism_checker import _recover_verdict_from_prose, RealismVerdict
    hedged = (
        "The violation type is REALISTIC for a security threat model, "
        "but specific witness values are UNCERTAIN given the CBMC trace."
    )
    assert _recover_verdict_from_prose(hedged) == RealismVerdict.UNCERTAIN


def test_recover_prose_unrealistic_with_realistic_substring():
    """``UNREALISTIC`` containing ``REALISTIC`` as substring must not flip the verdict."""
    from bmc_agent.realism_checker import _recover_verdict_from_prose, RealismVerdict
    text = "Conclusion: UNREALISTIC — the guard catches the witness state."
    assert _recover_verdict_from_prose(text) == RealismVerdict.UNREALISTIC


def test_witness_uninitialized_library_detects_xml_alloc_nulls():
    """When xmlMalloc/xmlFree/xmlRealloc are all NULL in the witness, the
    realism check must short-circuit to UNREALISTIC without an LLM call —
    those globals are NULL only before library init runs, never in a real
    public-API call chain.
    """
    from bmc_agent.realism_checker import _witness_indicates_uninitialized_library
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.pointer_dereference.1",
        variable_assignments={
            "xmlMalloc": "((xmlMallocFunc)NULL)",
            "xmlFree": "((xmlFreeFunc)NULL)",
            "xmlRealloc": "((xmlReallocFunc)NULL)",
            "ptr": "0x1234",  # genuine bug state
        },
    )
    cause = _witness_indicates_uninitialized_library(cex)
    assert cause is not None
    assert "library-init" in cause


def test_witness_uninitialized_library_ignores_single_null():
    """A single NULL global is not enough to call the witness uninitialized —
    legitimately NULL output sinks (e.g. xmlGenericError when error-mode is
    disabled) shouldn't trigger the auto-downgrade.
    """
    from bmc_agent.realism_checker import _witness_indicates_uninitialized_library
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.pointer.1",
        variable_assignments={
            "xmlGenericError": "((xmlGenericErrorFunc)NULL)",
            "ptr": "0x1234",
        },
    )
    cause = _witness_indicates_uninitialized_library(cex)
    assert cause is None


def test_path_divergent_unwind_detected_when_return_before_loop():
    """When a `.unwind.0` violation is reported but the witness trace shows
    function-return before any loop-head, mark UNREALISTIC.

    From feedback-loop arm (a) TODO on xmlXIncludeIncludeNode: CBMC fires
    unwind on a path the exhibited witness doesn't actually traverse.
    """
    from bmc_agent.realism_checker import _witness_indicates_path_divergent_unwind
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.unwind.0",
        trace=[
            "function-call at f:10",
            "list = NULL",
            "nb_elem = 0",
            "function-return at f:12",  # early exit
            # No loop-head — function returned before any loop
        ],
    )
    cause = _witness_indicates_path_divergent_unwind(cex)
    assert cause is not None
    assert "no loop" in cause.lower() or "non-exhibited" in cause.lower()


def test_path_divergent_unwind_not_detected_when_loop_was_entered():
    """If the witness DID enter the loop before the unwind fired, the
    finding is a legitimate loop-bound issue (might still be filtered
    elsewhere, but not by THIS detector)."""
    from bmc_agent.realism_checker import _witness_indicates_path_divergent_unwind
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.unwind.0",
        trace=[
            "function-call at f:10",
            "list != NULL",
            "loop-head at f:15",
            "list = list->next",
            "loop-head at f:15",
            "list = list->next",
            "function-return at f:20",  # after loop
        ],
    )
    assert _witness_indicates_path_divergent_unwind(cex) is None


def test_path_divergent_unwind_only_fires_on_unwind_properties():
    """Pointer-deref / OOB CEs are NOT unwind-property — detector must
    return None even if trace has early return."""
    from bmc_agent.realism_checker import _witness_indicates_path_divergent_unwind
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.pointer_dereference.1",
        trace=["function-return at f:5"],
    )
    assert _witness_indicates_path_divergent_unwind(cex) is None


def test_jv_stub_disconnect_detects_null_refcnt_with_array_kind():
    """jq jv tagged-union: when CE shows j.u.ptr=NULL plus stubbed
    jv_get_kind returning JV_KIND_ARRAY, it's a stub-disconnect artifact
    (real jv constructors always pair refcnt-backed kinds with valid
    refcnt). Shipped from jv_aux.c sweep 2026-05-13.
    """
    from bmc_agent.realism_checker import _witness_indicates_jv_stub_disconnect
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="parse_slice.assertion.1",
        variable_assignments={
            "j.u.ptr": "((struct jv_refcnt *)NULL)",
            "slice.u.ptr": "((struct jv_refcnt *)NULL)",
            "return_value_jv_get_kind": "/*enum*/JV_KIND_ARRAY",
            "return_value_jv_get_kind$0": "/*enum*/JV_KIND_STRING",
        },
    )
    cause = _witness_indicates_jv_stub_disconnect(cex)
    assert cause is not None
    assert "stub" in cause.lower() or "ptr" in cause.lower()


def test_jv_stub_disconnect_detects_out_of_range_enum_int():
    """jv_get_kind stub returning out-of-enum-range integers (e.g. 17,
    2097152) is a clear nondet-stub artifact even without NULL refcnt."""
    from bmc_agent.realism_checker import _witness_indicates_jv_stub_disconnect
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="jv_has.overflow.1",
        variable_assignments={
            "j.u.ptr": "((struct jv_refcnt *)NULL)",
            "return_value_jv_get_kind": "/*enum*/2097152",
            "return_value_jv_get_kind$1": "/*enum*/268435463",
        },
    )
    assert _witness_indicates_jv_stub_disconnect(cex) is not None


def test_jv_stub_disconnect_skips_valid_simple_kinds():
    """When jv_get_kind reports a non-refcnt kind (NULL/FALSE/TRUE/
    INVALID), it's fine for u.ptr to be NULL — these simple values don't
    use the refcnt. Detector must NOT flag this as an artifact.
    """
    from bmc_agent.realism_checker import _witness_indicates_jv_stub_disconnect
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.assertion.1",
        variable_assignments={
            "j.u.ptr": "((struct jv_refcnt *)NULL)",
            "return_value_jv_get_kind": "/*enum*/JV_KIND_NULL",
            "return_value_jv_get_kind$0": "/*enum*/JV_KIND_INVALID",
        },
    )
    assert _witness_indicates_jv_stub_disconnect(cex) is None


def test_witness_uninitialized_library_handles_libcurl_alloc_pattern():
    from bmc_agent.realism_checker import _witness_indicates_uninitialized_library
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="curl_url_dup.pointer.1",
        variable_assignments={
            "Curl_cmalloc": "(Curl_malloc_callback)NULL",
            "Curl_ccalloc": "(Curl_calloc_callback)NULL",
            "Curl_cfree": "(Curl_free_callback)NULL",
        },
    )
    assert _witness_indicates_uninitialized_library(cex) is not None


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


# ---------------------------------------------------------------------------
# 9. USB-serial framework-invariant witness detector
# ---------------------------------------------------------------------------

def _make_parsed_file_with_usb_serial_table(callback_slot: str, fn_name: str):
    """Build a minimal ParsedCFile whose preprocessed_source contains a
    ``struct usb_serial_driver`` registering *fn_name* in *callback_slot*."""
    from bmc_agent.parser import ParsedCFile
    src = (
        f"static int {fn_name}(struct tty_struct *tty) {{ return 0; }}\n"
        f"static struct usb_serial_driver some_device = {{\n"
        f"    .description = \"some\",\n"
        f"    .{callback_slot} = {fn_name},\n"
        f"    .num_ports = 1,\n"
        f"}};\n"
    )
    return ParsedCFile(
        path="/tmp/fake.c",
        functions={},
        call_graph={},
        function_bodies={},
        function_definitions={},
        preprocessed_source=src,
    )


def _make_func_info_named(fn_name: str):
    """Build a FunctionInfo stub with just a name and minimal signature."""
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name=fn_name, return_type="int", parameters=[], is_static=True
    )
    return FunctionInfo(
        name=fn_name,
        source_file="/tmp/fake.c",
        signature=sig,
        body="{ return 0; }",
        callees=set(),
    )


def test_usb_serial_framework_invariant_detects_tiocmset_null_witness():
    """pl2303-style false positive: ``pl2303_tiocmset`` registered as
    ``.tiocmset`` in a ``struct usb_serial_driver``, witness has
    ``tty == NULL``. Detector must short-circuit to UNREALISTIC.
    """
    from bmc_agent.realism_checker import (
        _witness_indicates_usb_serial_framework_invariant,
    )
    from bmc_agent.cbmc import Counterexample
    pf = _make_parsed_file_with_usb_serial_table("tiocmset", "pl2303_tiocmset")
    func = _make_func_info_named("pl2303_tiocmset")
    cex = Counterexample(
        failing_property="pl2303_tiocmset.pointer_dereference.1",
        variable_assignments={
            "tty": "((struct tty_struct *)NULL)",
            "set": "1",
            "clear": "0",
        },
    )
    cause = _witness_indicates_usb_serial_framework_invariant(func, cex, pf)
    assert cause is not None
    assert ".tiocmset" in cause
    assert "pl2303_tiocmset" in cause


def test_usb_serial_framework_invariant_detects_port_null_for_dtr_rts():
    """``pl2303_dtr_rts`` registered as ``.dtr_rts`` with the framework's
    ``port`` argument NULL in the witness."""
    from bmc_agent.realism_checker import (
        _witness_indicates_usb_serial_framework_invariant,
    )
    from bmc_agent.cbmc import Counterexample
    pf = _make_parsed_file_with_usb_serial_table("dtr_rts", "pl2303_dtr_rts")
    func = _make_func_info_named("pl2303_dtr_rts")
    cex = Counterexample(
        failing_property="pl2303_dtr_rts.pointer_dereference.1",
        variable_assignments={
            "port": "((struct usb_serial_port *)NULL)",
            "on": "1",
        },
    )
    cause = _witness_indicates_usb_serial_framework_invariant(func, cex, pf)
    assert cause is not None
    assert "dtr_rts" in cause


def test_usb_serial_framework_invariant_skips_when_not_registered():
    """A function NOT registered in any ``struct usb_serial_driver`` —
    e.g. a helper used internally — should NOT trigger the detector
    even if its witness has a NULL framework-named pointer."""
    from bmc_agent.realism_checker import (
        _witness_indicates_usb_serial_framework_invariant,
    )
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.cbmc import Counterexample
    pf = ParsedCFile(
        path="/tmp/fake.c",
        functions={}, call_graph={}, function_bodies={}, function_definitions={},
        preprocessed_source=(
            # No usb_serial_driver registration in this file
            "static int helper(struct tty_struct *tty) { return 0; }\n"
        ),
    )
    func = _make_func_info_named("helper")
    cex = Counterexample(
        failing_property="helper.pointer_dereference.1",
        variable_assignments={"tty": "NULL"},
    )
    assert _witness_indicates_usb_serial_framework_invariant(func, cex, pf) is None


def test_usb_serial_framework_invariant_skips_when_witness_has_no_null():
    """Registered as a callback but the witness pointers are non-NULL —
    detector returns None so the LLM can audit normally."""
    from bmc_agent.realism_checker import (
        _witness_indicates_usb_serial_framework_invariant,
    )
    from bmc_agent.cbmc import Counterexample
    pf = _make_parsed_file_with_usb_serial_table("open", "ch341_open")
    func = _make_func_info_named("ch341_open")
    cex = Counterexample(
        failing_property="ch341_open.bounds.1",
        variable_assignments={"tty": "0x1234", "i": "5"},
    )
    assert _witness_indicates_usb_serial_framework_invariant(func, cex, pf) is None


def test_phy_framework_invariant_detects_attached_dev_null_on_set_wol():
    """dp83tc811-style FP: function registered as ``.set_wol`` slot of a
    ``struct phy_driver`` array; witness sets
    ``phydev.attached_dev = NULL``. Detector must classify UNREALISTIC.
    """
    from bmc_agent.realism_checker import (
        _witness_indicates_phy_framework_invariant,
    )
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.cbmc import Counterexample
    pf = ParsedCFile(
        path="/tmp/fake.c",
        functions={}, call_graph={}, function_bodies={}, function_definitions={},
        preprocessed_source=(
            "static int dp83811_set_wol(struct phy_device *phydev, "
            "struct ethtool_wolinfo *wol) { return 0; }\n"
            "static struct phy_driver dp83811_driver[] = {\n"
            "    {\n"
            "        .phy_id = 0x2000a211,\n"
            "        .set_wol = dp83811_set_wol,\n"
            "        .config_init = dp83811_config_init,\n"
            "    },\n"
            "};\n"
        ),
    )
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name="dp83811_set_wol", return_type="int", parameters=[], is_static=True,
    )
    func = FunctionInfo(
        name="dp83811_set_wol", source_file="/tmp/fake.c",
        signature=sig, body="{ return 0; }", callees=set(),
    )
    cex = Counterexample(
        failing_property="dp83811_set_wol.pointer_dereference.13",
        variable_assignments={
            "phydev.attached_dev": "((struct net_device *)NULL)",
            "wol.wolopts": "32",  # WAKE_MAGIC
        },
    )
    cause = _witness_indicates_phy_framework_invariant(func, cex, pf)
    assert cause is not None
    assert ".set_wol" in cause


def test_phy_framework_invariant_detects_phydev_null():
    """Phydev itself NULL — the wrapper would have crashed before dispatch."""
    from bmc_agent.realism_checker import (
        _witness_indicates_phy_framework_invariant,
    )
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.cbmc import Counterexample
    pf = ParsedCFile(
        path="/tmp/fake.c",
        functions={}, call_graph={}, function_bodies={}, function_definitions={},
        preprocessed_source=(
            "static int foo_config_aneg(struct phy_device *phydev) { return 0; }\n"
            "static struct phy_driver foo_driver = {\n"
            "    .config_aneg = foo_config_aneg,\n"
            "};\n"
        ),
    )
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name="foo_config_aneg", return_type="int", parameters=[], is_static=True,
    )
    func = FunctionInfo(
        name="foo_config_aneg", source_file="/tmp/fake.c",
        signature=sig, body="{ return 0; }", callees=set(),
    )
    cex = Counterexample(
        failing_property="foo_config_aneg.pointer_dereference.1",
        variable_assignments={"phydev": "((struct phy_device *)NULL)"},
    )
    cause = _witness_indicates_phy_framework_invariant(func, cex, pf)
    assert cause is not None
    assert "phy_driver" in cause


def test_phy_framework_invariant_skips_when_not_registered():
    """Function not registered in any phy_driver — detector returns None."""
    from bmc_agent.realism_checker import (
        _witness_indicates_phy_framework_invariant,
    )
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.cbmc import Counterexample
    pf = ParsedCFile(
        path="/tmp/fake.c",
        functions={}, call_graph={}, function_bodies={}, function_definitions={},
        preprocessed_source="static int helper(struct phy_device *phydev) { return 0; }\n",
    )
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name="helper", return_type="int", parameters=[], is_static=True,
    )
    func = FunctionInfo(
        name="helper", source_file="/tmp/fake.c", signature=sig,
        body="{ return 0; }", callees=set(),
    )
    cex = Counterexample(
        failing_property="helper.pointer_dereference.1",
        variable_assignments={"phydev": "NULL"},
    )
    assert _witness_indicates_phy_framework_invariant(func, cex, pf) is None


def test_phy_framework_invariant_skips_probe_callback():
    """``.probe`` runs *during* attach where attached_dev may be NULL —
    don't auto-reject NULL-attached_dev witnesses on probe paths."""
    from bmc_agent.realism_checker import (
        _witness_indicates_phy_framework_invariant,
    )
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.cbmc import Counterexample
    pf = ParsedCFile(
        path="/tmp/fake.c",
        functions={}, call_graph={}, function_bodies={}, function_definitions={},
        preprocessed_source=(
            "static int foo_probe(struct phy_device *phydev) { return 0; }\n"
            "static struct phy_driver foo_driver = { .probe = foo_probe };\n"
        ),
    )
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name="foo_probe", return_type="int", parameters=[], is_static=True,
    )
    func = FunctionInfo(
        name="foo_probe", source_file="/tmp/fake.c", signature=sig,
        body="{ return 0; }", callees=set(),
    )
    cex = Counterexample(
        failing_property="foo_probe.pointer_dereference.1",
        variable_assignments={"phydev.attached_dev": "NULL"},
    )
    # ``.probe`` is intentionally NOT in _PHY_DRIVER_CALLBACKS — should not match.
    assert _witness_indicates_phy_framework_invariant(func, cex, pf) is None


def test_usb_serial_framework_invariant_ignores_unrelated_slot():
    """If a function name matches a slot name that ISN'T in our known
    USB-serial callback list (e.g. ``.foo = my_fn``), don't fire."""
    from bmc_agent.realism_checker import (
        _witness_indicates_usb_serial_framework_invariant,
    )
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.cbmc import Counterexample
    pf = ParsedCFile(
        path="/tmp/fake.c",
        functions={}, call_graph={}, function_bodies={}, function_definitions={},
        preprocessed_source=(
            "static int my_fn(void *port) { return 0; }\n"
            "static struct usb_serial_driver dev = {\n"
            "    .undocumented_slot = my_fn,\n"
            "};\n"
        ),
    )
    func = _make_func_info_named("my_fn")
    cex = Counterexample(
        failing_property="my_fn.pointer_dereference.1",
        variable_assignments={"port": "NULL"},
    )
    # ``undocumented_slot`` not in _USB_SERIAL_DRIVER_CALLBACKS → no match.
    assert _witness_indicates_usb_serial_framework_invariant(func, cex, pf) is None


# ---------------------------------------------------------------------------
# Intentional-truncation detector (r8125_fiber round-2 FP, 2026-05-18)
# ---------------------------------------------------------------------------


def test_intentional_truncation_u8_fires():
    """CBMC's --conversion-check description ``in (u8)expr`` matches the
    narrow-int allowlist; detector should fire."""
    from bmc_agent.realism_checker import _witness_indicates_intentional_truncation
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="rtl8125_check_fiber_mode_support.overflow.1",
        description=(
            "arithmetic overflow on unsigned to unsigned type conversion "
            "in (u8)return_value_rtl8125_mac_ocp_read"
        ),
    )
    result = _witness_indicates_intentional_truncation(cex)
    assert result is not None
    assert "u8" in result


def test_intentional_truncation_uint16_t_fires():
    """uint16_t is also in the narrow-int allowlist."""
    from bmc_agent.realism_checker import _witness_indicates_intentional_truncation
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.overflow.2",
        description=(
            "arithmetic overflow on signed to unsigned type conversion "
            "in (uint16_t)len"
        ),
    )
    result = _witness_indicates_intentional_truncation(cex)
    assert result is not None
    assert "uint16_t" in result


def test_intentional_truncation_non_narrow_type_no_fire():
    """A cast to a non-narrow-int type (struct, pointer) should NOT fire;
    we don't want to mask real bugs in domain-specific narrowing."""
    from bmc_agent.realism_checker import _witness_indicates_intentional_truncation
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.overflow.1",
        description=(
            "arithmetic overflow on signed to unsigned type conversion "
            "in (custom_handle_t)x"
        ),
    )
    assert _witness_indicates_intentional_truncation(cex) is None


def test_intentional_truncation_non_conversion_description_no_fire():
    """Descriptions that aren't type-conversion overflows (e.g. addition
    overflow) must not match — they're a different bug class."""
    from bmc_agent.realism_checker import _witness_indicates_intentional_truncation
    from bmc_agent.cbmc import Counterexample
    cex = Counterexample(
        failing_property="f.overflow.1",
        description="arithmetic overflow on unsigned + in a + b",
    )
    assert _witness_indicates_intentional_truncation(cex) is None


def test_intentional_truncation_empty_description_no_fire():
    """Defensive: an empty or missing description must return None,
    not crash."""
    from bmc_agent.realism_checker import _witness_indicates_intentional_truncation
    from bmc_agent.cbmc import Counterexample
    assert _witness_indicates_intentional_truncation(
        Counterexample(failing_property="f.x.1", description="")
    ) is None
    assert _witness_indicates_intentional_truncation(
        Counterexample(failing_property="f.x.1")
    ) is None


# ---------------------------------------------------------------------------
# Grounding-audit helpers (commit 94fc64d) — _extract_tool_names,
# _extract_grounding_field, _apply_grounding_consistency
# ---------------------------------------------------------------------------

def _make_realism_result(verdict, reasoning: str = "", key_concern: str = "", confidence: str = "high"):
    from bmc_agent.realism_checker import RealismCheckResult
    return RealismCheckResult(
        verdict=verdict,
        reasoning=reasoning,
        key_concern=key_concern,
        llm_confidence=confidence,
    )


def test_extract_tool_names_returns_call_order_with_args():
    """Tool-call walker must return (name, args) pairs in invocation order
    so the audit can check whether lookup_function was called on the target."""
    from bmc_agent.realism_checker import _extract_tool_names

    msgs = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "lookup_function",
                              "arguments": '{"name": "foo"}'}},
                {"function": {"name": "lookup_callee_postcondition",
                              "arguments": '{"name": "bar"}'}},
            ],
        },
        {"role": "tool", "content": "result"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "lookup_function",
                              "arguments": '{"name": "match_owner_name_mbs"}'}},
            ],
        },
    ]
    out = _extract_tool_names(msgs)
    assert out == [
        ("lookup_function", {"name": "foo"}),
        ("lookup_callee_postcondition", {"name": "bar"}),
        ("lookup_function", {"name": "match_owner_name_mbs"}),
    ]


def test_extract_tool_names_handles_dict_arguments_and_skips_non_assistant():
    """Tolerates arguments already parsed into a dict and skips non-assistant
    or malformed entries instead of crashing."""
    from bmc_agent.realism_checker import _extract_tool_names

    msgs = [
        "not a dict",
        {"role": "user", "tool_calls": [{"function": {"name": "ignored"}}]},
        {"role": "assistant", "tool_calls": [
            "not a dict",
            {"function": {"name": "lookup_function", "arguments": {"name": "x"}}},
            {"function": {"name": "", "arguments": "{}"}},  # empty name skipped
            {"function": {"name": "lookup_function", "arguments": "not json"}},
        ]},
    ]
    out = _extract_tool_names(msgs)
    assert out == [
        ("lookup_function", {"name": "x"}),
        ("lookup_function", {}),  # malformed args → empty dict, not crash
    ]


def test_extract_tool_names_empty_and_none_inputs():
    from bmc_agent.realism_checker import _extract_tool_names
    assert _extract_tool_names([]) == []
    assert _extract_tool_names(None) == []


def test_extract_grounding_field_from_bare_json():
    """Pure JSON output with a grounding sub-object returns the sub-object."""
    from bmc_agent.realism_checker import _extract_grounding_field

    raw = json.dumps({
        "verdict": "UNREALISTIC",
        "grounding": {
            "looked_up_target_body": True,
            "quoted_line": "if (p != NULL && strcmp(p, name) == 0)",
            "guard_search_result": "guard present: if (p != NULL && strcmp(p, name))",
        },
    })
    g = _extract_grounding_field(raw)
    assert g["looked_up_target_body"] is True
    assert g["quoted_line"].startswith("if (p")
    assert g["guard_search_result"].lower().startswith("guard present")


def test_extract_grounding_field_from_fenced_markdown():
    """Verdict JSON wrapped in ```json fences must still parse."""
    from bmc_agent.realism_checker import _extract_grounding_field

    raw = "```json\n" + json.dumps({
        "verdict": "REALISTIC",
        "grounding": {"looked_up_target_body": False, "quoted_line": "",
                      "guard_search_result": "no guard found"},
    }) + "\n```"
    g = _extract_grounding_field(raw)
    assert g.get("looked_up_target_body") is False
    assert g.get("guard_search_result") == "no guard found"


def test_extract_grounding_field_embedded_in_prose():
    """Some LLMs emit prose before/after the JSON; the embedded-JSON
    recovery path must still find the grounding field."""
    from bmc_agent.realism_checker import _extract_grounding_field

    raw = (
        "Here is my analysis:\n"
        + json.dumps({
            "verdict": "UNREALISTIC",
            "grounding": {"looked_up_target_body": True,
                          "quoted_line": "x", "guard_search_result": "no guard found"},
        })
        + "\nThat's all."
    )
    g = _extract_grounding_field(raw)
    assert g.get("looked_up_target_body") is True


def test_extract_grounding_field_missing_or_malformed_returns_empty():
    from bmc_agent.realism_checker import _extract_grounding_field

    assert _extract_grounding_field("") == {}
    assert _extract_grounding_field("not json at all") == {}
    # JSON without grounding key
    assert _extract_grounding_field(json.dumps({"verdict": "REALISTIC"})) == {}
    # grounding is wrong type (string, not dict)
    assert _extract_grounding_field(json.dumps({"grounding": "nope"})) == {}


def test_apply_grounding_consistency_passes_through_non_realistic():
    """UNREALISTIC and UNCERTAIN verdicts are not audited — only REALISTIC
    is held to the grounding bar."""
    from bmc_agent.realism_checker import (
        _apply_grounding_consistency, RealismVerdict,
    )
    for v in (RealismVerdict.UNREALISTIC, RealismVerdict.UNCERTAIN):
        r = _make_realism_result(v, reasoning="x")
        out = _apply_grounding_consistency(r, {}, "foo", looked_up_target=False)
        assert out is r  # exact same object, no modification


def test_apply_grounding_consistency_demotes_when_target_not_looked_up():
    """REALISTIC + LLM never called lookup_function(target) → demote to
    UNCERTAIN. Reasoning is annotated so audit trail survives."""
    from bmc_agent.realism_checker import (
        _apply_grounding_consistency, RealismVerdict,
    )
    r = _make_realism_result(
        RealismVerdict.REALISTIC,
        reasoning="strcmp(p, name) is unguarded",
        key_concern="null deref via strcmp",
    )
    out = _apply_grounding_consistency(
        r, {"looked_up_target_body": False}, "match_owner_name_mbs",
        looked_up_target=False,
    )
    assert out.verdict == RealismVerdict.UNCERTAIN
    assert out.reasoning.startswith("[grounding-audit demoted]")
    assert "strcmp(p, name) is unguarded" in out.reasoning
    assert out.key_concern == "null deref via strcmp"  # preserved
    assert out.llm_confidence == "low"  # downgraded


def test_apply_grounding_consistency_flips_when_guard_present():
    """REALISTIC verdict that contradicts its own grounding.guard_search_result
    (which says guard IS present) must flip to UNREALISTIC."""
    from bmc_agent.realism_checker import (
        _apply_grounding_consistency, RealismVerdict,
    )
    r = _make_realism_result(
        RealismVerdict.REALISTIC,
        reasoning="strcmp(p, name) called without NULL check",
    )
    grounding = {
        "looked_up_target_body": True,
        "quoted_line": "if (p != NULL && strcmp(p, name) == 0)",
        "guard_search_result": "guard present: if (p != NULL && ...)",
    }
    out = _apply_grounding_consistency(
        r, grounding, "match_owner_name_mbs", looked_up_target=True,
    )
    assert out.verdict == RealismVerdict.UNREALISTIC
    assert out.reasoning.startswith("[grounding-audit flipped from REALISTIC]")
    assert "guard present" in out.reasoning.lower()
    assert "strcmp(p, name) called without NULL check" in out.reasoning
    assert out.key_concern == "grounding contradicted verdict"


def test_apply_grounding_consistency_case_insensitive_guard_match():
    """The 'guard present:' prefix check must be case-insensitive so the LLM
    has some leeway in capitalisation."""
    from bmc_agent.realism_checker import (
        _apply_grounding_consistency, RealismVerdict,
    )
    r = _make_realism_result(RealismVerdict.REALISTIC, reasoning="x")
    for phrase in ("guard present: ...", "Guard Present: ...", "GUARD PRESENT: ..."):
        out = _apply_grounding_consistency(
            r, {"guard_search_result": phrase}, "f", looked_up_target=True,
        )
        assert out.verdict == RealismVerdict.UNREALISTIC, phrase


def test_apply_grounding_consistency_no_guard_keeps_realistic():
    """REALISTIC + lookup_function called + grounding says 'no guard found'
    → keep the original verdict. The audit isn't a blanket downgrade."""
    from bmc_agent.realism_checker import (
        _apply_grounding_consistency, RealismVerdict,
    )
    r = _make_realism_result(RealismVerdict.REALISTIC, reasoning="real bug")
    grounding = {
        "looked_up_target_body": True,
        "quoted_line": "strcmp(p, name)",
        "guard_search_result": "no guard found",
    }
    out = _apply_grounding_consistency(
        r, grounding, "f", looked_up_target=True,
    )
    assert out.verdict == RealismVerdict.REALISTIC
    assert out is r  # passthrough


def test_apply_grounding_consistency_missing_guard_field_keeps_realistic():
    """If grounding field is empty/missing the guard_search_result clause,
    don't flip — only the explicit 'guard present' signal counts as
    contradictory evidence."""
    from bmc_agent.realism_checker import (
        _apply_grounding_consistency, RealismVerdict,
    )
    r = _make_realism_result(RealismVerdict.REALISTIC, reasoning="real bug")
    # looked_up=True so the demotion arm doesn't trip; grounding has no
    # guard_search_result key at all
    out = _apply_grounding_consistency(
        r, {"looked_up_target_body": True}, "f", looked_up_target=True,
    )
    assert out.verdict == RealismVerdict.REALISTIC
    assert out is r
