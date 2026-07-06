"""
Streaming wrappers around AMCPipeline for the web chat front-end.

Runs the pipeline in a background thread and yields JSON-serialisable progress
events as they happen so the chat UI can paint a live timeline. Two entry
points share one streaming harness (``_stream_pipeline``):

- ``run_file_streaming``      — one existing file (e.g. from a cloned repo)
- ``run_directory_streaming`` — every supported source file under a directory (a repo)

A web run uses the same defaults as a bare ``bmc-agent verify`` (``_make_config``
no longer pins any knob before the options overlay, so ``Config.from_env()``
stands: realism + dynamic validation on, 120s solver budget, 5 refinement rounds,
3 spec retries). A visitor's "Run settings" panel overrides any of them via the
validated ``options`` dict — see ``web.options`` and ``_make_config``.

Pipeline log lines are captured per-run via ``bmc_agent.logger.set_log_sink``
(a context-local sink set inside each run's worker thread), so concurrent runs
stream their own logs with no shared logging state and no serialisation.
"""
from __future__ import annotations

import queue
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterator

from bmc_agent.config import Config
from bmc_agent.logger import reset_log_sink, set_log_sink
from bmc_agent.pipeline import AMCPipeline
from web.limits import MAX_VERIFY_FILES as _MAX_VERIFY_FILES
from web.limits import WALL_TIMEOUT_SEC as _WALL_TIMEOUT_SEC


def _make_config(
    work_dir: Path,
    *,
    provider: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    k2_backend: str = "",
    progress: "Callable[[dict], None] | None" = None,
    scale_down: bool = False,
    options: "dict | None" = None,
) -> Config:
    """Build a web-demo Config, routing the LLM through the visitor's selection.

    Setting ``llm_provider`` explicitly bypasses auto-detect, so the chosen
    backend ("anthropic" / "openai") is honoured; ``resolved_api_key()`` checks
    ``llm_api_key`` first, so this routes the whole pipeline (spec gen +
    refinement) through the caller's own key/model/endpoint.
    """
    config = Config.from_env()
    if provider:
        config.llm_provider = provider
    if model:
        config.llm_model = model
    if base_url:
        config.llm_base_url = base_url
    if api_key:
        config.llm_api_key = api_key
    if k2_backend:
        config.llm_k2_backend = k2_backend
    config.artifact_dir = str(work_dir / "artifacts")
    # Structured-progress hook for the workbench (phase / function / finding /
    # cost events). Attached dynamically — Config has no slots — so the CLI path
    # (progress=None) is unaffected. See AMCPipeline._emit.
    if progress is not None:
        config.progress = progress  # type: ignore[attr-defined]
    # The web inherits the same defaults as a bare ``bmc-agent verify`` run — i.e.
    # ``Config.from_env()`` (realism + dynamic validation on, 120s solver budget,
    # 5 refinement rounds, 3 spec retries). A run with options=None is therefore
    # CLI-identical. The visitor's "Run settings" panel overrides any knob via the
    # validated, clamped ``options`` (see web.options); the estimator assumes the
    # same defaults (see web.estimate).
    _apply_options(config, options or {}, api_key)

    # Recovery retry on a solver blow-up (the design's "retry · scaled"): bound
    # ML/numerics parametric sizes + restrict specs to safety so a function that
    # timed out at full size becomes tractable. Applied last so a scaled retry
    # wins over any scale-down toggle the options carried. See Config.scale_down.
    if scale_down:
        config.scale_down = True
        config.safety_only = True
    return config


# Config field groups whose option keys are 1:1 Config attributes — overlaid with
# a guarded setattr. Web-only keys (spec mode / per-role routing / run_mode) are
# handled explicitly, not by the generic loop.
_OVERLAY_GROUPS = ("depth", "ai_layers", "harness", "threat")


