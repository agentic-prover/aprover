"""Tests for include-dir auto-discovery feeding the C preprocessor.

Regression for: cloned-repo web runs reporting "harness build failed" for every
function because the repo's `include/` dir was never on the `-I` path, so
`cc -E` failed and preprocess() fell back to raw source with unresolved
#include directives surviving into harness.c.
"""
from __future__ import annotations

import shutil

import pytest

from bmc_agent.parser import parse_c_file
from bmc_agent.preprocessor import discover_include_dirs, preprocess


def _make_repo(root):
    """labwc-style layout: src/foo.c includes a header living under include/."""
    (root / "src").mkdir(parents=True)
    (root / "include").mkdir(parents=True)
    (root / "include" / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_marker_decl(int);\n#endif\n"
    )
    (root / "src" / "foo.c").write_text(
        '#include "foo.h"\nint foo(int x) { return foo_marker_decl(x); }\n'
    )
    return root / "src" / "foo.c"


def test_discover_finds_include_dir(tmp_path):
    _make_repo(tmp_path)
    dirs = discover_include_dirs(tmp_path)
    assert str((tmp_path / "include").resolve()) in dirs
    # source_dir itself is always present.
    assert str(tmp_path.resolve()) in dirs


def test_discover_skips_vcs_and_build_dirs(tmp_path):
    _make_repo(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "junk.h").write_text("int junk;\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "gen.h").write_text("int gen;\n")
    dirs = discover_include_dirs(tmp_path)
    assert not any(d.endswith("/.git") for d in dirs)
    assert not any(d.endswith("/build") for d in dirs)


def test_discover_respects_exclude_patterns(tmp_path):
    _make_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "mock_thing.h").write_text("int m;\n")
    dirs = discover_include_dirs(tmp_path, ["*mock*"])
    assert not any(d.endswith("/tests") for d in dirs)


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler available")
def test_preprocess_resolves_project_header(tmp_path):
    src = _make_repo(tmp_path)
    # With discovered include dirs, the project header expands in-line.
    expanded = preprocess(str(src), include_dirs=discover_include_dirs(tmp_path))
    assert "foo_marker_decl" in expanded
    assert '#include "foo.h"' not in expanded


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler available")
def test_preprocess_stubs_missing_external_header(tmp_path):
    """A present project header that itself pulls in an ABSENT external header
    must still preprocess: the project header expands, the external one is
    stubbed, and NO dangling #include survives (else CBMC can't build it)."""
    _make_repo(tmp_path)
    # foo.h now includes a third-party header that isn't in the tree (nested
    # missing include — the labwc -> <wlr/...> case).
    (tmp_path / "include" / "foo.h").write_text(
        '#ifndef FOO_H\n#define FOO_H\n'
        '#include <ext/missing.h>\n'
        'int foo_marker_decl(int);\n#endif\n'
    )
    src = tmp_path / "src" / "foo.c"

    out = preprocess(str(src), include_dirs=discover_include_dirs(tmp_path))
    # Project header still expanded despite the nested missing include.
    assert "foo_marker_decl" in out
    # No live includes left for either header — nothing for CBMC to choke on.
    assert "missing.h" not in out
    assert '#include "foo.h"' not in out


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler available")
def test_missing_project_header_regresses_parse_but_raw_recovers(tmp_path):
    """Rationale for run_directory's raw fallback: when the project header is
    ABSENT (not uploaded), preprocessing stubs it empty, leaving param types
    undeclared so the parser finds 0 functions. Raw source still parses them, so
    the pipeline must prefer raw over the empty-stub preprocess in that case."""
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "win.c").write_text(
        '#include <stdio.h>\n#include "win.h"\n'
        "int wr_apply(struct win_rule *r, int x){ return r ? x + r->id : -1; }\n"
        "void wr_reset(struct win_rule *r){ if (r) r->id = 0; }\n"
    )
    src = tmp_path / "src" / "win.c"
    # No include dir / header on disk → header stubbed empty → 0 functions.
    stubbed = parse_c_file(str(src), source_text=preprocess(str(src), include_dirs=[]))
    assert not stubbed.functions
    # Raw source recovers them — this is what the fallback uses.
    raw = parse_c_file(str(src))
    assert set(raw.functions) == {"wr_apply", "wr_reset"}
