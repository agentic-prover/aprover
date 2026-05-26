"""
Tests for ``bmc_agent.agents.base`` — the BaseAgent abstraction
that all per-task agents subclass.

Covers:
  * BaseAgent contract enforcement (must declare name + system_prompt)
  * run() success / LLM-error / parse-error / parse-returned-None paths
  * AgentResult.ok semantics
  * LLM routing via the agent's ``name`` (role kwarg)
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# A minimal concrete BaseAgent for testing
# ---------------------------------------------------------------------------

def _make_test_agent_class(*, system_prompt="test agent", name="test_role"):
    from bmc_agent.agents.base import BaseAgent
    cls_name = name
    cls_prompt = system_prompt

    class _TestAgent(BaseAgent[str]):
        name = cls_name
        system_prompt = cls_prompt

        def build_prompt(self, *, text: str = "hi"):
            return f"echo: {text}"

        def parse(self, response: str):
            # Treat empty string as None (parse failure); otherwise pass through
            return response.strip() or None

    return _TestAgent


def _make_agent(llm, **agent_kwargs):
    """Build a TestAgent + minimal Config."""
    from bmc_agent.config import Config
    cfg = Config(llm_api_key="t")
    Agent = _make_test_agent_class(**agent_kwargs)
    return Agent(config=cfg, llm=llm)


# ---------------------------------------------------------------------------
# Contract enforcement
# ---------------------------------------------------------------------------

def test_baseagent_rejects_subclass_without_name():
    """A subclass that forgets to set ``name`` must fail at construction
    time, not silently route to the wrong LLM (or no role at all)."""
    from bmc_agent.agents.base import BaseAgent
    from bmc_agent.config import Config

    class _NoName(BaseAgent[str]):
        system_prompt = "sp"

        def build_prompt(self): return ""
        def parse(self, response): return None

    with pytest.raises(ValueError, match="name"):
        _NoName(config=Config(llm_api_key="t"), llm=MagicMock())


def test_baseagent_rejects_subclass_without_system_prompt():
    from bmc_agent.agents.base import BaseAgent
    from bmc_agent.config import Config

    class _NoPrompt(BaseAgent[str]):
        name = "x"

        def build_prompt(self): return ""
        def parse(self, response): return None

    with pytest.raises(ValueError, match="system_prompt"):
        _NoPrompt(config=Config(llm_api_key="t"), llm=MagicMock())


def test_baseagent_rejects_subclass_missing_build_prompt():
    """Abstract method enforcement: a subclass that omits ``build_prompt``
    can't be instantiated."""
    from bmc_agent.agents.base import BaseAgent
    from bmc_agent.config import Config

    class _NoBuild(BaseAgent[str]):
        name = "x"
        system_prompt = "sp"
        def parse(self, response): return None

    with pytest.raises(TypeError):
        _NoBuild(config=Config(llm_api_key="t"), llm=MagicMock())


def test_baseagent_rejects_subclass_missing_parse():
    from bmc_agent.agents.base import BaseAgent
    from bmc_agent.config import Config

    class _NoParse(BaseAgent[str]):
        name = "x"
        system_prompt = "sp"
        def build_prompt(self): return ""

    with pytest.raises(TypeError):
        _NoParse(config=Config(llm_api_key="t"), llm=MagicMock())


# ---------------------------------------------------------------------------
# run() — happy path
# ---------------------------------------------------------------------------

def test_run_returns_parsed_output_on_success():
    llm = MagicMock()
    llm.complete.return_value = "  the answer  "
    agent = _make_agent(llm)
    result = agent.run(text="hello")
    assert result.ok is True
    assert result.output == "the answer"
    assert result.error is None
    assert result.raw_response == "  the answer  "


def test_run_passes_role_kwarg_to_llm_complete():
    """The agent's ``name`` must be passed as the ``role`` kwarg so per-
    role env-var routing kicks in. Without this, every agent routes
    through the global default."""
    llm = MagicMock()
    llm.complete.return_value = "x"
    agent = _make_agent(llm, name="my_special_role")
    agent.run(text="t")
    call = llm.complete.call_args
    assert call.kwargs.get("role") == "my_special_role"


def test_run_passes_system_prompt_to_llm_complete():
    llm = MagicMock()
    llm.complete.return_value = "x"
    agent = _make_agent(llm, system_prompt="custom sp")
    agent.run(text="t")
    # complete(system, user, role=...) — first positional arg is system
    args = llm.complete.call_args.args
    assert args[0] == "custom sp"


def test_run_passes_build_prompt_output_as_user_message():
    llm = MagicMock()
    llm.complete.return_value = "x"
    agent = _make_agent(llm)
    agent.run(text="my-input")
    args = llm.complete.call_args.args
    assert args[1] == "echo: my-input"


# ---------------------------------------------------------------------------
# run() — error paths
# ---------------------------------------------------------------------------

def test_run_handles_llm_error_cleanly():
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("network down")
    agent = _make_agent(llm)
    result = agent.run(text="t")
    assert result.ok is False
    assert result.output is None
    assert "LLMError" in result.error
    assert "network down" in result.error


