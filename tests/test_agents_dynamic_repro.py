"""Tests for ``bmc_agent.agents.dynamic_repro.DynamicReproAgent``.

Single-shot agent: takes the previous reproducer + GCC error + func
name, returns the corrected C source string (or the UNREPRODUCIBLE
marker verbatim) on success, or None from parse on unparseable input.
Mirrors the pre-migration ``DynamicValidator._regenerate_reproducer_with_error``
contract byte-for-byte so the call-site flip is a no-op behavior change.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _make_agent(llm):
    from bmc_agent.agents.dynamic_repro import DynamicReproAgent
    from bmc_agent.config import Config
    return DynamicReproAgent(config=Config(llm_api_key="t"), llm=llm)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_agent_routes_via_dynamic_repro_role():
    """Has its own role so BMC_AGENT_LLM_DYNAMIC_REPRO_* env vars
    control routing — previously this LLM call piggybacked on the
    realism role, which constrained model selection."""
    from bmc_agent.agents.dynamic_repro import DynamicReproAgent
    assert DynamicReproAgent.name == "dynamic_repro"


def test_system_prompt_is_set():
    """BaseAgent requires a non-empty system_prompt at instantiation;
    this is the safeguard that subclass forgot to set one."""
    from bmc_agent.agents.dynamic_repro import DynamicReproAgent
    assert DynamicReproAgent.system_prompt
    assert "verification" in DynamicReproAgent.system_prompt.lower()


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_embeds_func_name_and_truncates_error():
    """The 1500-char error truncation matches the pre-migration call
    site exactly — if this drifts, the agent and the legacy code diverge
    on identical inputs."""
    agent = _make_agent(MagicMock())
    long_err = "X" * 5000
    prompt = agent.build_prompt(
        previous_reproducer="int main(){}",
        compile_error=long_err,
        func_name="next_field",
    )
    assert "`next_field`" in prompt
    assert "int main(){}" in prompt
    # Error is truncated to first 1500 chars
    assert prompt.count("X") == 1500


def test_build_prompt_truncates_reproducer_at_4000():
    """4000-char source cap matches the pre-migration prompt budget."""
    agent = _make_agent(MagicMock())
    # Use a character that doesn't appear in the prompt template
    # itself ("Y" appears in "Your"); "Θ" (Greek capital theta)
    # is safe and counts cleanly.
    long_src = "Θ" * 8000
    prompt = agent.build_prompt(
        previous_reproducer=long_src,
        compile_error="some error",
        func_name="fn",
    )
    assert prompt.count("Θ") == 4000


def test_build_prompt_handles_empty_inputs():
    """Empty error / source should not crash; the prompt still renders."""
    agent = _make_agent(MagicMock())
    prompt = agent.build_prompt(
        previous_reproducer="",
        compile_error="",
        func_name="fn",
    )
    assert "`fn`" in prompt
    assert "HARD RULES" in prompt


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def test_parse_returns_none_on_empty():
    assert _make_agent(MagicMock()).parse("") is None


def test_parse_returns_none_on_no_json():
    assert _make_agent(MagicMock()).parse("just prose, no JSON anywhere") is None


def test_parse_returns_code_on_plain_json():
    agent = _make_agent(MagicMock())
    response = json.dumps({"reproducer_code": "#include <archive.h>\nint main(){}"})
    out = agent.parse(response)
    assert out == "#include <archive.h>\nint main(){}"


def test_parse_returns_code_from_fenced_markdown():
    """The legacy parser stripped any ``` opening as a fence, even
    without a language tag. Mirror that."""
    agent = _make_agent(MagicMock())
    response = (
        "```json\n"
        + json.dumps({"reproducer_code": "int main(void){return 0;}"})
        + "\n```"
    )
    out = agent.parse(response)
    assert out == "int main(void){return 0;}"


def test_parse_handles_fence_with_no_language_tag():
    """The fence-stripper used `startswith('```')` — must also work for
    bare opening backticks."""
    agent = _make_agent(MagicMock())
    response = (
        "```\n"
        + json.dumps({"reproducer_code": "ok"})
        + "\n```"
    )
    assert agent.parse(response) == "ok"


def test_parse_extracts_embedded_json_from_prose():
    """The LLM occasionally wraps the JSON in chatter; the parser does
    a `re.search(r'\\{.*\\}')` fallback. Mirror that."""
    agent = _make_agent(MagicMock())
    response = (
        "Sure, here is the fix:\n\n"
        + json.dumps({"reproducer_code": "int main(){return 0;}"})
        + "\n\nLet me know if you have questions."
    )
    assert agent.parse(response) == "int main(){return 0;}"


def test_parse_returns_none_on_empty_reproducer_field():
    """Empty ``reproducer_code`` is treated as a non-answer so the outer
    loop falls back to the prior source instead of replacing it with
    nothing."""
    agent = _make_agent(MagicMock())
    response = json.dumps({"reproducer_code": ""})
    assert agent.parse(response) is None


def test_parse_returns_none_on_missing_reproducer_field():
    agent = _make_agent(MagicMock())
    response = json.dumps({"explanation": "I can't fix this."})
    assert agent.parse(response) is None


def test_parse_passes_through_unreproducible_marker_verbatim():
    """The UNREPRODUCIBLE marker is an acceptable honest answer per the
    prompt's HARD RULE 4. It must come back through ``parse`` unchanged
    so the outer loop can detect-and-exit."""
    agent = _make_agent(MagicMock())
    response = json.dumps({
        "reproducer_code": "// UNREPRODUCIBLE: cannot link without -larchive",
    })
    out = agent.parse(response)
    assert out is not None
    assert out.startswith("// UNREPRODUCIBLE")
    assert "-larchive" in out


def test_parse_strips_whitespace_from_reproducer_field():
    """Mirrors the pre-migration ``.strip()`` on the extracted code."""
    agent = _make_agent(MagicMock())
    response = json.dumps({"reproducer_code": "   int main(){}\n  "})
    assert agent.parse(response) == "int main(){}"


# ---------------------------------------------------------------------------
# run() — end-to-end
# ---------------------------------------------------------------------------

def test_run_happy_path_returns_corrected_source():
    llm = MagicMock()
    llm.complete.return_value = json.dumps({
        "reproducer_code": "#include <archive.h>\nint main(){return 0;}",
    })
    result = _make_agent(llm).run(
        previous_reproducer="bogus",
        compile_error="error: 'archive_new' undeclared",
        func_name="archive_new",
    )
    assert result.ok
    assert result.output.startswith("#include <archive.h>")
    # role routing — confirms env-var override works
    kwargs = llm.complete.call_args.kwargs
    assert kwargs.get("role") == "dynamic_repro"


def test_run_failure_on_llm_error_returns_error_not_raises():
    """LLMError must be caught and reported via AgentResult so the
    dyn-val outer loop can fall through to UNREPRODUCIBLE — NOT
    propagated as an exception that would crash the sweep."""
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("backend down")
    result = _make_agent(llm).run(
        previous_reproducer="x",
        compile_error="e",
        func_name="fn",
    )
    assert not result.ok
    assert "LLMError" in result.error


def test_run_failure_on_unparseable_response():
    llm = MagicMock()
    llm.complete.return_value = "this is not JSON at all"
    result = _make_agent(llm).run(
        previous_reproducer="x",
        compile_error="e",
        func_name="fn",
    )
    assert not result.ok
    assert result.output is None
