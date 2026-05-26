"""``SpecGenAgent`` — base LLM-call boundary for v2 caller-grounded
spec generation.

Owns just the LLM call: prompt → response → validated → Spec. All the
upstream machinery (canonical short-circuit, boundary detection,
magic-check inference, evidence gathering, prompt rendering) stays in
``SpecGeneratorV2._generate_one``. All the downstream machinery
(callee-spec propagation, fallback to seed-only) also stays in the
orchestrator.

The v2.2 tool-use branch (``_generate_with_tools``) is a separate
LLM call shape (``complete_with_tools`` instead of ``complete``) and
is migrated in a follow-up commit — for now it stays in
``SpecGeneratorV2``.

This is C2 step 4. The base-call retry-on-parse-failure semantics are
implemented via ``BaseAgent.max_retries`` (the retry primitive added
alongside this agent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent
from bmc_agent.spec import Spec, SpecStatus
from bmc_agent.spec_generator_v2 import (
    MAX_PARSE_RETRIES,
    _build_spec_from_validated,
    _extract_json_object,
    _validate_and_extract,
)

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient


class SpecGenAgent(BaseAgent[Spec]):
    """Drafts a function-level spec from a fully-rendered caller-grounded
    prompt. Returns ``None`` on parse/validate failure (caller falls
    back to a seed-only spec).

    Routing: ``BMC_AGENT_LLM_SPEC_GEN_*`` env vars.

    The agent is constructed per-call with the language-aware system
    prompt (C vs Rust, strict_dsl, safety_only — all already computed
    in ``SpecGeneratorV2.generate_specs``).

    Inputs to ``run()``:
        * ``prompt`` (str) — fully rendered user prompt
                              (rendered by render_caller_grounded_spec_prompt)
        * ``fn_name`` (str) — for the disagreement-notes log line

    Output: ``Spec`` (status=SpecStatus.GENERATED) on success.
    """

    name = "spec_gen"
    max_retries = MAX_PARSE_RETRIES   # one retry on JSON parse failure

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        *,
        system_prompt: str,
    ) -> None:
        # Language-aware system prompt is passed in (computed by
        # SpecGeneratorV2 from config.strict_dsl / safety_only /
        # source language).
        self.system_prompt = system_prompt
        super().__init__(config, llm)
        # Stash fn_name so parse() can include it in disagreement logs;
        # the agent contract is "run(**kwargs) → AgentResult", so we
        # carry the per-call fn_name as an instance field set by
        # build_prompt before each call.
        self._fn_name = ""

    def build_prompt(
        self,
        *,
        prompt: str,
        fn_name: str = "",
        **_: Any,
    ) -> str:
        # The orchestrator already rendered the user prompt
        # (caller-grounded with evidence bundle). The agent's
        # build_prompt is a pass-through — present mainly so the
        # BaseAgent contract (build_prompt is the input transform) is
        # honoured uniformly across all agents.
        self._fn_name = fn_name
        return prompt

    def parse(self, response: str) -> Optional[Spec]:
        if not response:
            return None
        payload = _extract_json_object(response)
        if payload is None:
            return None
        validated = _validate_and_extract(payload, self._fn_name or "<via-agent>")
        if validated is None:
            return None
        pv, pp, post, loops, disagreement, notes = validated
        spec = _build_spec_from_validated(
            self._fn_name or "<via-agent>",
            pv, pp, post, loops, disagreement,
            status=SpecStatus.GENERATED,
        )
        return spec
