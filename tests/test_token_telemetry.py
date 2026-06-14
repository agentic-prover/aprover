"""Token usage plumbing: LLMClient accumulates per-completion token usage,
and the agent layer snapshots the per-invocation delta into agent_telemetry
(the previously-reserved ``tokens`` field). Deterministic, mock-driven."""

from unittest.mock import MagicMock

from bmc_agent import agent_telemetry
from bmc_agent.agents.base import BaseAgent, _usage_snapshot
from bmc_agent.config import Config
from bmc_agent.llm import LLMClient


def test_add_usage_accumulates():
    c = LLMClient(Config())
    c._add_usage(10, 5)
    c._add_usage(7, 3)
    assert c.usage_total_prompt_tokens == 17
    assert c.usage_total_completion_tokens == 8
    assert c.usage_total_tokens == 25


def test_add_usage_ignores_non_numeric():
    c = LLMClient(Config())
    c._add_usage(None, None)
    c._add_usage("oops", 4)   # prompt unparseable -> whole call skipped
    c._add_usage(2, None)
    assert c.usage_total_tokens == 2


def test_usage_snapshot_guards_against_mock():
    # A bare MagicMock would yield a Mock for the attribute; snapshot must
    # return a real 0 so the telemetry delta stays numeric.
    assert _usage_snapshot(MagicMock()) == 0
    assert _usage_snapshot(object()) == 0


class _FakeLLM:
    """Minimal LLMClient stand-in that bills a fixed token cost per call."""

    def __init__(self, cost):
        self.cost = cost
        self.usage_total_tokens = 0

    def complete(self, system, prompt, role=None, **kw):
        self.usage_total_tokens += self.cost
        return "OK"


class _Echo(BaseAgent):
    name = "spec_gen"
    system_prompt = "you are a test double"

    def build_prompt(self, **kwargs):
        return "go"

    def parse(self, response):
        return response.strip() or None


def test_agent_run_records_token_delta():
    agent_telemetry.reset()
    llm = _FakeLLM(cost=42)
    agent = _Echo(Config(), llm)
    result = agent.run()
    assert result.ok
    snap = agent_telemetry.snapshot()
    assert len(snap) == 1
    assert snap[0].role == "spec_gen"
    assert snap[0].tokens == 42


def test_agent_run_token_delta_is_isolated_per_invocation():
    # Two runs on the same client each attribute only their own delta, not
    # the cumulative client total.
    agent_telemetry.reset()
    llm = _FakeLLM(cost=10)
    agent = _Echo(Config(), llm)
    agent.run()
    agent.run()
    snap = agent_telemetry.snapshot()
    assert [s.tokens for s in snap] == [10, 10]
    assert llm.usage_total_tokens == 20
