"""Tests for the env-overridable web guardrails (``web.limits``).

The web layer's caps/timeouts default to large-repo-friendly values and can be
re-tightened per deployment via ``BMC_AGENT_WEB_*`` env vars. These tests pin the
override contract (int + byte-suffix parsing, safe fallback) rather than the
default magnitudes, which are tuning knobs.
"""
from __future__ import annotations

import importlib


def _reload_with(monkeypatch, **env) -> object:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import web.limits as limits
    return importlib.reload(limits)


def _reload_clean(monkeypatch, L, *names):
    """Drop the given override env vars and reload ``L`` to a clean module, so its
    MAX_* attrs don't leak into later (cross-file) tests that read them at import."""
    for n in names:
        monkeypatch.delenv(n, raising=False)
    importlib.reload(L)


def test_int_override(monkeypatch):
    try:
        L = _reload_with(monkeypatch, BMC_AGENT_WEB_MAX_VERIFY_FILES="7",
                         BMC_AGENT_WEB_WALL_TIMEOUT_SEC="99")
        assert L.MAX_VERIFY_FILES == 7
        assert L.WALL_TIMEOUT_SEC == 99
    finally:
        _reload_clean(monkeypatch, L, "BMC_AGENT_WEB_MAX_VERIFY_FILES",
                      "BMC_AGENT_WEB_WALL_TIMEOUT_SEC")


def test_byte_suffix_override(monkeypatch):
    try:
        L = _reload_with(monkeypatch, BMC_AGENT_WEB_MAX_REPO_BYTES="500MB",
                         BMC_AGENT_WEB_UPLOAD_MAX_BYTES="2GB")
        assert L.MAX_REPO_BYTES == 500 * 1024 * 1024
        assert L.UPLOAD_MAX_BYTES == 2 * 1024 ** 3
    finally:
        _reload_clean(monkeypatch, L, "BMC_AGENT_WEB_MAX_REPO_BYTES",
                      "BMC_AGENT_WEB_UPLOAD_MAX_BYTES")


def test_bad_value_falls_back_to_default(monkeypatch):
    # A typo must never silently disable a guardrail (e.g. to 0).
    try:
        L = _reload_with(monkeypatch, BMC_AGENT_WEB_MAX_SRC_FILES="notanumber")
        assert L.MAX_SRC_FILES == 100_000
    finally:
        _reload_clean(monkeypatch, L, "BMC_AGENT_WEB_MAX_SRC_FILES")


def test_defaults_are_large_repo_friendly(monkeypatch):
    for var in ("BMC_AGENT_WEB_MAX_VERIFY_FILES", "BMC_AGENT_WEB_WALL_TIMEOUT_SEC",
                "BMC_AGENT_WEB_MAX_SRC_FILES", "BMC_AGENT_WEB_MAX_REPO_BYTES"):
        monkeypatch.delenv(var, raising=False)
    import web.limits as limits
    L = importlib.reload(limits)
    # Far above the old public-demo throttles (15 files / 300s / 5000 / 100 MB).
    assert L.MAX_VERIFY_FILES >= 100_000
    assert L.WALL_TIMEOUT_SEC >= 3600
    assert L.MAX_SRC_FILES >= 100_000
    assert L.MAX_REPO_BYTES >= 1024 ** 3


def test_estimate_reports_configured_cap(monkeypatch, tmp_path):
    # estimate_scope surfaces the per-run cap override (used by the confirm
    # dialog's "verifies first N of M" warning).
    src = tmp_path / "demo.c"
    src.write_text("int add(int a, int b) { return a + b; }\n")
    from web import estimate
    est = estimate.estimate_scope(src, is_dir=False,
                                  llm={"model": "claude-sonnet-4-6"}, max_files=42)
    assert est["max_files"] == 42


# --- per-run knob ceilings (web.options clamp targets) ---------------------

def test_option_ceilings_have_safe_demo_defaults(monkeypatch):
    for var in ("BMC_AGENT_WEB_MAX_CBMC_UNWIND", "BMC_AGENT_WEB_MAX_CBMC_TIMEOUT",
                "BMC_AGENT_WEB_MAX_CBMC_DEFINES", "BMC_AGENT_WEB_MAX_THREAT_CONTEXT_CHARS"):
        monkeypatch.delenv(var, raising=False)
    import web.limits as limits
    L = importlib.reload(limits)
    # Tight by default for the public demo (raisable per self-host).
    assert L.MAX_CBMC_UNWIND == 16
    assert L.MAX_CBMC_TIMEOUT == 120
    assert L.MAX_CBMC_DEFINES == 32
    assert L.MAX_THREAT_CONTEXT_CHARS == 8000


def test_option_ceiling_env_override(monkeypatch):
    try:
        L = _reload_with(monkeypatch, BMC_AGENT_WEB_MAX_CBMC_UNWIND="256",
                         BMC_AGENT_WEB_MAX_PER_FN_BUDGET_S="60")
        assert L.MAX_CBMC_UNWIND == 256
        assert L.MAX_PER_FN_BUDGET_S == 60
    finally:
        monkeypatch.delenv("BMC_AGENT_WEB_MAX_CBMC_UNWIND", raising=False)
        monkeypatch.delenv("BMC_AGENT_WEB_MAX_PER_FN_BUDGET_S", raising=False)
        importlib.reload(L)


def test_option_ceiling_bad_value_falls_back(monkeypatch):
    try:
        L = _reload_with(monkeypatch, BMC_AGENT_WEB_MAX_CBMC_UNWIND="lots")
        assert L.MAX_CBMC_UNWIND == 16
    finally:
        monkeypatch.delenv("BMC_AGENT_WEB_MAX_CBMC_UNWIND", raising=False)
        importlib.reload(L)
