"""Tests for the extended bmc_agent.flag_selector (unwind + shift)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bmc_agent.flag_selector import (
    _MAX_TIMEOUT_OVERRIDE,
    _MAX_UNWIND_OVERRIDE,
    _MIN_TIMEOUT_OVERRIDE,
    FlagSelection,
    FlagSelector,
    _parse_response,
)


# ---------- FlagSelection ---------------------------------------------------


def test_default_flag_selection_has_no_flags():
    sel = FlagSelection()
    assert not sel.any_enabled()
    assert sel.enabled_flags() == []
    assert sel.unwind_override is None
    assert sel.undefined_shift_check is False


def test_undefined_shift_check_flag_emitted():
    sel = FlagSelection(undefined_shift_check=True)
    assert sel.any_enabled()
    assert "--undefined-shift-check" in sel.enabled_flags()


def test_unwind_override_emits_unwind_flag():
    sel = FlagSelection(unwind_override=16)
    assert sel.any_enabled()
    assert "--unwind 16" in sel.enabled_flags()


def test_unwind_override_none_emits_nothing():
    sel = FlagSelection(unwind_override=None)
    assert "--unwind" not in " ".join(sel.enabled_flags())


def test_all_flags_combined_enabled_list():
    sel = FlagSelection(
        unsigned_overflow_check=True,
        signed_overflow_check=True,
        conversion_check=True,
        pointer_overflow_check=True,
        undefined_shift_check=True,
        unwind_override=32,
    )
    flags = sel.enabled_flags()
    assert "--unsigned-overflow-check" in flags
    assert "--signed-overflow-check" in flags
    assert "--conversion-check" in flags
    assert "--pointer-overflow-check" in flags
    assert "--undefined-shift-check" in flags
    assert "--unwind 32" in flags


def test_to_dict_includes_new_fields():
    sel = FlagSelection(undefined_shift_check=True, unwind_override=8)
    d = sel.to_dict()
    assert d["undefined_shift_check"] is True
    assert d["unwind_override"] == 8


# ---------- _parse_response: new fields -----------------------------------


def test_parse_undefined_shift_check_true():
    raw = '{"unsigned_overflow_check": false, "signed_overflow_check": false, "conversion_check": false, "pointer_overflow_check": false, "undefined_shift_check": true, "unwind_override": null, "reasoning": "shifts on packet field"}'
    sel = _parse_response(raw, "fn")
    assert sel.undefined_shift_check is True
    assert sel.unwind_override is None


def test_parse_unwind_override_accepted():
    raw = '{"undefined_shift_check": false, "unwind_override": 16, "reasoning": "loop bound is 14"}'
    sel = _parse_response(raw, "fn")
    assert sel.unwind_override == 16


def test_parse_unwind_override_null_yields_none():
    raw = '{"unwind_override": null, "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.unwind_override is None


def test_parse_unwind_override_string_int_accepted():
    """LLM sometimes emits the integer as a string; we tolerate that."""
    raw = '{"unwind_override": "12", "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.unwind_override == 12


def test_parse_unwind_override_clamps_to_max():
    raw = f'{{"unwind_override": {_MAX_UNWIND_OVERRIDE + 100}, "reasoning": ""}}'
    sel = _parse_response(raw, "fn")
    assert sel.unwind_override == _MAX_UNWIND_OVERRIDE


def test_parse_unwind_override_rejects_too_small():
    """unwind values < 2 are nonsense (CBMC needs >= 2 to run any loop iter)."""
    for n in (-5, 0, 1):
        raw = f'{{"unwind_override": {n}, "reasoning": ""}}'
        sel = _parse_response(raw, "fn")
        assert sel.unwind_override is None, f"unwind={n} should be rejected"


def test_parse_unwind_override_rejects_non_numeric():
    raw = '{"unwind_override": "not a number", "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.unwind_override is None


def test_parse_unwind_override_missing_field_defaults_to_none():
    """Old-format responses without unwind_override should still parse."""
    raw = '{"unsigned_overflow_check": true, "signed_overflow_check": false, "conversion_check": false, "pointer_overflow_check": false, "reasoning": "old format"}'
    sel = _parse_response(raw, "fn")
    assert sel.unwind_override is None
    assert sel.unsigned_overflow_check is True
    # back-compat — old-format responses still work; undefined_shift_check
    # defaults to False since absent in old responses.
    assert sel.undefined_shift_check is False


def test_parse_unwind_override_boolean_rejected():
    """JSON `true`/`false` coerces to int 1/0 in Python, but neither is meaningful here."""
    raw = '{"unwind_override": true, "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.unwind_override is None


# ---------- FlagSelector.select_all integration ---------------------------


def _mock_config(enable_flag_selection=True, cbmc_unwind=4):
    from bmc_agent.config import Config
    cfg = Config(artifact_dir="/tmp/_flagsel_test")
    cfg.enable_flag_selection = enable_flag_selection
    cfg.cbmc_unwind = cbmc_unwind
    cfg.threat_model = "security"
    cfg.batch_size = 4
    return cfg


def _func_info(name="fn", body="{ return 0; }"):
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(name=name, return_type="int",
                            parameters=[("int", "x")])
    return FunctionInfo(name=name, signature=sig, body=body,
                        callees=set(), source_file="")


def test_select_all_disabled_returns_defaults():
    cfg = _mock_config(enable_flag_selection=False)
    llm = MagicMock()
    sel = FlagSelector(cfg, llm)
    result = sel.select_all({"foo": _func_info("foo")})
    assert "foo" in result
    assert not result["foo"].any_enabled()
    # No LLM call made when disabled.
    assert llm.complete.call_count == 0


def test_select_all_with_unwind_override_in_response():
    cfg = _mock_config(enable_flag_selection=True, cbmc_unwind=4)
    llm = MagicMock()
    llm.complete.return_value = '{"unsigned_overflow_check": false, "signed_overflow_check": false, "conversion_check": false, "pointer_overflow_check": false, "undefined_shift_check": false, "unwind_override": 12, "reasoning": "for (i=0; i<10; i++)"}'
    sel = FlagSelector(cfg, llm)
    result = sel.select_all({"foo": _func_info("foo")})
    assert result["foo"].unwind_override == 12
    assert result["foo"].any_enabled()
    assert "--unwind 12" in result["foo"].enabled_flags()


def test_select_all_with_shift_in_response():
    cfg = _mock_config(enable_flag_selection=True)
    llm = MagicMock()
    llm.complete.return_value = '{"unsigned_overflow_check": false, "signed_overflow_check": false, "conversion_check": false, "pointer_overflow_check": false, "undefined_shift_check": true, "unwind_override": null, "reasoning": "packet field shift"}'
    sel = FlagSelector(cfg, llm)
    result = sel.select_all({"foo": _func_info("foo")})
    assert result["foo"].undefined_shift_check is True
    assert "--undefined-shift-check" in result["foo"].enabled_flags()


def test_select_all_global_unwind_included_in_prompt():
    """The prompt should show the global default so the LLM only overrides
    when it has reason to."""
    cfg = _mock_config(cbmc_unwind=8)
    llm = MagicMock()
    llm.complete.return_value = '{"unwind_override": null}'
    sel = FlagSelector(cfg, llm)
    sel.select_all({"foo": _func_info("foo")})
    # Inspect the prompt sent to the LLM.
    args, kwargs = llm.complete.call_args
    prompt = kwargs.get("user_prompt") or args[1]
    assert "GLOBAL UNWIND DEFAULT: 8" in prompt


def test_select_one_passes_role_cbmc_driver():
    """The flag selector is part of the CBMC driver, so it routes on its OWN
    role 'cbmc_driver' (NOT 'spec_gen' anymore) — that decoupling lets it be
    agentic independently of spec-gen (e.g. spec-gen fast, CBMC driver agentic).
    The call must still pass a role so per-role overrides
    (BMC_AGENT_LLM_CBMC_DRIVER_*) route it.
    """
    cfg = _mock_config()
    llm = MagicMock()
    llm.complete.return_value = '{"unwind_override": null}'
    sel = FlagSelector(cfg, llm)
    sel.select_all({"foo": _func_info("foo")})
    _, kwargs = llm.complete.call_args
    assert kwargs.get("role") == "cbmc_driver", (
        "flag_selector.complete() must pass role='cbmc_driver' (its own role, "
        "decoupled from spec_gen) so per-role overrides apply."
    )


# ---------- timeout_override --------------------------------------------


def test_timeout_override_emitted_in_enabled_flags():
    sel = FlagSelection(timeout_override=300)
    assert sel.any_enabled()
    assert "timeout=300s" in sel.enabled_flags()


def test_timeout_override_none_emits_nothing():
    sel = FlagSelection(timeout_override=None)
    assert "timeout=" not in " ".join(sel.enabled_flags())


def test_timeout_override_in_to_dict():
    sel = FlagSelection(timeout_override=240)
    assert sel.to_dict()["timeout_override"] == 240


def test_parse_timeout_override_accepted():
    raw = '{"timeout_override": 180, "reasoning": "large parser"}'
    sel = _parse_response(raw, "fn")
    assert sel.timeout_override == 180


def test_parse_timeout_override_null_yields_none():
    raw = '{"timeout_override": null, "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.timeout_override is None


def test_parse_timeout_override_string_int_accepted():
    raw = '{"timeout_override": "240", "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.timeout_override == 240


def test_parse_timeout_override_clamps_to_max():
    raw = f'{{"timeout_override": {_MAX_TIMEOUT_OVERRIDE + 1000}, "reasoning": ""}}'
    sel = _parse_response(raw, "fn")
    assert sel.timeout_override == _MAX_TIMEOUT_OVERRIDE


def test_parse_timeout_override_rejects_below_min():
    """timeouts < _MIN_TIMEOUT_OVERRIDE are nonsense (CBMC needs setup time)."""
    for n in (-10, 0, 1, _MIN_TIMEOUT_OVERRIDE - 1):
        raw = f'{{"timeout_override": {n}, "reasoning": ""}}'
        sel = _parse_response(raw, "fn")
        assert sel.timeout_override is None, f"timeout={n} should be rejected"


def test_parse_timeout_override_rejects_boolean():
    raw = '{"timeout_override": true, "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.timeout_override is None


def test_parse_timeout_override_missing_field_defaults_to_none():
    raw = '{"unsigned_overflow_check": true, "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.timeout_override is None


def test_parse_timeout_override_non_numeric_rejected():
    raw = '{"timeout_override": "not a number", "reasoning": ""}'
    sel = _parse_response(raw, "fn")
    assert sel.timeout_override is None


def test_any_enabled_fires_when_only_timeout_set():
    sel = FlagSelection(timeout_override=200)
    assert sel.any_enabled() is True


def test_select_all_global_timeout_included_in_prompt():
    """The prompt should show the global default so the LLM only overrides
    when it has reason to."""
    cfg = _mock_config()
    cfg.cbmc_timeout = 90
    llm = MagicMock()
    llm.complete.return_value = '{"timeout_override": null}'
    sel = FlagSelector(cfg, llm)
    sel.select_all({"foo": _func_info("foo")})
    _, kwargs = llm.complete.call_args
    prompt = kwargs.get("user_prompt") or ""
    assert "default is 90s" in prompt or "90s" in prompt


def test_select_all_with_timeout_override_in_response():
    cfg = _mock_config()
    cfg.cbmc_timeout = 120
    llm = MagicMock()
    llm.complete.return_value = '{"unsigned_overflow_check": false, "signed_overflow_check": false, "conversion_check": false, "pointer_overflow_check": false, "undefined_shift_check": false, "unwind_override": null, "timeout_override": 300, "reasoning": "large parser"}'
    sel = FlagSelector(cfg, llm)
    result = sel.select_all({"foo": _func_info("foo")})
    assert result["foo"].timeout_override == 300
    assert "timeout=300s" in result["foo"].enabled_flags()
