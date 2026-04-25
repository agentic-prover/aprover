"""
LLM client wrapper for GRACE.

Wraps the Anthropic Python SDK with:
- Retry (up to 3 attempts) with exponential backoff on rate-limit / server errors
- Structured output: takes system + user prompts, returns a string
- Token usage logging to the artifact logger
- Clear error if ANTHROPIC_API_KEY is not set

NOTE (Phase 0): No actual spec-generation calls are made yet.
The class structure is ready for Phase 1.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from amc.config import Config
from amc.logger import get_logger

logger = get_logger("llm")

# Sentinel so we can detect a missing key at call time, not import time.
_UNSET = object()


class LLMError(Exception):
    """Raised when the LLM client cannot fulfil a request."""


class LLMClient:
    """
    Thin wrapper around the Anthropic ``Messages`` API.

    Parameters
    ----------
    config:
        GRACE configuration object.  The API key is read from
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
    ) -> str:
        """
        Send a request to the LLM and return the response text.

        Retries up to ``config.max_spec_retries`` times on transient errors
        (rate limits, server errors) with exponential backoff.

        Raises
        ------
        LLMError
            On permanent failure or missing API key.
        """
        client = self._get_client()
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_spec_retries):
            try:
                response = client.messages.create(  # type: ignore[attr-defined]
                    model=self.config.llm_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=[{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_prompt}],
                )
                # Log token usage
                usage = getattr(response, "usage", None)
                if usage:
                    logger.debug(
                        "LLM usage: input_tokens=%d output_tokens=%d "
                        "cache_creation=%d cache_read=%d",
                        getattr(usage, "input_tokens", 0),
                        getattr(usage, "output_tokens", 0),
                        getattr(usage, "cache_creation_input_tokens", 0),
                        getattr(usage, "cache_read_input_tokens", 0),
                    )
                # Extract text
                text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        text += block.text
                return text

            except Exception as exc:
                last_error = exc
                # Detect rate-limit / server errors by class name (avoids hard
                # dependency on specific anthropic exception hierarchy).
                cls_name = type(exc).__name__
                if any(
                    tag in cls_name.lower()
                    for tag in ("ratelimit", "overload", "server", "timeout", "connection")
                ):
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
                # Non-transient error — don't retry
                break

        raise LLMError(f"LLM request failed after {self.config.max_spec_retries} attempts: {last_error}") from last_error

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self):
        """Lazily initialise the Anthropic client."""
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
