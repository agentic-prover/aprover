"""Agent abstractions for bmc-agent's LLM-driven tasks.

Each per-task agent (realism, spec-gen, refinement, feedback-distill,
disagreement-diagnose, ...) encapsulates its system prompt, prompt
construction, response parsing, and (optionally) tool registry behind
a single ``BaseAgent`` interface. The pipeline orchestrates by calling
``agent.run(...)`` and consuming the structured result; the agent
internally talks to ``LLMClient`` using its declared ``name`` as the
LLM-routing role (so the OpenRouter per-role env-var routing landed in
f570111 still controls which backbone model handles which task).

This package is the C2 ("our own agent abstractions, modeled on the
Claude Agent SDK pattern, no Anthropic-specific lock-in") direction.
v1 covers just the prompt + parse + LLM-call cycle; future versions can
add tool registries, self-critique loops, and sub-agent composition
without changing the BaseAgent contract.
"""

from bmc_agent.agents.base import AgentResult, BaseAgent
from bmc_agent.agents.disagreement import DisagreementDiagnoseAgent

__all__ = ["AgentResult", "BaseAgent", "DisagreementDiagnoseAgent"]
