"""
Tests for the CBMC-errored / dyn-val skip-flip policy (commit 9e2bef3).

When BOTH CBMC reachability AND callee-feasibility checks errored
(exit code 6), the historical policy was to SKIP dynamic validation
entirely. That inverted the trust hierarchy — with the static checks
broken, the LLM-built dynamic harness becomes the ONLY mechanical
oracle. The new policy keeps dynamic validation in those cases UNLESS
the reproducer was marked UNREPRODUCIBLE, in which case there's no
harness to run anyway.

Tests cover the three boolean dimensions:
  (reach_errored × feas_errored × reproducer-state) → run or skip
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_validator(dynamic_validator):
    """Build a CExValidator without going through __init__ — the only
    state _try_dynamic_validation reads is self._dynamic_validator and
    the per-CEx _reach_errored / _feas_errored flags.
    """
    from bmc_agent.cex_validator import CExValidator
    v = object.__new__(CExValidator)
    v._dynamic_validator = dynamic_validator
    v._reach_errored = False
    v._feas_errored = False
    return v


def _make_validation_result(reproducer: str | None = "/* real reproducer */", caller_path=None):
    """Lightweight stand-in for ValidationResult — only the fields the
    method reads matter. dynamic_result is the field the method writes to."""
    vr = SimpleNamespace(
        system_entry_input=reproducer,
        caller_path=caller_path or ["entry_fn", "fn_under_test"],
        counterexample=SimpleNamespace(),  # only forwarded, never inspected
        dynamic_result=None,
    )
    return vr


def _make_func(name: str):
    return SimpleNamespace(name=name)


def _make_parsed_file(entry_func):
    """ParsedCFile stand-in: only get_function_info needed."""
    pf = SimpleNamespace()
    pf.get_function_info = MagicMock(return_value=entry_func)
    return pf


# ---------------------------------------------------------------------------
# Disabled / missing dynamic validator
# ---------------------------------------------------------------------------

def test_returns_silently_when_dynamic_validator_disabled():
    """No dynamic_validator (e.g. --no-dynamic-validation) → silent skip."""
    v = _make_validator(dynamic_validator=None)
    vr = _make_validation_result()
    v._try_dynamic_validation(vr, _make_func("fn"), {}, {}, _make_parsed_file(None))
    assert vr.dynamic_result is None


# ---------------------------------------------------------------------------
# Both errored + UNREPRODUCIBLE → skip (no oracle)
# ---------------------------------------------------------------------------

def test_skips_when_both_errored_and_reproducer_is_unreproducible_marker():
    """The whole point of the UNREPRODUCIBLE marker is to signal 'no
    real harness possible' — running it would be meaningless."""
    dyn = MagicMock()
    v = _make_validator(dynamic_validator=dyn)
    v._reach_errored = True
    v._feas_errored = True
    vr = _make_validation_result(
        reproducer="// UNREPRODUCIBLE: witness state unreachable via public API"
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {}, {}, _make_parsed_file(None))
    dyn.validate.assert_not_called()
    assert vr.dynamic_result is None


def test_skips_when_both_errored_and_reproducer_is_empty():
    """An empty reproducer string is treated as UNREPRODUCIBLE — no
    code to compile, no signal to gather."""
    dyn = MagicMock()
    v = _make_validator(dynamic_validator=dyn)
    v._reach_errored = True
    v._feas_errored = True
    vr = _make_validation_result(reproducer="")
    v._try_dynamic_validation(vr, _make_func("fn"), {}, {}, _make_parsed_file(None))
    dyn.validate.assert_not_called()


def test_skips_when_both_errored_and_reproducer_is_none():
    """A None reproducer is treated the same as empty."""
    dyn = MagicMock()
    v = _make_validator(dynamic_validator=dyn)
    v._reach_errored = True
    v._feas_errored = True
    vr = _make_validation_result(reproducer=None)
    v._try_dynamic_validation(vr, _make_func("fn"), {}, {}, _make_parsed_file(None))
    dyn.validate.assert_not_called()


def test_unreproducible_marker_is_recognised_after_leading_whitespace():
    """The check strips the reproducer before testing the marker prefix;
    leading whitespace shouldn't defeat the skip."""
    dyn = MagicMock()
    v = _make_validator(dynamic_validator=dyn)
    v._reach_errored = True
    v._feas_errored = True
    vr = _make_validation_result(reproducer="\n\n  // UNREPRODUCIBLE: bla")
    v._try_dynamic_validation(vr, _make_func("fn"), {}, {}, _make_parsed_file(None))
    dyn.validate.assert_not_called()


