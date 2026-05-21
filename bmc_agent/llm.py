"""
LLM client wrapper for BMC-Agent.

Dispatches between two providers, selected by ``config.resolved_provider()``:

* ``"anthropic"`` -- native Anthropic Messages API via the ``anthropic`` SDK
  (claude-* models on api.anthropic.com or via the OpenRouter proxy).
* ``"openai"`` -- OpenAI-compatible ``/v1/chat/completions`` over plain HTTPS,
  covering K2 Think (``api.k2think.ai``), OpenAI, and most self-hosted endpoints
  that mimic that schema.

Both paths share the same public surface (``complete(system, user) -> str``),
the same retry policy (exponential backoff on rate-limit / server / transient
errors), and the same token-usage logging.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from bmc_agent.config import Config
from bmc_agent.logger import get_logger

logger = get_logger("llm")

# Sentinel so we can detect a missing key at call time, not import time.
_UNSET = object()


def _strip_reasoning_blocks(text: str) -> str:
    """Strip `<think>...</think>` reasoning traces emitted by reasoning models.

    K2 Think and similar models on OpenAI-compatible endpoints fold their
    chain-of-thought into the response ``content`` as a `<think>...</think>`
    block followed by the actual answer. Downstream BMC-Agent stages expect
    a clean spec/JSON answer, so we strip the reasoning region. Handles
    three cases:

    * `<think>...</think>FINAL` -- balanced opening + closing tag
    * `RAW</think>FINAL` -- closing only (model started already inside the
      think context; observed in practice on K2)
    * no `</think>` tag at all -- return text unchanged
    """
    if not text:
        return text
    closing = text.rfind("</think>")
    if closing != -1:
        return text[closing + len("</think>"):].lstrip("\n")
    return text


class LLMError(Exception):
    """Raised when the LLM client cannot fulfil a request."""


class LLMClient:
    """
    Thin wrapper around the Anthropic ``Messages`` API.

    Parameters
    ----------
    config:
        BMC-Agent configuration object.  The API key is read from
        ``config.resolved_api_key()`` (which falls back to
        ``ANTHROPIC_API_KEY`` environment variable).
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Optional[object] = None  # lazy init

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        thinking: bool = False,
        thinking_budget: int = 8000,
        role: str | None = None,
    ) -> str:
        """
        Send a request to the LLM and return the response text.

        Retries up to ``config.max_spec_retries`` times on transient errors
        (rate limits, server errors) with exponential backoff.

        Parameters
        ----------
        thinking:
            Enable extended thinking (Claude's internal reasoning before responding).
            Improves quality on complex spec-generation and disagreement-resolution tasks.
            When True, temperature is forced to 1 (API requirement) and max_tokens is
            auto-expanded to at least thinking_budget + 1024.
        thinking_budget:
            Token budget for the thinking phase.  Ignored when thinking=False.
        role:
            Optional role identifier (e.g. "spec_gen", "feedback_distill") used to
            select a per-role LLM backend via ``config.llm_role_overrides``. When
            ``None`` (or the role isn't overridden), the global config is used.
            Enables hybrid setups: e.g. Claude for spec gen, K2 for refinement.

        Raises
        ------
        LLMError
            On permanent failure or missing API key.
        """
        # Per-role routing. Resolve effective settings for this call -- when the
        # caller passes a role with an override, we use that backend (model,
        # base_url, api_key, provider) for THIS one call. Implementation-wise,
        # we briefly swap the config fields on self.config so the existing
        # _complete_openai / _complete_anthropic paths see the right settings,
        # then restore them. This avoids threading per-call settings through
        # every internal helper.
        role_settings = self.config.role_settings(role) if role else None
        saved_settings = None
        if role_settings and (
            role_settings.get("model") != self.config.llm_model
            or role_settings.get("base_url") != self.config.llm_base_url
            or role_settings.get("api_key") != (self.config.llm_api_key or "")
            or role_settings.get("provider") != self.config.llm_provider
        ):
            saved_settings = {
                "model": self.config.llm_model,
                "base_url": self.config.llm_base_url,
                "api_key": self.config.llm_api_key,
                "provider": self.config.llm_provider,
                "client": self._client,
            }
            self.config.llm_model = role_settings["model"]
            self.config.llm_base_url = role_settings["base_url"]
            self.config.llm_api_key = role_settings["api_key"]
            self.config.llm_provider = role_settings["provider"]
            # Force-rebuild the SDK client lazily for the swapped settings.
            self._client = None

        provider = self.config.resolved_provider()
        last_error: Optional[Exception] = None

        # Extended thinking requires temperature=1 and enough token headroom.
        api_kwargs: dict = {}
        if thinking:
            api_kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
            temperature = 1.0
            max_tokens = max(max_tokens, thinking_budget + 1024)

        try:
            for attempt in range(self.config.max_spec_retries):
                try:
                    if provider == "openai":
                        # OpenAI-compatible endpoints (K2 Think etc.) ignore the
                        # Anthropic-only "thinking" knob — many reasoning models
                        # on this path already emit a <think>...</think> trace
                        # that _strip_reasoning_blocks() handles transparently.
                        return self._complete_openai(system_prompt, user_prompt, max_tokens, temperature)
                    return self._complete_anthropic(
                        system_prompt,
                        user_prompt,
                        max_tokens,
                        temperature,
                        api_kwargs,
                    )
                except Exception as exc:
                    last_error = exc
                    cls_name = type(exc).__name__
                    msg = str(exc).lower()
                    # HTTP 4xx is a permanent client error (bad request,
                    # auth, request-too-large) — retrying just burns
                    # another LLM round-trip and N×retry_backoff seconds.
                    # Observed: OpenRouter rejects realism/reproducer
                    # prompts >8MB with HTTP 400. The (now-fixed) cbmc.py
                    # raw_output blow-up made every kernel-TU realism call
                    # fire this. Burned ~90s/failure × 3 attempts before
                    # we used to give up.
                    is_4xx = bool(
                        re.search(r"http\s*4\d\d\b", msg)
                        or re.search(r'"code"\s*:\s*4\d\d', msg)
                    )
                    transient = (not is_4xx) and any(
                        tag in cls_name.lower() or tag in msg
                        for tag in ("ratelimit", "rate_limit", "overload", "server", "timeout", "connection", "503", "502", "504", "429")
                    )
                    if transient:
                        wait = 2 ** attempt  # 1, 2, 4 seconds
                        logger.warning(
                            "LLM transient error (%s); retrying in %ds (attempt %d/%d)",
                            cls_name,
                            wait,
                            attempt + 1,
                            self.config.max_spec_retries,
                        )
                        time.sleep(wait)
                        continue
                    break

            raise LLMError(f"LLM request failed after {self.config.max_spec_retries} attempts: {last_error}") from last_error
        finally:
            # Restore the original config + client even on exception/return.
            if saved_settings is not None:
                self.config.llm_model = saved_settings["model"]
                self.config.llm_base_url = saved_settings["base_url"]
                self.config.llm_api_key = saved_settings["api_key"]
                self.config.llm_provider = saved_settings["provider"]
                self._client = saved_settings["client"]

    # ------------------------------------------------------------------
    # Provider paths
    # ------------------------------------------------------------------

    def _complete_anthropic(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        api_kwargs: dict | None = None,
    ) -> str:
        client = self._get_client()
        # Per-request timeout: without it the SDK can block indefinitely on a
        # stuck connection and stall a multi-hour sweep. The Anthropic SDK
        # accepts an httpx-style timeout via with_options.
        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 180.0))
        extra = api_kwargs or {}
        response = client.with_options(timeout=timeout_s).messages.create(  # type: ignore[attr-defined]
            model=self.config.llm_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
            **extra,
        )
        usage = getattr(response, "usage", None)
        if usage:
            logger.debug(
                "LLM usage (anthropic): input_tokens=%d output_tokens=%d "
                "cache_creation=%d cache_read=%d",
                getattr(usage, "input_tokens", 0),
                getattr(usage, "output_tokens", 0),
                getattr(usage, "cache_creation_input_tokens", 0),
                getattr(usage, "cache_read_input_tokens", 0),
            )
        # Skip thinking blocks (only present when api_kwargs enabled extended thinking).
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "thinking":
                continue
            if hasattr(block, "text"):
                text += block.text
        return text

    def _complete_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """OpenAI-compatible /v1/chat/completions request (used for K2 Think etc.).

        Reasoning models on this path (K2 Think, R1-style) fold a verbose
        ``<think>...</think>`` trace into ``content`` before emitting the
        answer. If ``max_tokens`` is too tight, the model spends the entire
        budget on the trace and the answer never appears -- we observed this
        on spec-generation prompts with the SDK default of 4096. We pad the
        requested cap to a high floor on K2-style providers so the answer
        has room to land.
        """
        api_key = self.config.resolved_api_key()
        if not api_key:
            raise LLMError(
                "No API key for OpenAI-compatible provider. "
                "Export K2THINK_API_KEY (or ANTHROPIC_API_KEY) or set llm_api_key in Config."
            )

        base = self.config.llm_base_url.rstrip("/") if self.config.llm_base_url else "https://api.k2think.ai/v1"
        if not base.endswith("/v1") and not base.endswith("/v1/"):
            if "/v1" not in base:
                base = base + "/v1"
        url = base.rstrip("/") + "/chat/completions"

        # Pad max_tokens for reasoning models (K2 Think, R1-style, etc.).
        # Live K2 CCC sweep observed completion_tokens=16384 repeatedly --
        # the model was exhausting the prior 16k floor on the spec-gen
        # <think> trace and either emitting a truncated answer or failing
        # with finish_reason=length. Raise the floor to 24k so the
        # reasoning model has comfortable room for both the trace and a
        # full algebraic spec. Cheaper non-reasoning models on the same
        # endpoint simply stop earlier on finish_reason=stop.
        effective_max_tokens = max(max_tokens, 24576)
        payload = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": effective_max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise LLMError(
                "The 'httpx' package is required for the openai-compatible provider."
            ) from exc

        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 180.0))
        timeout = httpx.Timeout(timeout_s, connect=10.0)
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise LLMError(
                f"OpenAI-compatible request failed: HTTP {resp.status_code} {resp.reason_phrase}: {resp.text[:500]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"OpenAI-compatible response was not JSON: {resp.text[:500]}") from exc

        usage = data.get("usage") or {}
        if usage:
            logger.debug(
                "LLM usage (openai): prompt_tokens=%s completion_tokens=%s total_tokens=%s",
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
            )

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"OpenAI-compatible response contained no choices: {data}")
        choice = choices[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
        elif isinstance(content, str):
            text = content
        else:
            raise LLMError(f"OpenAI-compatible response had no message content: {choices[0]}")

        finish_reason = choice.get("finish_reason")
        stripped = _strip_reasoning_blocks(text)
        # When a reasoning model burns its whole budget on <think>... and is
        # cut off, finish_reason=='length' and we get no </think> closing
        # tag, so the strip is a no-op and `stripped` is just chain-of-thought.
        # Surface this loudly: the caller would otherwise parse the reasoning
        # text as a spec, which silently corrupts the pipeline.
        if finish_reason == "length" and "</think>" not in text:
            raise LLMError(
                "OpenAI-compatible response hit max_tokens before emitting the "
                "final answer (no </think> closing tag). Bump max_tokens or "
                "shorten the prompt. "
                f"prompt_tokens={usage.get('prompt_tokens')} "
                f"completion_tokens={usage.get('completion_tokens')}"
            )
        return stripped

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self):
        """Lazily initialise the Anthropic client (only used on the anthropic path)."""
        if self._client is not None:
            return self._client

        api_key = self.config.resolved_api_key()
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set.  "
                "Export it in your environment or pass llm_api_key in Config."
            )

        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise LLMError(
                "The 'anthropic' package is not installed.  "
                "Run: uv add anthropic"
            ) from exc

        kwargs: dict = {"api_key": api_key}
        if self.config.llm_base_url:
            kwargs["base_url"] = self.config.llm_base_url

        self._client = anthropic.Anthropic(**kwargs)
        return self._client
