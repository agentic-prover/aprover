"""
C preprocessor integration for AMC.

Expands #include directives via `cc -E` so that each source file
becomes a self-contained translation unit that the parser and CBMC
can handle without knowing the original include paths.

Also strips GCC/ARM64 extensions that CBMC does not accept:
  __attribute__((...)), __asm__/__asm blocks, _Noreturn, typeof,
  and register-asm variable declarations.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from bmc_agent.logger import get_logger

logger = get_logger("preprocessor")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def preprocess(
    source_file: str | Path,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    cc: str = "cc",
) -> str:
    """
    Return a cleaned, self-contained C source string for *source_file*.

    Steps:
      1. Run ``cc -E -P`` to expand all #include references.
      2. Strip lines that originate from system headers (``/usr/``, ``/lib/``).
      3. Strip GCC/ARM64 extensions that confuse CBMC.
      4. Prepend standard CBMC-friendly headers.

    Parameters
    ----------
    source_file:
        Path to the ``.c`` file to preprocess.
    include_dirs:
        List of ``-I`` paths to pass to the compiler.
    defines:
        List of ``-D`` macros to pass to the compiler.
    cc:
        C compiler binary to use for preprocessing (default ``cc``).
    """
    source_file = Path(source_file)
    include_dirs = include_dirs or []
    defines = defines or []

    expanded = _run_preprocessor(source_file, include_dirs, defines, cc)
    cleaned = _strip_system_content(expanded, source_file)
    cleaned = _strip_gcc_extensions(cleaned)
    cleaned = _prepend_cbmc_headers(cleaned)
    return cleaned


_HEADER_GLOBS = ("*.h", "*.hpp", "*.hh", "*.hxx")
# Directories never worth scanning for project headers (VCS / build / vendor).
_SKIP_DIRS = {".git", ".svn", ".hg", "build", "_build", "out", "node_modules"}
# Bound on the number of -I paths discovered, so a pathological repo can't
# explode the preprocessor command line. Shallower dirs are kept first.
_MAX_INCLUDE_DIRS = 200


def discover_include_dirs(
    source_dir: str | Path,
    exclude_patterns: "list[str] | tuple[str, ...]" = (),
) -> list[str]:
    """Best-effort discovery of a repo's ``-I`` include directories.

    A cloned repo's headers (e.g. ``include/window-rules.h``) aren't found by
    ``cc -E`` unless their directory is on the include path. The web path has no
    UI to supply ``-I`` paths, so without this every harness fails to build with
    ``fatal error: <header>: No such file or directory``. Heuristic:

    - every directory containing a header file (resolves ``#include "foo.h"``),
    - any ``include``/``inc`` ancestor of a header (resolves nested
      ``#include "labwc/foo.h"`` style), and
    - ``source_dir`` itself.

    Skips VCS/build/vendor dirs and anything matching *exclude_patterns* (note:
    these source-file patterns are matched against header names too, so a pattern
    like ``*test*`` also drops matching headers — at worst this discovers fewer
    include dirs, never an unsound result). Returns deduped absolute paths,
    shallowest first, capped at ``_MAX_INCLUDE_DIRS``.
    """
    import fnmatch

    source_dir = Path(source_dir).resolve()
    found: set[Path] = {source_dir}

    for pattern in _HEADER_GLOBS:
        for header in source_dir.rglob(pattern):
            parts = header.relative_to(source_dir).parts
            if any(p in _SKIP_DIRS for p in parts[:-1]):
                continue
            if any(fnmatch.fnmatch(header.name, pat) for pat in exclude_patterns):
                continue
            parent = header.parent
            found.add(parent)
            # Add any include/inc ancestor so `#include "sub/foo.h"` resolves.
            for anc in parent.parents:
                if anc == source_dir.parent:
                    break
                if anc.name in ("include", "inc"):
                    found.add(anc)

    # Shallowest first: keeps the most generally-useful roots when capped.
    ordered = sorted(found, key=lambda p: (len(p.parts), str(p)))
    if len(ordered) > _MAX_INCLUDE_DIRS:
        logger.info(
            "discover_include_dirs: %d candidate dirs, capping to %d",
            len(ordered), _MAX_INCLUDE_DIRS,
        )
        ordered = ordered[:_MAX_INCLUDE_DIRS]
    return [str(p) for p in ordered]


def preprocess_to_file(
    source_file: str | Path,
    output_file: str | Path,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    cc: str = "cc",
) -> Path:
    """Preprocess *source_file* and write the result to *output_file*."""
    result = preprocess(source_file, include_dirs, defines, cc)
    out = Path(output_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Step 1: run cc -E
# ---------------------------------------------------------------------------


# Missing-header diagnostics from cc/clang, e.g.
#   gcc:   fatal error: wlr/types/wlr_output.h: No such file or directory
#   clang: fatal error: 'wlr/types/wlr_output.h' file not found
_MISSING_HEADER_RES = (
    re.compile(r"fatal error:\s*([^\n:'\"]+?):\s*No such file or directory"),
    re.compile(r"fatal error:\s*['\"]([^'\"\n]+)['\"]\s*file not found"),
)
# Bound on empty header stubs created per file, so a pathological include graph
# can't spin forever (each stub costs one cc -E re-run).
_MAX_HEADER_STUBS = 100


def _missing_header(stderr: str) -> "str | None":
    """Header path from a cc/clang "No such file" fatal error, or None."""
    for rx in _MISSING_HEADER_RES:
        m = rx.search(stderr)
        if m:
            hdr = m.group(1).strip()
            # Reject obvious non-paths (defensive against odd compiler output)
            # and any path that would escape the stub dir: ``..`` traversal or an
            # absolute path (``stub_dir / "/abs"`` resolves to ``/abs`` itself,
            # writing the stub outside the temp dir).
            if hdr and ".." not in hdr.split("/") and not os.path.isabs(hdr):
                return hdr
    return None


def _run_preprocessor(
    source_file: Path,
    include_dirs: list[str],
    defines: list[str],
    cc: str,
) -> str:
    """Run ``cc -E -P``, stubbing unresolvable headers so it can complete.

    Real projects #include third-party headers that aren't in the uploaded tree
    (e.g. labwc -> <wlr/...>, <wayland-*>), often nested inside an otherwise
    resolvable project header. ``cc -E`` hard-fails on the first missing one,
    which previously dropped us to RAW source with the dangling #include intact
    — and CBMC then can't build the harness ("harness build failed" for every
    function). Instead, on each "No such file" error we create an EMPTY stub for
    the named header on a private -I dir (lowest priority, so real headers still
    win) and retry. Project + libc headers expand normally; only genuinely-absent
    externals become empty stubs, leaving NO dangling #include in the output.
    """
    stub_dir = Path(tempfile.mkdtemp(prefix="amc_hdrstub_"))
    stubbed: list[str] = []
    try:
        while True:
            cmd = [cc, "-E", "-P"]
            for d in include_dirs:
                cmd += ["-I", d]
            cmd += ["-I", str(stub_dir)]  # last: stubs only fill genuine gaps
            for define in defines:
                cmd += ["-D", define]
            # Suppress warnings; treat as plain C
            cmd += ["-w", "-x", "c", str(source_file)]

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                logger.warning("Cannot run preprocessor (%s): %s — reading file as-is", cc, exc)
                return source_file.read_text(encoding="utf-8", errors="replace")

            if result.returncode == 0 or result.stdout.strip():
                if stubbed:
                    # WARNING, not INFO: empty stubs drop the headers' macros /
                    # consts / types, so the preprocessed TU fed to the backend
                    # has altered semantics. A verdict on it should be auditable.
                    logger.warning(
                        "preprocess: stubbed %d unresolved header(s) for %s — "
                        "verdict rests on source with those definitions removed: %s",
                        len(stubbed), source_file.name,
                        ", ".join(sorted(set(stubbed))[:20]),
                    )
                return result.stdout

            hdr = _missing_header(result.stderr)
            if hdr is None or hdr in stubbed or len(stubbed) >= _MAX_HEADER_STUBS:
                logger.warning(
                    "Preprocessor failed for %s: %s", source_file, result.stderr[:200]
                )
                # Fall back to reading the file as-is.
                return source_file.read_text(encoding="utf-8", errors="replace")

            stub_path = stub_dir / hdr
            try:
                stub_path.parent.mkdir(parents=True, exist_ok=True)
                stub_path.write_text(
                    f"/* AMC: empty stub for unresolved header {hdr} */\n",
                    encoding="utf-8",
                )
            except OSError:
                # Can't materialize the stub (odd path) — give up cleanly.
                return source_file.read_text(encoding="utf-8", errors="replace")
            stubbed.append(hdr)
    finally:
        import shutil
        shutil.rmtree(stub_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Step 2: strip system-header content
# ---------------------------------------------------------------------------

# Marker lines emitted by cc -E (without -P): # <lineno> "<path>" <flags>
# We use them to track which file lines belong to.  With -P they are absent,
# but system headers expand inline.  We detect system content by checking
# whether it looks like it came from standard paths.
#
# Without -P we get linemarkers; strip content from system paths.
# With -P we don't — fall back to heuristic stripping of known system decls.

_SYSTEM_PATHS = ("/usr/", "/lib/", "/opt/homebrew/", "/Applications/Xcode")
_LINEMARKER = re.compile(r'^# \d+ "([^"]+)"')


def _strip_system_content(source: str, original_file: Path) -> str:
    """Remove content that expanded from system headers."""
    lines = source.splitlines(keepends=True)

    # Check if preprocessor emitted linemarkers (happens without -P).
    has_markers = any(_LINEMARKER.match(l) for l in lines[:50])
    if has_markers:
        return _strip_by_linemarkers(lines, original_file)

    # -P was used (no markers): keep everything — the system headers already
    # provided only type declarations that CBMC needs.  Just remove duplicate
    # blank lines to keep the file manageable.
    return _collapse_blanks(source)


def _strip_by_linemarkers(lines: list[str], original_file: Path) -> str:
    in_user_file = True
    out: list[str] = []
    for line in lines:
        m = _LINEMARKER.match(line)
        if m:
            path = m.group(1)
            in_user_file = not any(path.startswith(sp) for sp in _SYSTEM_PATHS)
            continue  # don't emit the marker itself
        if in_user_file:
            out.append(line)
    return "".join(out)


def _collapse_blanks(source: str) -> str:
    return re.sub(r'\n{3,}', '\n\n', source)


# ---------------------------------------------------------------------------
# Step 3: strip GCC / ARM64 extensions
# ---------------------------------------------------------------------------

def _strip_attributes_nested(source: str) -> str:
    """Remove __attribute__((...)) handling nested parentheses correctly."""
    result = []
    i = 0
    n = len(source)
    marker = "__attribute__"
    mlen = len(marker)
    while i < n:
        if source[i:i+mlen] == marker:
            j = i + mlen
            # skip whitespace
            while j < n and source[j] in ' \t\n\r':
                j += 1
            if j < n and source[j] == '(':
                # consume the outer paren pair with nesting count
                depth = 0
                while j < n:
                    if source[j] == '(':
                        depth += 1
                    elif source[j] == ')':
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
                i = j  # skip entire __attribute__((...))
            else:
                result.append(source[i])
                i += 1
        else:
            result.append(source[i])
            i += 1
    return ''.join(result)

_ATTR_RE = re.compile(r'__attribute__\s*\(.*?\)', re.DOTALL)  # fallback, unused
_DECLSPEC_RE = re.compile(r'__declspec\s*\([^)]*\)')
_TYPEOF_RE = re.compile(r'\btypeof\s*\(')
# __asm__ volatile ( "..." : ... : ... : ... ) or __asm__ ( "..." )
_ASM_RE = re.compile(
    r'__asm__\s*(?:volatile\s*)?\s*\([^;]*\)\s*;',
    re.DOTALL,
)
# register uint64_t x asm("reg") — ARM register variable
_REGVAR_RE = re.compile(
    r'\bregister\b([^;]+)\basm\s*\([^)]*\)\s*;',
)
# _Noreturn (C11 keyword CBMC may not handle)
_NORETURN_RE = re.compile(r'\b_Noreturn\b')
# __extension__
_EXTENSION_RE = re.compile(r'\b__extension__\b')
# __restrict / __restrict__
_RESTRICT_RE = re.compile(r'\b__restrict(?:__)?(\s)')
# __volatile__ (same as volatile)
_VOLATILE_RE = re.compile(r'\b__volatile__\b')
# __const__ (same as const)
_CONST_RE = re.compile(r'\b__const__\b')
# __signed__ (same as signed)
_SIGNED_RE = re.compile(r'\b__signed__\b')
# __inline__ / __inline (same as inline)
_INLINE_RE = re.compile(r'\b__inline(?:__)?\b')


def _strip_gcc_extensions(source: str) -> str:
    source = _strip_attributes_nested(source)
    source = _DECLSPEC_RE.sub('', source)
    source = _ASM_RE.sub(';', source)
    source = _REGVAR_RE.sub(r'/* register-asm variable removed */;', source)
    source = _NORETURN_RE.sub('', source)
    source = _EXTENSION_RE.sub('', source)
    source = _RESTRICT_RE.sub(r'\1', source)
    source = _VOLATILE_RE.sub('volatile', source)
    source = _CONST_RE.sub('const', source)
    source = _SIGNED_RE.sub('signed', source)
    source = _INLINE_RE.sub('inline', source)
    return source


# ---------------------------------------------------------------------------
# Step 4: prepend CBMC-friendly headers
# ---------------------------------------------------------------------------

_CBMC_PREAMBLE = """\
/* AMC preprocessor preamble — CBMC-friendly type definitions */
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <assert.h>
#ifndef NULL
#define NULL ((void*)0)
#endif
#ifndef true
#define true 1
#define false 0
#endif

"""


def _prepend_cbmc_headers(source: str) -> str:
    # Avoid duplicate preamble if preprocessing ran twice
    if "AMC preprocessor preamble" in source:
        return source
    return _CBMC_PREAMBLE + source
