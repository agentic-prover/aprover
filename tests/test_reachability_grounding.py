"""Unit tests for the channel-guarded grounded-reachability decision.

The hard property: only an argument-driven crash whose grounded reachability is an
explicit 'no' demotes; channel-driven / uncertain / no-call-sites always KEEP
(fail-safe — zero real-bug demotions)."""
import types

from bmc_agent.reachability_grounding import (
    crash_summary, extract_call_sites, grounded_immunity_decision,
)


class _LLM:
    """Mock LLM: returns canned JSON for the origin then the reachability call."""
    def __init__(self, origin, reachable=None):
        self._origin = origin
        self._reach = reachable
        self._calls = 0
    def complete(self, system_prompt, user_prompt, **kw):
        self._calls += 1
        # The origin (channel-guard) prompt asks for an "origin" verdict; the
        # reachability prompt asks for "attacker_reachable".
        if '"origin"' in system_prompt:
            return '{"origin":"%s","why":"x"}' % self._origin
        return '{"attacker_reachable":"%s","why":"x"}' % (self._reach or "yes")


def _func(name, body):
    sig = types.SimpleNamespace(name=name, parameters=[], is_static=True, return_type="void")
    return types.SimpleNamespace(name=name, body=body, signature=sig)


def _cex(prop, **va):
    return types.SimpleNamespace(failing_property=prop, variable_assignments=va, trace=[])


CALLER = {"draw_string": _func("draw_string", "void draw_string(){ fb_draw_char(x,y,c); }")}


def _decide(origin, reachable, all_funcs=CALLER):
    f = _func("fb_draw_char", "void fb_draw_char(int x,int y){ fb[y*W+x]=0; }")
    return grounded_immunity_decision(f, _cex("fb_draw_char.x.1", x="99999"),
                                      all_funcs=all_funcs, llm=_LLM(origin, reachable))


def test_channel_driven_keeps_immunity():
    action, _ = _decide("internal", "no")   # even if "reachable=no", internal -> keep
    assert action == "keep"


def test_uncertain_origin_keeps_immunity():
    action, _ = _decide("garbage", None)
    assert action == "keep"


def test_argdriven_unreachable_demotes():
    action, _ = _decide("argument", "no")
    assert action == "demote"


def test_argdriven_reachable_keeps():
    action, _ = _decide("argument", "yes")
    assert action == "keep"


def test_argdriven_no_callsites_keeps():
    # no in-tree callers -> nothing to ground on -> keep (fail-safe)
    action, _ = _decide("argument", "no", all_funcs={})
    assert action == "keep"


def test_crash_summary_skips_arrays_and_builtins():
    s = crash_summary(_cex("f.bounds.1", x="5", __CPROVER_dead="NULL",
                           big="<array: 256 elements>", y="7"))
    assert "x=5" in s and "y=7" in s
    assert "CPROVER" not in s and "array" not in s


def test_extract_call_sites_finds_caller_bodies():
    sites = extract_call_sites("fb_draw_char", CALLER)
    assert "draw_string" in sites and "fb_draw_char" in sites
