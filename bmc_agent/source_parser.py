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


def detect_language(path: str | Path) -> str:
    """Return ``"c"``, ``"rust"``, or ``"java"`` based on *path*'s extension.

    Raises ``UnsupportedSourceLanguage`` for any other extension.
    """
    ext = Path(path).suffix.lower()
    if ext in _C_EXTS:
        return "c"
    if ext in _RUST_EXTS:
        return "rust"
    if ext in _JAVA_EXTS:
        return "java"
    raise UnsupportedSourceLanguage(
        f"No parser registered for extension {ext!r} (path={path!r}). "
        "Supported: .c, .h, .i, .rs, .java"
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
