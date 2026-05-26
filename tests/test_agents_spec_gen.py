"""
Tests for ``bmc_agent.agents.spec_gen.SpecGenAgent``.

The agent owns the base LLM-call boundary of v2 spec-gen — given a
fully-rendered caller-grounded prompt, it returns a Spec or None.
The orchestrator (SpecGeneratorV2) handles all upstream machinery
(canonical short-circuit, boundary detection, magic-check inference,
evidence gathering, prompt rendering) and the fallback path.

The v2.2 tool-use branch is NOT covered here — it's a separate
agent slated for a follow-up commit.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _make_agent(llm, *, system_prompt="SP"):
    from bmc_agent.agents.spec_gen import SpecGenAgent
    from bmc_agent.config import Config
    return SpecGenAgent(
        config=Config(llm_api_key="t"), llm=llm,
        system_prompt=system_prompt,
    )


def _valid_spec_payload():
    """A v2 spec payload that passes _validate_and_extract — each
    clause section is a list of ``{clause, evidence}`` dicts."""
    return {
        "pre_validity": [
            {"clause": "p != NULL", "evidence": ["body:L3"]},
        ],
        "pre_protocol": [],
        "postcondition": [
            {"clause": "result >= 0", "evidence": ["body:L7"]},
        ],
        "spec_disagreement": False,
    }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_agent_name_is_spec_gen():
    """Routes via BMC_AGENT_LLM_SPEC_GEN_* env vars."""
    from bmc_agent.agents.spec_gen import SpecGenAgent
    assert SpecGenAgent.name == "spec_gen"


def test_agent_max_retries_matches_module_constant():
    """The agent's max_retries follows the module-level MAX_PARSE_RETRIES
    constant so the retry budget can be tuned in one place."""
    from bmc_agent.agents.spec_gen import SpecGenAgent
    from bmc_agent.spec_generator_v2 import MAX_PARSE_RETRIES
    assert SpecGenAgent.max_retries == MAX_PARSE_RETRIES


def test_agent_accepts_language_aware_system_prompt():
    """The constructor takes a per-instance ``system_prompt`` (computed
    per-language by the orchestrator). Different instances can carry
    different prompts without mutating class state."""
    from bmc_agent.agents.spec_gen import SpecGenAgent
    from bmc_agent.config import Config
    cfg = Config(llm_api_key="t")
    llm = MagicMock()
    a1 = SpecGenAgent(config=cfg, llm=llm, system_prompt="C-prompt")
    a2 = SpecGenAgent(config=cfg, llm=llm, system_prompt="Rust-prompt")
    assert a1.system_prompt == "C-prompt"
    assert a2.system_prompt == "Rust-prompt"
    # Mutating one shouldn't affect the other
    a1.system_prompt = "changed"
    assert a2.system_prompt == "Rust-prompt"


def test_agent_rejects_empty_system_prompt():
    """The system prompt is required — the orchestrator must compute it
    before constructing the agent."""
    from bmc_agent.agents.spec_gen import SpecGenAgent
    from bmc_agent.config import Config
    with pytest.raises(ValueError, match="system_prompt"):
        SpecGenAgent(
            config=Config(llm_api_key="t"), llm=MagicMock(),
            system_prompt="",
        )


# ---------------------------------------------------------------------------
# run() — happy path
# ---------------------------------------------------------------------------

def test_run_returns_spec_on_valid_response():
    from bmc_agent.spec import SpecStatus
    llm = MagicMock()
    llm.complete.return_value = json.dumps(_valid_spec_payload())
    result = _make_agent(llm).run(prompt="render-prompt", fn_name="fn")
    assert result.ok is True
    assert result.output.function_name == "fn"
    assert result.output.precondition == "p != NULL"
    assert result.output.status == SpecStatus.GENERATED


def test_run_passes_system_prompt_to_llm():
    llm = MagicMock()
    llm.complete.return_value = json.dumps(_valid_spec_payload())
    _make_agent(llm, system_prompt="MY_SP").run(prompt="P", fn_name="fn")
    args = llm.complete.call_args.args
    assert args[0] == "MY_SP"
    assert args[1] == "P"


def test_run_uses_spec_gen_role():
    llm = MagicMock()
    llm.complete.return_value = json.dumps(_valid_spec_payload())
    _make_agent(llm).run(prompt="P", fn_name="fn")
    assert llm.complete.call_args.kwargs.get("role") == "spec_gen"


# ---------------------------------------------------------------------------
# Retry behaviour (max_retries = MAX_PARSE_RETRIES = 1)
# ---------------------------------------------------------------------------

def test_run_retries_on_unparseable_first_response():
    """Validates that SpecGenAgent inherits BaseAgent's retry primitive."""
    llm = MagicMock()
    llm.complete.side_effect = [
        "not json at all",                 # attempt 1: parse fails
        json.dumps(_valid_spec_payload()),  # attempt 2: succeeds
    ]
    result = _make_agent(llm).run(prompt="P", fn_name="fn")
    assert result.ok is True
    assert result.output.precondition == "p != NULL"
    assert llm.complete.call_count == 2


def test_run_exhausts_retry_and_fails():
    """Both attempts return garbage → run() reports failure (caller
    falls back to seed-only spec)."""
    llm = MagicMock()
    llm.complete.return_value = "still not json"
    result = _make_agent(llm).run(prompt="P", fn_name="fn")
    assert result.ok is False
    # MAX_PARSE_RETRIES=1 → 2 total attempts
    from bmc_agent.spec_generator_v2 import MAX_PARSE_RETRIES
    assert llm.complete.call_count == MAX_PARSE_RETRIES + 1


# ---------------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------------

def test_parse_returns_none_on_empty():
    assert _make_agent(MagicMock()).parse("") is None


def test_parse_returns_none_on_no_json():
    """parse() returns None when the response has no JSON object —
    BaseAgent.run reports as "parse: returned None"."""
    assert _make_agent(MagicMock()).parse("just prose") is None


def test_parse_handles_fenced_markdown():
    agent = _make_agent(MagicMock())
    agent._fn_name = "fn"
    out = agent.parse(
        "```json\n" + json.dumps(_valid_spec_payload()) + "\n```"
    )
    assert out is not None
    assert out.precondition == "p != NULL"


def test_parse_carries_disagreement_flag():
    """When the LLM emits ``spec_disagreement: true``, the resulting
    Spec's ``spec_disagreement`` field is set so the orchestrator's
    tool-use trigger fires."""
    payload = _valid_spec_payload()
    payload["spec_disagreement"] = True
    payload["disagreement_notes"] = "body says A, caller says B"
    agent = _make_agent(MagicMock())
    agent._fn_name = "fn"
    out = agent.parse(json.dumps(payload))
    assert out is not None
    assert bool(out.spec_disagreement) is True


# ---------------------------------------------------------------------------
# build_prompt is a pass-through
# ---------------------------------------------------------------------------

def test_build_prompt_returns_input_unchanged():
    """The orchestrator pre-renders the prompt — the agent only carries
    it to the LLM. build_prompt is a transparent pass-through (kept
    for BaseAgent contract uniformity)."""
    agent = _make_agent(MagicMock())
    out = agent.build_prompt(prompt="my-rendered-prompt", fn_name="fn")
    assert out == "my-rendered-prompt"


def test_build_prompt_captures_fn_name_for_parse_logs():
    """fn_name is stored on the instance so parse() can include it in
    log lines / spec.function_name without an extra explicit
    argument."""
    agent = _make_agent(MagicMock())
    agent.build_prompt(prompt="P", fn_name="my_fn")
    assert agent._fn_name == "my_fn"
