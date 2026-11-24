"""Tests for ``bmc_agent.agents.reproducer_tools.ReproducerAgent``.

The reproducer agent turns a CBMC counterexample into a COMPILABLE,
RUNNABLE C reproducer by iterating compile -> run -> read-error -> fix
through a bounded tool loop. These tests mock the LLM and the compile/run
helper so NO real gcc / network runs in CI.

Covered:
  * parse() extracts a fenced ```c block.
  * the public-API guard rejects an internal-helper-only reproducer
    (becomes UNREPRODUCIBLE) and accepts a public-API one.
  * the compile_and_run tool handler returns the error string on a
    (mocked) compile failure and 'reproduced' on a (mocked) success.
  * the system_prompt mentions the public-API constraint and the
    iterate-compile-run loop.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _fake_tool_use_result(text, *, iterations=2, tool_calls=1, messages=None, error=""):
    return SimpleNamespace(
        text=text,
        iterations=iterations,
        tool_calls_made=tool_calls,
        messages=messages or [],
        error=error,
    )


def _make_agent(llm=None, *, include_dirs=None):
    from bmc_agent.agents.reproducer_tools import ReproducerAgent
    from bmc_agent.config import Config

    parsed = SimpleNamespace(
        functions={}, call_graph={}, function_bodies={},
        function_definitions={}, struct_definitions={},
        path="", primary_source="", preprocessed_source="",
    )
    cfg = Config(llm_api_key="t")
    if include_dirs is not None:
        cfg.include_dirs = list(include_dirs)
    return ReproducerAgent(
        config=cfg, llm=llm or MagicMock(),
        parsed_file=parsed, corpus_paths=[], all_specs={},
    )


# ---------------------------------------------------------------------------
# Identity / routing
# ---------------------------------------------------------------------------

def test_agent_name_is_dynamic_repro():
    """Reuse dynamic_repro role so BMC_AGENT_LLM_DYNAMIC_REPRO_* routing and
    the caller's UNREPRODUCIBLE marker handling keep working."""
    from bmc_agent.agents.reproducer_tools import ReproducerAgent
    assert ReproducerAgent.name == "dynamic_repro"


def test_budget_class_attrs():
    from bmc_agent.agents.reproducer_tools import ReproducerAgent
    assert ReproducerAgent.max_iterations_param == 12
    assert ReproducerAgent.max_tool_calls_param == 12
    assert ReproducerAgent.max_tokens_per_turn_param == 4096


# ---------------------------------------------------------------------------
# system_prompt content
# ---------------------------------------------------------------------------

def test_system_prompt_mentions_public_api_and_compile_run_loop():
    from bmc_agent.agents.reproducer_tools import ReproducerAgent
    sp = ReproducerAgent.system_prompt.lower()
    assert "public api" in sp
    # the iterate compile -> run -> fix loop
    assert "compile_and_run_reproducer" in ReproducerAgent.system_prompt
    assert "iterate" in sp
    assert "fix" in sp


# ---------------------------------------------------------------------------
# parse() extracts a fenced ```c block
# ---------------------------------------------------------------------------

def test_parse_extracts_fenced_c_block():
    agent = _make_agent()
    source = '#include <archive.h>\nint main(void){ return 0; }'
    response = f"Here is the reproducer:\n```c\n{source}\n```\nDone."
    out = agent.parse(response)
    assert out is not None
    assert out.startswith("#include <archive.h>")
    assert "int main(void)" in out


def test_parse_empty_returns_none():
    """Only an empty response yields None (BaseAgent reports an error then)."""
    assert _make_agent().parse("") is None


def test_parse_honours_unreproducible_marker():
    agent = _make_agent()
    out = agent.parse("// UNREPRODUCIBLE: needs internal state\nblah")
    assert out is not None
    assert out.startswith("// UNREPRODUCIBLE")


def test_parse_no_source_becomes_unreproducible():
    from bmc_agent.agents.reproducer_tools import UNREPRODUCIBLE_SENTINEL
    agent = _make_agent()
    out = agent.parse("I could not build a reproducer, sorry.")
    assert out == UNREPRODUCIBLE_SENTINEL


# ---------------------------------------------------------------------------
# Public-API guard
# ---------------------------------------------------------------------------

def test_public_api_guard_rejects_internal_helper_only():
    """A reproducer that does NOT include a real project public header
    (only calls / re-implements internal helpers) is rejected and reported
    as UNREPRODUCIBLE - a crash there proves nothing about the library."""
    from bmc_agent.agents.reproducer_tools import UNREPRODUCIBLE_SENTINEL
    agent = _make_agent()
    internal = (
        '#include <stdlib.h>\n'
        'static int internal_helper(int x){ int *p = 0; return *p; }\n'
        'int main(void){ return internal_helper(7); }'
    )
    out = agent.parse(f"```c\n{internal}\n```")
    assert out == UNREPRODUCIBLE_SENTINEL


def test_public_api_guard_accepts_public_api_reproducer():
    """A reproducer that includes a real project public header passes the
    guard and is returned verbatim."""
    agent = _make_agent()
    public = (
        '#include <archive.h>\n'
        'int main(void){ struct archive *a = archive_read_new(); '
        'archive_read_free(a); return 0; }'
    )
    out = agent.parse(f"```c\n{public}\n```")
    assert out is not None
    assert "#include <archive.h>" in out
    assert "archive_read_new" in out


