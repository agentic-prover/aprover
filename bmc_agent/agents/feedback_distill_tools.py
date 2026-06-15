"""Agentic (in-process tool-using) variant of FeedbackDistillAgent.

Same routing role / build_prompt / parse as the flat agent, but the LLM may
grep/read the real source while distilling a realism rejection into a
remediation. Gated by config.enable_feedback_distill_tools.
"""

from __future__ import annotations

from bmc_agent.agents.code_investigation_tools import CodeToolsCallMixin
from bmc_agent.agents.feedback_distill import FeedbackDistillAgent


class FeedbackDistillWithToolsAgent(CodeToolsCallMixin, FeedbackDistillAgent):
    """FeedbackDistillAgent + bounded code-investigation tool loop."""
