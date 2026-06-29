"""
Shallow git-clone for the AProver chat ``clone_repo`` tool.

Pulls a public repo into a session workspace so the agent can verify a whole
project (or one file from it). Safety constraints mirror ``web/fetch.py``:

- https only (no ssh/git/file — those bypass the host check or hit the FS)
- block loopback / RFC1918 / link-local hosts (reuses ``fetch._is_private_host``)
- shallow, single-branch, no submodules, credentials disabled, bounded timeout
- working-tree caps: total verifiable-source bytes and source-file count
- ``.git`` is removed after clone (history isn't needed for verification)

"Verifiable source" is any file whose extension is in the language registry
(``bmc_agent.source_parser.CODE_EXTENSIONS`` — C/Rust/Java today), so a new
language becomes cloneable here for free.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from bmc_agent.source_parser import CODE_EXTENSIONS, SUPPORTED_DISPLAY
from web.fetch import _is_private_host
from web.limits import CLONE_TIMEOUT as _CLONE_TIMEOUT
from web.limits import LIST_LIMIT as _LIST_LIMIT
from web.limits import MAX_REPO_BYTES as _MAX_REPO_BYTES
from web.limits import MAX_SRC_FILES as _MAX_SRC_FILES

_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_repo_name(url: str) -> str:
    """Derive a filesystem-safe directory name from a clone URL.

    ``.`` is permitted inside a name (e.g. ``libfoo.c``), so the result is
    explicitly checked against the path-traversal names ``.`` and ``..``: those
    would otherwise resolve to the workspace itself or its parent (the shared
    sessions root) and let an ``rmtree`` of the clone/upload dest escape.
    """
    tail = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
    tail = re.sub(r"\.git$", "", tail)
    name = _NAME_RE.sub("-", tail).strip("-")
    if name in {".", ".."}:
        return "repo"
    return name or "repo"


def _validate(url: str) -> str | None:
    """Return an error string if ``url`` is unsafe to clone, else ``None``."""
    parts = urllib.parse.urlparse(url.strip())
    if parts.scheme != "https":
        return f"Only https git URLs are supported (got {parts.scheme!r})."
    if not parts.hostname:
        return "URL has no host."
    if _is_private_host(parts.hostname):
        return f"Refusing to clone from private/loopback host: {parts.hostname}"
    return None


def _tree_stats(dest: Path) -> tuple[int, int, list[tuple[str, int]]]:
    """Return (total source bytes, source-file count, [(relpath, bytes)]).

    "Source" is any verifiable file (extension in the language registry).
    """
    total = 0
    src_count = 0
    files: list[tuple[str, int]] = []
    for p in sorted(dest.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in CODE_EXTENSIONS:
            continue
        size = p.stat().st_size
        total += size
        src_count += 1
        files.append((str(p.relative_to(dest)), size))
    return total, src_count, files


def clone_repo(url: str, ref: str, dest: Path) -> tuple[bool, dict | str]:
    """Shallow-clone ``url`` into ``dest``. Returns ``(ok, info | error)``.

    On success ``info`` is ``{repo, files:[{path,bytes}], n_files, truncated}``
    with paths relative to ``dest``. ``dest`` is removed on any failure.
    """
    err = _validate(url)
    if err:
        return False, err

    if shutil.which("git") is None:
        return False, "git is not installed on the server — cloning is unavailable."

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    # http.followRedirects=false stops a vetted public host from 301-ing the
    # fetch to a loopback/RFC1918 target after _validate's one-shot DNS check
    # (the TOCTOU / redirect-rebind gap). GIT_ALLOW_PROTOCOL pins the transport
    # so a redirect can't downgrade to file://, ssh://, ext::, etc. either.
    cmd = ["git", "-c", "http.followRedirects=false", "clone", "--depth", "1", "--single-branch"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url.strip(), str(dest)]

    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ASKPASS": "true",
        "GCM_INTERACTIVE": "never",
        "GIT_ALLOW_PROTOCOL": "https",
    }
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"Clone timed out after {_CLONE_TIMEOUT}s."
    except Exception as exc:  # pragma: no cover
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"{type(exc).__name__}: {exc}"

    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        msg = (proc.stderr or proc.stdout or "git clone failed").strip()
        return False, f"git clone failed: {msg[:400]}"

    # Drop history; verification only needs the working tree.
    shutil.rmtree(dest / ".git", ignore_errors=True)

    total, src_count, files = _tree_stats(dest)
    if src_count == 0:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"Repo has no verifiable source files ({SUPPORTED_DISPLAY})."
    if src_count > _MAX_SRC_FILES:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"Repo has too many source files ({src_count}; cap is {_MAX_SRC_FILES})."
    if total > _MAX_REPO_BYTES:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"Repo sources too large ({total} bytes; cap is {_MAX_REPO_BYTES})."

    truncated = len(files) > _LIST_LIMIT
    listing = [{"path": p, "bytes": b} for p, b in files[:_LIST_LIMIT]]
    return True, {
        "repo": dest.name,
        "files": listing,
        "n_files": src_count,
        "truncated": truncated,
    }
