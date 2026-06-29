"""
Language-dispatching source-file parser.

Maps a file path to the right per-language parser and returns its parsed
result.  The C and Rust parsers return distinct ``Parsed*File`` types but
they are *structurally* compatible — same field names, same
``get_function_info`` / ``all_function_infos`` API — so the rest of
AProver's pipeline can consume either without branching on language.

Dispatch is by extension only.  Unknown extensions raise
``UnsupportedSourceLanguage`` rather than silently falling back, so a
mistyped path surfaces immediately rather than producing an empty parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from bmc_agent.java_parser import ParsedJavaFile, parse_java_file
from bmc_agent.parser import ParsedCFile, parse_c_file
from bmc_agent.rust_parser import ParsedRustFile, parse_rust_file


ParsedSourceFile = Union[ParsedCFile, ParsedRustFile, ParsedJavaFile]


class UnsupportedSourceLanguage(ValueError):
    """Raised when a path's extension does not map to a known parser."""


# Extension -> parser entry-point, in the form expected at the call site.
# Header files are routed to the C parser because header-only specs are
# the dominant pattern in AProver's existing C corpus. ``.i`` is the
# conventional output of ``cc -E`` / kernel ``make foo.i`` — a
# preprocessed C translation unit — so it routes to the C parser too.
_C_EXTS = frozenset({".c", ".h", ".i"})
_RUST_EXTS = frozenset({".rs"})
_JAVA_EXTS = frozenset({".java"})


@dataclass(frozen=True)
class LanguageInfo:
    """Static metadata for one supported source language.

    This is the single source of truth that the rest of AProver — including
    the web layer's upload/clone/tree filters — derives from. Adding a new
    language means appending one entry here, adding its parser branch to
    :func:`parse_source_file`, and its backend to
    :func:`bmc_agent.backends.backend_for`. No call site enumerates
    extensions itself.
    """

    id: str                       # "c" | "rust" | "java"
    display: str                  # human-facing name, e.g. "C", "Rust", "Java"
    extensions: frozenset[str]    # file suffixes (lower-case, dot-prefixed)
    verifier: str                 # backend display name, e.g. "CBMC"


# Registry of supported languages, in dispatch order.
LANGUAGES: tuple[LanguageInfo, ...] = (
    LanguageInfo("c", "C", _C_EXTS, "CBMC"),
    LanguageInfo("rust", "Rust", _RUST_EXTS, "Kani"),
    LanguageInfo("java", "Java", _JAVA_EXTS, "JBMC"),
)

# Union of every verifiable source extension.
CODE_EXTENSIONS: frozenset[str] = frozenset().union(
    *(lang.extensions for lang in LANGUAGES)
)

# Comma-separated display list, e.g. "C, Rust, Java" — for user-facing copy.
SUPPORTED_DISPLAY: str = ", ".join(lang.display for lang in LANGUAGES)

_EXT_TO_LANG: dict[str, str] = {
    ext: lang.id for lang in LANGUAGES for ext in lang.extensions
}


def language_for_ext(ext: str) -> Optional[str]:
    """Return the language id for a file suffix (e.g. ``".rs"`` -> ``"rust"``).

    Returns ``None`` for any unregistered extension. Case-insensitive.
    """
    return _EXT_TO_LANG.get(ext.lower())


def is_code_file(path: str | Path) -> bool:
    """True if *path*'s extension maps to a supported language."""
    return Path(path).suffix.lower() in CODE_EXTENSIONS


def detect_language(path: str | Path) -> str:
    """Return ``"c"``, ``"rust"``, or ``"java"`` based on *path*'s extension.

    Raises ``UnsupportedSourceLanguage`` for any other extension.
    """
    ext = Path(path).suffix.lower()
    lang = _EXT_TO_LANG.get(ext)
    if lang is not None:
        return lang
    supported = ", ".join(sorted(CODE_EXTENSIONS))
    raise UnsupportedSourceLanguage(
        f"No parser registered for extension {ext!r} (path={path!r}). "
        f"Supported: {supported}"
    )


def parse_source_file(
    path: str | Path,
    source_text: Optional[str] = None,
) -> ParsedSourceFile:
    """Parse *path* with the right per-language backend.

    Parameters
    ----------
    path:
        Path to the source file.  The extension selects the parser.
    source_text:
        Optional in-memory source.  Both parsers accept this so the
        caller can pass preprocessed C (after ``cc -E``) or
        macro-expanded Rust to the parser without re-reading from disk.

    Returns
    -------
    Either a :class:`ParsedCFile` or :class:`ParsedRustFile`.  Both
    expose ``functions``, ``call_graph``, ``function_bodies``,
    ``preprocessed_source``, ``get_function_info`` and
    ``all_function_infos``, so downstream code does not need to branch.
    """
    lang = detect_language(path)
    if lang == "c":
        parsed = parse_c_file(path, source_text=source_text)
        # When the input is a preprocessed translation unit (cpp ``# N
        # "filename"`` line directives present, ``primary_source`` is
        # set), automatically drop header-inlined functions. A kernel
        # driver ``.i`` pulls in ~4400 ``static inline`` helpers from
        # ``include/linux/*.h``; the pipeline cares only about the
        # ~25 functions actually defined in the driver. Filtering here
        # (rather than at each CLI command's parse call) ensures every
        # entry point — generate, check, verify, verify-dir, baselines
        # — sees a consistent set and the harness generator's
        # type-decl extractor doesn't choke on byte-range mismatches
        # against thousands of complex function bodies.
        if getattr(parsed, "primary_source", None) and hasattr(parsed, "restrict_to_primary_source"):
            parsed.restrict_to_primary_source()
        return parsed
    if lang == "rust":
        return parse_rust_file(path, source_text=source_text)
    # lang == "java"
    return parse_java_file(path, source_text=source_text)
