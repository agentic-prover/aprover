"""Test the harness-text unwind-floor parser (keeps the per-function unwind in
lockstep with the string-copy SOURCE widths the harness actually applied, even
when func.body carries an unexpanded macro destination size)."""
from bmc_agent.bmc_engine import _copy_widen_floor_from_harness as floor


def test_return_stub_width():
    h = "/* copy-source RETURN modeling: 'data' ... widen it (256 chars, nondet) */"
    assert floor(h) == 258


def test_param_widen_width():
    h = "/* copy-sink source 'p': widened to 16 chars to expose overflow */"
    assert floor(h) == 18


def test_field_widen_width():
    h = "/* copy-source source field 'name': widened to 64 chars */"
    assert floor(h) == 66


def test_max_across_multiple():
    h = ("widened to 16 chars\n"
         "widen it (256 chars\n"
         "widened to 32 chars\n")
    assert floor(h) == 258            # max width 256 -> 258


def test_none_when_no_widening():
    assert floor("/* ordinary harness, no copy widening */") == 0


def test_empty_input():
    assert floor("") == 0
    assert floor(None) == 0
