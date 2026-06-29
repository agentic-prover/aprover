"""Mapping + safe-by-default tests for ``web.runner._make_config``.

The crux invariant: ``options=None`` reproduces the historical web-demo Config
byte-for-byte (so the public demo stays safe by default), and a run-settings
overlay sets exactly the requested knobs while leaving everything else at its
Config default. Also covers per-role routing (the single BYOK key is injected
into every role) and the scaled-recovery path.
"""
from __future__ import annotations

from pathlib import Path

from web import limits, options, runner


def _cfg(tmp_path, **kw):
    return runner._make_config(Path(tmp_path), **kw)


def test_options_none_reproduces_demo_defaults(tmp_path):
    c = _cfg(tmp_path)
    assert c.enable_dynamic_validation is False
    assert c.enable_realism_check is False
    assert c.enable_realism_thinking is False
    assert c.cbmc_timeout == 60
    assert c.cbmc_unwind == 4
    assert c.max_refinement_iters == 2
    assert c.max_spec_retries == 5


def test_empty_options_equals_none(tmp_path):
    a, b = _cfg(tmp_path, options=None), _cfg(tmp_path, options={})
    for f in ("enable_dynamic_validation", "enable_realism_check", "cbmc_timeout",
              "cbmc_unwind", "max_refinement_iters", "max_spec_retries"):
        assert getattr(a, f) == getattr(b, f)


def test_overlay_sets_only_requested(tmp_path):
    opts = options.parse_options({
        "ai_layers": {"enable_realism_check": True, "enable_flag_selection": False},
        "depth": {"cbmc_unwind": 8},
        "threat": {"threat_model": "safety", "threat_model_context": "trusted callers"},
    })
    c = _cfg(tmp_path, options=opts)
    assert c.enable_realism_check is True       # overlaid over the demo default
    assert c.enable_flag_selection is False
    assert c.cbmc_unwind == 8
    assert c.threat_model == "safety"
    assert c.threat_model_context == "trusted callers"
    # Untouched knobs keep their Config defaults — overlay never resets them.
    assert c.enable_feedback_loop is True
    assert c.enable_spec_refiner is True
    # Demo defaults the overlay didn't touch are still in force.
    assert c.enable_dynamic_validation is False
    assert c.cbmc_timeout == 60


def test_harness_and_math_ints_overlay(tmp_path):
    opts = options.parse_options({
        "harness": {"raw_bytes": True, "lite_mode": True,
                    "cbmc_defines": ["FOO=1"], "scale_down_size": 8},
        "spec_mode": {"math_ints": True},
    })
    c = _cfg(tmp_path, options=opts)
    assert c.raw_bytes is True
    assert c.lite_mode is True
    assert c.cbmc_defines == ["FOO=1"]
    assert c.scale_down_size == 8
    assert c.math_ints is True


def test_per_role_routing_injects_byok_key(tmp_path):
    opts = options.parse_options({"agentic": {"llm": {"roles": {
        "spec_gen": {"model": "claude-opus-4-8"},
        "refinement": {"model": "claude-sonnet-4-6", "provider": "anthropic"},
    }}}})
    c = _cfg(tmp_path, api_key="sk-byok", options=opts)
    assert c.llm_role_overrides["spec_gen"] == {"model": "claude-opus-4-8", "api_key": "sk-byok"}
    assert c.llm_role_overrides["refinement"]["api_key"] == "sk-byok"
    assert c.llm_role_overrides["refinement"]["provider"] == "anthropic"


def test_clamped_unwind_reaches_config(tmp_path):
    opts = options.parse_options({"depth": {"cbmc_unwind": 99999}})
    assert _cfg(tmp_path, options=opts).cbmc_unwind == limits.MAX_CBMC_UNWIND


def test_scale_down_kwarg_forces_safety_only(tmp_path):
    # The recovery "retry · scaled" path forces both, independent of options.
    c = _cfg(tmp_path, scale_down=True)
    assert c.scale_down is True
    assert c.safety_only is True


def test_oracle_option_is_ignored(tmp_path):
    # The oracle (frama-c/openjml) is only honored by the CLI-only spec-synthesis
    # path, not by pipeline.run(). The web drops it in parse_options and never
    # sets config.oracle, so a web run always uses the default cbmc oracle.
    assert options.parse_options({"oracle": {"oracle": "frama-c"}}) == {}
    c = _cfg(tmp_path, options={"oracle": {"oracle": "frama-c"}})
    assert c.oracle == "cbmc"


def test_harness_and_agentic_overlay(tmp_path):
    opts = options.parse_options({
        "harness": {"cbmc_real_libc": True, "infer_array_param_bounds": True,
                    "cbmc_defines": ["FOO=1", "bad; rm"]},
        "agentic": {"enable_agentic_harness": True, "agentic_refine_rounds": 99},
    })
    c = _cfg(tmp_path, options=opts)
    assert c.cbmc_real_libc is True
    assert c.infer_array_param_bounds is True
    assert c.cbmc_defines == ["FOO=1"]                 # injection sanitized away
    assert c.enable_agentic_harness is True
    assert c.agentic_refine_rounds == limits.MAX_AGENTIC_REFINE_ROUNDS   # clamped


def test_autonomous_forces_no_self_patch_and_converges(tmp_path, monkeypatch):
    (tmp_path / "a.c").write_text("int f(int x){return x;}\n")
    seen = {}

    class _FakePipe:
        def __init__(self, config):
            seen["config"] = config

        def verify_tree(self, **kw):
            return {"a.c": []}

    monkeypatch.setattr(runner, "AMCPipeline", _FakePipe)
    evs = list(runner.run_autonomous_streaming(
        str(tmp_path), api_key="sk-test", provider="anthropic", max_rounds=3,
        options={"harness": {"lite_mode": True}}))
    cfg = seen["config"]
    assert cfg.allow_self_patch == "deny"          # the web never auto-patches source
    assert cfg.lite_mode is True                   # user run-option still applied
    # Two identical empty rounds → fixed point → stops before the 3rd round.
    assert sum(1 for e in evs if e.get("type") == "run") == 2
    assert evs[-1]["type"] == "result" and evs[-1]["result"]["ok"] is True
