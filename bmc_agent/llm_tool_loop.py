"""Provider-agnostic tool-use LLM client.

Extracted from llm_judge.py so multiple modules (judge, agentic harness
gen, future agents) can share one Anthropic+OpenAI translation layer
without duplicating ~250 lines of provider plumbing.

Public surface:
    LLMToolClient(config, tools_schema, role="realism")
        .call(messages, tool_choice="auto", max_tokens=16384) -> dict

The returned dict is always in OpenAI shape:
    {"choices": [{"message": {"content": str|None,
                              "tool_calls": [{"id","function":{"name","arguments"}}]|None},
                  "finish_reason": str|None}],
     "usage": {"prompt_tokens": int, "completion_tokens": int}}

messages[] are also OpenAI shape (role=system|user|assistant|tool,
content + tool_calls/tool_call_id). The Anthropic path translates to/from
the Messages API internally so callers stay provider-agnostic.
"""

from __future__ import annotations

import json
from typing import Optional

from bmc_agent.config import Config
from bmc_agent.logger import get_logger

logger = get_logger("llm_tool_loop")


class LLMToolClient:
    def __init__(
        self,
        config: Config,
        tools_schema: list[dict],
        role: str = "realism",
    ) -> None:
        self.config = config
        self.tools_schema = list(tools_schema or [])
        self.role = role

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def call(
        self,
        messages: list[dict],
        tool_choice="auto",
        max_tokens: int = 16384,
    ) -> dict:
        rs = self.config.role_settings(self.role)
        provider = (
            rs.get("provider")
            or getattr(self.config, "llm_provider", "")
            or self.config.resolved_provider()
        )
        if (provider or "").lower() == "anthropic":
            return self._call_anthropic(messages, tool_choice, rs, max_tokens)
        return self._call_openai(messages, tool_choice, rs, max_tokens)

    # ------------------------------------------------------------------
    # OpenAI-compatible /chat/completions
    # ------------------------------------------------------------------

    def _call_openai(
        self, messages: list[dict], tool_choice, rs: dict, max_tokens: int,
    ) -> dict:
        api_key = rs.get("api_key") or self.config.resolved_api_key()
        base_url = (
            rs.get("base_url")
            or self.config.llm_base_url
            or "https://openrouter.ai/api/v1"
        )
        model = rs.get("model") or self.config.llm_model
        base = base_url.rstrip("/")
        if not base.endswith("/v1") and not base.endswith("/v1/"):
            if "/v1" not in base:
                base = base + "/v1"
        url = base.rstrip("/") + "/chat/completions"

        payload = {
            "model": model,
            "messages": messages,
            "tools": self.tools_schema,
            "tool_choice": tool_choice,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }

        try:
            import httpx  # type: ignore
        except ImportError as exc:
            raise RuntimeError("httpx required for llm_tool_loop") from exc

        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 600.0))
        timeout = httpx.Timeout(timeout_s, connect=15.0)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"LLMToolClient HTTP {resp.status_code}: {resp.text[:600]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"LLMToolClient non-JSON response: {resp.text[:600]}"
            ) from exc
        usage = data.get("usage") or {}
        logger.info(
            "LLM turn (openai): prompt_tokens=%s completion_tokens=%s role=%s",
            usage.get("prompt_tokens"), usage.get("completion_tokens"), self.role,
        )
        return data

    # ------------------------------------------------------------------
    # Anthropic native Messages API
    # ------------------------------------------------------------------

    def _call_anthropic(
        self, messages: list[dict], tool_choice, rs: dict, max_tokens: int,
    ) -> dict:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "anthropic package required for the anthropic provider path"
            ) from exc

        api_key = rs.get("api_key") or self.config.resolved_api_key()
        base_url = rs.get("base_url") or self.config.llm_base_url or ""
        model = rs.get("model") or self.config.llm_model

        client_kwargs: dict = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = anthropic.Anthropic(**client_kwargs)

        # Translate tools schema (OpenAI → Anthropic).
        a_tools = []
        for t in self.tools_schema:
            fn = (t or {}).get("function") or {}
            a_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {
                    "type": "object", "properties": {},
                },
            })
        if a_tools:
            a_tools[-1] = dict(a_tools[-1])
            a_tools[-1]["cache_control"] = {"type": "ephemeral"}

        # Translate tool_choice.
        if tool_choice == "auto":
            a_tool_choice = {"type": "auto"}
        elif isinstance(tool_choice, dict):
            forced_name = (
                (tool_choice.get("function") or {}).get("name")
                or tool_choice.get("name")
            )
            a_tool_choice = (
                {"type": "tool", "name": forced_name}
                if forced_name else {"type": "auto"}
            )
        else:
            a_tool_choice = {"type": "auto"}

        # Translate messages: system → top-level, tool → user/tool_result
        # batches, assistant tool_calls → tool_use blocks.
        system_text = ""
        a_messages: list[dict] = []
        pending_tool_results: list[dict] = []

        def flush_pending():
            if pending_tool_results:
                a_messages.append({
                    "role": "user",
                    "content": list(pending_tool_results),
                })
                pending_tool_results.clear()

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                txt = msg.get("content") or ""
                if isinstance(txt, list):
                    txt = "".join(
                        b.get("text", "") for b in txt if isinstance(b, dict)
                    )
                system_text = (
                    system_text + ("\n\n" if system_text else "") + str(txt)
                )
                continue

            if role == "tool":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id") or "",
                    "content": str(msg.get("content") or ""),
                })
                continue

            flush_pending()

            if role == "user":
                content_str = str(msg.get("content") or "")
                # Cache the first user message (typically the rich initial
                # context — stable across all turns of one tool-use loop).
                first_user_so_far = not any(
                    m.get("role") == "user" and isinstance(m.get("content"), list)
                    and any(
                        b.get("cache_control")
                        for b in m["content"] if isinstance(b, dict)
                    )
                    for m in a_messages
                )
                if first_user_so_far:
                    a_messages.append({
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": content_str,
                            "cache_control": {"type": "ephemeral"},
                        }],
                    })
                else:
                    a_messages.append({"role": "user", "content": content_str})
                continue

            if role == "assistant":
                blocks: list[dict] = []
                content = msg.get("content")
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in (msg.get("tool_calls") or []) or []:
                    fn = (tc.get("function") or {})
                    try:
                        tc_input = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        tc_input = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name", ""),
                        "input": tc_input,
                    })
                if not blocks:
                    blocks.append({"type": "text", "text": "(continuing)"})
                a_messages.append({"role": "assistant", "content": blocks})
                continue

        flush_pending()

        if not a_messages or a_messages[0]["role"] != "user":
            a_messages.insert(0, {"role": "user", "content": "(begin)"})

        timeout_s = float(getattr(self.config, "llm_request_timeout_s", 600.0))
        system_payload = [{
            "type": "text",
            "text": system_text or "You are a helpful assistant.",
            "cache_control": {"type": "ephemeral"},
        }]
        try:
            response = client.with_options(timeout=timeout_s).messages.create(
                model=model,
                system=system_payload,
                messages=a_messages,
                tools=a_tools,
                tool_choice=a_tool_choice,
                max_tokens=max_tokens,
                temperature=0.2,
            )
        except anthropic.APIError as exc:
            raise RuntimeError(f"anthropic API error: {exc}") from exc

        # Translate Anthropic response back to OpenAI shape.
        out_text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in response.content or []:
            btype = (
                getattr(block, "type", None)
                or (block.get("type") if isinstance(block, dict) else None)
            )
            if btype == "text":
                txt = (
                    getattr(block, "text", None)
                    or (block.get("text") if isinstance(block, dict) else "")
                )
                out_text_parts.append(str(txt))
            elif btype == "tool_use":
                tu_id = (
                    getattr(block, "id", None)
                    or (block.get("id") if isinstance(block, dict) else "")
                )
                tu_name = (
                    getattr(block, "name", None)
                    or (block.get("name") if isinstance(block, dict) else "")
                )
                tu_input = getattr(block, "input", None)
                if tu_input is None and isinstance(block, dict):
                    tu_input = block.get("input", {})
                try:
                    args_json = json.dumps(tu_input or {})
                except (TypeError, ValueError):
                    args_json = "{}"
                tool_calls.append({
                    "id": tu_id,
                    "type": "function",
                    "function": {"name": tu_name, "arguments": args_json},
                })

        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            logger.info(
                "LLM turn (anthropic, role=%s): input=%s output=%s "
                "cache_write=%s cache_hit=%s tool_calls=%d",
                self.role, in_tok, out_tok, cache_create, cache_read,
                len(tool_calls),
            )

        return {
            "choices": [{
                "message": {
                    "content": "".join(out_text_parts) or None,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": getattr(response, "stop_reason", None),
            }],
            "usage": {
                "prompt_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            },
        }
