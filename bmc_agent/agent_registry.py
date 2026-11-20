"""Canonical agent-role registry — the single source of truth for the
LLM-routing roles used across the pipeline.

Historically three hand-synced places enumerated these roles and drifted:
``ALL_AGENT_ROLES`` (cli, the --agentic-claude-code routing set), the
env-routing tuple (config, which ``BMC_AGENT_LLM_<ROLE>_*`` vars are parsed),
and — informally — the AI-layers printout. That drift is exactly how
``AgenticHarnessGen`` ended up borrowing ``role="realism"`` instead of having
its own routed role. This module is now the ONE place that enumerates the
roles; ``config`` and ``cli`` derive their lists from ``AGENT_ROLES``.

Keep this a LEAF module (no other ``bmc_agent`` imports) so ``config.py`` can
import it at module load without a cycle.

Adding or retiring an agent role = one edit to ``REGISTRY`` here.

NOTE: ``role`` is the LLM-routing identifier (== ``BaseAgent.name`` for agents
that have one). Several agents share a role on purpose (e.g. ``SoundnessAgent``
routes on ``refinement``; ``RealismToolsAgent`` shares ``realism``), so this is
a registry of ROLES, not of agent classes. ``label`` is for display
(AI-layers / inventory); ``tools`` marks whether the role's primary in-pipeline
implementation drives a multi-turn tool loop (informational only — actual
enablement stays config-driven).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    role: str
    label: str
    tools: bool = False


#: Ordered canonical registry. The role set here MUST stay in sync with the
#: ``role=...`` strings threaded through LLMClient.complete()/complete_with_tools()
#: at the call sites — test_agent_registry.py pins it against the historical set.
REGISTRY: "tuple[AgentSpec, ...]" = (
    AgentSpec("spec_gen",              "spec-gen",             tools=True),
    AgentSpec("feedback_distill",      "feedback distill",     tools=False),
    AgentSpec("refinement",            "refinement",           tools=False),
    AgentSpec("realism",               "realism",              tools=False),
    AgentSpec("classifier",            "classifier",           tools=True),
    AgentSpec("disagreement_diagnose", "disagreement diagnose", tools=False),
    AgentSpec("triage",                "triage",               tools=True),
    AgentSpec("dynamic_repro",         "reproducer",           tools=True),
    AgentSpec("dynval_triage",         "dynval triage",        tools=False),
    AgentSpec("cbmc_driver",           "bmc-config",           tools=True),
    AgentSpec("harness_gen",           "harness-gen",          tools=True),
)

#: Canonical role tuple — consumed by config (env routing) and cli
#: (ALL_AGENT_ROLES). Order is not significant (both consumers treat it as a set
#: / iterate it), but is kept stable for readable diffs.
AGENT_ROLES: "tuple[str, ...]" = tuple(spec.role for spec in REGISTRY)


def label_for(role: str) -> str:
    """Display label for a role; falls back to the role string."""
    for spec in REGISTRY:
        if spec.role == role:
            return spec.label
    return role
