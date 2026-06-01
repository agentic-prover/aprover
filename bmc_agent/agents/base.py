"""``BaseAgent`` — foundation for bmc-agent's per-task agents.

Each agent encapsulates one LLM-driven task in the pipeline (realism
classification, spec drafting, disagreement diagnosis, etc.) behind a
single contract:

    agent.run(**inputs) -> AgentResult(output=<parsed-output>, ...)

The agent owns its system prompt, prompt construction logic, response
parsing, and optional retry / tool-use augmentation. Pipeline orchestrators
treat each agent as a black box: pass typed inputs, receive a typed result.

Why an explicit ``BaseAgent`` instead of just functions:

* **Encapsulation** — system prompt, prompt template, output schema, and
  parser are all bound to one class; no risk of mismatching them
  across the codebase.
* **Routing** — the agent's ``name`` matches the LLM-routing role
  (BMC_AGENT_LLM_<NAME>_* env vars), so per-task model selection
  follows the agent boundary automatically.
* **Testability** — each agent can be unit-tested in isolation by
  mocking just the ``LLMClient`` it depends on.
* **Composability (v2)** — future agents can call other agents
  (e.g., a ``RealismAgent`` could invoke a ``SpecRefinementAgent``
  inline when realism rejects a CEx and a tightening clause is
  obvious).

Modeled on the Claude Agent SDK pattern but intentionally minimal and
backend-agnostic — bmc-agent's existing OpenAI-compatible /
OpenRouter / Anthropic-native LLMClient dispatch is preserved so
per-role models keep working.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Optional, TypeVar

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient

T = TypeVar("T")  # the agent's parsed output type


@dataclass
class AgentResult(Generic[T]):
    """Wraps an agent's invocation result.

    Successful run:    ``output`` is the parsed domain object, ``error``
                        is None, ``raw_response`` is the raw LLM text.
    LLM call failure:   ``output`` is None, ``error`` is set, ``raw_response``
                        is empty.
    Parse failure:      ``output`` is None, ``error`` describes the parse
                        problem, ``raw_response`` is the offending text.

    Tool-use agents additionally populate ``tool_use_result`` with the
    full ``ToolUseResult`` (messages, iterations, tool_calls_made)
    so the orchestrator can run downstream audits (which tools were
    called, with what args). Non-tool agents leave this as None.
    """

    output: Optional[T] = None
    raw_response: str = ""
    error: Optional[str] = None
    tool_calls_made: int = 0
    #: Populated by tool-using agents only. Type is ``ToolUseResult``;
    #: typed as ``Any`` to avoid importing llm.py at module load.
    tool_use_result: Optional[Any] = None

    @property
    def ok(self) -> bool:
        return self.output is not None and self.error is None


class BaseAgent(abc.ABC, Generic[T]):
    """Abstract base for a single-task agent.

    Subclasses MUST set the two class attributes ``name`` and
    ``system_prompt``, and MUST implement ``build_prompt`` and ``parse``.

    Optional hooks (v2 extensions): subclasses can override ``_call_llm``
    to add tool-use augmentation, retry logic, self-critique passes, or
    sub-agent delegation. The default implementation is a single
    ``LLMClient.complete`` call routed by ``self.name``.
    """

    #: Role identifier — used as the ``role`` kwarg to LLMClient.complete,
    #: which routes to BMC_AGENT_LLM_<NAME>_* env vars when set.
    name: str = ""

    #: System prompt for this agent. Subclasses set as a class attribute.
    system_prompt: str = ""

    #: Max retries on LLM-error or parse-error inside ``run()``. Default 0
    #: (single attempt, fail-fast). Set higher in subclasses where the
    #: structured-output schema is occasionally violated by the LLM.
    max_retries: int = 0

    def __init__(self, config: "Config", llm: "LLMClient") -> None:
        if not self.name:
            raise ValueError(
                f"{type(self).__name__} must declare a class-level 'name' "
                "(LLM-routing role identifier)"
            )
        if not self.system_prompt:
            raise ValueError(
                f"{type(self).__name__} must declare a class-level "
                "'system_prompt'"
            )
        self.config = config
        self.llm = llm

    # ------------------------------------------------------------------
    # Contract — subclasses implement these
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def build_prompt(self, **kwargs: Any) -> str:
        """Construct the user prompt for one invocation. Receives the
        per-call inputs as keyword arguments — each agent declares its
        own input contract via the kwargs it accepts."""
        ...

    @abc.abstractmethod
    def parse(self, response: str) -> Optional[T]:
        """Parse the LLM response into the domain output type. Return
        None on unparseable input (the agent reports this as an error
        in AgentResult); raise on programming errors that should
        propagate (e.g. wrong response shape that suggests a prompt
        bug, not a runtime issue)."""
        ...

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, **kwargs: Any) -> AgentResult[T]:
        """Drive one full invocation: build prompt → call LLM → parse.

        Retries on LLM-error or parse-failure up to ``self.max_retries``
        additional attempts (default 0 = single attempt). The prompt is
        re-built once at the top — retries pay only for the LLM round
        trip + parse, which is the right behaviour for the common case
        where the LLM occasionally violates a structured-output schema.

        Returns an ``AgentResult``. Callers can check ``result.ok`` for
        a quick success/failure boolean, or inspect ``output`` / ``error``
        directly.
        """
        try:
            prompt = self.build_prompt(**kwargs)
        except Exception as exc:
            return AgentResult(error=f"build_prompt: {exc!r}")

        last_err = ""
        last_raw = ""
        for attempt in range(self.max_retries + 1):
            # Tool-using subclasses stash the ToolUseResult on the
            # instance before _call_llm returns; non-tool agents leave
            # it None.
            self._last_tool_use_result = None
            raw, llm_err = self._call_llm(prompt)
            if llm_err is not None:
                last_err = llm_err
                last_raw = raw or ""
                continue

            try:
                output = self.parse(raw or "")
            except Exception as exc:
                last_err = f"parse: {exc!r}"
                last_raw = raw or ""
                continue
            if output is None:
                last_err = "parse: returned None (unparseable response)"
                last_raw = raw or ""
                continue
            tu = getattr(self, "_last_tool_use_result", None)
            return AgentResult(
                output=output,
                raw_response=raw or "",
                tool_calls_made=(tu.tool_calls_made if tu else 0),
                tool_use_result=tu,
            )

        return AgentResult(raw_response=last_raw, error=last_err)

    # ------------------------------------------------------------------
    # Hook for subclass overrides (tool-use, retry, critique, …)
    # ------------------------------------------------------------------

    def _llm_call_kwargs(self) -> dict:
        """Extra kwargs passed to ``LLMClient.complete``. Override in
        subclasses that need non-default ``max_tokens``, ``thinking``,
        ``temperature``, etc. Default: empty dict.

        Common overrides:
          * ``max_tokens`` — when the response includes a long reasoning
            block (K2-Think) before the structured payload.
          * ``thinking`` — turn extended-thinking on/off per agent.
        """
        return {}

    #: Appended to the system prompt when this agent runs on the claude-code
    #: agent with tools (i.e. under --agentic / claude_code_agentic). Turns a
    #: routed-but-flat call into an *investigating* agent: it must read the real
    #: code rather than answer from the prompt.
    _AGENTIC_INVESTIGATION = (
        "\n\n[Agentic mode] You are running as an agent with read-only tools "
        "(Read, Grep, Glob) over the project source ({dirs}). BEFORE you answer, "
        "USE them to ground your response in the REAL code — read the relevant "
        "function bodies, callers, callees, struct/type definitions and headers "
        "rather than guessing from this prompt. Never cite a function, caller, "
        "type or file you have not actually read."
    )

    def _agent_runs_on_claude_code(self) -> bool:
        """True iff this agent's resolved backend is the claude-code agent with
        tools (so it can investigate). Gated on ``claude_code_agentic``."""
        if not getattr(self.config, "claude_code_agentic", False):
            return False
        try:
            prov = self.config.role_settings(self.name).get("provider") or ""
            if not prov:
                prov = self.config.resolved_provider()
        except Exception:
            return False
        return prov == "claude-code"

    def _system_prompt_for_call(self) -> str:
        """System prompt for this call — augmented with the investigation
        directive when the agent runs on the claude-code agent."""
        if not self._agent_runs_on_claude_code():
            return self.system_prompt
        dirs = ", ".join(getattr(self.config, "claude_code_add_dirs", None) or []) \
            or "the project source tree"
        return self.system_prompt + self._AGENTIC_INVESTIGATION.format(dirs=dirs)

    def _call_llm(self, prompt: str) -> tuple[str, Optional[str]]:
        """One LLM round-trip. Returns ``(raw_text, error_or_None)``.

        Default implementation: single ``LLMClient.complete`` call, routed by
        ``self.name``. Under --agentic, when this agent's backend is the
        claude-code agent, the system prompt is augmented so the agent
        *investigates the real code* (Read/Grep/Glob) instead of answering flat.
        Subclasses that need bmc's in-process tool loop override this.
        """
        from bmc_agent.llm import LLMError
        try:
            response = self.llm.complete(
                self._system_prompt_for_call(), prompt, role=self.name,
                **self._llm_call_kwargs(),
            )
        except LLMError as exc:
            return "", f"LLMError: {exc!r}"
        except Exception as exc:
            return "", f"unexpected: {exc!r}"
        return response or "", None
