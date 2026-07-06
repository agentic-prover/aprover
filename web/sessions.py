"""
Per-chat server-side sessions for the AProver web chat.

Each browser gets a random, ``HttpOnly`` cookie (``aprover_session``) bound to an
isolated temp **workspace** on disk. Cloned repos live under that workspace so the
agent can reference them across tool calls within one chat — and only that chat:
tools resolve every repo path through :func:`safe_path`, which refuses anything
outside the requester's own workspace, so no other visitor can reach it.

Sessions are kept in memory (cleared on restart), evicted after an idle TTL and
trimmed to a maximum count (LRU). Nothing here is durable by design — it's a
scratch space for a live demo.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from web._evict import evict
from web.limits import MAX_SESSIONS as _MAX_SESSIONS
from web.limits import SESSION_TTL as _TTL_SECONDS

COOKIE_NAME = "aprover_session"

# Idle lifetime and population cap (env-overridable, see web/limits.py). A
# session is touched on every access; one idle longer than TTL (or beyond the
# LRU cap) has its workspace removed.

_ROOT = Path(tempfile.gettempdir()) / "aprover_sessions"


class SessionStore:
    """In-memory registry of ``session_id -> {workspace, last_used}``."""

    def __init__(self, root: Path = _ROOT, ttl: int = _TTL_SECONDS, cap: int = _MAX_SESSIONS) -> None:
        self._root = root
        self._ttl = ttl
        self._cap = cap
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    def get_or_create(self, session_id: str | None) -> tuple[str, Path, bool]:
        """Resolve ``session_id`` (from the cookie) to a live workspace.

        Returns ``(session_id, workspace, is_new)``. Mints a fresh id + workspace
        when the cookie is missing or its session has expired. Runs eviction as a
        side effect so stale workspaces don't accumulate.
        """
        now = time.time()
        with self._lock:
            self._evict(now)
            sess = self._sessions.get(session_id or "")
            if sess is not None and sess["workspace"].is_dir():
                sess["last_used"] = now
                return session_id, sess["workspace"], False  # type: ignore[return-value]

            new_id = uuid.uuid4().hex
            workspace = self._root / new_id
            workspace.mkdir(parents=True, exist_ok=True)
            self._sessions[new_id] = {"workspace": workspace, "last_used": now}
            self._evict(now)  # honour the cap including the one just added
            return new_id, workspace, True

    def touch(self, session_id: str | None) -> bool:
        """Refresh a session's idle timer so a long run isn't evicted under it.

        Returns ``True`` if the session is still live. A run streamed only over
        SSE issues no other request for its whole duration, so without this the
        idle TTL could ``rmtree`` the workspace mid-pipeline.
        """
        with self._lock:
            sess = self._sessions.get(session_id or "")
            if sess is None:
                return False
            sess["last_used"] = time.time()
            return True

    def _evict(self, now: float) -> None:
        """Remove timed-out sessions, then LRU-trim to the cap. Caller holds lock."""
        evict(
            self._sessions, now, self._ttl, self._cap,
            last_used=lambda s: s["last_used"], on_drop=self._drop,
        )

    def _drop(self, sid: str) -> None:
        sess = self._sessions.pop(sid, None)
        if sess is not None:
            shutil.rmtree(sess["workspace"], ignore_errors=True)


def safe_path(workspace: Path, rel: str) -> Path:
    """Resolve ``rel`` strictly inside ``workspace``.

    Raises ``ValueError`` on absolute paths, ``..`` traversal, or anything that
    resolves outside the workspace — the isolation boundary for session files.
    """
    rel = (rel or "").strip()
    if not rel:
        raise ValueError("Empty path.")
    candidate = (workspace / rel).resolve()
    root = workspace.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Path escapes the session workspace.")
    return candidate


# Module-level singleton shared by the web app.
STORE = SessionStore()
