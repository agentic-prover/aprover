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