def _apply_options(config: Config, opts: dict, api_key: str) -> None:
    """Overlay the validated, clamped run ``options`` (``web.options.parse_options``)
    onto ``config``. Only keys the visitor actually sent are present, so this
    never resets an untouched knob away from its Config (CLI) default."""
    for group in _OVERLAY_GROUPS:
        for key, val in (opts.get(group) or {}).items():
            if hasattr(config, key):
                setattr(config, key, val)
    # agentic group: the bool/int knobs are 1:1 Config fields; the nested ``llm``
    # block carries per-role routing and is applied separately below.
    for key, val in (opts.get("agentic") or {}).items():
        if key != "llm" and hasattr(config, key):
            setattr(config, key, val)
    # math_ints is the only knob the web honors from the spec_mode group. The
    # CLI-only synthesis modes (specs-bench / standalone / loop-invariants) run a
    # different code path than pipeline.run(), so they're not exposed on the web.
    spec_mode = opts.get("spec_mode") or {}
    if "math_ints" in spec_mode:
        config.math_ints = bool(spec_mode["math_ints"])
    # Per-role LLM routing: reuse the single BYOK key for every role (per-role
    # keys are deliberately not accepted in the body). The pipeline auto-disables
    # parallel validation when role overrides are set (pipeline.py), so this is
    # thread-safe.
    roles = ((opts.get("agentic") or {}).get("llm") or {}).get("roles") or {}
    if roles:
        config.llm_role_overrides = {
            role: {**spec, "api_key": api_key} for role, spec in roles.items()
        }


def _bug_dict(report, file: str | None = None) -> dict:
    d = {
        "function": report.function_name,
        "bug_type": report.bug_type,
        "violated_property": report.violated_property,
        "confidence": report.confidence,
        "call_chain": report.call_chain or [],
        "reasoning": (report.reasoning_trail or "")[:600],
    }
    if file is not None:
        d["file"] = file
    return d


