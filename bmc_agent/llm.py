"""
LLM client wrapper for BMC-Agent.

Dispatches between three providers, selected by ``config.resolved_provider()``:

* ``"anthropic"`` -- native Anthropic Messages API via the ``anthropic`` SDK
  (claude-* models on api.anthropic.com or via the OpenRouter proxy).
* ``"openai"`` -- OpenAI-compatible ``/v1/chat/completions`` over plain HTTPS,
  covering OpenAI and most self-hosted endpoints that mimic that schema.
* ``"claude-code"`` -- the Claude Code CLI in non-interactive mode (``claude -p``).
  No API key required: the host's existing Claude Code login is reused. Useful
  when you want bmc-agent's reasoning to run through your local subscription
  rather than the API.

All paths share the same public surface (``complete(system, user) -> str``),
the same retry policy (exponential backoff on rate-limit / server / transient
errors), and the same token-usage logging.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from bmc_agent.config import Config
from bmc_agent.logger import get_logger

logger = get_logger("llm")


#: Anthropic model families that reject the ``temperature`` parameter (newer
#: tiers deprecate it -> HTTP 400 "temperature is deprecated for this model").
#: For these we omit ``temperature`` from the request entirely.
_TEMP_UNSUPPORTED = ("opus-4-8",)


def _model_rejects_temperature(model: str) -> bool:
    m = (model or "").lower()
    return any(s in m for s in _TEMP_UNSUPPORTED)


def openrouter_attribution_headers() -> dict:
    """HTTP headers attributing AProver's traffic on OpenRouter's public
    rankings and analytics (https://openrouter.ai/docs/app-attribution).

    ``HTTP-Referer`` is required for an app page to be created; ``X-OpenRouter-
    Title`` is the current spec header for the display name and ``X-Title`` is
    kept for backwards compatibility. Other providers silently ignore these, so
    they are attached unconditionally to every OpenAI-compatible request."""
    return {
        "HTTP-Referer": "https://example.com",
        "X-OpenRouter-Title": "AProver",
        "X-Title": "AProver",
    }


def _default_openai_max_tokens_cap(model: str) -> int:
    """Return a conservative completion-token cap for OpenAI-compatible models.

    The cap must sit at or above the reasoning floor (24576) for reasoning
    models, otherwise ``min(max(max_tokens, 24576), cap)`` silently clamps the
    floor back down and the model truncates mid-``<think>``. Older OpenAI chat
    models have hard per-request ceilings and must stay capped below the floor.
    """

    m = (model or "").lower()
    # gpt-3.5-turbo-1106 is the original SpecGen model and rejects completion
    # requests above 4096 tokens.
    if "gpt-3.5-turbo" in m:
        return 4096
    # gpt-4o-mini hard-caps completions at 16384.
    if "gpt-4o-mini" in m:
        return 16384
    # Reasoning models and other modern OpenAI-compatible endpoints:
    # a generous cap so the 24576 reasoning floor actually applies and large
    # explicit requests aren't clipped. Override via BMC_AGENT_LLM_MAX_TOKENS_CAP.
    return 32768


def _resolved_openai_cap(model: str) -> int:
    """Effective completion cap: env override else the per-model default."""
    return int(
        os.environ.get(
            "BMC_AGENT_LLM_MAX_TOKENS_CAP",
            str(_default_openai_max_tokens_cap(model)),
        )
    )

# Sentinel so we can detect a missing key at call time, not import time.
_UNSET = object()


def _strip_reasoning_blocks(text: str) -> str:
    """Strip `<think>...</think>` reasoning traces emitted by reasoning models.

    Reasoning models on OpenAI-compatible endpoints fold their
    chain-of-thought into the response ``content`` as a `<think>...</think>`
    block followed by the actual answer. Downstream BMC-Agent stages expect
    a clean spec/JSON answer, so we strip the reasoning region. Handles
    three cases:

    * `<think>...</think>FINAL` -- balanced opening + closing tag
    * `RAW</think>FINAL` -- closing only (model started already inside the
      think context; observed in practice with some reasoning models)
    * no `</think>` tag at all -- return text unchanged
    """
    if not text:
        return text
    closing = text.rfind("</think>")
    if closing != -1:
        text = text[closing + len("</think>"):].lstrip("\n")
    return _unwrap_code_fence(text)


def _unwrap_code_fence(text: str) -> str:
    """Remove a single wrapping ```lang ... ``` fence, if the whole text is one.

    Reasoning models sometimes wrap the requested answer in a Markdown fence
    despite being told not to. Only unwraps when the trimmed text starts with a
    fence so plain answers (and answers that merely contain a fence inline) are
    returned untouched.
    """
    s = text.strip()
    if not s.startswith("```"):
        return text
    first_nl = s.find("\n")
    if first_nl == -1:
        return text
    s = s[first_nl + 1:]
    s = s.rstrip()
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


_AGENTIC_INVESTIGATION = (
    "\n\n[Agentic mode] You are running as an agent with read-only tools "
    "(Read, Grep, Glob) over the project source ({dirs}). BEFORE you answer, "
    "USE them to ground your response in the REAL code — read the relevant "
    "function bodies, callers, callees, struct/type definitions and headers "
    "rather than guessing from this prompt. Never cite a function, caller, "
    "type or file you have not actually read."
)


# Roles that make a TRUST decision — whether an input is attacker-controlled or
# caller/hardware-guaranteed. The trust-boundary context is injected only for
# these (it's irrelevant to, e.g., feedback_distill or cbmc_driver). Spec_gen
# leads because the precondition it writes IS the encoded trust boundary; getting
# it right there means fewer masked bugs AND fewer spurious cex downstream.
THREAT_MODEL_CONTEXT_ROLES = frozenset({
    "spec_gen", "refinement", "classifier",
    "dynamic_repro", "dynval_triage", "realism",
})

# Standing conservative-default instruction shipped WITH every context block.
# Shifting the trust boundary left (into spec-gen) is only safe if the bias is
# "attacker unless proven otherwise" — a too-generous "trusted" list would mask
# bugs at generation time, before any gate could catch them.
_THREAT_MODEL_CONSERVATIVE_RULE = (
    "\n\nDefault assumption: treat EVERY input (parameters, globals, data read "
    "from files/network/syscalls/devices) as ATTACKER-CONTROLLED unless the "
    "note above, a caller, or hardware provably guarantees otherwise. Never add "
    "a precondition that bounds or validates attacker-controlled data — that "
    "masks the very bugs we are looking for. Only encode as a precondition the "
    "structural validity that a caller genuinely establishes."
)


def render_threat_model_context(config, role) -> str:
    """Return the trust-boundary block to append to a system prompt for ``role``,
    or "" when there is nothing to add. Ungated by --agentic: the context is
    plain text that helps flat and agentic backends alike. Injected only for the
    trust-deciding roles in :data:`THREAT_MODEL_CONTEXT_ROLES`. Reads the raw note
    from ``config.threat_model_context`` (user-supplied or auto-derived).
    """
    if role not in THREAT_MODEL_CONTEXT_ROLES:
        return ""
    note = (getattr(config, "threat_model_context", "") or "").strip()
    if not note:
        return ""
    return (
        "\n\n## Trust boundary for this target\n"
        + note
        + _THREAT_MODEL_CONSERVATIVE_RULE
    )


def agentic_system_prompt(config, role, system_prompt: str) -> str:
    """Augment ``system_prompt`` with the investigation directive when this
    ``role`` runs on the claude-code agent with tools (i.e. under --agentic /
    ``claude_code_agentic``). No-op otherwise — so flat LLM call sites become
    *investigating* agents under --agentic by wrapping their system prompt with
    this, exactly like BaseAgent does for the agent classes.

    NOTE: the trust-boundary note is NOT added here — it is injected centrally
    in :meth:`LLM.complete` / :meth:`LLM.complete_with_tools` (keyed by role) so
    it reaches every trust-deciding call site uniformly, including the ones that
    do not wrap their prompt with this helper (e.g. the main spec-gen path).
    Adding it here too would double-inject at wrapped sites.
    """
    if not getattr(config, "claude_code_agentic", False):
        return system_prompt
    try:
        prov = ""
        if role:
            prov = (config.role_settings(role) or {}).get("provider") or ""
        if not prov:
            prov = config.resolved_provider()
    except Exception:
        return system_prompt
    if prov != "claude-code":
        return system_prompt
    dirs = ", ".join(getattr(config, "claude_code_add_dirs", None) or []) \
        or "the project source tree"
    return system_prompt + _AGENTIC_INVESTIGATION.format(dirs=dirs)


def _is_openrouter(base_url: "str | None") -> bool:
    """True when the OpenAI-compatible endpoint is OpenRouter, which exposes
    extra body params (e.g. ``usage: {include: true}`` for cost accounting) that
    plain OpenAI endpoints would reject."""
    return "openrouter" in (base_url or "").lower()


def _supports_explicit_prompt_cache(base_url: "str | None") -> bool:
    """Anthropic / OpenRouter honour an explicit ``cache_control: ephemeral``
    breakpoint on a content block. OpenAI auto-caches long prefixes with no
    param (and may reject the unknown field), so we only emit it for the former.
    """
    b = (base_url or "").lower()
    return "anthropic" in b or "openrouter" in b


def _system_msg_with_cache(system_prompt: str, base_url: "str | None") -> dict:
    """Build the system message, marking it as a cache breakpoint when the
    endpoint supports explicit caching. Caching the (large, stable) system
    prefix means it's reused across the multi-turn tool loop AND across calls
    with the same system prompt (5-min TTL) instead of re-billed every turn.
    """
    if _supports_explicit_prompt_cache(base_url):
        return {
            "role": "system",
            "content": [
                {"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}},
            ],
        }
    return {"role": "system", "content": system_prompt}


class LLMError(Exception):
    """Raised when the LLM client cannot fulfil a request."""


class LLMRetryableError(LLMError):
    """A response the caller can't use, but a fresh re-query may fix.

    Raised for non-conforming output (e.g. the response failed a caller-supplied
    ``validate`` predicate). The ``complete()`` retry loop re-samples the model
    rather than giving up — reasoning models vary across samples, so re-running
    often yields conforming output.
    """


class LLMTruncatedError(LLMRetryableError):
    """The model ran out of completion budget before emitting its answer.

    Observed on reasoning models when the ``<think>`` trace consumes the whole
    ``max_tokens`` budget (``finish_reason=length`` with no closing ``</think>``).
    Retryable, and the retry loop additionally escalates ``max_tokens``.
    """


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
        # Cumulative token usage across every completion on this client.
        # The agent layer (agents/base.py) snapshots usage_total_tokens per
        # invocation to attribute tokens (-> dollars/finding) into agent_telemetry.
        self.usage_total_prompt_tokens = 0
        self.usage_total_completion_tokens = 0
        self.usage_total_tokens = 0
        # Exact spend reported by providers that price the call for us (the
        # claude-code CLI returns ``total_cost_usd``). 0.0 => no exact figure,
        # so the web layer falls back to its token-based estimate.
        self.usage_total_cost_usd = 0.0
        # Per-attempt reliability + latency telemetry (one record per underlying
        # provider request, retries included). Surfaced to the web workbench via
        # pipeline._cost_snapshot for the reliability badge. Best-effort, no lock
        # — same contract as the usage counters above.
        self.reliability_total = 0
        self.reliability_success = 0
        self.reliability_timeout = 0
        self.reliability_decode = 0     # invalid / truncated / non-conforming response
        self.reliability_other = 0
        self._reliability_recent: "deque[int]" = deque(maxlen=5)  # 1=failed, 0=ok
        self._latency_total_s = 0.0
        self._latency_recent: "deque[float]" = deque(maxlen=5)    # recent durations (s)

    def _add_usage(self, prompt_tokens, completion_tokens) -> None:
        """Accumulate one completion token usage. Best-effort: non-numeric
        or None values count as 0 and never raise."""
        try:
            p = int(prompt_tokens or 0)
            c = int(completion_tokens or 0)
        except (TypeError, ValueError):
            return
        self.usage_total_prompt_tokens += p
        self.usage_total_completion_tokens += c
        self.usage_total_tokens += p + c

    def _add_cost(self, usd) -> None:
        """Accumulate one completion's exact USD cost. Best-effort: non-numeric
        or None values count as 0 and never raise."""
        try:
            self.usage_total_cost_usd += float(usd or 0)
        except (TypeError, ValueError):
            return

    def _record_call(self, ok: bool, category: "str | None" = None,
                     duration_s: float = 0.0) -> None:
        """Record one provider attempt's outcome + latency for the reliability
        badge. ``category`` is "timeout" / "decode" / "other" when ``ok`` is
        False. Best-effort: never raises."""
        self.reliability_total += 1
        if ok:
            self.reliability_success += 1
        elif category == "timeout":
            self.reliability_timeout += 1
        elif category == "decode":
            self.reliability_decode += 1
        else:
            self.reliability_other += 1
        self._reliability_recent.append(0 if ok else 1)
        try:
            d = float(duration_s)
        except (TypeError, ValueError):
            return
        self._latency_total_s += d
        self._latency_recent.append(d)

    def reliability_snapshot(self) -> dict:
        """Cumulative reliability + latency for this client's LLM calls.

        ``recent_fail``/``recent_total`` are the rolling last-5-attempts window
        the web badge colours its forecast dot from."""
        total = self.reliability_total
        recent_lat = list(self._latency_recent)
        return {
            "total": total,
            "success": self.reliability_success,
            "timeout": self.reliability_timeout,
            "decode": self.reliability_decode,
            "other": self.reliability_other,
            "recent_fail": sum(self._reliability_recent),
            "recent_total": len(self._reliability_recent),
            "latency_ms_avg": round(1000 * self._latency_total_s / total) if total else None,
            "latency_ms_recent": round(1000 * sum(recent_lat) / len(recent_lat)) if recent_lat else None,
        }

    @staticmethod
    def _classify_failure(exc: Exception, cls_name: str, msg: str) -> str:
        """Bucket a failed attempt: timeout / decode / other."""
        if "timeout" in cls_name.lower() or "timeout" in msg:
            return "timeout"
        if isinstance(exc, (LLMTruncatedError, LLMRetryableError)):
            return "decode"  # truncated / non-conforming response
        return "other"

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
        cache_prefix: str = "",
        validate: "Optional[Callable[[str], bool]]" = None,
    ) -> str:
        """
        Send a request to the LLM and return the response text.

        Retries up to ``config.max_spec_retries`` times on transient errors
        (rate limits, server errors) with exponential backoff.

        validate:
            Optional predicate run on the (reasoning-stripped) response text.
            When it returns ``False`` the response is treated as non-conforming
            and the request is re-sampled (up to ``config.max_spec_retries``).
            Use it to retry on, e.g., output that doesn't parse as the expected
            JSON — reasoning models vary across samples, so a re-run usually
            yields conforming output. See also ``complete_json()``.

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
            Enables hybrid setups: e.g. Claude for spec gen, another model for
            refinement.

        Raises
        ------
        LLMError
            On permanent failure or missing API key.
        """
        # Trust-boundary note: injected centrally (not at call sites) so every
        # trust-deciding role gets it uniformly — including the unwrapped main
        # spec-gen path. No-op for other roles / when no note is configured.
        system_prompt = system_prompt + render_threat_model_context(self.config, role)

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
                _t0 = time.monotonic()
                try:
                    if provider == "openai":
                        # OpenAI-compatible endpoints ignore the Anthropic-only
                        # "thinking" knob — many reasoning models on this path
                        # already emit a <think>...</think> trace that
                        # _strip_reasoning_blocks() handles transparently.
                        # No prompt-cache breakpoint API here, so fold the shared
                        # prefix into the system text to preserve its content.
                        sys_oa = (cache_prefix + "\n\n" + system_prompt) if cache_prefix else system_prompt
                        result = self._complete_openai(sys_oa, user_prompt, max_tokens, temperature)
                    elif provider == "claude-code":
                        sys_cc = (cache_prefix + "\n\n" + system_prompt) if cache_prefix else system_prompt
                        result = self._complete_claude_code(sys_cc, user_prompt, max_tokens, temperature)
                    else:
                        result = self._complete_anthropic(
                            system_prompt,
                            user_prompt,
                            max_tokens,
                            temperature,
                            api_kwargs,
                            cache_prefix=cache_prefix,
                        )
                    _dur = time.monotonic() - _t0
                    if validate is not None and not validate(result):
                        # Non-conforming output (e.g. unparseable JSON). Counts as
                        # a "decode" failure for the reliability badge. Re-sample
                        # while attempts remain; reasoning models vary per draw.
                        # On exhaustion, return the best-effort last response so
                        # the caller's own parse-failure handling still applies
                        # (rather than turning a soft miss into a hard error).
                        self._record_call(False, "decode", _dur)
                        if attempt < self.config.max_spec_retries - 1:
                            wait = 2 ** attempt
                            logger.warning(
                                "LLM response failed validation; re-querying in "
                                "%ds (attempt %d/%d)",
                                wait, attempt + 1, self.config.max_spec_retries,
                            )
                            time.sleep(wait)
                            continue
                        logger.warning(
                            "LLM response still failed validation after %d "
                            "attempts; returning last response",
                            self.config.max_spec_retries,
                        )
                        return result
                    self._record_call(True, None, _dur)
                    return result
                except Exception as exc:
                    self._record_call(
                        False,
                        self._classify_failure(exc, type(exc).__name__, str(exc).lower()),
                        time.monotonic() - _t0,
                    )
                    last_error = exc
                    cls_name = type(exc).__name__
                    msg = str(exc).lower()
                    # Truncated reasoning trace: the model ran out of completion
                    # budget mid-<think>. Give it more room and re-sample.
                    if isinstance(exc, LLMTruncatedError):
                        if provider == "openai":
                            max_tokens = min(
                                max(max_tokens, 24576) * 2,
                                _resolved_openai_cap(self.config.llm_model),
                            )
                        retryable = True
                    # Non-conforming response (failed `validate`): re-sample as-is.
                    elif isinstance(exc, LLMRetryableError):
                        retryable = True
                    else:
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
                        retryable = (not is_4xx) and any(
                            tag in cls_name.lower() or tag in msg
                            for tag in ("ratelimit", "rate_limit", "overload", "server", "timeout", "connection", "503", "502", "504", "429")
                        )
                    if retryable:
                        wait = 2 ** attempt  # 1, 2, 4 seconds
                        logger.warning(
                            "LLM retryable error (%s); retrying in %ds (attempt %d/%d)",
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

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs,
    ) -> dict:
        """Like :meth:`complete`, but return a parsed JSON object.

        Validates each response with :func:`bmc_agent.json_utils.extract_json_object`
        and re-queries (via ``complete``'s retry loop) when the output isn't
        parseable JSON — the common failure mode for reasoning models that fold
        prose or a truncated ``<think>`` trace into the answer. Raises
        ``LLMError`` if no parseable object is produced within the retry budget.
        ``**kwargs`` are forwarded to ``complete`` (``role``, ``max_tokens`` …);
        passing ``validate`` is not allowed.
        """
        if "validate" in kwargs:
            raise TypeError("complete_json() manages 'validate' itself")
        from bmc_agent.json_utils import extract_json_object

        holder: dict = {}

        def _validate(text: str) -> bool:
            obj = extract_json_object(text)
            if obj is None:
                return False
            holder["obj"] = obj
            return True

        self.complete(system_prompt, user_prompt, validate=_validate, **kwargs)
        if "obj" not in holder:
            raise LLMError(
                "complete_json: no parseable JSON object in the LLM response "
                f"after {self.config.max_spec_retries} attempt(s)."
            )
        return holder["obj"]

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
        cache_prefix: str = "",
    ) -> str:
        client = self._get_client()
        # Per-request timeout: without it the SDK can block indefinitely on a
        # stuck connection and stall a multi-hour sweep. The Anthropic SDK
        # accepts an httpx-style timeout via with_options.
        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 180.0))
        extra = api_kwargs or {}
        # System payload + prompt-cache breakpoints. Render order is
        # system -> messages, and a breakpoint caches the whole prefix up to
        # and including its block. When a caller supplies ``cache_prefix`` (the
        # codebase-wide domain summary — byte-identical across every function
        # AND every agent role in a sweep), put it FIRST as its own cached
        # block so all those calls share one cache entry for it. The per-role
        # ``system_prompt`` is the second cached block (stable within a role).
        # Without a prefix this is unchanged: a single cached system block.
        if cache_prefix:
            system_payload = [
                {"type": "text", "text": cache_prefix,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}},
            ]
        else:
            system_payload = [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        create_kwargs: dict = dict(
            model=self.config.llm_model,
            max_tokens=max_tokens,
            system=system_payload,
            messages=[{"role": "user", "content": user_prompt}],
            **extra,
        )
        # Newer models (e.g. claude-opus-4-8) reject ``temperature`` outright;
        # only send it where the model still accepts it.
        if not _model_rejects_temperature(self.config.llm_model):
            create_kwargs["temperature"] = temperature
        response = client.with_options(timeout=timeout_s).messages.create(  # type: ignore[attr-defined]
            **create_kwargs
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
            self._add_usage(
                getattr(usage, "input_tokens", 0),
                getattr(usage, "output_tokens", 0),
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
        """OpenAI-compatible /v1/chat/completions request.

        Reasoning models on this path (R1-style) fold a verbose
        ``<think>...</think>`` trace into ``content`` before emitting the
        answer. If ``max_tokens`` is too tight, the model spends the entire
        budget on the trace and the answer never appears -- we observed this
        on spec-generation prompts with the SDK default of 4096. We pad the
        requested cap to a high floor on reasoning providers so the answer
        has room to land.
        """
        api_key = self.config.resolved_api_key()
        if not api_key:
            raise LLMError(
                "No API key for OpenAI-compatible provider. "
                "Export BMC_AGENT_LLM_API_KEY (or ANTHROPIC_API_KEY) or set llm_api_key in Config."
            )

        base = self.config.llm_base_url.rstrip("/") if self.config.llm_base_url else "https://api.openai.com/v1"
        if not base.endswith("/v1") and not base.endswith("/v1/"):
            if "/v1" not in base:
                base = base + "/v1"
        url = base.rstrip("/") + "/chat/completions"

        # Pad max_tokens for reasoning models (R1-style, etc.).
        # Observed completion_tokens=16384 repeatedly --
        # the model was exhausting the prior 16k floor on the spec-gen
        # <think> trace and either emitting a truncated answer or failing
        # with finish_reason=length. Raise the floor to 24k so the
        # reasoning model has comfortable room for both the trace and a
        # full algebraic spec. Cheaper non-reasoning models on the same
        # endpoint simply stop earlier on finish_reason=stop.
        # Ceiling: the 24576 floor above is for reasoning models, but it must
        # never exceed the active model's completion-token limit. gpt-3.5-turbo
        # caps at 4096, and gpt-4o-mini caps at 16384. Configurable via
        # BMC_AGENT_LLM_MAX_TOKENS_CAP for models that allow more.
        _cap = _resolved_openai_cap(self.config.llm_model)
        effective_max_tokens = min(max(max_tokens, 24576), _cap)
        payload = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": effective_max_tokens,
            "temperature": temperature,
            # Stream the response. Non-streaming requests make httpx's read
            # timeout bound the WHOLE generation (the server sends nothing until
            # it's done), so a long reasoning <think> trace trips ReadTimeout
            # even when the model is working fine. With streaming the read
            # timeout bounds only the gap BETWEEN chunks, which stays small
            # while tokens are flowing.
            "stream": True,
            # Ask the server to emit a trailing usage chunk so we keep the
            # per-call token telemetry (self._add_usage) that the agent layer
            # uses for cost attribution. Endpoints that don't support it simply
            # omit usage; extraction below treats it as best-effort.
            "stream_options": {"include_usage": True},
        }
        # OpenRouter reports the authoritative per-request USD spend in
        # ``usage.cost``, but only when usage accounting is requested.
        # ``stream_options.include_usage`` above is the OpenAI flag for *token*
        # telemetry; it does NOT surface cost. Gated to OpenRouter — a stray
        # ``usage`` body param can 400 plain OpenAI endpoints.
        if _is_openrouter(base):
            payload["usage"] = {"include": True}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "aprover",
            **openrouter_attribution_headers(),
        }

        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise LLMError(
                "The 'httpx' package is required for the openai-compatible provider."
            ) from exc

        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 180.0))
        timeout = httpx.Timeout(timeout_s, connect=10.0)
        # Server-Sent-Events stream: each frame is a "data: {json}" line, the
        # content arrives as incremental ``choices[0].delta.content`` fragments,
        # ``finish_reason`` lands on the final content chunk, and (when
        # stream_options is honoured) a trailing chunk carries ``usage`` with an
        # empty ``choices`` array. The stream ends with "data: [DONE]".
        text_parts: list[str] = []
        finish_reason = None
        usage: dict = {}
        stream_error: str | None = None
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    resp.read()  # body isn't loaded for a streamed response
                    raise LLMError(
                        f"OpenAI-compatible request failed: HTTP {resp.status_code} "
                        f"{resp.reason_phrase}: {resp.text[:500]}"
                    )
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        # Partial/keep-alive frame -- skip rather than abort.
                        continue
                    # Some providers (e.g. OpenRouter) signal a mid-stream failure
                    # as an ``error`` object inside an HTTP-200 SSE body. Capture
                    # it so an empty result fails loudly instead of being parsed
                    # downstream as a benign/default response.
                    if chunk.get("error"):
                        err = chunk["error"]
                        stream_error = err.get("message") if isinstance(err, dict) else str(err)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for ch in chunk.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content")
                        if piece:
                            text_parts.append(piece)
                        if ch.get("finish_reason"):
                            finish_reason = ch["finish_reason"]
        text = "".join(text_parts)

        # An empty stream (dropped connection, no frames, or an error chunk over
        # HTTP 200) must not silently return "" — the old non-streaming path
        # raised here, and the retry loop relies on it firing. Treat as
        # retryable: a fresh re-query commonly recovers a transient drop.
        if not text:
            detail = stream_error or "no content frames received"
            raise LLMRetryableError(
                f"OpenAI-compatible response carried no content ({detail}). "
                f"finish_reason={finish_reason!r}"
            )

        if usage:
            logger.debug(
                "LLM usage (openai): prompt_tokens=%s completion_tokens=%s "
                "total_tokens=%s cost=%s",
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
                usage.get("cost"),
            )
            self._add_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))
            # OpenRouter returns the authoritative per-request USD spend inline as
            # ``usage.cost`` in the trailing chunk (token x price can't be
            # reconstructed locally — OpenRouter routes one model id to different
            # upstream providers/prices and applies cache discounts). Plain OpenAI
            # endpoints omit it, so _add_cost(None) is a no-op there.
            self._add_cost(usage.get("cost"))

        stripped = _strip_reasoning_blocks(text)
        # When a reasoning model burns its whole budget on <think>... and is
        # cut off, finish_reason=='length' and we get no </think> closing
        # tag, so the strip is a no-op and `stripped` is just chain-of-thought.
        # Surface this loudly: the caller would otherwise parse the reasoning
        # text as a spec, which silently corrupts the pipeline.
        if finish_reason == "length" and "</think>" not in text:
            raise LLMTruncatedError(
                "OpenAI-compatible response hit max_tokens before emitting the "
                "final answer (no </think> closing tag). Retrying with more "
                "budget. "
                f"prompt_tokens={usage.get('prompt_tokens')} "
                f"completion_tokens={usage.get('completion_tokens')}"
            )
        return stripped

    def _complete_claude_code(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Shell out to the Claude Code CLI in non-interactive mode.

        Uses ``claude -p`` (--print) with all tools disabled, no session
        persistence, and JSON output. The user prompt is piped via stdin so
        we don't hit ARG_MAX on long C source bodies. Authentication is
        delegated to the local Claude Code install — no API key required
        in bmc-agent's environment.

        ``temperature`` is currently ignored: the Claude Code CLI doesn't
        expose a temperature flag. ``max_tokens`` is also not directly
        configurable per call; the CLI uses the model's default cap.
        """
        import subprocess

        cli = (self.config.claude_code_bin or "claude").strip()
        cmd: list[str] = [cli, "-p"]

        if getattr(self.config, "claude_code_agentic", False):
            # Agentic mode: grant read-only tools so the model can go read
            # caller sites / adjacent code to ground a precondition, scoped to
            # the project directories. ``--permission-mode`` auto-denies
            # anything outside the allowlist (no interactive prompt / hang).
            tools = (getattr(self.config, "claude_code_tools", "") or "Read,Grep,Glob").strip()
            cmd += ["--allowed-tools", tools]
            for d in getattr(self.config, "claude_code_add_dirs", None) or []:
                if d:
                    cmd += ["--add-dir", str(d)]
            cmd += [
                "--permission-mode",
                (getattr(self.config, "claude_code_permission_mode", "") or "bypassPermissions").strip(),
            ]
        else:
            # Text-only mode: zero tools — a one-shot completion identical in
            # shape to the API path.
            cmd += ["--disallowed-tools", "Read Grep Glob Bash Edit Write WebFetch WebSearch"]

        cmd += ["--output-format", "json"]
        # --model is optional; when ``llm_model`` is empty or looks like a
        # non-claude name (e.g. left over from an OpenAI-compatible config), we let the
        # CLI pick the default for the user's session. Also skip provider-prefixed
        # ids like "anthropic/claude-sonnet-4.5" (OpenRouter/LiteLLM form): the
        # claude CLI wants a bare alias ("sonnet"/"opus") or native id, not a
        # path — so when a global OpenRouter model leaks into a claude-code-routed
        # role, fall back to the CLI default instead of passing an invalid id.
        model = (self.config.llm_model or "").strip()
        if (
            model
            and "/" not in model
            and ("claude" in model.lower() or model.lower() in ("sonnet", "opus", "haiku"))
        ):
            cmd += ["--model", model]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]

        # Use the claude-code-specific timeout (default 600s). The API-mode
        # default (~180s from BMC_AGENT_LLM_TIMEOUT_S) is too tight here:
        # ``claude -p`` carries ~5-6k tokens of fixed CLI overhead per call
        # and runs serially, so prompts that legitimately produce thousands
        # of output tokens (reproducer generation, large spec-gen) will
        # blow past 180s on the first try. Fall back to ``llm_request_timeout_s``
        # if the new field is absent (forward-compat with older Config objects).
        timeout_s = float(
            getattr(self.config, "claude_code_timeout_s", None)
            or getattr(self.config, "llm_request_timeout_s", 180.0)
        )
        try:
            proc = subprocess.run(
                cmd,
                input=user_prompt,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude -p timed out after {timeout_s}s") from exc
        except FileNotFoundError as exc:
            raise LLMError(
                f"claude CLI not found at {cli!r}. Install Claude Code or set "
                "BMC_AGENT_CLAUDE_CODE_BIN to its path."
            ) from exc

        if proc.returncode != 0:
            raise LLMError(
                f"claude -p exited {proc.returncode}: "
                f"stderr={proc.stderr[:400]!r} stdout={proc.stdout[:200]!r}"
            )

        stdout = proc.stdout.strip()
        if not stdout:
            raise LLMError("claude -p produced empty stdout")

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # Fall back to treating the raw stdout as the response (e.g. if
            # an older CLI version doesn't honour --output-format json).
            return _strip_reasoning_blocks(stdout)

        if data.get("is_error"):
            raise LLMError(f"claude -p reported is_error: {data.get('result', '')[:400]}")

        usage = data.get("usage") or {}
        if usage:
            logger.debug(
                "LLM usage (claude-code): input_tokens=%s output_tokens=%s "
                "cache_creation=%s cache_read=%s cost_usd=%s",
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("cache_creation_input_tokens"),
                usage.get("cache_read_input_tokens"),
                data.get("total_cost_usd"),
            )
            # Feed the spend meter. Include cache tokens in the prompt count so
            # the "N tok" pill reflects billed volume; USD comes from the CLI's
            # authoritative ``total_cost_usd`` (on ``data``, not ``usage``).
            prompt_tokens = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
            )
            self._add_usage(prompt_tokens, usage.get("output_tokens"))
            self._add_cost(data.get("total_cost_usd"))

        result = data.get("result")
        if not isinstance(result, str):
            raise LLMError(f"claude -p response missing 'result' string: {data}")
        return _strip_reasoning_blocks(result)

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


# ---------------------------------------------------------------------------
# Tool-use foundation
# ---------------------------------------------------------------------------
#
# Multi-turn LLM dialogue with tool dispatch. Used by spec_gen v2.2 and the
# realism check's walk_call_chain extension. Provider-portable in principle
# but currently only the OpenAI-compatible path is wired (OpenRouter+Claude,
# etc.); the Anthropic native path raises a NotImplementedError
# until / unless we need it.
#
# Safety rails are mandatory: max_iterations bounds the LLM round-trips,
# max_tool_calls bounds total tool executions, per-tool-result content is
# truncated, handlers run in-process (no subprocess) with exception capture
# fed back to the LLM as is_error results.


_DEFAULT_TOOL_RESULT_TRUNCATE = 8000


@dataclass
class ToolDef:
    """One tool the LLM may call. Schemas use JSON Schema-style param spec.

    Provider-specific format conversion lives in ``_tools_to_openai_schema``.
    """

    name: str
    description: str
    parameters: dict   # JSON Schema for the tool's arguments


@dataclass
class ToolCall:
    """A single tool invocation parsed from the LLM's response."""

    id: str
    name: str
    arguments: dict


@dataclass
class ToolUseResult:
    """Final result of a multi-turn tool-use dialogue."""

    text: str                           # final assistant text (after all tool calls)
    iterations: int                     # how many LLM round-trips occurred
    tool_calls_made: int                # total successful tool calls
    messages: list                      # full message history for debugging
    error: str = ""                     # non-empty when terminated by a cap or error


def _tools_to_openai_schema(tools: list[ToolDef]) -> list[dict]:
    """Render ToolDef → OpenAI-compatible tools list (used by /v1/chat/completions)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _tools_to_anthropic_schema(tools: "list[ToolDef]") -> "list[dict]":
    """Render ToolDef -> Anthropic Messages API tools list (input_schema form)."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools
    ]


