"""Agentic (in-process tool-using) variants of the normally-flat agents.

Pin that the variants (a) preserve the flat agent's routing role, (b) route
their _call_llm through complete_with_tools with code-investigation tools, and
(c) fall back to the flat call when routed to the claude-code backend."""

from unittest.mock import MagicMock

from bmc_agent.config import Config
from bmc_agent.llm import LLMClient, ToolUseResult
from bmc_agent.agents.refinement_tools import RefinementWithToolsAgent
from bmc_agent.agents.feedback_distill_tools import FeedbackDistillWithToolsAgent


def _cfg():
    c = Config()
    c.include_dirs = ["examples/vibeos/repo/kernel"]
    return c


def test_variants_preserve_routing_role():
    c = _cfg(); llm = LLMClient(c)
    assert RefinementWithToolsAgent(config=c, llm=llm).name == "refinement"
    assert FeedbackDistillWithToolsAgent(config=c, llm=llm).name == "feedback_distill"


def test_call_llm_routes_through_complete_with_tools():
    c = _cfg(); llm = LLMClient(c)
    agent = RefinementWithToolsAgent(config=c, llm=llm)
    agent.llm = MagicMock()
    agent.llm.complete_with_tools.return_value = ToolUseResult(
        text="PROPOSAL", iterations=2, tool_calls_made=1, messages=[])
    raw, err = agent._call_llm("prompt")
    assert err is None and raw == "PROPOSAL"
    # built code tools and passed them
    _, kwargs = agent.llm.complete_with_tools.call_args
    names = {t.name for t in kwargs["tools"]}
    assert {"grep_code", "read_lines", "read_function"} <= names
    assert kwargs["role"] == "refinement"


def test_claude_code_backend_falls_back_to_flat(monkeypatch):
    c = _cfg(); llm = LLMClient(c)
    agent = FeedbackDistillWithToolsAgent(config=c, llm=llm)
    monkeypatch.setattr(agent, "_agent_runs_on_claude_code", lambda: True)
    # flat path calls self.llm.complete (not complete_with_tools)
    agent.llm = MagicMock()
    agent.llm.complete.return_value = "FLAT"
    raw, err = agent._call_llm("prompt")
    assert raw == "FLAT" and err is None
    agent.llm.complete_with_tools.assert_not_called()


def test_classifier_adjudicator_parse_and_verdict():
    from bmc_agent.agents.classifier_tools import ClassifierAdjudicatorAgent
    from unittest.mock import MagicMock
    c = _cfg(); a = ClassifierAdjudicatorAgent(c, LLMClient(c))
    assert a.name == "classifier"
    assert a.parse('{"verdict":"real","reasoning":"caller X reaches it"}')["verdict"] == "real"
    assert a.parse('noise {"verdict":"artifact","reasoning":"unreachable"} tail')["verdict"] == "artifact"
    assert a.parse('{"verdict":"maybe"}') is None
    assert a.parse("not json") is None
    # keeps_real_bug True only when verdict == real
    a.llm = MagicMock()
    from bmc_agent.llm import ToolUseResult
    a.llm.complete_with_tools.return_value = ToolUseResult(
        text='{"verdict":"real","reasoning":"r"}', iterations=1, tool_calls_made=0, messages=[])
    assert a.keeps_real_bug(fn="f", prop="p", reasoning="r") is True