def _stream_pipeline(
    config: Config,
    work_dir: Path,
    run_fn: Callable[[AMCPipeline], list[tuple[str | None, object]]],
    started_meta: dict,
    pause_check: "Callable[[], bool] | None" = None,
) -> Iterator[dict]:
    """Run ``run_fn(pipeline)`` in a thread, streaming log/started/result events.

    ``run_fn`` returns a flat list of ``(file_or_None, BugReport)`` pairs so both
    single-file and directory runs share one summary shape. ``work_dir`` is a
    scratch dir (artifacts live under it) and is removed when the run ends — it
    must NOT contain the caller's source (clone/session files live elsewhere).
    """
    if config.resolved_provider() != "claude-code" and not config.resolved_api_key():
        yield {
            "type": "error",
            "message": "No API key configured for the selected provider — verification cannot run.",
        }
        shutil.rmtree(work_dir, ignore_errors=True)
        return

    events: queue.Queue = queue.Queue()
    sentinel = object()
    holder: dict = {}

    def worker() -> None:
        # Route this run's bmc_agent log lines onto our queue. The sink is
        # context-local to this thread, so parallel runs don't cross streams.
        token = set_log_sink(
            lambda level, msg: events.put({"type": "log", "level": level, "message": msg})
        )
        try:
            pipeline = AMCPipeline(config)
            holder["pairs"] = run_fn(pipeline)
        except Exception as exc:  # pragma: no cover - surfaced to user
            holder["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            reset_log_sink(token)
            events.put(sentinel)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    yield {"type": "started", **started_meta}

    timed_out = False
    idle = 0.0
    _POLL = 1.0
    while True:
        try:
            ev = events.get(timeout=_POLL)
            idle = 0.0
        except queue.Empty:
            # A deliberate pause (workbench) is not inactivity — don't let it
            # trip the wall timeout. Only genuine idle time counts.
            if pause_check is not None and pause_check():
                continue
            idle += _POLL
            if idle >= _WALL_TIMEOUT_SEC:
                timed_out = True
                break
            continue
        if ev is sentinel:
            break
        yield ev

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

    pairs = holder.get("pairs") or []
    bugs = [_bug_dict(r, file) for file, r in pairs]
    yield {"type": "result", "result": {"ok": True, "n_bugs": len(bugs), "bugs": bugs}}


def run_file_streaming(
    file_path: str,
    function: str | None = None,
    domain_knowledge: str = "",
    api_key: str = "",
    provider: str = "",
    model: str = "",
    base_url: str = "",
    k2_backend: str = "",
    progress: "Callable[[dict], None] | None" = None,
    pause_check: "Callable[[], bool] | None" = None,
    scale_down: bool = False,
    options: "dict | None" = None,
    source_root: str = "",
) -> Iterator[dict]:
    """Verify one existing file (e.g. from a cloned repo). No paste-size cap.

    ``source_root`` is the cloned repo root (when the file came from one). It's
    used to discover the repo's include dirs so ``cc -E`` resolves project
    headers — without it a file that ``#include``\\s a project header fails to
    preprocess, the raw include survives into harness.c, and CBMC reports
    "harness build failed" for every function. Mirrors the directory path."""
    src = Path(file_path)
    if not src.is_file():
        yield {"type": "error", "message": f"File not found: {file_path}"}
        return

    work_dir = Path(tempfile.mkdtemp(prefix="aprover_web_"))
    config = _make_config(work_dir, provider=provider, model=model, base_url=base_url,
                          api_key=api_key, k2_backend=k2_backend, progress=progress,
                          scale_down=scale_down, options=options)
    # Discover the repo's include dirs and turn on cc -E preprocessing so a
    # single-file run resolves project headers (the directory path does this in
    # AMCPipeline.run_directory; the single-file path needs it set explicitly).
    if source_root:
        from bmc_agent.preprocessor import discover_include_dirs
        config.include_dirs = discover_include_dirs(source_root)
        config.preprocess = True

    def run_fn(pipeline: AMCPipeline) -> list[tuple[str | None, object]]:
        reports = pipeline.run(
            source_file=str(src),
            driver_name="webfile",
            domain_knowledge=domain_knowledge,
        ) or []
        if function:
            reports = [r for r in reports if r.function_name == function]
        return [(None, r) for r in reports]

    yield from _stream_pipeline(config, work_dir, run_fn, {"file": src.name}, pause_check)


def run_directory_streaming(
    source_dir: str,
    only_functions: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    domain_knowledge: str = "",
    api_key: str = "",
    provider: str = "",
    model: str = "",
    base_url: str = "",
    k2_backend: str = "",
    progress: "Callable[[dict], None] | None" = None,
    pause_check: "Callable[[], bool] | None" = None,
    scale_down: bool = False,
    options: "dict | None" = None,
    max_files: int = _MAX_VERIFY_FILES,
) -> Iterator[dict]:
    """Verify every supported source file under ``source_dir`` (a cloned repo).

    Dispatches by language (C via cross-file analysis, Rust/Java per-file)
    through ``AMCPipeline.verify_tree``, which owns the single tree walk and
    the file cap — so the runner no longer scans the tree itself. ``max_files``
    defaults to the env-tunable web cap and may be overridden per run (the
    workbench surfaces it as a setting)."""
    root = Path(source_dir)
    if not root.is_dir():
        yield {"type": "error", "message": f"Directory not found: {source_dir}"}
        return

    exclude = exclude_patterns or ["*test*", "*mock*"]
    funcs = set(only_functions) if only_functions else None

    work_dir = Path(tempfile.mkdtemp(prefix="aprover_web_"))
    config = _make_config(work_dir, provider=provider, model=model, base_url=base_url,
                          api_key=api_key, k2_backend=k2_backend, progress=progress,
                          scale_down=scale_down, options=options)

    def run_fn(pipeline: AMCPipeline) -> list[tuple[str | None, object]]:
        results = pipeline.verify_tree(
            source_dir=str(root),
            driver_name="webrepo",
            domain_knowledge=domain_knowledge,
            exclude_patterns=list(exclude),
            only_functions=funcs,
            max_files=max_files,
        ) or {}
        pairs: list[tuple[str | None, object]] = []
        for fname, reports in results.items():
            for r in reports or []:
                pairs.append((fname, r))
        return pairs

    yield from _stream_pipeline(config, work_dir, run_fn, {"dir": root.name}, pause_check)


def _pump_round(config: Config, run_fn, pause_check):
    """Run one pipeline call in a worker thread, streaming its log events; return
    ``(pairs, error)`` via the generator's StopIteration value (consume with
    ``yield from``). Unlike ``_stream_pipeline`` this does NOT remove the work
    dir — the autonomous loop reuses it across rounds and owns its lifetime."""
    events: queue.Queue = queue.Queue()
    sentinel = object()
    holder: dict = {}

    def worker() -> None:
        token = set_log_sink(
            lambda level, msg: events.put({"type": "log", "level": level, "message": msg})
        )
        try:
            pipeline = AMCPipeline(config)
            holder["pairs"] = run_fn(pipeline)
        except Exception as exc:  # pragma: no cover - surfaced to user
            holder["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            reset_log_sink(token)
            events.put(sentinel)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    idle = 0.0
    _POLL = 1.0
    while True:
        try:
            ev = events.get(timeout=_POLL)
            idle = 0.0
        except queue.Empty:
            # A deliberate pause isn't inactivity — only genuine idle counts.
            if pause_check is not None and pause_check():
                continue
            idle += _POLL
            if idle >= _WALL_TIMEOUT_SEC:
                t.join(timeout=5)
                return [], f"Pipeline exceeded {_WALL_TIMEOUT_SEC}s wall timeout."
            continue
        if ev is sentinel:
            break
        yield ev
    t.join(timeout=5)
    if "error" in holder:
        return [], holder["error"]
    return holder.get("pairs") or [], None


def run_autonomous_streaming(
    source_dir: str,
    only_functions: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    domain_knowledge: str = "",
    api_key: str = "",
    provider: str = "",
    model: str = "",
    base_url: str = "",
    k2_backend: str = "",
    progress: "Callable[[dict], None] | None" = None,
    pause_check: "Callable[[], bool] | None" = None,
    scale_down: bool = False,
    options: "dict | None" = None,
    max_files: int = _MAX_VERIFY_FILES,
    max_rounds: int = 3,
) -> Iterator[dict]:
    """Round-based autonomous verification — the web port of the CLI ``autonomous``.

    Re-runs ``verify_tree`` over the SAME Config up to ``max_rounds`` times, so the
    Phase-2b auto-retry session-strip sets accumulate across rounds, stopping early
    at a fixed point (a round that adds no new findings and no new recovery state).
    ``allow_self_patch`` is forced ``"deny"`` — the web never lets an agent edit
    AProver's own source. Each round emits a ``run`` event so the ETA estimator
    times rounds independently (see web.jobs / bmc_agent.eta)."""
    root = Path(source_dir)
    if not root.is_dir():
        yield {"type": "error", "message": f"Directory not found: {source_dir}"}
        return

    exclude = exclude_patterns or ["*test*", "*mock*"]
    funcs = set(only_functions) if only_functions else None

    work_dir = Path(tempfile.mkdtemp(prefix="aprover_web_"))
    config = _make_config(work_dir, provider=provider, model=model, base_url=base_url,
                          api_key=api_key, k2_backend=k2_backend, progress=progress,
                          scale_down=scale_down, options=options)
    # Hard safety floor for a public, bring-your-own-key surface: the autonomous
    # loop may NEVER let an LLM patch AProver's own source, whatever the options.
    config.allow_self_patch = "deny"
    # Cross-round convergence relies on the feedback loop persisting learned
    # clauses, and on the auto-retry recovery state, into the shared Config.
    config.enable_feedback_loop = True

    if config.resolved_provider() != "claude-code" and not config.resolved_api_key():
        yield {
            "type": "error",
            "message": "No API key configured for the selected provider — verification cannot run.",
        }
        shutil.rmtree(work_dir, ignore_errors=True)
        return

    rounds = max(1, int(max_rounds or 1))
    all_pairs: list[tuple[str | None, object]] = []
    last_fp = None
    try:
        for rnd in range(rounds):
            # A "run" event resets the per-round ETA clock (see Job.ingest).
            yield {"type": "run", "round": rnd + 1, "max_rounds": rounds, "dir": root.name}
            yield {"type": "log", "level": "info",
                   "message": f"Autonomous round {rnd + 1}/{rounds}"}

            def run_fn(pipeline: AMCPipeline) -> list[tuple[str | None, object]]:
                results = pipeline.verify_tree(
                    source_dir=str(root),
                    driver_name="webrepo",
                    domain_knowledge=domain_knowledge,
                    exclude_patterns=list(exclude),
                    only_functions=funcs,
                    max_files=max_files,
                ) or {}
                pairs: list[tuple[str | None, object]] = []
                for fname, reports in results.items():
                    for r in reports or []:
                        pairs.append((fname, r))
                return pairs

            pairs, err = yield from _pump_round(config, run_fn, pause_check)
            if err is not None:
                yield {"type": "result", "result": {"ok": False, "error": err}}
                return
            all_pairs = pairs   # verify_tree re-reports the whole tree each round

            # Fixed point: a round that neither found anything new nor grew the
            # auto-retry recovery state. Cheap proxy for the CLI's coverage check.
            fp = (
                len(pairs),
                len(config.session_strip_typedefs),
                len(config.session_strip_structs),
                len(config.session_opaque_param_structs),
                len(config.session_stub_functions),
            )
            if last_fp is not None and fp == last_fp:
                yield {"type": "log", "level": "info",
                       "message": f"Converged after round {rnd + 1} — no new findings or recovery state."}
                break
            last_fp = fp

        bugs = [_bug_dict(r, file) for file, r in all_pairs]
        yield {"type": "result", "result": {"ok": True, "n_bugs": len(bugs), "bugs": bugs}}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
