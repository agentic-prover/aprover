"""Boundary detection: which functions are exposed externally?

The v2 spec generator treats boundary functions differently from internal
ones. Internal functions get caller-grounded specs (the LLM reads K call
sites and reconciles with the body). Boundary functions get trivial
specs (true/true) because the "caller" of a boundary is attacker-
controlled input — there's no honest caller to ground against.

Without this distinction, caller-grounding *over-protects* libarchive's
public format-decoders: the LLM sees benign in-tree callers, concludes
"all callers pass well-formed bytes," and the spec excludes the very
adversarial inputs the parser is supposed to handle. Bugs that fuzzers
exist to find disappear in the spec.

This module is intentionally syntactic: parse the project's public
headers, extract declared function names, expose `is_boundary(name)`.
No semantic analysis, no preprocessing — just "is this name declared in
a header the build exposes."
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# ---------- declaration extractor -------------------------------------------

# Matches a function declaration's tail: `<name>(<args>);` after any
# return-type / attribute prefix. We extract <name>.
#
# Conservative — handles libarchive style and most standard C:
#   `__LA_DECL int archive_version_number(void);`
#   `extern int foo(int, char *);`
#   `static inline int bar(void) { ... }`  (we skip definitions; only `;` after)
#   `int baz(int);`
#
# We do NOT match:
#   `typedef int (*fn_t)(int);`         — function-pointer typedef
#   `int (*p)(int);`                    — function-pointer variable
#   `#define FOO(x) ...`                — preprocessor
#   `struct foo { int (*cb)(int); };`   — struct member function pointer
_DECL_TAIL_RX = re.compile(
    r"""
    (?<![A-Za-z0-9_])    # left boundary: prev char isn't part of an identifier
                         # (we allow `*` so `struct archive *foo(` matches `foo`)
    ([A-Za-z_]\w*)       # captured: function name
    \s*\(                # open paren
    [^)]*                # arg list (no nested parens — handled by accumulation below)
    \)\s*;               # close paren and semicolon
    """,
    re.VERBOSE | re.DOTALL,
)


def _line_is_preproc_or_comment(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("#") or s.startswith("//") or s.startswith("*") or s.startswith("/*")


def _strip_block_comments(text: str) -> str:
    """Strip /* ... */ comments. Single-line // are handled per-line."""
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)


_EXTERN_C_OPEN_RX = re.compile(r'extern\s*"C"\s*\{')


def _strip_extern_c_wrapper(text: str) -> str:
    """Remove `extern "C" {` ... matching `}` wrappers so brace tracking
    in the declaration accumulator sees the header's top-level scope.

    Handles any number of nested wrappers (rare but possible). Matches
    the close brace by balanced-paren walk from each open.
    """
    while True:
        m = _EXTERN_C_OPEN_RX.search(text)
        if not m:
            return text
        # Find the matching close by brace balance, ignoring braces in
        # strings/chars (cheap; doesn't strip comments — caller already did).
        depth = 1
        i = m.end()
        while i < len(text) and depth > 0:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        if depth != 0:
            # Unbalanced — bail out without modifying.
            return text
        # Replace opener + matching closer with whitespace of same width
        # to preserve byte offsets (line counts stay sane for any
        # diagnostics downstream).
        text = (
            text[: m.start()]
            + " " * (m.end() - m.start())
            + text[m.end() : i - 1]
            + " "
            + text[i:]
        )


def _accumulate_declarations(text: str) -> Iterable[str]:
    """Yield logical declaration statements from header text.

    A logical declaration is everything from after the previous `;` (or
    start of file) up to the next `;`, with parenthesis balance honored
    so `int foo(int (*)(int));` arrives as one token. Preprocessor lines
    and { ... } blocks are skipped.
    """
    text = _strip_block_comments(text)
    text = _strip_extern_c_wrapper(text)
    buf: list[str] = []
    paren_depth = 0
    brace_depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        # Skip preprocessor lines wholesale.
        if (i == 0 or text[i - 1] == "\n") and c == "#":
            nl = text.find("\n", i)
            i = nl + 1 if nl >= 0 else len(text)
            continue
        # Skip single-line // comments.
        if c == "/" and i + 1 < len(text) and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl >= 0 else len(text)
            continue
        if c == "{":
            brace_depth += 1
            buf.append(c)
        elif c == "}":
            brace_depth = max(0, brace_depth - 1)
            buf.append(c)
        elif brace_depth > 0:
            # Inside a function body or struct body — skip nothing,
            # but don't try to extract declarations from here.
            buf.append(c)
        elif c == "(":
            paren_depth += 1
            buf.append(c)
        elif c == ")":
            paren_depth = max(0, paren_depth - 1)
            buf.append(c)
        elif c == ";" and paren_depth == 0 and brace_depth == 0:
            chunk = "".join(buf).strip()
            buf.clear()
            if chunk:
                yield chunk + ";"
        else:
            buf.append(c)
        i += 1
    # Trailing buffer (no final ;) — ignore.


