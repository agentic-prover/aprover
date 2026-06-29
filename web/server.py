"""
FastAPI server for the AProver web app.

Two surfaces, both static + a small JSON/SSE API:

- ``GET /``            marketing landing page (web/static/index.html)
- ``GET /workbench``   the verification workbench (web/static/workbench.html)

The workbench is a 3-step flow — connect a source, choose a scope, run — wired
to a thin API:

    POST /api/clone              shallow-clone a public repo into the session workspace
    GET  /api/tree               project tree + per-file/-dir function counts
    GET  /api/file               source text for the run view
    POST /api/run                start a verification job (returns {run_id})
    GET  /api/run/{id}/events     Server-Sent Events: phase / function / finding / log / cost / done
    GET  /api/run/{id}            JSON state snapshot (for reconnect / recovery)
    POST /api/run/{id}/pause|resume|cancel|retry   run control + granular recovery

Each browser gets a cookie-bound (``HttpOnly``) server-side session with an
isolated workspace (see web.sessions); cloned repos live there and are reachable
only by the holder of that cookie.

The visitor picks their provider in the workbench Settings modal. The selection
arrives per request as headers (kept out of the body so the key doesn't land in
request logs) and drives the verification pipeline:

    X-LLM-Backend   "anthropic" (native Messages API) or "openai"
                    (OpenAI-compatible /v1/chat/completions: OpenRouter, OpenAI,
                    self-hosted)
    X-LLM-Model     model id / slug
    X-LLM-Base-Url  base URL for the openai backend (empty => provider default)
    X-LLM-Key       API key

Run locally:
    uv run uvicorn web.server:app --port 7860
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bmc_agent.source_parser import CODE_EXTENSIONS, SUPPORTED_DISPLAY
from web import estimate, gitclone, jobs, options, pricing, sessions, tree
from web.limits import FILE_VIEW_BYTES as _MAX_FILE_VIEW_BYTES
from web.limits import UPLOAD_MAX_BYTES as _UPLOAD_MAX_BYTES
from web.limits import UPLOAD_MAX_FILES as _UPLOAD_MAX_FILES
from web.runner import (
    run_autonomous_streaming,
    run_directory_streaming,
    run_file_streaming,
)


WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(title="AProver web")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Brand assets live at the repo root in assets/ — mount only if present so the
# Docker image (which copies assets/) and local runs both work.
_ASSETS_DIR = WEB_DIR.parent / "assets"
if _ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _read_llm_config(request: Request) -> dict[str, str]:
    """Resolve the per-request LLM selection from headers.

    The front-end wizard sends X-LLM-Backend / -Model / -Base-Url / -Key. The
    legacy X-Anthropic-Key header is still honoured (treated as the anthropic
    backend) for back-compat. Empty fields fall back to server-side env so a
    locally-run instance with ANTHROPIC_API_KEY set still works key-free.
    """
    backend = request.headers.get("X-LLM-Backend", "").strip().lower()
    model = request.headers.get("X-LLM-Model", "").strip()
    base_url = request.headers.get("X-LLM-Base-Url", "").strip()
    key = request.headers.get("X-LLM-Key", "").strip()
    # K2 Think inference backend: "auto" | "cerebras" | "nvidia" (else ignored).
    k2_backend = request.headers.get("X-LLM-K2-Backend", "").strip().lower()
    if k2_backend not in ("auto", "cerebras", "nvidia"):
        k2_backend = ""

    legacy = request.headers.get("X-Anthropic-Key", "").strip()
    if not key and legacy:
        backend = backend or "anthropic"
        key = legacy

    if backend not in ("anthropic", "openai"):
        backend = "anthropic"
    if not model:
        model = os.environ.get("BMC_AGENT_LLM_MODEL", "claude-sonnet-4-6")
    if not key:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {"backend": backend, "model": model, "base_url": base_url,
            "key": key, "k2_backend": k2_backend}


def _resolve_repo_dir(workspace: Path, repo: str) -> Path:
    """Resolve the cloned-repo directory for ``repo`` within ``workspace``.

    When ``repo`` is empty and exactly one repo was cloned this chat, use it.
    Raises ``ValueError`` on ambiguity / traversal / missing repo.
    """
    if repo:
        path = sessions.safe_path(workspace, repo)
        if not path.is_dir():
            raise ValueError(f"No cloned repo named {repo!r} in this chat.")
        return path
    subdirs = [p for p in workspace.iterdir() if p.is_dir()] if workspace.is_dir() else []
    if not subdirs:
        raise ValueError("No repo has been cloned yet — call clone_repo first.")
    if len(subdirs) > 1:
        raise ValueError(
            "Multiple repos cloned; specify which with `repo`: "
            + ", ".join(p.name for p in subdirs)
        )
    return subdirs[0]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "cbmc_installed": shutil.which("cbmc") is not None,
            "default_model": os.environ.get("BMC_AGENT_LLM_MODEL", "claude-sonnet-4-6"),
        }
    )


@app.get("/workbench")
async def workbench() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "workbench.html"))


# ======================================================================
# Workbench API — clone → scope tree → run (job) → live events / control
# ======================================================================

def _attach_session_cookie(resp: Any, session_id: str) -> Any:
    resp.set_cookie(
        sessions.COOKIE_NAME, session_id,
        httponly=True, samesite="lax", path="/", max_age=sessions._TTL_SECONDS,
    )
    return resp


def _resolve_scope_target(workspace: Path, scope: dict) -> tuple[Path, bool, Path]:
    """Resolve a run scope to (path, is_dir, repo_dir).

    ``scope`` = {mode: whole|subdir|file, repo, path}. Confined to the session
    workspace via safe_path; raises ValueError on a bad/missing target. The
    repo root is returned so a single-file run can still discover the repo's
    own include dirs (project headers usually live in a sibling ``include/``)."""
    repo_dir = _resolve_repo_dir(workspace, (scope.get("repo") or "").strip())
    mode = scope.get("mode") or "whole"
    rel = (scope.get("path") or "").strip()
    if mode == "whole":
        return repo_dir, True, repo_dir
    if not rel:
        raise ValueError(f"{mode} scope requires a path.")
    target = sessions.safe_path(repo_dir, rel)
    if mode == "file":
        if not target.is_file():
            raise ValueError(f"File not found: {rel}")
        return target, False, repo_dir
    # subdir
    if not target.is_dir():
        raise ValueError(f"Directory not found: {rel}")
    return target, True, repo_dir


def _build_gen_factory(
    target: Path, is_dir: bool, only_functions: list[str] | None,
    domain_knowledge: str, llm: dict, scale_down: bool = False,
    source_root: Path | None = None, max_files: int | None = None,
    opts: dict | None = None,
) -> Callable[..., Iterator[dict]]:
    """Return ``gen_factory(progress, pause_check)`` for ``jobs.run_job``.

    ``max_files`` (directory sweeps only) overrides the env-default file cap;
    None keeps ``run_directory_streaming``'s default. ``opts`` is the validated
    run-settings dict (``web.options.parse_options``) the pipeline Config is
    built from; None keeps the safe demo defaults."""
    def factory(progress=None, pause_check=None) -> Iterator[dict]:
        if is_dir:
            kw = {"max_files": max_files} if max_files is not None else {}
            # Autonomous run mode: a round-based sweep (directory scope only).
            if (opts or {}).get("run_mode") == "autonomous":
                rounds = ((opts or {}).get("autonomous") or {}).get("max_rounds", 3)
                return run_autonomous_streaming(
                    source_dir=str(target),
                    only_functions=only_functions or None,
                    domain_knowledge=domain_knowledge,
                    api_key=llm["key"], provider=llm["backend"],
                    model=llm["model"], base_url=llm["base_url"],
                    k2_backend=llm.get("k2_backend", ""),
                    progress=progress, pause_check=pause_check, scale_down=scale_down,
                    options=opts, max_rounds=rounds,
                    **kw,
                )
            return run_directory_streaming(
                source_dir=str(target),
                only_functions=only_functions or None,
                domain_knowledge=domain_knowledge,
                api_key=llm["key"], provider=llm["backend"],
                model=llm["model"], base_url=llm["base_url"],
                k2_backend=llm.get("k2_backend", ""),
                progress=progress, pause_check=pause_check, scale_down=scale_down,
                options=opts,
                **kw,
            )
        return run_file_streaming(
            file_path=str(target),
            function=(only_functions[0] if only_functions else None),
            domain_knowledge=domain_knowledge,
            api_key=llm["key"], provider=llm["backend"],
            model=llm["model"], base_url=llm["base_url"],
            k2_backend=llm.get("k2_backend", ""),
            progress=progress, pause_check=pause_check, scale_down=scale_down,
            options=opts,
            source_root=str(source_root) if source_root else "",
        )
    return factory


@app.post("/api/clone")
async def api_clone(request: Request) -> JSONResponse:
    body = await request.json()
    url = (body.get("url") or "").strip()
    ref = (body.get("ref") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "Missing repository URL."}, status_code=400)
    session_id, workspace, _ = sessions.STORE.get_or_create(
        request.cookies.get(sessions.COOKIE_NAME)
    )
    # safe_path confines the dest to the workspace as defense-in-depth; the
    # sanitizer already rejects ``.``/``..`` so this should never raise.
    try:
        dest = sessions.safe_path(workspace, gitclone.sanitize_repo_name(url))
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid repository name."}, status_code=400)
    ok, info = await asyncio.to_thread(gitclone.clone_repo, url, ref, dest)
    if ok:
        return _attach_session_cookie(JSONResponse({"ok": True, **info}), session_id)
    return _attach_session_cookie(
        JSONResponse({"ok": False, "error": info}, status_code=400), session_id
    )


# Local-folder upload (workbench "Local path" via the File System Access API).
# The browser reads the chosen subtree lazily and posts only the verifiable
# source files; we materialize them in the session workspace as a synthetic
# "repo" so the rest of the flow (tree/run/file) is identical to a clone.
# Upload caps (env-overridable, see web/limits.py); mirror the clone caps.


def _write_upload(workspace: Path, name: str, files: list) -> dict | str:
    repo = gitclone.sanitize_repo_name(name or "local") or "local"
    # Confine the dest to the workspace (defense-in-depth alongside the
    # sanitizer's ``.``/``..`` rejection) so the rmtree below can't escape.
    try:
        dest = sessions.safe_path(workspace, repo)
    except ValueError:
        return "Invalid upload name."
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    n_files = 0
    listing: list[dict] = []
    for f in files[:_UPLOAD_MAX_FILES]:
        rel = (f.get("path") or "").strip().lstrip("/")
        if not rel or Path(rel).suffix.lower() not in CODE_EXTENSIONS:
            continue
        data = (f.get("content") or "").encode("utf-8")
        total += len(data)
        if total > _UPLOAD_MAX_BYTES:
            shutil.rmtree(dest, ignore_errors=True)
            return f"Selection too large (cap {_UPLOAD_MAX_BYTES // (1024 * 1024)}MB of source)."
        try:
            target = sessions.safe_path(dest, rel)
        except ValueError:
            continue  # skip anything that escapes the workspace
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        n_files += 1
        listing.append({"path": rel, "bytes": len(data)})
    if not listing:
        shutil.rmtree(dest, ignore_errors=True)
        return f"No verifiable source files in the selection (supported: {SUPPORTED_DISPLAY})."
    return {"repo": repo, "files": listing, "n_files": n_files,
            "truncated": len(files) > _UPLOAD_MAX_FILES}


@app.post("/api/upload")
async def api_upload(request: Request) -> JSONResponse:
    body = await request.json()
    files = body.get("files")
    name = (body.get("name") or "local").strip()
    if not isinstance(files, list) or not files:
        return JSONResponse({"ok": False, "error": "No files to upload."}, status_code=400)
    session_id, workspace, _ = sessions.STORE.get_or_create(
        request.cookies.get(sessions.COOKIE_NAME)
    )
    result = await asyncio.to_thread(_write_upload, workspace, name, files)
    if isinstance(result, str):
        return _attach_session_cookie(
            JSONResponse({"ok": False, "error": result}, status_code=400), session_id
        )
    return _attach_session_cookie(JSONResponse({"ok": True, **result}), session_id)


@app.get("/api/tree")
async def api_tree(request: Request, repo: str = "", path: str = "") -> JSONResponse:
    session_id, workspace, _ = sessions.STORE.get_or_create(
        request.cookies.get(sessions.COOKIE_NAME)
    )
    try:
        repo_dir = _resolve_repo_dir(workspace, repo.strip())
        if path.strip():
            sessions.safe_path(repo_dir, path)  # confine subdir to the workspace
        result = await asyncio.to_thread(tree.build_tree, repo_dir, path.strip())
    except ValueError as exc:
        return _attach_session_cookie(
            JSONResponse({"ok": False, "error": str(exc)}, status_code=400), session_id
        )
    return _attach_session_cookie(JSONResponse({"ok": True, "tree": result}), session_id)


@app.get("/api/file")
async def api_file(request: Request, repo: str = "", path: str = "") -> JSONResponse:
    """Source text for the run-view left column (capped)."""
    session_id, workspace, _ = sessions.STORE.get_or_create(
        request.cookies.get(sessions.COOKIE_NAME)
    )
    try:
        repo_dir = _resolve_repo_dir(workspace, repo.strip())
        target = sessions.safe_path(repo_dir, path)
        if not target.is_file():
            raise ValueError(f"File not found: {path}")
        data = target.read_bytes()[:_MAX_FILE_VIEW_BYTES]
    except ValueError as exc:
        return _attach_session_cookie(
            JSONResponse({"ok": False, "error": str(exc)}, status_code=400), session_id
        )
    return _attach_session_cookie(
        JSONResponse({"ok": True, "path": path, "content": data.decode("utf-8", "replace")}),
        session_id,
    )


@app.get("/api/functions")
async def api_functions(request: Request, repo: str = "", path: str = "") -> JSONResponse:
    """Function names defined in a single source file, for the scope screen's
    per-function picker (``only_functions``). Same session-gated, workspace-
    confined access as ``/api/file``."""
    session_id, workspace, _ = sessions.STORE.get_or_create(
        request.cookies.get(sessions.COOKIE_NAME)
    )
    try:
        repo_dir = _resolve_repo_dir(workspace, repo.strip())
        target = sessions.safe_path(repo_dir, path)
        if not target.is_file():
            raise ValueError(f"File not found: {path}")
        funcs = await asyncio.to_thread(tree.list_functions, target)
    except ValueError as exc:
        return _attach_session_cookie(
            JSONResponse({"ok": False, "error": str(exc)}, status_code=400), session_id
        )
    return _attach_session_cookie(
        JSONResponse({"ok": True, "path": path, "functions": funcs}), session_id
    )


@app.get("/api/models")
async def api_models() -> JSONResponse:
    """Per-provider model presets for the Settings dropdown (single source of
    truth shared with the pre-run cost estimate).

    OpenRouter prices are pulled live (cached) from its public models API so the
    dropdown and the estimate price from the source of truth; ``openrouter_prices``
    is the full id→[input, output] map the frontend uses to price *custom* ids.
    The fetch swallows its own errors, falling back to the static presets."""
    presets = await asyncio.to_thread(pricing.presets_with_live_prices)
    or_prices = await asyncio.to_thread(pricing.openrouter_price_map)
    return JSONResponse(
        {"ok": True, "presets": presets, "openrouter_prices": or_prices}
    )


@app.post("/api/estimate")
async def api_estimate(request: Request) -> JSONResponse:
    """Pre-run token/USD estimate for a scope. No LLM calls — mirrors /api/run's
    scope + LLM-config parsing so the figure matches what the run will spend."""
    body = await request.json()
    llm = _read_llm_config(request)
    opts = options.parse_options(body.get("options"))
    session_id, workspace, _ = sessions.STORE.get_or_create(
        request.cookies.get(sessions.COOKIE_NAME)
    )
    scope = {
        "mode": body.get("mode") or "whole",
        "repo": (body.get("repo") or "").strip(),
        "path": (body.get("path") or "").strip(),
    }
    only_functions = set(body.get("only_functions") or []) or None
    try:
        target, is_dir, _repo_dir = _resolve_scope_target(workspace, scope)
        result = await asyncio.to_thread(
            estimate.estimate_scope, target, is_dir, llm,
            _max_files(body.get("max_files")), opts, only_functions,
        )
    except ValueError as exc:
        return _attach_session_cookie(
            JSONResponse({"ok": False, "error": str(exc)}, status_code=400), session_id
        )
    return _attach_session_cookie(JSONResponse({"ok": True, "estimate": result}), session_id)


def _budget_cap(value: Any) -> float | None:
    try:
        cap = float(value)
        return cap if cap > 0 else None
    except (TypeError, ValueError):
        return None


def _max_files(value: Any) -> int | None:
    """Per-run file-cap override from the workbench settings; None = use the
    env-default. A non-positive or unparseable value falls back to the default."""
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


@app.post("/api/run")
async def api_run(request: Request) -> JSONResponse:
    body = await request.json()
    llm = _read_llm_config(request)
    session_id, workspace, _ = sessions.STORE.get_or_create(
        request.cookies.get(sessions.COOKIE_NAME)
    )
    if not llm["key"]:
        return _attach_session_cookie(
            JSONResponse({"ok": False, "error": "Add an API key in Settings to run a verification."},
                         status_code=400),
            session_id,
        )
    scope = {
        "mode": body.get("mode") or "whole",
        "repo": (body.get("repo") or "").strip(),
        "path": (body.get("path") or "").strip(),
        "only_functions": list(body.get("only_functions") or []),
        "domain_knowledge": body.get("domain_knowledge", ""),
        # Per-run file-cap override (directory sweeps); persisted in scope so a
        # retry/continue keeps the same coverage.
        "max_files": _max_files(body.get("max_files")),
        # Validated + clamped run-settings knobs. Persisted in scope so a
        # retry/continue re-applies the exact same configuration (dict(prev.scope)).
        "options": options.parse_options(body.get("options")),
    }
    try:
        target, is_dir, repo_dir = _resolve_scope_target(workspace, scope)
    except ValueError as exc:
        return _attach_session_cookie(
            JSONResponse({"ok": False, "error": str(exc)}, status_code=400), session_id
        )

    cap = _budget_cap(body.get("budget_cap"))
    job = jobs.STORE.create(session_id, scope, llm, budget_cap=cap)
    factory = _build_gen_factory(
        target, is_dir, scope["only_functions"], scope["domain_knowledge"], llm,
        source_root=repo_dir, max_files=scope["max_files"], opts=scope["options"],
    )
    jobs.run_job(job, factory)
    return _attach_session_cookie(JSONResponse({"ok": True, "run_id": job.run_id}), session_id)


def _get_job_or_none(request: Request, run_id: str):
    session_id, _, _ = sessions.STORE.get_or_create(
        request.cookies.get(sessions.COOKIE_NAME)
    )
    return jobs.STORE.get(run_id, session_id), session_id


@app.get("/api/run/{run_id}")
async def api_run_state(request: Request, run_id: str) -> JSONResponse:
    job, session_id = _get_job_or_none(request, run_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "No such run."}, status_code=404)
    return _attach_session_cookie(JSONResponse({"ok": True, **job.snapshot()}), session_id)


@app.get("/api/run/{run_id}/events")
async def api_run_events(request: Request, run_id: str) -> StreamingResponse:
    job, session_id = _get_job_or_none(request, run_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "No such run."}, status_code=404)

    async def gen() -> AsyncIterator[str]:
        idx = 0
        while True:
            # Keep the session alive: a run watched only over SSE makes no other
            # request, so without this its idle TTL could evict the workspace
            # (rmtree) out from under the still-running pipeline.
            sessions.STORE.touch(session_id)
            evs, done, total = job.events_since(idx)
            for ev in evs:
                yield _sse(ev.get("type", "message"), ev)
            idx += len(evs)
            if done and idx >= total:
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.15)

    resp = StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    return _attach_session_cookie(resp, session_id)


@app.post("/api/run/{run_id}/pause")
async def api_run_pause(request: Request, run_id: str) -> JSONResponse:
    job, session_id = _get_job_or_none(request, run_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "No such run."}, status_code=404)
    job.pause.set()
    return _attach_session_cookie(JSONResponse({"ok": True, "status": "paused"}), session_id)


@app.post("/api/run/{run_id}/resume")
async def api_run_resume(request: Request, run_id: str) -> JSONResponse:
    job, session_id = _get_job_or_none(request, run_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "No such run."}, status_code=404)
    job.pause.clear()
    return _attach_session_cookie(JSONResponse({"ok": True, "status": "running"}), session_id)


@app.post("/api/run/{run_id}/cancel")
async def api_run_cancel(request: Request, run_id: str) -> JSONResponse:
    job, session_id = _get_job_or_none(request, run_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "No such run."}, status_code=404)
    job.halt_reason = "cancelled"
    job.cancel.set()
    job.pause.clear()
    return _attach_session_cookie(JSONResponse({"ok": True, "status": "halting"}), session_id)


@app.post("/api/run/{run_id}/retry")
async def api_run_retry(request: Request, run_id: str) -> JSONResponse:
    """Granular recovery. Starts a NEW job from the halted run's scope:

    - ``retry_function``: re-run only ``function`` (optionally ``scale_down``);
    - ``continue``: re-run the run's remaining (not-yet-verified) functions,
      or the explicit ``functions`` list the client supplies;
    - ``rerun_all``: re-run the full original scope.

    Scoping the re-run to one/few functions is how recovery avoids paying for
    the whole pipeline again (per-spec caching for true zero-respend is a
    future optimization — see plan)."""
    body = await request.json()
    prev, session_id = _get_job_or_none(request, run_id)
    if prev is None:
        return JSONResponse({"ok": False, "error": "No such run."}, status_code=404)
    _, workspace, _ = sessions.STORE.get_or_create(request.cookies.get(sessions.COOKIE_NAME))

    mode = body.get("mode") or "rerun_all"
    scope = dict(prev.scope)
    scale_down = bool(body.get("scale_down"))

    if mode == "retry_function":
        fn = (body.get("function") or "").strip()
        if not fn:
            return JSONResponse({"ok": False, "error": "retry_function needs a function name."},
                                status_code=400)
        scope["only_functions"] = [fn]
    elif mode == "continue":
        fns = list(body.get("functions") or [])
        if not fns:
            # Derive remaining = functions seen but not verified in the prior run.
            fns = [name for name, ev in
                   {f["name"]: f for f in prev.snapshot()["functions"]}.items()
                   if ev.get("status") != "verified"]
        scope["only_functions"] = fns
    else:  # rerun_all
        scope["only_functions"] = list(prev.scope.get("only_functions") or [])

    try:
        target, is_dir, repo_dir = _resolve_scope_target(workspace, scope)
    except ValueError as exc:
        return _attach_session_cookie(
            JSONResponse({"ok": False, "error": str(exc)}, status_code=400), session_id
        )

    # Reuse the original run's LLM selection but refresh the key from this
    # request — finish() scrubs prev.llm["key"], and the client always resends
    # X-LLM-Key, so the run isn't gated on a stale (now-empty) key.
    llm = {**prev.llm, "key": _read_llm_config(request).get("key", "")}
    job = jobs.STORE.create(session_id, scope, llm, budget_cap=prev.budget_cap)
    factory = _build_gen_factory(
        target, is_dir, scope["only_functions"], scope.get("domain_knowledge", ""),
        llm, scale_down=scale_down, source_root=repo_dir,
        max_files=scope.get("max_files"), opts=scope.get("options"),
    )
    jobs.run_job(job, factory)
    return _attach_session_cookie(JSONResponse({"ok": True, "run_id": job.run_id}), session_id)
