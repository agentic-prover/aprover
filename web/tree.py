"""
Project-tree + per-file function counts for the workbench "Choose scope" screen.

Walks a cloned repo (already size/file-count capped by ``web.gitclone``) and
returns a nested tree of code files/dirs, each annotated with how many
functions it defines, so the UI can show the "18 fns" badges and estimate run
cost from the selected scope.

Counts come from the same per-language parsers the pipeline uses (dispatched
through ``bmc_agent.source_parser.parse_source_file``), so a "function" here is
exactly what the pipeline will try to verify. The set of recognised file
extensions comes from the language registry
(``bmc_agent.source_parser.CODE_EXTENSIONS``) so new languages appear here for
free. Parse results are cached by (path, size) since a workbench session
re-requests the tree as the user navigates.
"""
from __future__ import annotations

import threading
from pathlib import Path

from bmc_agent.source_parser import (
    CODE_EXTENSIONS,
    detect_language,
    parse_source_file,
)
from web.limits import TREE_CACHE as _TREE_CACHE

# (path, size) -> n_functions. Bounded; cleared wholesale past the cache cap.
_count_cache: dict[tuple[str, int], int] = {}
_cache_lock = threading.Lock()


def _count_functions(path: Path) -> int:
    """Number of functions defined in a single source file (0 on parse error)."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    key = (str(path), size)
    with _cache_lock:
        if key in _count_cache:
            return _count_cache[key]

    n = 0
    try:
        # Same dispatch the pipeline uses, so every registered language
        # (C/Rust/Java today) is counted with the right parser.
        parsed = parse_source_file(str(path))
        n = len(parsed.functions or {})
    except Exception:
        n = 0

    with _cache_lock:
        if len(_count_cache) > _TREE_CACHE:
            _count_cache.clear()
        _count_cache[key] = n
    return n


def list_functions(path: Path) -> list[str]:
    """Names of the functions defined in a single source file (empty on parse
    error). Same dispatch as ``_count_functions`` so the names match exactly what
    the pipeline will try to verify — they feed the scope screen's per-function
    picker (``only_functions``)."""
    try:
        parsed = parse_source_file(str(path))
        return list(parsed.functions or {})
    except Exception:
        return []


def _build(node_dir: Path, repo_root: Path) -> dict | None:
    """Recursively build a tree node for ``node_dir``. Returns None if the
    subtree contains no code files (so empty/asset-only dirs are pruned)."""
    children: list[dict] = []
    total = 0
    try:
        entries = sorted(node_dir.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return None

    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        if entry.is_dir():
            child = _build(entry, repo_root)
            if child is not None:
                children.append(child)
                total += child["n_functions"]
        elif entry.is_file() and entry.suffix.lower() in CODE_EXTENSIONS:
            n = _count_functions(entry)
            total += n
            children.append({
                "name": entry.name,
                "type": "file",
                "path": str(entry.relative_to(repo_root)),
                "lang": detect_language(entry),
                "n_functions": n,
            })

    if not children:
        return None

    rel = "" if node_dir == repo_root else str(node_dir.relative_to(repo_root))
    return {
        "name": node_dir.name,
        "type": "dir",
        "path": rel,
        "n_functions": total,
        "children": children,
    }


def build_tree(repo_dir: Path, subdir: str = "") -> dict:
    """Build the scope tree for ``repo_dir`` (optionally rooted at ``subdir``).

    Returns a single dir node (``{name,type,path,n_functions,children}``). Raises
    ValueError if the resolved root isn't a directory. Caller is responsible for
    confining ``subdir`` to the session workspace via ``sessions.safe_path``."""
    root = repo_dir / subdir if subdir else repo_dir
    if not root.is_dir():
        raise ValueError(f"Not a directory: {subdir or repo_dir.name}")
    tree = _build(root, repo_dir)
    if tree is None:
        return {"name": root.name, "type": "dir", "path": subdir or "",
                "n_functions": 0, "children": []}
    return tree
