"""Agentic (in-process tool-using) variant of RefinementAgent.

Same routing role / build_prompt / parse as the flat RefinementAgent, but the
LLM may grep/read the real source (callers, structs, adjacent code) before
proposing a PRE clause — so the precondition is grounded in the actual code
rather than the fixed prompt. Gated by config.enable_refinement_tools.
"""

from __future__ import annotations

from bmc_agent.agents.code_investigation_tools import CodeToolsCallMixin
from bmc_agent.agents.refinement import RefinementAgent


class RefinementWithToolsAgent(CodeToolsCallMixin, RefinementAgent):
    """RefinementAgent + bounded code-investigation tool loop."""
