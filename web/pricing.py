"""
Model presets + token→dollar estimation for the workbench.

``bmc_agent`` stays provider/price agnostic: the pipeline only reports token
counts (``LLMClient.usage_total_*``). This module turns a token snapshot into
an approximate USD figure for the live spend pill and the budget cap, and is the
single source of truth for the **model presets** the Settings dropdown offers.

There are two price surfaces here:

* ``MODEL_PRESETS`` — the curated per-provider dropdown list. Each preset
  carries an explicit ``(input, output)`` $/Mtok price (or ``None`` when we
  don't have an authoritative figure). The **pre-run estimate** prices only via
  these exact entries (``preset_price``): a known preset gets a dollar range, a
  *custom* (free-text) model gets tokens only — never a guessed figure.
* ``_PRICES`` — a coarse substring fallback table used by the **live spend
  meter** (``estimate_usd``), which must show *something* for whatever model id
  actually ran, including custom ones. Deliberately approximate; WILL drift.

Prices are USD per **million** tokens (input, output). Override the substring
table for self-hosted endpoints via ``BMC_AGENT_PRICE_IN`` / ``_OUT``.
"""
from __future__ import annotations

import os
import threading
import time

# ---------------------------------------------------------------------------
# Model presets — the Settings dropdown + the basis for the pre-run estimate.
# Keyed by the workbench provider id (see PROVIDERS in workbench.js). Each entry:
#   {id, label, input $/Mtok, output $/Mtok, default?, free?}
# input/output == None means "no authoritative price" → estimate shows tokens
# only for that preset. The "Custom…" option is added by the frontend, not here.
# ---------------------------------------------------------------------------
MODEL_PRESETS: dict[str, list[dict]] = {
    "anthropic": [
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "input": 3.0, "output": 15.0, "default": True},
        {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "input": 5.0, "output": 25.0},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "input": 1.0, "output": 5.0},
        {"id": "claude-fable-5", "label": "Claude Fable 5", "input": 10.0, "output": 50.0},
    ],
    # Prices here are an offline *fallback*; the live OpenRouter models API
    # (fetch_openrouter_prices) overrides them on every page load, so a None or
    # stale figure self-heals as long as the endpoint is reachable.
    "openrouter": [
        {"id": "anthropic/claude-sonnet-4.6", "label": "Claude Sonnet 4.6", "input": 3.0, "output": 15.0, "default": True},
        {"id": "anthropic/claude-opus-4.8", "label": "Claude Opus 4.8", "input": 5.0, "output": 25.0},
        {"id": "openai/gpt-5.5", "label": "GPT-5.5", "input": 5.0, "output": 30.0},
        # OpenRouter has no "gpt-5.5-mini"; gpt-5.4-mini is the current GPT-5 mini.
        {"id": "openai/gpt-5.4-mini", "label": "GPT-5.4 mini", "input": 0.75, "output": 4.5},
        {"id": "z-ai/glm-5.2", "label": "GLM 5.2", "input": 0.95, "output": 3.0},
        {"id": "deepseek/deepseek-v4-flash", "label": "DeepSeek V4 Flash", "input": 0.09, "output": 0.22},
        {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro", "input": 0.44, "output": 0.87},
    ],
    "openai": [
        {"id": "gpt-5.5", "label": "GPT-5.5", "input": None, "output": None},                 # TODO: confirm price
        {"id": "gpt-4o", "label": "GPT-4o", "input": 2.5, "output": 10.0, "default": True},
        {"id": "gpt-4o-mini", "label": "GPT-4o mini", "input": 0.15, "output": 0.60},
    ],
}

# Exact model-id → (input, output) $/Mtok, built from the presets. First entry
# for an id wins (ids are unique per provider but may repeat across providers
# with identical prices, so collisions are harmless).
_PRESET_PRICE: dict[str, tuple[float, float] | None] = {}
for _provider_presets in MODEL_PRESETS.values():
    for _p in _provider_presets:
        if _p["id"] in _PRESET_PRICE:
            continue
        if _p.get("input") is None or _p.get("output") is None:
            _PRESET_PRICE[_p["id"]] = None
        else:
            _PRESET_PRICE[_p["id"]] = (float(_p["input"]), float(_p["output"]))


# ---------------------------------------------------------------------------
# Live OpenRouter prices. OpenRouter publishes authoritative per-token prices
# at GET /api/v1/models (public, no auth). We pull them lazily, cache for a TTL,
# and let them override the static fallback above — so both the Settings UI and
# the pre-run estimate price OpenRouter models (presets *and* custom ids) from
# the source of truth instead of a hand-maintained table that drifts.
# ---------------------------------------------------------------------------
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OR_TTL_SECONDS = 900.0  # 15 min
_or_lock = threading.Lock()
_or_cache: dict[str, tuple[float, float]] = {}
_or_fetched_at: float = 0.0


