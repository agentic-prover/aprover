"""
Per-run verification knobs from the workbench "Run settings" panel.

The browser sends an ``options`` object in the ``/api/run`` and ``/api/estimate``
bodies mirroring the CLI's ``verify`` flags, grouped into
``depth / ai_layers / agentic / harness / spec_mode / threat`` plus a
top-level ``run_mode``. This module is the single place that turns that untrusted
JSON into a validated, **clamped** dict the runner overlays onto ``Config``
(``web.runner._make_config``) and the estimator reads (``web.estimate``). BYOK
secrets never travel here — they stay in headers; per-role routing carries only
model/base_url/provider and reuses the single header key.

Two guarantees the rest of the web layer relies on:

* **Clamp, never trust.** Every resource knob (unwind, timeouts, per-function
  budget, worker count, ...) is clamped to an env-raisable ceiling in
  ``web.limits`` so a hand-crafted body can't pin the host. Clamping is
  silent-and-continue (a bad value is clamped or dropped, never an error) so a
  typo can't disable a run — the same discipline as ``limits._env_int``.
* **Absent means default.** Only keys the browser actually sent survive, so the
  runner can tell "user left it alone" (→ the Config/CLI default) from "user
  changed it". The runner starts from ``Config.from_env()`` and overlays whatever
  this returns; the estimator defaults the same way.

Field names inside each group match ``Config`` attributes exactly, so the runner
overlays the bool/int groups with a guarded ``setattr``. Web-only keys that don't
map 1:1 (``spec_mode.math_ints``, ``agentic.llm.roles``, ``run_mode``) are handled
explicitly by the runner.
"""
from __future__ import annotations

import re

from bmc_agent.agent_registry import AGENT_ROLES
from web import limits


# Boolean Config knobs, grouped exactly as the request body groups them. Every
# name is a real Config field (the runner guards with ``hasattr`` regardless).
_BOOL_FIELDS = {
    "ai_layers": (
        "enable_realism_check", "enable_realism_thinking",
        "enable_dynamic_validation", "enable_reproducer_agent",
        "enable_flag_selection", "enable_bmc_config_agent",
        "enable_feedback_loop", "enable_spec_refiner", "enable_spec_strengthen",
        "enable_inlining_advisor", "enable_spec_gen_tools", "enable_realism_tools",
        "enable_global_invariants", "enable_soundness_gate",
        "soundness_gate_fail_closed", "enforce_spec_refiner_retier",
        "enable_oracle_disagreement_diagnosis", "enable_phase_3e_triage",
    ),
    "harness": (
        "cbmc_real_libc", "raw_bytes", "strict_dsl", "infer_field_validity",
        "infer_struct_field_validity", "infer_array_param_bounds",
        "scale_down", "safety_only", "enable_string_copy_source_modeling",
        "lite_mode",
    ),
    "agentic": (
        "claude_code_agentic", "enable_agentic_harness",
        "enable_agentic_harness_repair", "enable_split_spec_gen",
    ),
}

# Integer Config knobs → the ``limits`` ceiling each is clamped to (to [0, ceil]).
# An unparseable value drops the key (→ the runner/estimator default).
_INT_FIELDS = {
    "depth": {
        "cbmc_timeout": "MAX_CBMC_TIMEOUT",
        "cbmc_unwind": "MAX_CBMC_UNWIND",
        "cbmc_object_bits": "MAX_CBMC_OBJECT_BITS",
        "per_function_time_budget_s": "MAX_PER_FN_BUDGET_S",
        "max_workers": "MAX_WORKERS",
        "max_refinement_iters": "MAX_REFINEMENT_ITERS",
        "max_spec_retries": "MAX_SPEC_RETRIES",
        "dedup_max_per_type": "MAX_DEDUP_PER_TYPE",
    },
    "harness": {
        "infer_array_param_bounds_max": "MAX_ARRAY_PARAM_BOUNDS",
        "scale_down_size": "MAX_SCALE_DOWN_SIZE",
    },
    "agentic": {
        "agentic_refine_rounds": "MAX_AGENTIC_REFINE_ROUNDS",
    },
    "autonomous": {
        "max_rounds": "MAX_AUTO_ROUNDS",
    },
}

_ENUMS = {
    "threat": {"threat_model": ("security", "safety", "functional")},
}

_RUN_MODES = ("verify", "autonomous")
_ROLE_PROVIDERS = ("anthropic", "openai", "claude-code")
# A preprocessor define: NAME or NAME=VALUE, value restricted to word / path-ish
# chars so nothing shell- or arg-injectable reaches CBMC's ``-D``.
_DEFINE_RE = re.compile(r"^[A-Za-z_]\w*(=[\w./-]+)?$")


# --- scalar coercion -------------------------------------------------------

