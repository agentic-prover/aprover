"""
Central, env-overridable guardrails for the web layer.

Every cap/timeout the web demo enforces lives here so there is ONE place to audit
and tune them. Each is read from a ``BMC_AGENT_WEB_*`` environment variable
(matching the ``BMC_AGENT_*`` convention used by ``bmc_agent.config`` and
``web.estimate``), falling back to a default tuned for **large-repo testing** —
the historical public-demo values were far smaller (file cap 15, 5-minute idle
timeout, 100 MB / 5000-file clone cap) and blocked any real codebase.

Raising these enables large repos; it does NOT make them cheap. The pipeline
spends ~1 spec-gen LLM call + a CBMC run (+ classification per counterexample)
*per function*, so a big repo is hours of wall time and real API spend. The
per-run ``budget_cap`` and ``BMC_AGENT_PER_FUNCTION_TIME_BUDGET_S`` are the
safety valves. Re-tighten any value by exporting the matching env var.
"""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from ``name``; fall back to ``default`` on unset
    or unparseable input (a typo never silently disables a guardrail to 0)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return v if v >= 0 else default


def _env_bytes(name: str, default: int) -> int:
    """Like ``_env_int`` but accepts a plain byte count or a ``<n>MB`` / ``<n>GB``
    / ``<n>KB`` suffix (case-insensitive) for readability in env files."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    s = raw.strip().upper()
    mult = 1
    for suffix, factor in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if s.endswith(suffix):
            s, mult = s[: -len(suffix)].strip(), factor
            break
    try:
        return int(float(s) * mult)
    except (ValueError, AttributeError):
        return default


# --- runner.py: per-run execution -----------------------------------------
# Idle (no-event) ceiling on a single run — resets on every streamed event, so
# an actively-progressing large sweep won't trip it; only a stall (e.g. one CBMC
# call going silent) does. Raised from the demo's 300s.
WALL_TIMEOUT_SEC = _env_int("BMC_AGENT_WEB_WALL_TIMEOUT_SEC", 3600)
# Files actually verified in a directory sweep (verify_tree max_files). Raised
# from the demo's 15 to effectively-unlimited; the clone caps below bound the
# repo size instead. Per-run overridable from the workbench settings.
MAX_VERIFY_FILES = _env_int("BMC_AGENT_WEB_MAX_VERIFY_FILES", 100_000)

# --- gitclone.py: clone admission -----------------------------------------
CLONE_TIMEOUT = _env_int("BMC_AGENT_WEB_CLONE_TIMEOUT", 600)
MAX_REPO_BYTES = _env_bytes("BMC_AGENT_WEB_MAX_REPO_BYTES", 2 * 1024**3)  # 2 GB
MAX_SRC_FILES = _env_int("BMC_AGENT_WEB_MAX_SRC_FILES", 100_000)
LIST_LIMIT = _env_int("BMC_AGENT_WEB_LIST_LIMIT", 100_000)

# --- server.py: local upload + file view ----------------------------------
UPLOAD_MAX_FILES = _env_int("BMC_AGENT_WEB_UPLOAD_MAX_FILES", 100_000)
UPLOAD_MAX_BYTES = _env_bytes("BMC_AGENT_WEB_UPLOAD_MAX_BYTES", 2 * 1024**3)  # 2 GB
FILE_VIEW_BYTES = _env_bytes("BMC_AGENT_WEB_FILE_VIEW_BYTES", 256 * 1024)

# --- sessions.py / jobs.py: lifecycle -------------------------------------
SESSION_TTL = _env_int("BMC_AGENT_WEB_SESSION_TTL", 3600)
MAX_SESSIONS = _env_int("BMC_AGENT_WEB_MAX_SESSIONS", 50)
JOB_TTL = _env_int("BMC_AGENT_WEB_JOB_TTL", 3600)
MAX_JOBS = _env_int("BMC_AGENT_WEB_MAX_JOBS", 60)

# --- tree.py: function-count cache ----------------------------------------
TREE_CACHE = _env_int("BMC_AGENT_WEB_TREE_CACHE", 4000)

# --- options.py: per-run knob ceilings ------------------------------------
# Upper bounds the workbench "Run settings" panel is clamped to, server-side, so
# a public visitor can't request a run that pins the host (a 1000x loop unwind,
# an hour-long per-call timeout, a 64-worker fan-out, ...). Each clamps the
# matching ``Config`` knob in ``web.options.parse_options``; clamping is
# silent-and-continue (an out-of-range value is pulled to the ceiling, never an
# error) so a typo can't disable a run. Tight by default for the public BYOK
# demo — a self-host that wants the full CLI range raises any of them by
# exporting the matching ``BMC_AGENT_WEB_MAX_*`` var (the per-function budget +
# the per-run ``budget_cap`` + ``WALL_TIMEOUT_SEC`` remain the real backstops).
MAX_CBMC_TIMEOUT = _env_int("BMC_AGENT_WEB_MAX_CBMC_TIMEOUT", 120)
MAX_CBMC_UNWIND = _env_int("BMC_AGENT_WEB_MAX_CBMC_UNWIND", 16)
MAX_CBMC_OBJECT_BITS = _env_int("BMC_AGENT_WEB_MAX_CBMC_OBJECT_BITS", 16)
MAX_PER_FN_BUDGET_S = _env_int("BMC_AGENT_WEB_MAX_PER_FN_BUDGET_S", 1800)
MAX_WORKERS = _env_int("BMC_AGENT_WEB_MAX_WORKERS", 16)
MAX_REFINEMENT_ITERS = _env_int("BMC_AGENT_WEB_MAX_REFINEMENT_ITERS", 8)
MAX_SPEC_RETRIES = _env_int("BMC_AGENT_WEB_MAX_SPEC_RETRIES", 10)
MAX_DEDUP_PER_TYPE = _env_int("BMC_AGENT_WEB_MAX_DEDUP_PER_TYPE", 8)
MAX_SCALE_DOWN_SIZE = _env_int("BMC_AGENT_WEB_MAX_SCALE_DOWN_SIZE", 64)
MAX_ARRAY_PARAM_BOUNDS = _env_int("BMC_AGENT_WEB_MAX_ARRAY_PARAM_BOUNDS", 256)
MAX_STANDALONE_UNWIND = _env_int("BMC_AGENT_WEB_MAX_STANDALONE_UNWIND", 128)
MAX_AGENTIC_REFINE_ROUNDS = _env_int("BMC_AGENT_WEB_MAX_AGENTIC_REFINE_ROUNDS", 5)
MAX_AUTO_ROUNDS = _env_int("BMC_AGENT_WEB_MAX_AUTO_ROUNDS", 10)
MAX_THREAT_CONTEXT_CHARS = _env_int("BMC_AGENT_WEB_MAX_THREAT_CONTEXT_CHARS", 8000)
MAX_CBMC_DEFINES = _env_int("BMC_AGENT_WEB_MAX_CBMC_DEFINES", 32)
