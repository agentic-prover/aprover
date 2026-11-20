"""Tests for the public-API reproducer-validation helpers in cex_validator.

Project-agnostic: covers libarchive, libcurl, libxml2, and a custom
project's headers via the autodiscovery path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bmc_agent.cex_validator import (
    _autodiscover_public_headers,
    _reproducer_uses_public_api,
)


# ---------- _reproducer_uses_public_api: built-in fallback set ------------


def test_libarchive_angle_include_accepted():
    code = "#include <archive.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(code) is True


def test_libarchive_entry_only_accepted():
    code = "#include <archive_entry.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(code) is True


def test_curl_include_accepted_via_builtin():
    code = "#include <curl/curl.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(code) is True


def test_libxml2_include_accepted_via_builtin():
    code = '#include "libxml/parser.h"\nint main(){return 0;}'
    assert _reproducer_uses_public_api(code) is True


def test_zlib_include_accepted_via_builtin():
    code = "#include <zlib.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(code) is True


def test_no_public_include_rejected():
    """The canonical bad pattern: LLM fabricated standalone reproducer."""
    code = """
    #include <stdio.h>
    #include <stdlib.h>
    struct archive_match { int x; };
    void some_fn(struct archive_match *a) { (void)a; }
    int main() { struct archive_match a = {0}; some_fn(&a); return 0; }
    """
    assert _reproducer_uses_public_api(code) is False


def test_only_libc_includes_rejected():
    """stdio.h / stdlib.h alone aren't a project library — reject."""
    code = "#include <stdio.h>\n#include <stdlib.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(code) is False


def test_quote_form_include_accepted():
    code = '#include "archive.h"\nint main(){return 0;}'
    assert _reproducer_uses_public_api(code) is True


def test_empty_source_rejected():
    assert _reproducer_uses_public_api("") is False
    assert _reproducer_uses_public_api("   ") is False


# ---------- _reproducer_uses_public_api: project-specific allowlist -------


def test_project_specific_allowlist_overrides_builtin():
    """Passing public_headers replaces the built-in set entirely."""
    code = "#include <archive.h>\nint main(){return 0;}"
    # If we explicitly say only myproj.h counts, libarchive include is rejected.
    assert _reproducer_uses_public_api(code, public_headers=["myproj.h"]) is False
    # And myproj.h IS accepted.
    code2 = "#include <myproj.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(code2, public_headers=["myproj.h"]) is True


def test_project_specific_allowlist_multiple():
    code = "#include <foo/bar.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(
        code, public_headers=["other.h", "foo/bar.h", "third.h"]
    ) is True


def test_project_specific_empty_list_rejects_everything():
    """Empty list means 'no headers count as project' — defensive."""
    code = "#include <archive.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(code, public_headers=[]) is False


# ---------- _autodiscover_public_headers ----------------------------------


def test_autodiscover_finds_top_level_h(tmp_path):
    (tmp_path / "foo.h").write_text("void foo(void);")
    (tmp_path / "bar.h").write_text("void bar(void);")
    out = _autodiscover_public_headers([str(tmp_path)])
    assert set(out) == {"foo.h", "bar.h"}


def test_autodiscover_excludes_private_and_internal(tmp_path):
    (tmp_path / "pub.h").write_text("// public")
    (tmp_path / "thing_private.h").write_text("// private")
    (tmp_path / "thing_internal.h").write_text("// internal")
    out = _autodiscover_public_headers([str(tmp_path)])
    assert out == ["pub.h"]


def test_autodiscover_dedupes_across_dirs(tmp_path):
    d1 = tmp_path / "d1"; d1.mkdir()
    d2 = tmp_path / "d2"; d2.mkdir()
    (d1 / "shared.h").write_text("// 1")
    (d2 / "shared.h").write_text("// 2 — same name, different content")
    out = _autodiscover_public_headers([str(d1), str(d2)])
    assert out == ["shared.h"]


def test_autodiscover_empty_dirs(tmp_path):
    """No headers in the supplied dirs → empty list (caller falls back to built-in)."""
    out = _autodiscover_public_headers([str(tmp_path)])
    assert out == []


def test_autodiscover_no_dirs_returns_empty():
    assert _autodiscover_public_headers([]) == []
    assert _autodiscover_public_headers(None) == []


def test_autodiscover_tolerates_nonexistent_dirs():
    """Bad paths shouldn't crash — silently skipped."""
    out = _autodiscover_public_headers(["/nonexistent/path/that/does/not/exist"])
    assert out == []


# ---------- Integration: autodiscover feeds into validate -----------------


def test_autodiscover_then_validate_libarchive_layout(tmp_path):
    """Real libarchive shape: 'archive.h' + 'archive_entry.h' + lots of
    *_private.h. The autodiscovery returns only the public pair; a
    reproducer including one of them validates."""
    (tmp_path / "archive.h").write_text("void archive_read_new(void);")
    (tmp_path / "archive_entry.h").write_text("void archive_entry_new(void);")
    (tmp_path / "archive_acl_private.h").write_text("// private — skip")
    (tmp_path / "archive_random_private.h").write_text("// private — skip")
    public = _autodiscover_public_headers([str(tmp_path)])
    assert set(public) == {"archive.h", "archive_entry.h"}
    reproducer = "#include <archive.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(reproducer, public) is True


def test_autodiscover_then_validate_custom_project_layout(tmp_path):
    """Different project — custom header names. Built-in set wouldn't
    catch it, but autodiscovery does."""
    (tmp_path / "myproj.h").write_text("void myproj_init(void);")
    (tmp_path / "myproj_util.h").write_text("void myproj_log(const char*);")
    public = _autodiscover_public_headers([str(tmp_path)])
    assert set(public) == {"myproj.h", "myproj_util.h"}
    reproducer = '#include "myproj.h"\nint main(){myproj_init();return 0;}'
    assert _reproducer_uses_public_api(reproducer, public) is True
    # Reproducer for myproj that included <archive.h> would NOT validate
    # since archive.h isn't in this project's allowlist.
    wrong = "#include <archive.h>\nint main(){return 0;}"
    assert _reproducer_uses_public_api(wrong, public) is False
