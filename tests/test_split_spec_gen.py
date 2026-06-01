"""Tests for split spec generation (pass-1 postcondition / pass-2 contract precondition)."""
from dataclasses import dataclass
from bmc_agent.spec_generator_v2 import SpecGeneratorV2
from bmc_agent.spec import Spec


def _gen(enable):
    g = SpecGeneratorV2.__new__(SpecGeneratorV2)   # bypass __init__
    class _Cfg: pass
    cfg = _Cfg(); cfg.enable_split_spec_gen = enable
    g.config = cfg
    return g


@dataclass
class _Sig:
    return_type: str = "int"
    name: str = "f"
    parameters: tuple = ()


@dataclass
class _Func:
    name: str = "f"
    body: str = "int f(const unsigned char*b,unsigned long n){return b[0]?tab[b[0]]:0;}"
    signature: _Sig = None
    def __post_init__(self):
        if self.signature is None:
            self.signature = _Sig()


class _Bundle:
    callers = []


def _spec():
    s = Spec(function_name="f",
             precondition="!null(b) && b[0] < 16 && n >= 1",   # caller-grounded (bug-masking)
             postcondition="\\result >= 0")
    s.pre_validity = "!null(b) && b[0] < 16 && n >= 1"
    s.pre_protocol = ""
    return s


def test_split_disabled_is_noop():
    g = _gen(enable=False)
    s = _spec()
    out = g._maybe_split_precondition(s, _Func(), _Bundle())
    assert out.precondition == s.precondition   # unchanged


def test_split_overrides_precondition_keeps_postcondition(monkeypatch):
    g = _gen(enable=True)
    # mock pass-2 to return the contract-only precondition (data-value clause dropped)
    monkeypatch.setattr(g, "_contract_precondition", lambda func_info, bundle: "!null(b) && n >= 1")
    s = _spec()
    out = g._maybe_split_precondition(s, _Func(), _Bundle())
    # precondition is now contract-only — the bug-masking `b[0] < 16` is GONE
    assert "b[0] < 16" not in out.precondition
    assert out.precondition == "!null(b) && n >= 1"
    assert out.pre_validity == "!null(b) && n >= 1"
    # postcondition untouched (pass 1 preserved)
    assert out.postcondition == "\\result >= 0"
    assert out.function_name == "f"


def test_split_keeps_pre_protocol(monkeypatch):
    g = _gen(enable=True)
    monkeypatch.setattr(g, "_contract_precondition", lambda func_info, bundle: "!null(b)")
    s = _spec(); s.pre_protocol = "locked(lk)"
    out = g._maybe_split_precondition(s, _Func(), _Bundle())
    assert out.precondition == "!null(b) && locked(lk)"


def test_split_pass2_failure_keeps_original(monkeypatch):
    g = _gen(enable=True)
    monkeypatch.setattr(g, "_contract_precondition", lambda func_info, bundle: None)
    s = _spec()
    out = g._maybe_split_precondition(s, _Func(), _Bundle())
    assert out.precondition == s.precondition   # unchanged on pass-2 failure
