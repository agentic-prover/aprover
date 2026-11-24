"""
Streaming wrapper around AMCPipeline for the web chat front-end.

Runs the pipeline in a background thread and yields JSON-serialisable progress
events as they happen so the chat UI can paint a live timeline.

Web-demo defaults differ from the CLI on purpose:
- dynamic validation off (Stage 3 needs a writable build dir on every host)
- realism check off (extra LLM calls; the demo is already slow)
- short refinement loop and CBMC timeout, since visitors won't wait minutes
"""
from __future__ import annotations

import logging
import queue
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Iterator

import bmc_agent.logger as _bmc_log_mod
from bmc_agent.config import Config
from bmc_agent.pipeline import AMCPipeline


_MAX_SOURCE_BYTES = 64 * 1024  # 64KB cap for pasted source
_WALL_TIMEOUT_SEC = 300        # hard ceiling on a single web run

# bmc_agent loggers set propagate=False, so a handler attached to the
# package root does not receive child records. We attach the queue handler
# to each "bmc_agent.*" logger directly, plus monkey-patch get_logger so
# lazily-created component loggers also pick up the handler.
#
# This mutates global logging state, so runs are serialised through a lock.
_RUN_LOCK = threading.Lock()


def run_aprover_streaming(
    source_code: str,
    function: str | None = None,
    domain_knowledge: str = "",
    api_key: str = "",
) -> Iterator[dict]:
    """Run AMCPipeline on a snippet, yielding progress events.

    Each yielded value is a dict with a ``type`` field. Types:
      - ``started``: pipeline has begun
      - ``log``: a log line from bmc_agent.* loggers
      - ``error``: fatal error before/while running
      - ``result``: terminal event with the bug summary
    """
    if not source_code.strip():
        yield {"type": "error", "message": "No source code provided."}
        return

    if len(source_code.encode("utf-8")) > _MAX_SOURCE_BYTES:
        yield {
            "type": "error",
            "message": f"Source too large ({len(source_code)} bytes); web demo cap is {_MAX_SOURCE_BYTES}B.",
        }
        return

    work_dir = Path(tempfile.mkdtemp(prefix="aprover_web_"))
    src_path = work_dir / "input.c"
    src_path.write_text(source_code, encoding="utf-8")

    config = Config.from_env()
    # Visitor-supplied key takes precedence over any server-side env key.
    # resolved_api_key() checks llm_api_key first, so this routes the whole
    # pipeline (spec gen + refinement) through the caller's own key.
    if api_key:
        config.llm_api_key = api_key
    config.artifact_dir = str(work_dir / "artifacts")
    config.enable_dynamic_validation = False
    config.enable_realism_check = False
    config.enable_realism_thinking = False
    config.cbmc_timeout = 60
    config.cbmc_unwind = 4
    config.max_refinement_iters = 2
    config.max_spec_retries = 2

    if not config.resolved_api_key():
        yield {
            "type": "error",
            "message": "Server is missing ANTHROPIC_API_KEY — verification cannot run.",
        }
        return

    events: queue.Queue = queue.Queue()
    sentinel = object()

    class _QueueHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                msg = self.format(record)
            except Exception:
                msg = record.getMessage()
            events.put({"type": "log", "level": record.levelname.lower(), "message": msg})

    handler = _QueueHandler(level=logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))

    def _attach_to_existing() -> list[logging.Logger]:
        attached: list[logging.Logger] = []
        for name, lg in list(logging.Logger.manager.loggerDict.items()):
            if name.startswith("bmc_agent.") and isinstance(lg, logging.Logger):
                if handler not in lg.handlers:
                    lg.addHandler(handler)
                    attached.append(lg)
        return attached

    holder: dict = {}

    with _RUN_LOCK:
        attached = _attach_to_existing()
        original_get_logger = _bmc_log_mod.get_logger

        def _wrapped_get_logger(component: str, *a, **kw):  # type: ignore[no-untyped-def]
            lg = original_get_logger(component, *a, **kw)
            if handler not in lg.handlers:
                lg.addHandler(handler)
                attached.append(lg)
            return lg

        _bmc_log_mod.get_logger = _wrapped_get_logger  # type: ignore[assignment]

        def worker() -> None:
            try:
                pipeline = AMCPipeline(config)
                holder["reports"] = pipeline.run(
                    source_file=str(src_path),
                    driver_name="webdemo",
                    domain_knowledge=domain_knowledge,
                )
            except Exception as exc:  # pragma: no cover - surfaced to user
                holder["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                events.put(sentinel)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        yield {"type": "started", "function": function or ""}

        timed_out = False
        while True:
            try:
                ev = events.get(timeout=_WALL_TIMEOUT_SEC)
            except queue.Empty:
                timed_out = True
                break
            if ev is sentinel:
                break
            yield ev

        for lg in attached:
            try:
                lg.removeHandler(handler)
            except ValueError:
                pass
        _bmc_log_mod.get_logger = original_get_logger  # type: ignore[assignment]

    t.join(timeout=5)
    shutil.rmtree(work_dir, ignore_errors=True)

    if timed_out:
        yield {
            "type": "result",
            "result": {"ok": False, "error": f"Pipeline exceeded {_WALL_TIMEOUT_SEC}s wall timeout."},
        }
        return

    if "error" in holder:
        yield {"type": "result", "result": {"ok": False, "error": holder["error"]}}
        return

    reports = holder.get("reports", []) or []
    if function:
        reports = [r for r in reports if r.function_name == function]

    summary = {
        "ok": True,
        "function_filter": function or "",
        "n_bugs": len(reports),
        "bugs": [
            {
                "function": r.function_name,
                "bug_type": r.bug_type,
                "violated_property": r.violated_property,
                "confidence": r.confidence,
                "call_chain": r.call_chain or [],
                "reasoning": (r.reasoning_trail or "")[:600],
            }
            for r in reports
        ],
    }
    yield {"type": "result", "result": summary}
