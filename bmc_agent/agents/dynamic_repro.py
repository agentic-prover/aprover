"""``DynamicReproAgent`` — regenerates a failed dyn-val reproducer.

When the system-entry reproducer (LLM-generated C exercising the real
call chain) fails to compile, this agent is given the previous source
plus the GCC error and asked to emit a corrected version. The caller
(``DynamicValidator._regenerate_reproducer_with_error``) is responsible
for the compile-retry loop bookkeeping and the downstream
``_reproducer_uses_public_api`` gate — the agent itself just owns the
single LLM-driven repair step.

Same shape as ``FeedbackDistillAgent`` / ``RefinementAgent``: one
structured-JSON call, no tool use. The agent's role is
``dynamic_repro`` so the per-role routing knobs
(``BMC_AGENT_LLM_DYNAMIC_REPRO_*``) can dial up a stronger compiler-
aware model without affecting the realism budget the call site
previously shared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent

if TYPE_CHECKING:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient


_SYSTEM_PROMPT = (
    "You are a formal verification expert for C programs. Your "
    "task is to fix a C reproducer that failed to compile. "
    "Return only valid JSON."
)


_USER_PROMPT_TEMPLATE = (
    "A previous reproducer attempt for the buggy function "
    "`{func_name}` failed to compile. Your task: emit a "
    "CORRECTED version of the C source that compiles.\n\n"
    "=== PREVIOUS REPRODUCER (failed to compile) ===\n"
    "```c\n"
    "{previous_reproducer}\n"
    "```\n\n"
    "=== COMPILER ERROR ===\n"
    "```\n"
    "{err_snippet}\n"
    "```\n\n"
    "HARD RULES:\n"
    "  1. Same constraints as the original prompt: MUST "
    "     #include the project's public-API header, MUST use "
    "     only public-API calls, NO inline reimplementation of "
    "     project functions, NO fabricated copies of opaque "
    "     structs.\n"
    "  2. Fix the specific error reported. Typical fixes: "
    "     missing #include, wrong function name (typo or "
    "     misremembered API), wrong argument count, "
    "     header-order conflict.\n"
    "  3. If the error is a LINKER 'undefined reference' to a "
    "     project API call you correctly used, the source is "
    "     already correct — the build is missing -l<libname>. "
    "     In that case respond with the UNREPRODUCIBLE marker.\n"
    "  4. If you cannot honestly fix the source at the LLM "
    "     level, respond with the UNREPRODUCIBLE marker.\n\n"
    "Respond with ONLY this JSON:\n"
    "{{\n"
    '  "reproducer_code": "<corrected C source OR '
    '// UNREPRODUCIBLE: <reason>>"\n'
    "}}"
)


class DynamicReproAgent(BaseAgent[str]):
    """Returns the corrected reproducer C source as a string, or the
    UNREPRODUCIBLE marker verbatim (the caller's outer loop treats the
    marker as a graceful give-up). Returns None from ``parse`` when the
    response has no usable JSON or the ``reproducer_code`` field is
    empty — BaseAgent reports this as an error so the caller falls back
    to the prior reproducer.

    Routing: ``BMC_AGENT_LLM_DYNAMIC_REPRO_*`` env vars (with default
    fallback chain). Previously this LLM call piggybacked on the
    ``realism`` role; splitting it lets you upgrade compiler-fix
    quality independently.
    """

    name = "dynamic_repro"
    system_prompt = _SYSTEM_PROMPT

    def build_prompt(
        self,
        *,
        previous_reproducer: str,
        compile_error: str,
        func_name: str,
        **_: Any,
    ) -> str:
        # Trim error to keep prompt budget reasonable — first 1500 chars
        # are almost always enough (multi-line GCC errors repeat once
        # the first symbol fails to resolve). Match the call-site's
        # historical truncation byte-for-byte so the agent and the
        # pre-migration code agree on the prompt.
        err_snippet = (compile_error or "")[:1500]
        prev = (previous_reproducer or "")[:4000]
        return _USER_PROMPT_TEMPLATE.format(
            func_name=func_name,
            previous_reproducer=prev,
            err_snippet=err_snippet,
        )

    def parse(self, response: str) -> Optional[str]:
        import json
        import re
        text = (response or "").strip()
        if not text:
            return None
        # Fenced markdown — strip the fence and re-parse. The original
        # call-site parser treated any ``` opening as a fence even
        # without a language tag, so mirror that.
        if text.startswith("```"):
            lines = text.splitlines()
            inner: list[str] = []
            in_fence = False
            for line in lines:
                if line.startswith("```"):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    inner.append(line)
            text = "\n".join(inner).strip()

        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m is None:
                return None
            try:
                data = json.loads(m.group(0))
            except Exception:
                return None

        code = (data.get("reproducer_code") or "").strip()
        if not code:
            return None
        # UNREPRODUCIBLE marker is honoured verbatim — the outer loop
        # sees it doesn't differ usefully and exits. Pass-through.
        return code