def fetch_openrouter_prices() -> dict[str, tuple[float, float]]:
    """Refresh (network, TTL-throttled) and return ``{id: (input, output)}`` $/Mtok.

    Called by the ``/api/models`` endpoint to *warm* the cache; the price-lookup
    functions read the cache without touching the network so they stay fast and
    offline-safe. Never raises: on any network/parse failure it returns the last
    good cache (empty on a cold miss)."""
    global _or_fetched_at
    now = time.time()
    with _or_lock:
        if _or_cache and (now - _or_fetched_at) < _OR_TTL_SECONDS:
            return dict(_or_cache)
    try:
        import httpx

        resp = httpx.get(_OPENROUTER_MODELS_URL, timeout=8.0)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        prices: dict[str, tuple[float, float]] = {}
        for entry in data:
            mid = (entry.get("id") or "").strip()
            entry_pricing = entry.get("pricing") or {}
            try:
                # OpenRouter quotes USD *per token* as strings → $/Mtok.
                pin = float(entry_pricing.get("prompt")) * 1_000_000.0
                pout = float(entry_pricing.get("completion")) * 1_000_000.0
            except (TypeError, ValueError):
                continue
            if not mid or pin < 0 or pout < 0:
                continue
            prices[mid] = (pin, pout)
        if prices:
            with _or_lock:
                _or_cache.clear()
                _or_cache.update(prices)
                _or_fetched_at = now
            return dict(prices)
    except Exception:
        pass
    with _or_lock:
        return dict(_or_cache)


def openrouter_price(model: str) -> tuple[float, float] | None:
    """Cached live OpenRouter price for an exact model id (no fetch), or None.

    Reads only what ``fetch_openrouter_prices`` last warmed — so the estimate and
    spend meter never block on the network, and stay correct offline."""
    with _or_lock:
        return _or_cache.get((model or "").strip())


def openrouter_price_map() -> dict[str, tuple[float, float]]:
    """The full OpenRouter id→(input, output) map, refreshing first (for the UI)."""
    return fetch_openrouter_prices()


def presets_with_live_prices() -> dict[str, list[dict]]:
    """A copy of MODEL_PRESETS with OpenRouter entries' prices refreshed live.

    Falls back to the static fallback price when a preset id has no live entry
    (e.g. the endpoint is unreachable)."""
    live = fetch_openrouter_prices()
    out: dict[str, list[dict]] = {}
    for provider, presets in MODEL_PRESETS.items():
        rows = []
        for p in presets:
            row = dict(p)
            if provider == "openrouter":
                hit = live.get(row["id"])
                if hit is not None:
                    row["input"], row["output"] = round(hit[0], 4), round(hit[1], 4)
            rows.append(row)
        out[provider] = rows
    return out


def preset_price(model: str) -> tuple[float, float] | None:
    """Return (input, output) $/Mtok for an **exact** preset model id.

    Prefers the live OpenRouter price when available, then the static preset
    table. Returns ``None`` when the id isn't a priced preset (unknown id, or a
    preset we have no authoritative price for). The pre-run estimate uses only
    this — so a custom/free-text model id yields no dollar figure, by design."""
    mid = (model or "").strip()
    live = openrouter_price(mid)
    if live is not None:
        return live
    return _PRESET_PRICE.get(mid)


# (input $/Mtok, output $/Mtok), matched by substring against the model id.
# Order matters: first match wins, so list more specific keys first. This is the
# coarse fallback for the live spend meter only.
_PRICES: list[tuple[str, tuple[float, float]]] = [
    ("claude-opus", (5.0, 25.0)),
    ("opus", (5.0, 25.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("sonnet", (3.0, 15.0)),
    ("claude-haiku", (1.0, 5.0)),
    ("haiku", (1.0, 5.0)),
    ("fable", (10.0, 50.0)),
    ("gpt-4o-mini", (0.15, 0.60)),
    ("gpt-4o", (2.5, 10.0)),
    ("gpt-4.1", (2.0, 8.0)),
    ("o3", (2.0, 8.0)),
    # Common non-Anthropic OpenRouter families, so the live meter still shows a
    # figure when the provider gives no exact cost. Rough public list prices.
    ("kimi", (0.6, 2.5)),
    ("deepseek", (0.5, 2.2)),
    ("qwen", (0.4, 1.2)),
    ("llama", (0.2, 0.8)),
    ("gemini", (1.25, 5.0)),
]


def price_for_model(model: str) -> tuple[float, float] | None:
    """Return (input, output) $/Mtok for ``model``, or None if unknown.

    Exact presets win; otherwise the coarse substring table is consulted. An
    explicit ``BMC_AGENT_PRICE_IN`` / ``BMC_AGENT_PRICE_OUT`` env pair overrides
    everything (useful for self-hosted endpoints)."""
    env_in = os.environ.get("BMC_AGENT_PRICE_IN", "")
    env_out = os.environ.get("BMC_AGENT_PRICE_OUT", "")
    if env_in and env_out:
        try:
            return (float(env_in), float(env_out))
        except ValueError:
            pass
    exact = preset_price(model)
    if exact is not None:
        return exact
    m = (model or "").lower()
    # OpenRouter ids are namespaced ("anthropic/claude-sonnet-4.5",
    # "deepseek/deepseek-chat"); match on the bare model name after the prefix.
    m = m.rsplit("/", 1)[-1]
    # Coarse substring fallback only — exact OpenRouter ids are already handled
    # by preset_price above (which consults the live cache).
    for key, price in _PRICES:
        if key in m:
            return price
    return None


def estimate_usd(cost: dict) -> float | None:
    """Estimate spend in USD from a pipeline cost snapshot.

    ``cost`` is the dict emitted by ``AMCPipeline._cost_snapshot``:
    ``{prompt_tokens, completion_tokens, total_tokens, model}``.
    Returns None when the model is unpriced (caller shows tokens only)."""
    if not isinstance(cost, dict):
        return None
    price = price_for_model(cost.get("model", "") or "")
    if price is None:
        return None
    pin, pout = price
    prompt = int(cost.get("prompt_tokens", 0) or 0)
    completion = int(cost.get("completion_tokens", 0) or 0)
    return round((prompt * pin + completion * pout) / 1_000_000.0, 4)
