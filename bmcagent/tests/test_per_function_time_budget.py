"""Per-function CBMC time budget (bmc_engine.check_function wrapper).

A pathological parser function (deep unwind + 600s timeout, stacked across
auto-retry + refinement + spec_refiner) could grind a sweep for hours. The
wrapper caps a function's TOTAL CBMC wall-clock: once exhausted, further checks
short-circuit to an errored verdict (which the pipeline routes to unresolved)
instead of invoking CBMC again.
"""

from types import SimpleNamespace

from bmc_agent.bmc_engine import BMCEngine, BMCVerdict


def _engine(budget):
    # Build a bare engine without constructing real backends.
    eng = object.__new__(BMCEngine)
    eng.config = SimpleNamespace(per_function_time_budget_s=budget)
    eng._fn_cumulative_time = {}
    return eng


def _stub_impl(eng, calls):
    def impl(func, spec, parsed_file, driver_name, all_funcs=None, flag_selection=None):
        calls.append(func.name)
        return BMCVerdict(function_name=func.name, verified=True)
    eng._check_function_impl = impl


def test_under_budget_passes_through():
    eng = _engine(1200)
    calls = []
    _stub_impl(eng, calls)
    v = eng.check_function(SimpleNamespace(name="f"), None, None, "drv")
    assert v.verified is True
    assert calls == ["f"]


def test_over_budget_short_circuits_without_running_cbmc():
    eng = _engine(1200)
    calls = []
    _stub_impl(eng, calls)
    # Simulate the function having already consumed its budget.
    eng._fn_cumulative_time["f"] = 1200.0
    v = eng.check_function(SimpleNamespace(name="f"), None, None, "drv")
    # Errored verdict (verified=False, error set, no CExs) -> pipeline unresolved.
    assert v.verified is False
    assert not v.counterexamples
    assert v.error and "budget" in v.error
    # The real check was NOT invoked.
    assert calls == []


def test_budget_zero_is_unlimited():
    eng = _engine(0)
    calls = []
    _stub_impl(eng, calls)
    eng._fn_cumulative_time["f"] = 99999.0  # would exceed any finite budget
    v = eng.check_function(SimpleNamespace(name="f"), None, None, "drv")
    assert v.verified is True
    assert calls == ["f"]  # still ran


def test_time_accumulates_per_function():
    eng = _engine(1200)
    _stub_impl(eng, [])
    eng.check_function(SimpleNamespace(name="f"), None, None, "drv")
    eng.check_function(SimpleNamespace(name="f"), None, None, "drv")
    eng.check_function(SimpleNamespace(name="g"), None, None, "drv")
    # Both calls to 'f' accumulated; 'g' tracked separately.
    assert eng._fn_cumulative_time["f"] >= 0.0
    assert "g" in eng._fn_cumulative_time
    # Two distinct functions tracked.
    assert set(eng._fn_cumulative_time) == {"f", "g"}


def test_other_functions_unaffected_by_one_exhausted():
    eng = _engine(1200)
    calls = []
    _stub_impl(eng, calls)
    eng._fn_cumulative_time["hot"] = 5000.0  # exhausted
    # 'hot' is blocked...
    vh = eng.check_function(SimpleNamespace(name="hot"), None, None, "drv")
    assert vh.verified is False and vh.error
    # ...but a different function still runs.
    vc = eng.check_function(SimpleNamespace(name="cold"), None, None, "drv")
    assert vc.verified is True
    assert calls == ["cold"]