def extract_public_functions(header_paths: Iterable[Path]) -> set[str]:
    """Parse declarations from the given header paths.

    Returns the set of function names that appear as declarations
    (i.e., `name(args);` after any return type, attributes, or storage
    class). Function-pointer typedefs and members are excluded.
    """
    names: set[str] = set()
    for path in header_paths:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            logger.debug("boundary_detector: skipping unreadable header %s", path)
            continue
        for stmt in _accumulate_declarations(text):
            # Reject typedefs and macro-expanded chunks.
            head = stmt.lstrip()
            if head.startswith("typedef"):
                continue
            # Reject function-pointer-only declarations like `int (*p)(int);`
            # by checking for `(*` before the first `(`.
            first_paren = stmt.find("(")
            if first_paren > 0 and stmt[first_paren - 1 : first_paren + 2] == "(*":
                continue
            # Match the final `name(...)`; tail of the statement.
            m = None
            for candidate in _DECL_TAIL_RX.finditer(stmt):
                m = candidate  # take the last match (the actual declarator)
            if not m:
                continue
            name = m.group(1)
            # Reject obvious non-functions: keywords + common type tokens.
            if name in _C_KEYWORDS:
                continue
            names.add(name)
    return names


_C_KEYWORDS = {
    "if", "while", "for", "switch", "return", "sizeof", "typeof",
    "do", "else", "case", "default", "break", "continue", "goto",
    "struct", "union", "enum", "typedef",
}


# ---------- detector class --------------------------------------------------


@dataclass
class BoundaryDetector:
    """Stateful boundary detector. Construct once per sweep.

    Typical use:
        bd = BoundaryDetector.from_paths([Path("libarchive/archive.h"),
                                          Path("libarchive/archive_entry.h")])
        if bd.is_boundary("archive_read_open_filename"):
            # skip caller-grounding; use trivial spec
            ...

    The set of names is computed eagerly at construction. If no headers
    are supplied, ``is_boundary`` always returns False — equivalent to
    treating every function as internal (back-compat with corpora that
    don't ship public headers).
    """

    public_names: frozenset[str] = field(default_factory=frozenset)
    header_paths: tuple[Path, ...] = ()

    @classmethod
    def from_paths(cls, paths: Iterable[Path]) -> "BoundaryDetector":
        paths_t = tuple(paths)
        names = extract_public_functions(paths_t)
        return cls(public_names=frozenset(names), header_paths=paths_t)

    @classmethod
    def autodiscover(
        cls,
        source_dir: Path,
        *,
        explicit_headers: Optional[Iterable[Path]] = None,
    ) -> "BoundaryDetector":
        """Discover headers near ``source_dir`` automatically.

        Strategy: take every *.h file at the top level of ``source_dir``
        whose basename does NOT match ``*_private.h`` or ``*_internal.h``.
        That's a reasonable proxy for "header the build system installs."
        Caller can supplement with ``explicit_headers``.
        """
        if explicit_headers:
            paths = list(explicit_headers)
        else:
            paths = []
        if source_dir.is_dir():
            for h in sorted(source_dir.glob("*.h")):
                name = h.name
                if name.endswith("_private.h") or name.endswith("_internal.h"):
                    continue
                paths.append(h)
        return cls.from_paths(paths)

    def is_boundary(self, fn_name: str) -> bool:
        return fn_name in self.public_names

    def __len__(self) -> int:
        return len(self.public_names)