def test_run_handles_unexpected_exception_in_llm():
    """Non-LLMError exceptions in the LLM call must not propagate —
    they get captured into AgentResult.error so the pipeline never
    crashes from one stray agent call."""
    llm = MagicMock()
    llm.complete.side_effect = RuntimeError("kaboom")
    agent = _make_agent(llm)
    result = agent.run(text="t")
    assert result.ok is False
    assert "unexpected" in result.error
    assert "kaboom" in result.error


def test_run_handles_build_prompt_failure():
    """If build_prompt raises (e.g. missing required kwarg), the error
    is reported via AgentResult instead of propagating."""
    from bmc_agent.agents.base import BaseAgent
    from bmc_agent.config import Config

    class _BuildBoom(BaseAgent[str]):
        name = "x"
        system_prompt = "sp"
        def build_prompt(self, **kwargs):
            raise KeyError("missing 'foo'")
        def parse(self, response): return response

    llm = MagicMock()
    result = _BuildBoom(config=Config(llm_api_key="t"), llm=llm).run()
    assert result.ok is False
    assert "build_prompt" in result.error
    # LLM was never called (build failed)
    llm.complete.assert_not_called()


def test_run_handles_parse_returning_none():
    """parse() returning None indicates "couldn't parse this response" —
    becomes a clean error, not a crash."""
    llm = MagicMock()
    llm.complete.return_value = "  "   # parses to None
    agent = _make_agent(llm)
    result = agent.run(text="t")
    assert result.ok is False
    assert result.output is None
    assert "returned None" in result.error
    # Raw response preserved for debugging
    assert result.raw_response == "  "


def test_run_handles_parse_raising_exception():
    """A parse() that raises is captured as an error, not propagated."""
    from bmc_agent.agents.base import BaseAgent
    from bmc_agent.config import Config

    class _ParseBoom(BaseAgent[str]):
        name = "x"
        system_prompt = "sp"
        def build_prompt(self): return "u"
        def parse(self, response):
            raise ValueError(f"bad shape: {response[:10]}")

    llm = MagicMock()
    llm.complete.return_value = "ignored"
    result = _ParseBoom(config=Config(llm_api_key="t"), llm=llm).run()
    assert result.ok is False
    assert "parse" in result.error
    assert "bad shape" in result.error
    assert result.raw_response == "ignored"


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------

def test_agent_result_ok_property():
    from bmc_agent.agents.base import AgentResult
    assert AgentResult(output="x").ok is True
    assert AgentResult(output=None).ok is False
    assert AgentResult(output="x", error="oops").ok is False
    assert AgentResult(output=None, error="oops").ok is False


# ---------------------------------------------------------------------------
# Retry semantics
# ---------------------------------------------------------------------------

def test_run_no_retry_by_default():
    """max_retries=0 (the default) means one attempt total. An LLM error
    on attempt 1 yields a failure result; the LLM is called once."""
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("boom")
    agent = _make_agent(llm)
    result = agent.run(text="t")
    assert result.ok is False
    assert llm.complete.call_count == 1


def test_run_retries_on_parse_returned_none():
    """When parse() returns None on the first response but valid on the
    second, run() succeeds on the second attempt."""
    llm = MagicMock()
    llm.complete.side_effect = ["", "  the answer  "]   # 1st parses to None, 2nd OK
    Agent = _make_test_agent_class()
    Agent.max_retries = 2
    from bmc_agent.config import Config
    agent = Agent(config=Config(llm_api_key="t"), llm=llm)
    result = agent.run(text="t")
    assert result.ok is True
    assert result.output == "the answer"
    assert llm.complete.call_count == 2


def test_run_retries_on_llm_error():
    """LLMError on attempt 1 + success on attempt 2 → run() succeeds."""
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = [LLMError("transient"), "second try"]
    Agent = _make_test_agent_class()
    Agent.max_retries = 1
    from bmc_agent.config import Config
    agent = Agent(config=Config(llm_api_key="t"), llm=llm)
    result = agent.run(text="t")
    assert result.ok is True
    assert result.output == "second try"
    assert llm.complete.call_count == 2


def test_run_exhausts_retries_then_reports_last_error():
    """If every attempt fails, run() returns the LAST error."""
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = [
        LLMError("first failure"),
        LLMError("second failure"),
        LLMError("third failure"),
    ]
    Agent = _make_test_agent_class()
    Agent.max_retries = 2
    from bmc_agent.config import Config
    agent = Agent(config=Config(llm_api_key="t"), llm=llm)
    result = agent.run(text="t")
    assert result.ok is False
    assert "third failure" in result.error
    assert llm.complete.call_count == 3


def test_run_rebuilds_prompt_only_once_across_retries():
    """The prompt is fixed at the top — retries don't re-call
    build_prompt. Saves work + ensures consistent inputs across
    attempts (otherwise the comparison "did retries help?" is muddled)."""
    from bmc_agent.llm import LLMError
    from bmc_agent.agents.base import BaseAgent
    from bmc_agent.config import Config
    call_count = {"build": 0}

    class _Counter(BaseAgent[str]):
        name = "x"
        system_prompt = "sp"
        max_retries = 3
        def build_prompt(self, **kw):
            call_count["build"] += 1
            return "p"
        def parse(self, response): return response.strip() or None

    llm = MagicMock()
    llm.complete.side_effect = [LLMError("e"), LLMError("e"), "ok"]
    _Counter(config=Config(llm_api_key="t"), llm=llm).run()
    assert call_count["build"] == 1
