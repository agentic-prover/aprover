"""Lightweight progress artifacts for long BMC-Agent sweeps.

The normal logs are useful for humans but hard to check while a full run is
still moving.  This module writes append-only JSONL events plus an optional
snapshot JSON under the run artifact directory.  It is deliberately best-effort:
progress recording must never change the verifier's outcome.
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_UNDEFINED_SYMBOL_RE = re.compile(r"failed to find symbol '([^']+)'")


def append_progress_event(config: Any, event: str, **payload: Any) -> Path | None:
    """Append one JSON event to the configured progress JSONL file."""

    if not getattr(config, "enable_progress_artifacts", True):
        return None
    path = _progress_jsonl_path(config)
    entry = {
        "ts": _utcnow(),
        "event": event,
        **payload,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, sort_keys=True, default=str)
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
        return path
    except Exception:
        return None


def write_progress_summary(config: Any, payload: dict[str, Any]) -> Path | None:
    """Write the latest sweep-level progress snapshot as JSON."""

    if not getattr(config, "enable_progress_artifacts", True):
        return None
    path = _progress_summary_path(config)
    data = {
        "updated_at": _utcnow(),
        **payload,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(path)
        return path
    except Exception:
        return None


def summarize_cbmc_result(result: Any) -> dict[str, Any]:
    """Return a compact, grep-friendly summary of a CBMCResult-like object."""

    verified = bool(getattr(result, "verified", False))
    counterexamples = getattr(result, "counterexamples", None) or []
    error = getattr(result, "error", None)
    raw_output = getattr(result, "raw_output", "") or ""
    missing = Counter(_UNDEFINED_SYMBOL_RE.findall(raw_output))

    if verified:
        status = "verified"
    elif counterexamples:
        status = "counterexample"
    elif error:
        status = "error"
    else:
        status = "unknown"

    error_kind = None
    haystack = f"{error or ''}\n{raw_output}"
    if error:
        err_lower = str(error).lower()
        if "timed out" in err_lower:
            error_kind = "timeout"
        elif "cbmc not found" in err_lower:
            error_kind = "cbmc_not_found"
        elif missing:
            error_kind = "undefined_symbol"
        elif "parsing error" in haystack.lower():
            error_kind = "parsing_error"
        elif "conversion error" in haystack.lower():
            error_kind = "conversion_error"
        else:
            error_kind = "other_error"

    return {
        "status": status,
        "verified": verified,
        "counterexamples": len(counterexamples),
        "error": error,
        "error_kind": error_kind,
        "undefined_symbols": dict(missing.most_common(8)),
    }


def progress_file_paths(config: Any) -> dict[str, str]:
    return {
        "jsonl": str(_progress_jsonl_path(config)),
        "summary": str(_progress_summary_path(config)),
    }


def _progress_jsonl_path(config: Any) -> Path:
    override = getattr(config, "progress_jsonl_path", "") or ""
    if override:
        return Path(override)
    return Path(getattr(config, "artifact_dir", "artifacts")) / "progress.jsonl"


def _progress_summary_path(config: Any) -> Path:
    override = getattr(config, "progress_summary_path", "") or ""
    if override:
        return Path(override)
    return Path(getattr(config, "artifact_dir", "artifacts")) / "progress_summary.json"


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