# ---------------------------------------------------------------------------
# compile_and_run_reproducer tool handler
# ---------------------------------------------------------------------------

def test_compile_and_run_tool_returns_error_on_compile_failure(monkeypatch):
    agent = _make_agent()
    monkeypatch.setattr(
        agent, "_compile_and_run_source",
        lambda src: "COMPILE ERROR:\nfoo.c:1: error: unknown type 'struct foo'",
    )
    _tool, handler = agent._make_compile_and_run_tool()
    out = handler({"source": "#include <archive.h>\nint main(){}"})
    assert "COMPILE ERROR" in out["result"]
    assert "unknown type" in out["result"]


def test_compile_and_run_tool_returns_reproduced_on_success(monkeypatch):
    agent = _make_agent()
    monkeypatch.setattr(
        agent, "_compile_and_run_source",
        lambda src: "OK: reproduced SIGSEGV\nAddressSanitizer: SEGV on unknown address",
    )
    _tool, handler = agent._make_compile_and_run_tool()
    out = handler({"source": "#include <archive.h>\nint main(){}"})
    assert out["result"].startswith("OK: reproduced")
    assert "SIGSEGV" in out["result"]


def test_compile_and_run_tool_missing_source_arg():
    agent = _make_agent()
    _tool, handler = agent._make_compile_and_run_tool()
    out = handler({})
    assert "error" in out


def test_compile_and_run_tool_handler_never_raises(monkeypatch):
    """Even if the underlying compile/run raises, the handler returns an
    error dict (fail-safe), never propagating an exception into the loop."""
    agent = _make_agent()

    def _boom(src):
        raise RuntimeError("disk full")

    monkeypatch.setattr(agent, "_compile_and_run_source", _boom)
    _tool, handler = agent._make_compile_and_run_tool()
    out = handler({"source": "int main(){}"})
    assert "error" in out


# ---------------------------------------------------------------------------
# _call_llm wires in the compile_and_run tool + dispatches to complete_with_tools
# ---------------------------------------------------------------------------

def test_call_llm_registers_compile_and_run_tool(monkeypatch):
    llm = MagicMock()
    public = '#include <archive.h>\nint main(void){ return 0; }'
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        f"```c\n{public}\n```"
    )
    agent = _make_agent(llm)
    agent.run(
        function_name="fn", cbmc_property="pointer OOB",
        counterexample="x=5", call_chain=["pub", "fn"],
        function_source="void fn(){}",
    )
    llm.complete_with_tools.assert_called_once()
    kwargs = llm.complete_with_tools.call_args.kwargs
    tool_names = {t.name for t in kwargs["tools"]}
    assert "compile_and_run_reproducer" in tool_names
    assert "compile_and_run_reproducer" in kwargs["tool_handlers"]
    # spec-gen tools are also present
    assert "lookup_function" in tool_names
    assert kwargs["role"] == "dynamic_repro"


def test_call_llm_passes_budget_kwargs(monkeypatch):
    llm = MagicMock()
    public = '#include <archive.h>\nint main(void){ return 0; }'
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        f"```c\n{public}\n```"
    )
    agent = _make_agent(llm)
    agent.run(
        function_name="fn", cbmc_property="p", counterexample="c",
        call_chain="pub -> fn", function_source="",
    )
    kwargs = llm.complete_with_tools.call_args.kwargs
    assert kwargs["max_iterations"] == 12
    assert kwargs["max_tool_calls"] == 12
    assert kwargs["max_tokens_per_turn"] == 4096


def test_call_llm_capped_loop_still_parses_final_text():
    """A budget/cap termination is not fatal: if the final text carries a
    public-API reproducer it is still returned."""
    llm = MagicMock()
    public = '#include <archive.h>\nint main(void){ return 0; }'
    llm.complete_with_tools.return_value = _fake_tool_use_result(
        f"```c\n{public}\n```", error="max_tool_calls reached",
    )
    agent = _make_agent(llm)
    result = agent.run(
        function_name="fn", cbmc_property="p", counterexample="c",
        call_chain=None, function_source="",
    )
    assert result.ok is True
    assert "#include <archive.h>" in result.output


def test_call_llm_llm_error_surfaces():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete_with_tools.side_effect = LLMError("network down")
    agent = _make_agent(llm)
    result = agent.run(
        function_name="fn", cbmc_property="p", counterexample="c",
        call_chain=None, function_source="",
    )
    assert result.ok is False
    assert "LLMError" in result.error


# ---------------------------------------------------------------------------
# build_prompt renders the key inputs
# ---------------------------------------------------------------------------

def test_build_prompt_contains_property_and_counterexample():
    agent = _make_agent()
    prompt = agent.build_prompt(
        function_name="parse_header",
        cbmc_property="dereference failure: pointer NULL",
        counterexample="len=4294967295  buf=0x0",
        call_chain=["archive_read_data", "parse_header"],
        function_source="void parse_header(){}",
    )
    assert "parse_header" in prompt
    assert "dereference failure" in prompt
    assert "len=4294967295" in prompt
    assert "archive_read_data" in prompt


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
