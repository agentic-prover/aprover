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
    assert a3.enable_agentic_harness_repair is False  # the --enable arg flag itself
    assert a3.no_agentic_harness_repair is False
    # Default ON now: the fail-safe build-error fallback runs unless disabled.
    assert Config().enable_agentic_harness_repair is True
    # --no-agentic-harness-repair is the off-switch.
    a4 = build_parser().parse_args(
        ["verify", "--source", "x.c", "--driver", "d", "--no-agentic-harness-repair"])
    assert a4.no_agentic_harness_repair is True


def test_repair_flag_env(monkeypatch):
    monkeypatch.setenv("BMC_AGENT_ENABLE_AGENTIC_HARNESS_REPAIR", "1")
    from bmc_agent.config import Config
    assert Config.from_env().enable_agentic_harness_repair is True


# --- claude-code harness path -----------------------------------------------

def test_extract_c_code():
    from bmc_agent.agentic_harness_gen import _extract_c_code
    assert _extract_c_code("x\n```c\nint main(void){return 0;}\n```\ny") == "int main(void){return 0;}"
    assert _extract_c_code("int main(void){return 1;}") == "int main(void){return 1;}"
    assert _extract_c_code("") == ""


def test_generate_via_claude_code_forces_provider_and_scopes_dirs(monkeypatch):
    from pathlib import Path
    import bmc_agent.agentic_harness_gen as m
    from bmc_agent.agentic_harness_gen import AgenticHarnessGen
    from bmc_agent.config import Config
    from bmc_agent.parser import FunctionInfo, FunctionSignature

    g = AgenticHarnessGen.__new__(AgenticHarnessGen)
    g.config = Config(); g.config.llm_provider = "openai"; g.config.claude_code_add_dirs = []
    g.corpus_root = Path("/tmp/some/src")

    seen = {}
    def fake_complete(self, sysp, prompt, **kw):
        seen["provider"] = self.config.llm_provider
        seen["agentic"] = self.config.claude_code_agentic
        seen["dirs"] = list(self.config.claude_code_add_dirs)
        return "```c\nint main(void){return 0;}\n```"
    monkeypatch.setattr(m.LLMClient, "complete", fake_complete)
    g._compile_check = lambda h: (True, "")

    sig = FunctionSignature(return_type="int", name="f", parameters=[])
    fi = FunctionInfo(name="f", signature=sig, body="int f(){return 0;}", callees=set(), source_file="x.c")
    res = g.generate_via_claude_code(fi, {}, include_dirs=["/tmp/inc"], defines=["NDEBUG"])

    assert "int main(void)" in res.harness
    assert seen["provider"] == "claude-code"   # forced for this call
    assert seen["agentic"] is True
    assert "/tmp/some/src" in seen["dirs"] and "/tmp/inc" in seen["dirs"]
