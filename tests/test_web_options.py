"""Tests for the run-settings validator (``web.options.parse_options``).

The browser sends an untrusted ``options`` object; ``parse_options`` coerces
types, drops unknown keys, clamps every resource knob to a ``web.limits`` ceiling,
and sanitizes free-text / defines. These tests pin that contract — the public
BYOK demo's resource-safety guarantee rests on the clamping, and "absent ⇒
default" rests on only-sent keys surviving (so the runner can tell "untouched"
from "changed").
"""
from __future__ import annotations

import importlib

from web import limits, options


def test_absent_and_malformed_yield_empty():
    assert options.parse_options(None) == {}
    assert options.parse_options("nope") == {}
    assert options.parse_options({}) == {}
    # A group that isn't a dict is ignored, not fatal.
    assert options.parse_options({"depth": "notadict"}) == {}


def test_only_sent_keys_survive():
    o = options.parse_options({"ai_layers": {"enable_realism_check": True}})
    assert o == {"ai_layers": {"enable_realism_check": True}}


def test_unknown_keys_and_groups_dropped_not_errored():
    o = options.parse_options({
        "ai_layers": {"enable_realism_check": True, "bogus_knob": 1},
        "totally_unknown_group": {"x": 1},
    })
    assert o["ai_layers"] == {"enable_realism_check": True}
    assert "totally_unknown_group" not in o


def test_bool_false_is_kept():
    # Turning a default-on layer OFF is meaningful and must survive.
    o = options.parse_options({"ai_layers": {"enable_flag_selection": False}})
    assert o["ai_layers"]["enable_flag_selection"] is False


def test_bool_string_coercion():
    o = options.parse_options({"harness": {"raw_bytes": "true", "strict_dsl": "0"}})
    assert o["harness"]["raw_bytes"] is True
    assert o["harness"]["strict_dsl"] is False


def test_ints_clamped_to_ceiling():
    o = options.parse_options({"depth": {
        "cbmc_unwind": 10_000, "cbmc_timeout": 10_000,
        "per_function_time_budget_s": 10 ** 9, "max_workers": 999,
    }})
    d = o["depth"]
    assert d["cbmc_unwind"] == limits.MAX_CBMC_UNWIND
    assert d["cbmc_timeout"] == limits.MAX_CBMC_TIMEOUT
    assert d["per_function_time_budget_s"] == limits.MAX_PER_FN_BUDGET_S
    assert d["max_workers"] == limits.MAX_WORKERS


def test_negative_int_floored_to_zero():
    o = options.parse_options({"depth": {"cbmc_unwind": -5}})
    assert o["depth"]["cbmc_unwind"] == 0


def test_unparseable_int_dropped():
    o = options.parse_options({"depth": {"cbmc_unwind": "abc", "cbmc_timeout": 30}})
    assert "cbmc_unwind" not in o["depth"]
    assert o["depth"]["cbmc_timeout"] == 30


def test_enums_validated_and_bad_values_dropped():
    assert options.parse_options(
        {"threat": {"threat_model": "safety"}})["threat"]["threat_model"] == "safety"
    assert options.parse_options({"threat": {"threat_model": "chaos"}}) == {}
    # The oracle is a CLI-only synthesis knob — never wired into the web run path,
    # so it's dropped entirely rather than validated/passed through.
    assert options.parse_options({"oracle": {"oracle": "frama-c"}}) == {}


def test_threat_context_truncated():
    o = options.parse_options({"threat": {"threat_model_context": "x" * 99999}})
    assert len(o["threat"]["threat_model_context"]) == limits.MAX_THREAT_CONTEXT_CHARS


def test_cbmc_defines_sanitized():
    defs = ["FOO", "BAR=1", "PATH_OK=a/b.c", "bad; rm -rf /", "WITH SPACE", "$(evil)"]
    o = options.parse_options({"harness": {"cbmc_defines": defs}})
    assert o["harness"]["cbmc_defines"] == ["FOO", "BAR=1", "PATH_OK=a/b.c"]


def test_cbmc_defines_count_capped():
    o = options.parse_options({"harness": {"cbmc_defines": [f"D{i}" for i in range(1000)]}})
    assert len(o["harness"]["cbmc_defines"]) <= limits.MAX_CBMC_DEFINES


def test_spec_mode_keeps_only_math_ints():
    # math_ints is the one spec_mode knob the web runner honors.
    o = options.parse_options({"spec_mode": {"math_ints": True}})
    assert o["spec_mode"] == {"math_ints": True}
    # The CLI-only synthesis knobs (mode / entry / no_overflow_rigor) are not
    # wired into pipeline.run(), so they're dropped — never validated-but-ignored.
    o2 = options.parse_options(
        {"spec_mode": {"mode": "standalone", "entry": "do_thing",
                       "no_overflow_rigor": True}})
    assert o2 == {}


def test_roles_drop_unknown_role_and_provider():
    o = options.parse_options({"agentic": {"llm": {"roles": {
        "spec_gen": {"model": "claude-opus-4-8", "provider": "anthropic"},
        "refinement": {"provider": "evil"},   # bad provider → field gone → spec empty → dropped
        "not_a_role": {"model": "x"},          # unknown role → dropped
    }}}})
    assert o["agentic"]["llm"]["roles"] == {
        "spec_gen": {"model": "claude-opus-4-8", "provider": "anthropic"}
    }


def test_roles_never_carry_a_key():
    # Per-role keys are intentionally not accepted in the body (secrets stay in
    # headers); a key field in a role spec is dropped.
    o = options.parse_options({"agentic": {"llm": {"roles": {
        "spec_gen": {"model": "m", "api_key": "sk-LEAK"},
    }}}})
    assert o["agentic"]["llm"]["roles"]["spec_gen"] == {"model": "m"}
    assert "sk-LEAK" not in repr(o)


def test_run_mode_enum():
    assert options.parse_options({"run_mode": "autonomous"})["run_mode"] == "autonomous"
    assert "run_mode" not in options.parse_options({"run_mode": "rm -rf"})


def test_autonomous_max_rounds_clamped():
    assert options.parse_options(
        {"autonomous": {"max_rounds": 999}})["autonomous"]["max_rounds"] == limits.MAX_AUTO_ROUNDS


def test_clamp_respects_env_override(monkeypatch):
    # A self-host raises a ceiling via env; parse_options honors it after reload
    # (it reads limits.MAX_* at call time). Restore the clean module afterwards.
    monkeypatch.setenv("BMC_AGENT_WEB_MAX_CBMC_UNWIND", "500")
    importlib.reload(limits)
    try:
        assert options.parse_options({"depth": {"cbmc_unwind": 400}})["depth"]["cbmc_unwind"] == 400
        assert options.parse_options({"depth": {"cbmc_unwind": 9999}})["depth"]["cbmc_unwind"] == 500
    finally:
        monkeypatch.delenv("BMC_AGENT_WEB_MAX_CBMC_UNWIND", raising=False)
        importlib.reload(limits)
