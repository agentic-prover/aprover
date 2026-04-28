"""
Structured logging setup for BMC-Agent.

Uses Python's logging module with rich for pretty console output,
and logs to both console and file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

# Try to import rich; fall back to plain logging if unavailable.
try:
    from rich.logging import RichHandler

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

_LOGGERS: dict[str, logging.Logger] = {}
_FILE_HANDLER: Optional[logging.FileHandler] = None


def _ensure_file_handler(artifact_dir: str) -> logging.FileHandler:
    """Create (or return existing) file handler for the given artifact directory."""
    global _FILE_HANDLER
    if _FILE_HANDLER is not None:
        return _FILE_HANDLER

    log_path = Path(artifact_dir) / "amc.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _FILE_HANDLER = logging.FileHandler(log_path, mode="a", encoding="utf-8")
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

    # File handler
    file_handler = _ensure_file_handler(artifact_dir)
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
