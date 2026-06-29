"""
Structured logging setup for BMC-Agent.

Uses Python's logging module with rich for pretty console output,
and logs to both console and file.
"""

from __future__ import annotations

import contextvars
import logging
import os
from pathlib import Path
from typing import Callable, Optional

# Try to import rich; fall back to plain logging if unavailable.
try:
    from rich.logging import RichHandler

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

_LOGGERS: dict[str, logging.Logger] = {}
_FILE_HANDLER: Optional[logging.FileHandler] = None


# -- per-run log sink -----------------------------------------------------
# A context-local sink lets a caller (e.g. the web runner) capture this run's
# log lines without touching global logging state. The sink is held in a
# ContextVar, so concurrent runs — each in its own thread/context — route to
# their own sink with no shared mutable state and no serialisation. The CLI
# leaves the sink unset, so _SinkHandler is a no-op there.
_LOG_SINK: contextvars.ContextVar[Optional[Callable[[str, str], None]]] = (
    contextvars.ContextVar("bmc_log_sink", default=None)
)


class _SinkHandler(logging.Handler):
    """Forward each record's formatted message to the context-local sink, if any."""

    def emit(self, record: logging.LogRecord) -> None:
        sink = _LOG_SINK.get()
        if sink is None:
            return
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        try:
            sink(record.levelname.lower(), msg)
        except Exception:
            pass  # a broken sink must never break logging


_SINK_HANDLER = _SinkHandler(level=logging.INFO)
_SINK_HANDLER.setFormatter(logging.Formatter("%(message)s"))


def set_log_sink(sink: Callable[[str, str], None]) -> contextvars.Token:
    """Route bmc_agent log lines in the current context to ``sink(level, msg)``.

    Returns a token to pass to :func:`reset_log_sink`. Set this inside the
    thread whose logs you want to capture (the value does not propagate to
    threads spawned afterwards)."""
    return _LOG_SINK.set(sink)


def reset_log_sink(token: contextvars.Token) -> None:
    """Undo a :func:`set_log_sink`, restoring the previous sink."""
    _LOG_SINK.reset(token)


def _ensure_file_handler(artifact_dir: str) -> Optional[logging.FileHandler]:
    """Create (or return existing) file handler for the given artifact directory.

    Returns ``None`` if no writable log location is available (e.g. a
    read-only container workdir), in which case logging stays console-only
    rather than crashing the importing process.
    """
    global _FILE_HANDLER
    if _FILE_HANDLER is not None:
        return _FILE_HANDLER

    import tempfile

    # Try the requested dir first, then a temp-dir fallback. A logger must
    # never take down the app just because its file sink is unwritable.
    for candidate in (artifact_dir, os.path.join(tempfile.gettempdir(), "aprover-logs")):
        try:
            log_path = Path(candidate) / "amc.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _FILE_HANDLER = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            break
        except OSError:
            continue
    if _FILE_HANDLER is None:
        return None

    _FILE_HANDLER.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    _FILE_HANDLER.setFormatter(fmt)
    return _FILE_HANDLER


def get_logger(
    component: str,
    artifact_dir: str = "artifacts",
    level: int = logging.DEBUG,
) -> logging.Logger:
    """
    Return a logger for the named component.

    The logger writes to:
    - Console via RichHandler (if rich is installed) or StreamHandler
    - File at ``{artifact_dir}/amc.log``

    Repeated calls with the same *component* return the cached logger.
    """
    global _LOGGERS

    if component in _LOGGERS:
        return _LOGGERS[component]

    logger = logging.getLogger(f"bmc_agent.{component}")
    logger.setLevel(level)
    logger.propagate = False  # avoid double-logging via root

    # Console handler
    if _RICH_AVAILABLE:
        console_handler: logging.Handler = RichHandler(
            level=logging.DEBUG,
            show_path=False,
            rich_tracebacks=True,
            markup=True,
        )
    else:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        plain_fmt = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
            datefmt="%H:%M:%S",
        )
        console_handler.setFormatter(plain_fmt)

    logger.addHandler(console_handler)

    # Per-run sink handler (no-op unless a caller sets a context-local sink).
    logger.addHandler(_SINK_HANDLER)

    # File handler. An env override lets read-only deployments (e.g. HF
    # Spaces) redirect logs to a writable path; if even that fails we run
    # console-only rather than crash.
    file_handler = _ensure_file_handler(
        os.environ.get("BMC_AGENT_LOG_DIR", artifact_dir)
    )
    if file_handler is not None:
        logger.addHandler(file_handler)

    _LOGGERS[component] = logger
    return logger


def reset_loggers() -> None:
    """Reset all cached loggers (useful in tests)."""
    global _LOGGERS, _FILE_HANDLER
    for logger in _LOGGERS.values():
        logger.handlers.clear()
    _LOGGERS.clear()
    if _FILE_HANDLER is not None:
        _FILE_HANDLER.close()
        _FILE_HANDLER = None