def _add_complete_with_tools_to_llm():
    """Bind ``complete_with_tools`` onto :class:`LLMClient`. Done as a
    separate function rather than inlining inside the class body because
    this file is already large and ``LLMClient`` is far above.
    """

    def complete_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolDef],
        tool_handlers: "dict[str, Callable[[dict], object]]",
        *,
        max_iterations: int = 10,
        max_tool_calls: int = 5,
        max_tokens_per_turn: int = 4096,
        temperature: float = 0.0,
        result_truncate: int = _DEFAULT_TOOL_RESULT_TRUNCATE,
        role: str = "",
    ) -> ToolUseResult:
        """Multi-turn LLM dialogue with tool dispatch.

        The LLM may emit ``tool_calls`` in its response; we execute the
        corresponding handler from ``tool_handlers`` and feed the result
        back as a ``role="tool"`` message. Terminates when the LLM
        emits a non-tool-call response, ``max_iterations`` round-trips
        complete, or ``max_tool_calls`` tool invocations occur.

        Provider routing: currently requires the openai-compatible path
        (OpenRouter+Claude, etc.). The anthropic native path
        raises NotImplementedError. Use a per-role override to ensure
        spec-gen / realism land on the openai path.
        """
        # Trust-boundary note — central injection, mirrors :meth:`complete`.
        system_prompt = system_prompt + render_threat_model_context(self.config, role)

        # Per-role config swap (mirrors :meth:`complete`).
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
            self._client = None

        try:
            provider = self.config.resolved_provider()
            if provider != "openai":
                return self._anthropic_tool_use_loop(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    tool_handlers=tool_handlers,
                    max_iterations=max_iterations,
                    max_tool_calls=max_tool_calls,
                    max_tokens_per_turn=max_tokens_per_turn,
                    temperature=temperature,
                    result_truncate=result_truncate,
                )

            return self._openai_tool_use_loop(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tools,
                tool_handlers=tool_handlers,
                max_iterations=max_iterations,
                max_tool_calls=max_tool_calls,
                max_tokens_per_turn=max_tokens_per_turn,
                temperature=temperature,
                result_truncate=result_truncate,
            )
        finally:
            if saved_settings is not None:
                self.config.llm_model = saved_settings["model"]
                self.config.llm_base_url = saved_settings["base_url"]
                self.config.llm_api_key = saved_settings["api_key"]
                self.config.llm_provider = saved_settings["provider"]
                self._client = saved_settings["client"]

    def _openai_tool_use_loop(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolDef],
        tool_handlers: "dict[str, Callable[[dict], object]]",
        max_iterations: int,
        max_tool_calls: int,
        max_tokens_per_turn: int,
        temperature: float,
        result_truncate: int,
    ) -> ToolUseResult:
        """The actual OpenAI-compatible /v1/chat/completions tool-use loop."""
        api_key = self.config.resolved_api_key()
        if not api_key:
            raise LLMError(
                "No API key for OpenAI-compatible provider. Export "
                "BMC_AGENT_LLM_API_KEY / ANTHROPIC_API_KEY "
                "or configure a per-role override."
            )
        base = (self.config.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        if not base.endswith("/v1") and not base.endswith("/v1/"):
            if "/v1" not in base:
                base = base + "/v1"
        url = base.rstrip("/") + "/chat/completions"

        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise LLMError(
                "The 'httpx' package is required for the openai-compatible provider."
            ) from exc

        tool_schemas = _tools_to_openai_schema(tools)
        # Cache the (stable) system prefix across the tool loop's turns and across
        # calls (Anthropic/OpenRouter). The corpus the agent reads via tools lands
        # in later messages; the conversation grows behind this cached prefix so
        # each turn re-pays only for the new tool results, not the whole prefix.
        messages: list[dict] = [
            _system_msg_with_cache(system_prompt, getattr(self.config, "llm_base_url", "")),
            {"role": "user", "content": user_prompt},
        ]

        tool_calls_made = 0
        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 180.0))
        timeout = httpx.Timeout(timeout_s, connect=10.0)

        for iteration in range(max_iterations):
            payload = {
                "model": self.config.llm_model,
                "messages": messages,
                "max_tokens": max_tokens_per_turn,
                "temperature": temperature,
                "tools": tool_schemas,
                "tool_choice": "auto",
                "stream": False,
            }
            # OpenRouter reports the authoritative per-request USD spend in
            # ``usage.cost``, but only when usage accounting is requested (mirrors
            # the non-tool _complete_openai path). Gated to OpenRouter — a stray
            # ``usage`` body param can 400 plain OpenAI endpoints.
            if _is_openrouter(base):
                payload["usage"] = {"include": True}
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise LLMError(
                    f"Tool-use request failed: HTTP {resp.status_code}: {resp.text[:500]}"
                )
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise LLMError(f"Tool-use response not JSON: {resp.text[:500]}") from exc

            usage = data.get("usage") or {}
            if usage:
                logger.debug(
                    "LLM usage (tool-use iter %d): prompt=%s completion=%s total=%s",
                    iteration + 1,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    usage.get("total_tokens"),
                )
                self._add_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))
                # OpenRouter returns the authoritative per-request USD spend inline
                # as ``usage.cost``; plain OpenAI omits it, so this is a no-op
                # there (same contract as _complete_openai).
                self._add_cost(usage.get("cost"))

            choices = data.get("choices") or []
            if not choices:
                return ToolUseResult(
                    text="", iterations=iteration + 1,
                    tool_calls_made=tool_calls_made, messages=messages,
                    error="no choices in response",
                )
            choice = choices[0]
            msg = choice.get("message") or {}
            messages.append(msg)

            raw_tool_calls = msg.get("tool_calls") or []
            if not raw_tool_calls:
                # Final assistant message — done.
                text = msg.get("content") or ""
                if isinstance(text, list):
                    text = "".join(
                        p.get("text", "")
                        for p in text if isinstance(p, dict)
                    )
                return ToolUseResult(
                    text=_strip_reasoning_blocks(text),
                    iterations=iteration + 1,
                    tool_calls_made=tool_calls_made,
                    messages=messages,
                )

            # Execute each tool call; append a tool result message per call.
            for raw in raw_tool_calls:
                tc_id = raw.get("id", "")
                fn = raw.get("function") or {}
                name = fn.get("name", "")
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except json.JSONDecodeError:
                    args = {}

                if tool_calls_made >= max_tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": (
                            "ERROR: max_tool_calls cap reached. "
                            "Emit a final answer now using the evidence you've gathered."
                        ),
                    })
                    continue

                handler = tool_handlers.get(name)
                if handler is None:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": f"ERROR: tool '{name}' is not registered",
                    })
                    continue

                try:
                    result = handler(args)
                except Exception as exc:  # noqa: BLE001
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": f"ERROR: tool '{name}' raised {type(exc).__name__}: {exc}",
                    })
                    tool_calls_made += 1
                    continue

                if isinstance(result, str):
                    content = result
                else:
                    try:
                        content = json.dumps(result, default=str)
                    except TypeError:
                        content = str(result)
                if len(content) > result_truncate:
                    content = content[:result_truncate] + "\n…[truncated]"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": content,
                })
                tool_calls_made += 1

        return ToolUseResult(
            text="", iterations=max_iterations,
            tool_calls_made=tool_calls_made, messages=messages,
            error=f"max_iterations ({max_iterations}) exceeded",
        )

    def _anthropic_tool_use_loop(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools,
        tool_handlers,
        max_iterations: int,
        max_tool_calls: int,
        max_tokens_per_turn: int,
        temperature: float,
        result_truncate: int,
    ) -> ToolUseResult:
        """Anthropic-native Messages API tool-use loop (mirrors the openai one).

        Tools carry an ``input_schema``; the model returns ``tool_use`` content
        blocks; we run each handler and feed ``tool_result`` blocks back as a
        user turn. Token usage is accumulated into the client telemetry.
        """
        client = self._get_client()
        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 180.0))
        tool_schemas = _tools_to_anthropic_schema(tools)
        messages: list = [{"role": "user", "content": user_prompt}]
        tool_calls_made = 0

        for iteration in range(max_iterations):
            create_kwargs: dict = dict(
                model=self.config.llm_model,
                max_tokens=max_tokens_per_turn,
                system=system_prompt,
                messages=messages,
                tools=tool_schemas,
            )
            if not _model_rejects_temperature(self.config.llm_model):
                create_kwargs["temperature"] = temperature
            try:
                response = client.with_options(timeout=timeout_s).messages.create(**create_kwargs)
            except Exception as exc:
                raise LLMError(f"anthropic tool-use request failed: {exc!r}") from exc

            usage = getattr(response, "usage", None)
            if usage:
                self._add_usage(
                    getattr(usage, "input_tokens", 0),
                    getattr(usage, "output_tokens", 0),
                )

            assistant_blocks: list = []
            tool_use_blocks: list = []
            text_out = ""
            for block in (getattr(response, "content", None) or []):
                btype = getattr(block, "type", None)
                if btype == "text":
                    t = getattr(block, "text", "")
                    text_out += t
                    assistant_blocks.append({"type": "text", "text": t})
                elif btype == "tool_use":
                    tu = {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}) or {},
                    }
                    assistant_blocks.append(tu)
                    tool_use_blocks.append(tu)

            if not tool_use_blocks:
                return ToolUseResult(
                    text=_strip_reasoning_blocks(text_out),
                    iterations=iteration + 1,
                    tool_calls_made=tool_calls_made,
                    messages=messages,
                )

            messages.append({"role": "assistant", "content": assistant_blocks})

            results_content: list = []
            for tu in tool_use_blocks:
                tc_id = tu["id"]
                name = tu["name"]
                args = tu["input"] if isinstance(tu["input"], dict) else {}

                if tool_calls_made >= max_tool_calls:
                    results_content.append({
                        "type": "tool_result",
                        "tool_use_id": tc_id,
                        "content": (
                            "ERROR: max_tool_calls cap reached. Emit a final "
                            "answer now using the evidence you have gathered."
                        ),
                    })
                    continue

                handler = tool_handlers.get(name)
                if handler is None:
                    results_content.append({
                        "type": "tool_result",
                        "tool_use_id": tc_id,
                        "content": f"ERROR: tool {name!r} is not registered",
                    })
                    continue

                try:
                    result = handler(args)
                except Exception as exc:  # noqa: BLE001
                    results_content.append({
                        "type": "tool_result",
                        "tool_use_id": tc_id,
                        "content": f"ERROR: tool {name!r} raised {type(exc).__name__}: {exc}",
                    })
                    tool_calls_made += 1
                    continue

                if isinstance(result, str):
                    content = result
                else:
                    try:
                        content = json.dumps(result, default=str)
                    except TypeError:
                        content = str(result)
                if len(content) > result_truncate:
                    content = content[:result_truncate] + "\n...[truncated]"
                results_content.append({
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": content,
                })
                tool_calls_made += 1

            messages.append({"role": "user", "content": results_content})

        return ToolUseResult(
            text="", iterations=max_iterations,
            tool_calls_made=tool_calls_made, messages=messages,
            error=f"max_iterations ({max_iterations}) exceeded",
        )

    LLMClient.complete_with_tools = complete_with_tools  # type: ignore[attr-defined]
    LLMClient._openai_tool_use_loop = _openai_tool_use_loop  # type: ignore[attr-defined]
    LLMClient._anthropic_tool_use_loop = _anthropic_tool_use_loop  # type: ignore[attr-defined]


_add_complete_with_tools_to_llm()