def _as_bool(v):
    """Coerce a JSON value to bool; return None (→ drop the key) if it isn't a
    recognizable bool. A real ``False`` is kept (turning a default-on layer off
    is meaningful)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return None


def _as_int_clamped(v, ceiling):
    """Coerce to a non-negative int clamped to ``ceiling``; None (→ drop) if
    unparseable."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n < 0:
        n = 0
    if ceiling is not None and n > ceiling:
        n = ceiling
    return n


# --- group collectors ------------------------------------------------------

def _collect_bools(raw_group, names):
    out: dict = {}
    if not isinstance(raw_group, dict):
        return out
    for name in names:
        if name in raw_group:
            b = _as_bool(raw_group[name])
            if b is not None:
                out[name] = b
    return out


def _collect_ints(raw_group, spec):
    out: dict = {}
    if not isinstance(raw_group, dict):
        return out
    for name, ceil_attr in spec.items():
        if name in raw_group:
            n = _as_int_clamped(raw_group[name], getattr(limits, ceil_attr))
            if n is not None:
                out[name] = n
    return out


def _collect_enums(raw_group, spec):
    out: dict = {}
    if not isinstance(raw_group, dict):
        return out
    for name, allowed in spec.items():
        v = raw_group.get(name)
        if isinstance(v, str) and v in allowed:
            out[name] = v
    return out


def _parse_defines(v):
    """Sanitize ``-D`` defines: NAME[=VALUE], strict charset, count-capped."""
    if not isinstance(v, list):
        return []
    out = []
    for item in v[: limits.MAX_CBMC_DEFINES]:
        if isinstance(item, str) and _DEFINE_RE.match(item.strip()):
            out.append(item.strip())
    return out


def _parse_roles(raw_llm):
    """Per-role model/base_url/provider overrides (non-secret; the BYOK key is
    injected server-side in ``_make_config``). Unknown roles / providers and
    empty specs are dropped."""
    if not isinstance(raw_llm, dict):
        return {}
    raw_roles = raw_llm.get("roles")
    if not isinstance(raw_roles, dict):
        return {}
    out: dict = {}
    for role, spec in raw_roles.items():
        if role not in AGENT_ROLES or not isinstance(spec, dict):
            continue
        clean: dict = {}
        for k in ("model", "base_url", "provider"):
            val = spec.get(k)
            if isinstance(val, str) and val.strip():
                clean[k] = val.strip()
        if clean.get("provider") and clean["provider"] not in _ROLE_PROVIDERS:
            del clean["provider"]
        if clean:
            out[role] = clean
    return out


def _parse_spec_mode(raw_group):
    """Only ``math_ints`` is honored by the web runner. The CLI-only synthesis
    knobs (mode / entry / no_overflow_rigor) run a different code path than
    ``pipeline.run()`` and are not wired into the web flow, so they're dropped."""
    if not isinstance(raw_group, dict):
        return {}
    out: dict = {}
    if "math_ints" in raw_group:
        b = _as_bool(raw_group["math_ints"])
        if b is not None:
            out["math_ints"] = b
    return out


def parse_options(raw):
    """Validate + clamp the request's ``options`` object.

    Returns a grouped dict of only the knobs the browser actually sent (absent ⇒
    runner/estimator default). Never raises — a malformed body yields ``{}``.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}

    depth = _collect_ints(raw.get("depth"), _INT_FIELDS["depth"])
    if depth:
        out["depth"] = depth

    ai = _collect_bools(raw.get("ai_layers"), _BOOL_FIELDS["ai_layers"])
    if ai:
        out["ai_layers"] = ai

    harness = _collect_bools(raw.get("harness"), _BOOL_FIELDS["harness"])
    harness.update(_collect_ints(raw.get("harness"), _INT_FIELDS["harness"]))
    defines = _parse_defines((raw.get("harness") or {}).get("cbmc_defines"))
    if defines:
        harness["cbmc_defines"] = defines
    if harness:
        out["harness"] = harness

    agentic = _collect_bools(raw.get("agentic"), _BOOL_FIELDS["agentic"])
    agentic.update(_collect_ints(raw.get("agentic"), _INT_FIELDS["agentic"]))
    roles = _parse_roles((raw.get("agentic") or {}).get("llm"))
    if roles:
        agentic["llm"] = {"roles": roles}
    if agentic:
        out["agentic"] = agentic

    spec_mode = _parse_spec_mode(raw.get("spec_mode"))
    if spec_mode:
        out["spec_mode"] = spec_mode

    threat = _collect_enums(raw.get("threat"), _ENUMS["threat"])
    ctx = (raw.get("threat") or {}).get("threat_model_context")
    if isinstance(ctx, str) and ctx.strip():
        threat["threat_model_context"] = ctx.strip()[: limits.MAX_THREAT_CONTEXT_CHARS]
    if threat:
        out["threat"] = threat

    auto = _collect_ints(raw.get("autonomous"), _INT_FIELDS["autonomous"])
    if auto:
        out["autonomous"] = auto

    rm = raw.get("run_mode")
    if isinstance(rm, str) and rm in _RUN_MODES:
        out["run_mode"] = rm

    return out
