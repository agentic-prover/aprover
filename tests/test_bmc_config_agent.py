"""Tests for ``bmc_agent.agents.bmc_config_tools.BmcConfigAgent``.

The merged BMC configuration agent replaces the two single-LLM-call
configurators (``FlagSelector`` + ``InliningAdvisor``) with one tool-using
agent that produces, per function, BOTH the per-function CBMC flag/unwind/
timeout selection AND the inline-vs-stub promotions for stubbed callees.

These tests mock the LLM so NO network / tool calls happen — they exercise
``parse()`` directly on hand-written JSON, plus the merged system prompt and
the ``select_all`` driver.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_agent(llm=None, parsed_file=None, corpus_paths=None):
    from bmc_agent.agents.bmc_config_tools import BmcConfigAgent
    from bmc_agent.config import Config
    return BmcConfigAgent(
        config=Config(llm_api_key="t"),
        llm=llm or MagicMock(),
        parsed_file=parsed_file or MagicMock(),
        corpus_paths=corpus_paths or [],
    )


def _func(name="fn", ret="int", params=None, body="return 0;"):
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name=name, return_type=ret, parameters=params or [("int", "x")],
    )
    return FunctionInfo(
        name=name, signature=sig, body=body, callees=set(),
        source_file="t.c",
    )


# ---------------------------------------------------------------------------
# Identity / routing
# ---------------------------------------------------------------------------


def test_routes_via_cbmc_driver_role():
    from bmc_agent.agents.bmc_config_tools import BmcConfigAgent
    assert BmcConfigAgent.name == "cbmc_driver"


def test_tool_budget_attrs():
    from bmc_agent.agents.bmc_config_tools import BmcConfigAgent
    assert BmcConfigAgent.max_iterations_param == 8
    assert BmcConfigAgent.max_tool_calls_param == 10
    assert BmcConfigAgent.max_tokens_per_turn_param == 2048


# ---------------------------------------------------------------------------
# Merged system prompt contains BOTH guidance bodies + tools directive
# ---------------------------------------------------------------------------


def test_system_prompt_merges_flag_and_inline_and_tools():
    from bmc_agent.agents.bmc_config_tools import BmcConfigAgent
    sp = BmcConfigAgent.system_prompt
    # flag-selection markers
    assert "--unsigned-overflow-check" in sp
    assert "--undefined-shift-check" in sp
    assert "unwind override" in sp
    assert "timeout override" in sp
    # inline-vs-stub markers
    assert "INLINE" in sp and "STUB" in sp
    assert "the default is STUB" in sp.lower() or "default is STUB" in sp
    # tools directive
    assert "lookup_function" in sp
    assert "grep_corpus" in sp
    assert "lookup_struct" in sp


# ---------------------------------------------------------------------------
# parse(): flag bits map through
# ---------------------------------------------------------------------------


def test_parse_flag_bits_map_through():
    agent = _make_agent()
    raw = json.dumps({
        "unsigned_overflow_check": True,
        "signed_overflow_check": False,
        "conversion_check": True,
        "pointer_overflow_check": False,
        "undefined_shift_check": True,
        "unwind_override": None,
        "timeout_override": None,
        "inline": {},
        "reasoning": "size math",
    })
    cfg = agent.parse(raw)
    assert cfg is not None
    f = cfg.flags
    assert f.unsigned_overflow_check is True
    assert f.signed_overflow_check is False
    assert f.conversion_check is True
    assert f.pointer_overflow_check is False
    assert f.undefined_shift_check is True
    assert f.unwind_override is None
    assert f.timeout_override is None
    assert cfg.reasoning == "size math"


# ---------------------------------------------------------------------------
# parse(): unwind/timeout clamping (same caps as flag_selector)
# ---------------------------------------------------------------------------


def test_parse_unwind_clamped_to_max():
    cfg = _make_agent().parse(json.dumps({"unwind_override": 999}))
    assert cfg.flags.unwind_override == 64  # _MAX_UNWIND_OVERRIDE


def test_parse_unwind_below_two_dropped():
    # CBMC requires unwind >= 2; 1 -> None.
    cfg = _make_agent().parse(json.dumps({"unwind_override": 1}))
    assert cfg.flags.unwind_override is None


def test_parse_unwind_valid_passes():
    cfg = _make_agent().parse(json.dumps({"unwind_override": 16}))
    assert cfg.flags.unwind_override == 16


def test_parse_timeout_below_min_dropped():
    # 5s is below _MIN_TIMEOUT_OVERRIDE (30) -> None.
    cfg = _make_agent().parse(json.dumps({"timeout_override": 5}))
    assert cfg.flags.timeout_override is None


def test_parse_timeout_clamped_to_max():
    cfg = _make_agent().parse(json.dumps({"timeout_override": 99999}))
    assert cfg.flags.timeout_override == 600  # _MAX_TIMEOUT_OVERRIDE


def test_parse_timeout_valid_passes():
    cfg = _make_agent().parse(json.dumps({"timeout_override": 300}))
    assert cfg.flags.timeout_override == 300


def test_parse_unwind_as_string():
    cfg = _make_agent().parse(json.dumps({"unwind_override": "8"}))
    assert cfg.flags.unwind_override == 8


# ---------------------------------------------------------------------------
# parse(): inline promotions parse; unknown callees recorded but stub-default
# ---------------------------------------------------------------------------


def test_parse_inline_promotions():
    raw = json.dumps({
        "inline": {
            "get_kind": {"inline": True, "reason": "tag getter"},
            "big_parser": {"inline": False, "reason": "has loops"},
        },
    })
    cfg = _make_agent().parse(raw)
    assert cfg.inline_overrides["get_kind"].inline is True
    assert cfg.inline_overrides["get_kind"].reason == "tag getter"
    assert cfg.inline_overrides["big_parser"].inline is False
    # only the genuine promotion shows in promotions()
    assert set(cfg.promotions().keys()) == {"get_kind"}


def test_parse_inline_malformed_entries_skipped():
    raw = json.dumps({
        "inline": {
            "ok": {"inline": True, "reason": "good"},
            "bad": "not a dict",
            "": {"inline": True},
        },
    })
    cfg = _make_agent().parse(raw)
    assert "ok" in cfg.inline_overrides
    assert "bad" not in cfg.inline_overrides
    assert "" not in cfg.inline_overrides


def test_parse_inline_missing_block_empty():
    cfg = _make_agent().parse(json.dumps({"unsigned_overflow_check": True}))
    assert cfg.inline_overrides == {}
    assert cfg.promotions() == {}


# ---------------------------------------------------------------------------
# parse(): fences + surrounding prose tolerated
# ---------------------------------------------------------------------------


def test_parse_code_fences_and_prose():
    raw = (
        "Here is my decision:\n```json\n"
        + json.dumps({"unsigned_overflow_check": True, "inline": {}})
        + "\n```\nDone."
    )
    cfg = _make_agent().parse(raw)
    assert cfg is not None
    assert cfg.flags.unsigned_overflow_check is True


# ---------------------------------------------------------------------------
# parse(): fail-safe behaviour
# ---------------------------------------------------------------------------


def test_parse_malformed_json_safe_default():
    # Non-empty text but no recoverable JSON -> safe default (flags off),
    # NOT None.
    cfg = _make_agent().parse("this is not json at all {oops")
    assert cfg is not None
    assert cfg.flags.any_enabled() is False
    assert cfg.inline_overrides == {}


def test_parse_empty_returns_none():
    assert _make_agent().parse("") is None
    assert _make_agent().parse("   \n  ") is None


# ---------------------------------------------------------------------------
# build_prompt(): renders signature, FULL body, defaults, candidates
# ---------------------------------------------------------------------------


def test_build_prompt_includes_full_body_and_candidates():
    agent = _make_agent()
    body = "int q = 0;\n" * 200  # long body — must NOT be truncated
    func = _func(name="parse_thing", body=body)
    prompt = agent.build_prompt(
        func=func,
        global_unwind=4,
        global_timeout=120,
        stub_candidates=["helper_a", "helper_b"],
    )
    assert "parse_thing" in prompt
    assert "GLOBAL UNWIND DEFAULT: 4" in prompt
    assert "GLOBAL TIMEOUT DEFAULT: 120s" in prompt
    assert "helper_a" in prompt and "helper_b" in prompt
    # full body present (length proves no 1500/2000-char truncation)
    assert prompt.count("int q = 0;") == 200


def test_build_prompt_no_candidates():
    agent = _make_agent()
    prompt = agent.build_prompt(
        func=_func(), global_unwind=4, global_timeout=120,
        stub_candidates=[],
    )
    assert "none" in prompt.lower()


# ---------------------------------------------------------------------------
# select_all(): drives run() per function, falls back safely
# ---------------------------------------------------------------------------


def test_select_all_empty_returns_empty():
    assert _make_agent().select_all({}) == {}


def test_select_all_uses_parsed_results(monkeypatch):
    from bmc_agent.agents.bmc_config_tools import BmcConfig
    from bmc_agent.flag_selector import FlagSelection
    agent = _make_agent()

    # Stub the LLM round-trip so no tools/network fire: return a fixed JSON.
    def fake_call_llm(prompt):
        return json.dumps({
            "unsigned_overflow_check": True,
            "inline": {"h": {"inline": True, "reason": "getter"}},
            "reasoning": "ok",
        }), None

    monkeypatch.setattr(agent, "_call_llm", fake_call_llm)

    funcs = {"f1": _func("f1"), "f2": _func("f2")}
    out = agent.select_all(
        funcs, stub_candidates_by_func={"f1": ["h"], "f2": []},
    )
    assert set(out.keys()) == {"f1", "f2"}
    assert isinstance(out["f1"], BmcConfig)
    assert out["f1"].flags.unsigned_overflow_check is True
    assert out["f1"].promotions().keys() == {"h"}


def test_select_all_falls_back_on_failure(monkeypatch):
    agent = _make_agent()

    def fake_call_llm(prompt):
        return "", "LLMError: boom"

    monkeypatch.setattr(agent, "_call_llm", fake_call_llm)
    out = agent.select_all({"f1": _func("f1")})
    assert "f1" in out
    # safe default: flags off, no promotions
    assert out["f1"].flags.any_enabled() is False
    assert out["f1"].promotions() == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
