"""Focused tests for the adaptive top-level PlanAgent."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace


def test_initial_plan_marks_size_arithmetic_for_overflow():
    from bmc_agent.agents.plan_agent import PlanAgent

    parsed = SimpleNamespace(
        function_bodies={
            "main": "return allocish(n);",
            "allocish": "size_t bytes = len * sizeof(char); return bytes;",
            "plain": "return 0;",
        },
        functions={},
        call_graph={"main": {"allocish", "plain"}},
    )

    plan = PlanAgent().initial_plan(parsed, entry="main", property_class="memsafety")

    assert plan.func_props["allocish"] == "all"
    assert plan.func_props["plain"] == "memsafety"


def test_replan_preserves_function_property_map_and_arch():
    from bmc_agent.agents.plan_agent import Plan, PlanAgent

    template = Plan(
        strategy="scope_from_entry",
        entry="main",
        property_class="memsafety",
        arch="ILP32",
        timeout=45,
        func_props={"main": "memsafety", "grow": "all"},
    )

    replanned = PlanAgent().plan_for_strategy(
        "frame_havoc",
        entry="main",
        property_class=template.property_class,
        unwind=2,
        template=template,
    )

    assert replanned.strategy == "frame_havoc"
    assert replanned.unwind == 2
    assert replanned.arch == "ILP32"
    assert replanned.timeout == 45
    assert replanned.func_props == template.func_props
    assert replanned.func_props is not template.func_props


def test_apply_plan_sets_and_clears_function_property_map(monkeypatch):
    from bmc_agent.agents.plan_agent import Plan, apply_plan

    monkeypatch.delenv("BMC_FUNC_PROP_MAP", raising=False)

    apply_plan(
        None,
        Plan(
            strategy="scope_from_entry",
            property_class="memsafety",
            func_props={"main": "memsafety", "grow": "all"},
        ),
    )

    assert json.loads(os.environ["BMC_FUNC_PROP_MAP"]) == {
        "main": "memsafety",
        "grow": "all",
    }

    apply_plan(None, Plan(strategy="scope_from_entry", property_class="memsafety"))

    assert "BMC_FUNC_PROP_MAP" not in os.environ


def test_apply_plan_uses_lean_scope_for_svcomp_unreach(monkeypatch):
    from bmc_agent.agents.plan_agent import Plan, apply_plan

    class Cfg:
        cbmc_unwind = 4
        enable_bmc_config_agent = True
        enable_flag_selection = True
        enable_spec_gen_tools = True
        use_legacy_spec_gen = False
        lite_mode = False
        lite_with_contracts = True

    monkeypatch.setenv("BMC_SVCOMP_MODE", "1")
    cfg = Cfg()

    only = apply_plan(
        cfg,
        Plan(
            strategy="scope_from_entry",
            entry="main",
            property_class="unreach-call",
            targets={"main"},
        ),
    )

    assert only == {"main"}
    assert cfg.enable_bmc_config_agent is False
    assert cfg.enable_flag_selection is False
    assert cfg.enable_spec_gen_tools is False
    assert cfg.use_legacy_spec_gen is True
    assert cfg.lite_mode is True
    assert cfg.lite_with_contracts is False


def test_svcomp_frame_havoc_bug_requires_scope_confirmation(monkeypatch):
    from bmc_agent.agents.plan_agent import Plan
    from bmc_agent.cli import (
        _svcomp_frame_havoc_bmc_confirmation_strategy,
        _svcomp_frame_havoc_confirmation_strategy,
    )

    monkeypatch.delenv("BMC_SVCOMP_MODE", raising=False)
    args = SimpleNamespace(svcomp=True)
    plan = Plan(
        strategy="frame_havoc",
        entry="main",
        property_class="unreach-call",
        fallback_ladder=["scope_from_entry"],
    )

    assert (
        _svcomp_frame_havoc_confirmation_strategy(
            args, plan, bug_reports=[object()], tried=["frame_havoc"]
        )
        == "scope_from_entry"
    )
    assert (
        _svcomp_frame_havoc_bmc_confirmation_strategy(
            args,
            plan,
            verdict_summary={"bug_candidates": 1},
            tried=["frame_havoc"],
        )
        == "scope_from_entry"
    )
    assert (
        _svcomp_frame_havoc_bmc_confirmation_strategy(
            args,
            plan,
            verdict_summary={"bug_candidates": 0},
            tried=["frame_havoc"],
        )
        is None
    )


def test_svcomp_frame_havoc_confirmation_is_narrow(monkeypatch):
    from bmc_agent.agents.plan_agent import Plan
    from bmc_agent.cli import _svcomp_frame_havoc_confirmation_strategy

    monkeypatch.delenv("BMC_SVCOMP_MODE", raising=False)
    base = Plan(
        strategy="frame_havoc",
        entry="main",
        property_class="unreach-call",
        fallback_ladder=["scope_from_entry"],
    )

    assert (
        _svcomp_frame_havoc_confirmation_strategy(
            SimpleNamespace(svcomp=True), base, bug_reports=[], tried=["frame_havoc"]
        )
        is None
    )
    assert (
        _svcomp_frame_havoc_confirmation_strategy(
            SimpleNamespace(svcomp=True),
            base,
            bug_reports=[object()],
            tried=["frame_havoc", "scope_from_entry"],
        )
        is None
    )
    assert (
        _svcomp_frame_havoc_confirmation_strategy(
            SimpleNamespace(svcomp=False),
            base,
            bug_reports=[object()],
            tried=["frame_havoc"],
        )
        is None
    )
    non_reach = Plan(
        strategy="frame_havoc",
        entry="main",
        property_class="memsafety",
        fallback_ladder=["scope_from_entry"],
    )
    assert (
        _svcomp_frame_havoc_confirmation_strategy(
            SimpleNamespace(svcomp=True),
            non_reach,
            bug_reports=[object()],
            tried=["frame_havoc"],
        )
        is None
    )


def test_verify_plan_is_default_with_no_plan_escape():
    from bmc_agent.cli import build_parser

    parser = build_parser()

    default_args = parser.parse_args(
        ["verify", "--source", "example.c", "--driver", "drv"]
    )
    assert default_args.plan is True

    manual_args = parser.parse_args(
        ["verify", "--source", "example.c", "--driver", "drv", "--no-plan"]
    )
    assert manual_args.plan is False


def test_agentic_codex_implies_codex_provider():
    from bmc_agent.cli import _apply_provider_args, build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["verify", "--source", "example.c", "--driver", "drv", "--agentic-codex"]
    )
    config = SimpleNamespace()

    _apply_provider_args(config, args)

    assert config.llm_provider == "codex"

    explicit = parser.parse_args(
        [
            "verify",
            "--source",
            "example.c",
            "--driver",
            "drv",
            "--agentic-codex",
            "--provider",
            "openai",
        ]
    )
    explicit_config = SimpleNamespace()

    _apply_provider_args(explicit_config, explicit)

    assert explicit_config.llm_provider == "openai"
