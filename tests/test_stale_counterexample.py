"""Stale-counterexample gate: a CEx from a superseded (regenerated/repaired)
harness must not be emitted as a confirmed finding.

Root cause (vibeos net icmp/ip/tcp_handle): the harness file is mutated in place
across phases. An early under-modelled `(buf,len)` harness (`_pkt_buf[5]`) yields
a spurious OOB CEx; agentic repair later regenerates a correct `malloc(len)`
harness that verifies clean — but the frozen CEx is still reported. Detection:
the CEx assigns `_pkt_buf`, which is absent from the current `malloc(len)` harness.
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from bmc_agent.pipeline import _stale_counterexample_reason


def _ce(va):
    return SimpleNamespace(variable_assignments=va, failing_property="f.x.1")


def _harness(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".c", delete=False)
    f.write(text); f.close()
    return f.name


def test_stale_when_local_absent_from_current_harness():
    # CEx from the OLD under-sized harness; current harness uses malloc(len).
    h = _harness("uint8_t *pkt = (uint8_t *)malloc(len);\n  icmp_handle(pkt, len);\n")
    ce = _ce({"_pkt_buf": "<array: 5 elements>", "_pkt_buf[1l]": "0", "len": "319u"})
    reason = _stale_counterexample_reason(ce, h)
    assert reason is not None
    assert "_pkt_buf" in reason


def test_not_stale_when_local_present():
    # Same harness that produced the CEx -> consistent -> not stale.
    h = _harness("uint8_t _pkt_buf[5];\n  uint8_t *pkt = _pkt_buf;\n")
    ce = _ce({"_pkt_buf": "<array: 5 elements>", "len": "319u"})
    assert _stale_counterexample_reason(ce, h) is None


def test_cprover_and_param_vars_ignored():
    # __CPROVER_* and non-underscore names are not harness locals -> ignored.
    h = _harness("int f(int len){ return len; }\n")
    ce = _ce({"__CPROVER_dead_object": "NULL", "len": "5", "src_ip": "0u"})
    assert _stale_counterexample_reason(ce, h) is None


def test_no_harness_path_is_noop():
    ce = _ce({"_pkt_buf": "x"})
    assert _stale_counterexample_reason(ce, "") is None
    assert _stale_counterexample_reason(ce, "/nonexistent/harness.c") is None


def test_empty_or_missing_assignments():
    h = _harness("int main(void){return 0;}")
    assert _stale_counterexample_reason(_ce({}), h) is None
    assert _stale_counterexample_reason(SimpleNamespace(), h) is None


def test_word_boundary_no_false_match():
    # '_pkt_buf' must not match '_pkt_buffer' as a substring.
    h = _harness("uint8_t _pkt_buffer[5];\n")
    ce = _ce({"_pkt_buf": "<array: 5 elements>"})
    assert _stale_counterexample_reason(ce, h) is not None  # _pkt_buf != _pkt_buffer