# ---------------------------------------------------------------------------
# Both errored + REAL reproducer → RUN (the policy flip)
# ---------------------------------------------------------------------------

def test_runs_dyn_val_when_both_errored_but_reproducer_is_real():
    """The 9e2bef3 flip: CBMC dead, but if the public-API-validated
    reproducer is present, we lean on it as the only oracle."""
    expected = MagicMock(name="dyn_result")
    dyn = MagicMock()
    dyn.validate.return_value = expected
    v = _make_validator(dynamic_validator=dyn)
    v._reach_errored = True
    v._feas_errored = True

    entry = _make_func("entry_fn")
    pf = _make_parsed_file(entry)
    vr = _make_validation_result(
        reproducer="#include <archive.h>\nint main(){ return 0; }",
        caller_path=["entry_fn", "fn"],
    )
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, pf)

    dyn.validate.assert_called_once()
    assert vr.dynamic_result is expected


# ---------------------------------------------------------------------------
# Only one (or neither) errored → RUN regardless of reproducer
# ---------------------------------------------------------------------------

def test_runs_dyn_val_when_only_reachability_errored():
    """Skip only triggers on BOTH errors — a single CBMC failure
    still leaves the other check as ground truth, so dyn-val runs."""
    dyn = MagicMock()
    v = _make_validator(dynamic_validator=dyn)
    v._reach_errored = True
    v._feas_errored = False
    entry = _make_func("entry_fn")
    vr = _make_validation_result(reproducer="// UNREPRODUCIBLE: bla")
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed_file(entry))
    dyn.validate.assert_called_once()


def test_runs_dyn_val_when_only_feasibility_errored():
    dyn = MagicMock()
    v = _make_validator(dynamic_validator=dyn)
    v._reach_errored = False
    v._feas_errored = True
    entry = _make_func("entry_fn")
    vr = _make_validation_result(reproducer="// UNREPRODUCIBLE: bla")
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed_file(entry))
    dyn.validate.assert_called_once()


def test_runs_dyn_val_when_neither_errored():
    dyn = MagicMock()
    v = _make_validator(dynamic_validator=dyn)
    # flags default False
    entry = _make_func("entry_fn")
    vr = _make_validation_result(reproducer="ANYTHING")
    v._try_dynamic_validation(vr, _make_func("fn"), {"entry_fn": entry}, {}, _make_parsed_file(entry))
    dyn.validate.assert_called_once()


# ---------------------------------------------------------------------------
# Defensive: entry function not found
# ---------------------------------------------------------------------------

def test_returns_when_entry_func_not_found_in_either_source():
    """If neither all_funcs nor parsed_file.get_function_info knows the
    entry, the method returns without calling dyn-val (warning logged)."""
    dyn = MagicMock()
    v = _make_validator(dynamic_validator=dyn)
    v._reach_errored = True
    v._feas_errored = True
    pf = SimpleNamespace(get_function_info=MagicMock(return_value=None))
    vr = _make_validation_result(reproducer="real reproducer")
    v._try_dynamic_validation(vr, _make_func("fn"), {}, {}, pf)
    dyn.validate.assert_not_called()


def test_target_backend_runs_when_entry_func_missing():
    """For full-system target/QEMU validation, a system entry may live outside
    the current ParsedCFile. Missing entry metadata must not skip the target
    replay hook."""
    expected = MagicMock(name="dyn_result")
    dyn = MagicMock()
    dyn.config = SimpleNamespace(dynamic_validation_backend="both")
    dyn.validate.return_value = expected
    v = _make_validator(dynamic_validator=dyn)
    pf = SimpleNamespace(get_function_info=MagicMock(return_value=None))
    func = _make_func("console_clear")
    vr = _make_validation_result(
        reproducer="#include <stdint.h>\nint main(void){ return 0; }",
        caller_path=["kernel_main", "console_clear"],
    )

    v._try_dynamic_validation(vr, func, {}, {}, pf)

    dyn.validate.assert_called_once()
    kwargs = dyn.validate.call_args.kwargs
    assert kwargs["entry_func"] is func
    assert kwargs["caller_path"] == ["kernel_main", "console_clear"]
    assert kwargs["system_entry_reproducer"] == vr.system_entry_input
    assert vr.dynamic_result is expected
