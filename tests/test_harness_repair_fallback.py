"""Tests for the agentic harness-repair fallback (build-error gating + wiring)."""
from bmc_agent.bmc_engine import _is_harness_build_error, _harness_entry_of


# --- build-error classification (the fallback trigger) ----------------------

def test_build_errors_detected():
    assert _is_harness_build_error("cbmc exited with code 6; CONVERSION ERROR")
    assert _is_harness_build_error("incomplete type not permitted here")
    assert _is_harness_build_error("redefinition of body of 'struct _IO_FILE'")
    assert _is_harness_build_error("conflicting types for 'foo'")
    assert _is_harness_build_error("syntax error before token")


def test_resource_and_property_failures_are_not_build_errors():
    # Resource limits and verification outcomes must NOT trigger the fallback.
    assert not _is_harness_build_error("unwind bound exhausted")
    assert not _is_harness_build_error("cbmc timed out after 120s")
    assert not _is_harness_build_error("VERIFICATION FAILED: pointer dereference")
    assert not _is_harness_build_error("")
    assert not _is_harness_build_error(None)


# --- harness-entry extraction ----------------------------------------------

def test_harness_entry_tag(tmp_path):
    h = tmp_path / "harness.c"
    h.write_text("/* Harness entry: __bmc_harness_foo */\n#include <stdint.h>\nint x;\n")
    assert _harness_entry_of(str(h)) == "__bmc_harness_foo"


def test_harness_entry_main_is_none(tmp_path):
    h = tmp_path / "h2.c"
    h.write_text("/* Harness entry: main */\nint main(void){return 0;}\n")
    assert _harness_entry_of(str(h)) is None
    h3 = tmp_path / "h3.c"
    h3.write_text("#include <stdio.h>\nint main(void){return 0;}\n")
    assert _harness_entry_of(str(h3)) is None


# --- flag / config wiring ---------------------------------------------------

def test_repair_flag_on_verify_and_verify_dir(monkeypatch):
    monkeypatch.delenv("BMC_AGENT_ENABLE_AGENTIC_HARNESS_REPAIR", raising=False)
    from bmc_agent.cli import build_parser
    from bmc_agent.config import Config
    a = build_parser().parse_args(
        ["verify", "--source", "x.c", "--driver", "d", "--enable-agentic-harness-repair"])
    assert a.enable_agentic_harness_repair is True
    a2 = build_parser().parse_args(
        ["verify-dir", "--source-dir", "x", "--driver", "d", "--enable-agentic-harness-repair"])
    assert a2.enable_agentic_harness_repair is True
    a3 = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d"])
    assert a3.enable_agentic_harness_repair is False
    assert Config().enable_agentic_harness_repair is False


def test_repair_flag_env(monkeypatch):
    monkeypatch.setenv("BMC_AGENT_ENABLE_AGENTIC_HARNESS_REPAIR", "1")
    from bmc_agent.config import Config
    assert Config.from_env().enable_agentic_harness_repair is True
