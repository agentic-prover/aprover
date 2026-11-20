"""Lightweight per-agent runtime telemetry.

Records one entry per agent invocation — role, wall-clock duration, outcome,
and (for tool-using agents) LLM round-trips + tool calls — into a thread-safe
in-process collector. The pipeline resets it at the start of a run and dumps a
JSON summary + logs a table at the end, so an operator can see which agents
actually fired, how often they fell through, and where the time goes.

Design constraints:
  * Recording is best-effort and MUST NEVER raise into the caller (every public
    entry point swallows its own exceptions).
  * Thread-safe: agents run concurrently under parallel validation.
  * No token accounting yet — LLMClient logs usage but does not return it to the
    agent layer. Threading usage out of complete()/complete_with_tools() is a
    follow-up; the schema reserves a ``tokens`` field (0 until then).
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Optional

from bmc_agent.logger import get_logger

logger = get_logger("agent_telemetry")


@dataclass
class AgentInvocation:
    """One agent call. ``outcome`` is one of ok | empty | error."""

    role: str
    outcome: str
    duration_s: float
    iterations: int = 1   # LLM round-trips (tool loops > 1; flat == 1)
    tool_calls: int = 0
    tokens: int = 0       # reserved; 0 until usage is plumbed through
    error: str = ""


class _Collector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[AgentInvocation] = []

    def record(self, inv: AgentInvocation) -> None:
        with self._lock:
            self._records.append(inv)

    def reset(self) -> None:
        with self._lock:
            self._records = []

    def snapshot(self) -> list[AgentInvocation]:
        with self._lock:
            return list(self._records)

    def summary(self) -> dict:
        """Per-role aggregate: calls, outcome counts, total/avg duration,
        total tool_calls + iterations."""
        agg: dict[str, dict] = defaultdict(
            lambda: {
                "calls": 0, "ok": 0, "empty": 0, "error": 0,
                "total_duration_s": 0.0, "tool_calls": 0, "iterations": 0,
            }
        )
        for r in self.snapshot():
            a = agg[r.role]
            a["calls"] += 1
            a[r.outcome] = a.get(r.outcome, 0) + 1
            a["total_duration_s"] += r.duration_s
            a["tool_calls"] += r.tool_calls
            a["iterations"] += r.iterations
        out: dict[str, dict] = {}
        for role, a in agg.items():
            calls = a["calls"] or 1
            a["avg_duration_s"] = round(a["total_duration_s"] / calls, 3)
            a["total_duration_s"] = round(a["total_duration_s"], 3)
            out[role] = a
        return out


_collector = _Collector()


def reset() -> None:
    """Clear all records (call at the start of a pipeline run)."""
    try:
        _collector.reset()
    except Exception:  # pragma: no cover - defensive
        pass


def snapshot() -> "list[AgentInvocation]":
    return _collector.snapshot()


def summary() -> dict:
    return _collector.summary()


def record(
    role: str,
    duration_s: float,
    *,
    outcome: str,
    iterations: int = 1,
    tool_calls: int = 0,
    tokens: int = 0,
    error: str = "",
) -> None:
    """Record one invocation. Never raises."""
    try:
        _collector.record(AgentInvocation(
            role=role or "?",
            outcome=outcome if outcome in ("ok", "empty", "error") else "ok",
            duration_s=round(float(duration_s), 4),
            iterations=int(iterations) if iterations else 1,
            tool_calls=int(tool_calls) if tool_calls else 0,
            tokens=int(tokens) if tokens else 0,
            error=(str(error) or "")[:200],
        ))
    except Exception:  # pragma: no cover - defensive
        pass


def record_agent_result(
    role: str, duration_s: float, result: Any, *, tokens: int = 0
) -> None:
    """Derive outcome/iterations/tool_calls from a BaseAgent ``AgentResult``
    and record it. ``result`` may be None (agent raised). Never raises."""
    _tok = max(0, int(tokens or 0))
    try:
        if result is None:
            record(role, duration_s, outcome="error", error="run raised", tokens=_tok)
            return
        err = getattr(result, "error", None)
        if err:
            outcome = "error"
        elif getattr(result, "output", None) is None:
            outcome = "empty"
        else:
            outcome = "ok"
        tu = getattr(result, "tool_use_result", None)
        iterations = getattr(tu, "iterations", 1) if tu is not None else 1
        tool_calls = getattr(result, "tool_calls_made", 0) or 0
        record(
            role, duration_s,
            outcome=outcome, iterations=iterations or 1,
            tool_calls=tool_calls, tokens=_tok, error=err or "",
        )
    except Exception:  # pragma: no cover - defensive
        pass


def dump(path: str) -> dict:
    """Write ``{records, summary}`` JSON to ``path`` and return the summary.
    Best-effort: returns the summary even if the file write fails."""
    summ = summary()
    try:
        payload = {
            "records": [asdict(r) for r in snapshot()],
            "summary": summ,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("agent_telemetry dump failed: %s", exc)
    return summ


def log_summary() -> None:
    """Emit a compact per-role table to the log. Never raises."""
    try:
        summ = summary()
        if not summ:
            return
        logger.info("=== agent telemetry (per role) ===")
        logger.info(
            "%-22s %6s %5s %5s %6s %8s %8s",
            "role", "calls", "ok", "err", "tools", "avg_s", "total_s",
        )
        for role in sorted(summ, key=lambda r: -summ[r]["total_duration_s"]):
            a = summ[role]
            logger.info(
                "%-22s %6d %5d %5d %6d %8.3f %8.3f",
                role, a["calls"], a.get("ok", 0),
                a.get("error", 0), a["tool_calls"],
                a["avg_duration_s"], a["total_duration_s"],
            )
    except Exception:  # pragma: no cover - defensive
        pass
