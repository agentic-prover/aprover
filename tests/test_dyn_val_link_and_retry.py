"""
Tests for the dynamic-validator link-flag auto-detect and the LLM
compile-error retry loop (companion commits to dynamic_validator.py).

Background: every system-entry reproducer in the libarchive smoke test
failed to compile with ``undefined reference to archive_match_new``
because GCC wasn't being passed ``-larchive``. Beyond the linker
issue, LLM-generated reproducers occasionally fail compile on
fixable source-level mistakes (missing #include, wrong API name).
These tests cover both fixes:

  (1) _detect_link_flags derives -l<libname> from #include'd project
      headers (archive.h → -larchive, curl.h → -lcurl, ...).
  (2) _is_link_only_error distinguishes pure linker failures (where
      LLM regen can't help) from compile errors (where it can).
  (3) DynamicValidator._regenerate_reproducer_with_error invokes the
      LLM with the failed source + GCC error and returns a corrected
      version.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# _detect_link_flags
# ---------------------------------------------------------------------------

def test_detect_link_flags_libarchive_angle_include():
    """``#include <archive.h>`` → ``-larchive``."""
    from bmc_agent.dynamic_validator import _detect_link_flags
    from bmc_agent.config import Config
    src = "#include <archive.h>\nint main(void){return 0;}"
    flags = _detect_link_flags(src, Config(llm_api_key="x"))
    assert "-larchive" in flags


def test_detect_link_flags_libarchive_quote_include():
    """``#include \"archive.h\"`` also → ``-larchive`` (quote form)."""
    from bmc_agent.dynamic_validator import _detect_link_flags
    from bmc_agent.config import Config
    src = '#include "archive.h"\nint main(void){return 0;}'
    flags = _detect_link_flags(src, Config(llm_api_key="x"))
    assert "-larchive" in flags


def test_detect_link_flags_libcurl():
    from bmc_agent.dynamic_validator import _detect_link_flags
    from bmc_agent.config import Config
    flags = _detect_link_flags(
        "#include <curl/curl.h>\nint main(void){return 0;}",
        Config(llm_api_key="x"),
    )
    assert "-lcurl" in flags


def test_detect_link_flags_libxml2_multiple_headers_dedup():
    """Multiple libxml2 headers → single -lxml2 (no duplicates)."""
    from bmc_agent.dynamic_validator import _detect_link_flags
    from bmc_agent.config import Config
    src = (
        "#include <libxml/parser.h>\n"
        "#include <libxml/tree.h>\n"
        "int main(void){return 0;}"
    )
    flags = _detect_link_flags(src, Config(llm_api_key="x"))
    assert flags.count("-lxml2") == 1


def test_detect_link_flags_unknown_header_no_flags():
    """A header we don't have a mapping for produces no link flags —
    let the LLM-retry path handle source-level mistakes."""
    from bmc_agent.dynamic_validator import _detect_link_flags
    from bmc_agent.config import Config
    src = "#include <some_random_proj.h>\nint main(void){return 0;}"
    flags = _detect_link_flags(src, Config(llm_api_key="x"))
    assert flags == []


def test_detect_link_flags_no_includes_no_flags():
    from bmc_agent.dynamic_validator import _detect_link_flags
    from bmc_agent.config import Config
    assert _detect_link_flags("int main(void){return 0;}", Config(llm_api_key="x")) == []


def test_detect_link_flags_honours_BMC_AGENT_DYN_LIB_DIRS(monkeypatch, tmp_path):
    """User-provided ``BMC_AGENT_DYN_LIB_DIRS`` adds ``-L<dir>`` flags
    BEFORE the ``-l`` flags so a local libarchive build path takes
    precedence over the system default."""
    from bmc_agent.dynamic_validator import _detect_link_flags
    from bmc_agent.config import Config
    d = tmp_path / "lib"
    d.mkdir()
    monkeypatch.setenv("BMC_AGENT_DYN_LIB_DIRS", str(d))
    flags = _detect_link_flags(
        "#include <archive.h>\nint main(void){return 0;}",
        Config(llm_api_key="x"),
    )
    # -L and -l both appear; -L precedes -l for linker search order
    assert "-L" in flags
    assert str(d) in flags
    assert "-larchive" in flags
    assert flags.index("-L") < flags.index("-larchive")
    # rpath added so the runtime loader finds the .so too
    assert any(f.startswith("-Wl,-rpath,") for f in flags)


# ---------------------------------------------------------------------------
# _is_link_only_error
# ---------------------------------------------------------------------------

def test_is_link_only_error_undefined_reference_only():
    """Pure linker failure (no file:line: error: marker) → True so the
    LLM-retry path is skipped (LLM can't fix linker issues)."""
    from bmc_agent.dynamic_validator import _is_link_only_error
    err = (
        "/usr/bin/ld: /tmp/cc12345.o: in function `_amc_reproducer_main':\n"
        "/tmp/tmpfoo.c:26: undefined reference to `archive_match_new'\n"
        "/usr/bin/ld: /tmp/tmpfoo.c:30: undefined reference to "
        "`archive_match_include_uname_w'\n"
        "collect2: error: ld returned 1 exit status"
    )
    assert _is_link_only_error(err) is True


def test_is_link_only_error_cannot_find_lib():
    from bmc_agent.dynamic_validator import _is_link_only_error
    assert _is_link_only_error(
        "/usr/bin/ld: cannot find -larchive"
    ) is True


def test_is_link_only_error_compile_error_marker_makes_false():
    """When the error has a ``file:line:col: error:`` marker, the
    source-level LLM retry IS worth trying — return False."""
    from bmc_agent.dynamic_validator import _is_link_only_error
    err = (
        "/tmp/tmpfoo.c:134:5: error: conflicting types for 'div_t'; "
        "have 'struct <anonymous>'"
    )
    assert _is_link_only_error(err) is False


def test_is_link_only_error_mixed_compile_and_link_returns_false():
    """If both compile errors AND link errors appear, the LLM retry
    might still fix the compile half — return False to give it a shot."""
    from bmc_agent.dynamic_validator import _is_link_only_error
    err = (
        "/tmp/tmpfoo.c:50:3: error: implicit declaration of function 'foo'\n"
        "/tmp/tmpfoo.c:26: undefined reference to `archive_match_new'"
    )
    assert _is_link_only_error(err) is False


def test_is_link_only_error_empty_returns_false():
    """Defensive: empty error string is NOT link-only — fall through to
    whatever default behaviour the caller has."""
    from bmc_agent.dynamic_validator import _is_link_only_error
    assert _is_link_only_error("") is False
    assert _is_link_only_error(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _regenerate_reproducer_with_error
# ---------------------------------------------------------------------------

def _make_validator_for_regen(llm):
    """Skip __init__ — we only need self._llm + self._reproducer_retry_max."""
    from bmc_agent.dynamic_validator import DynamicValidator
    v = object.__new__(DynamicValidator)
    v._llm = llm
    v._reproducer_retry_max = 2
    return v


def test_regenerate_returns_corrected_source(monkeypatch):
    """LLM returns JSON with a corrected source — method returns that source."""
    llm = MagicMock()
    llm.complete.return_value = (
        '{"reproducer_code": "#include <archive.h>\\n'
        'int main(void){ archive_match_new(); return 0; }"}'
    )
    v = _make_validator_for_regen(llm)
    out = v._regenerate_reproducer_with_error(
        previous_reproducer='#include <archive.h>\nint main(void){...}',
        compile_error="/tmp/foo.c:5:3: error: 'archive_matchnew' undeclared",
        func_name="archive_match_new",
    )
    assert out is not None
    assert "archive_match_new" in out
    # complete() called once
    assert llm.complete.call_count == 1
    # The prompt to the LLM must include the previous source AND the error
    call_kwargs = llm.complete.call_args
    args = call_kwargs.args if call_kwargs.args else ()
    user_prompt = args[1] if len(args) >= 2 else ""
    assert "archive_match_new" in user_prompt
    assert "undeclared" in user_prompt


def test_regenerate_passes_through_unreproducible_marker():
    """LLM honestly says 'I can't fix this' via UNREPRODUCIBLE — pass
    through verbatim (caller bails out)."""
    llm = MagicMock()
    llm.complete.return_value = (
        '{"reproducer_code": "// UNREPRODUCIBLE: needs -larchive linker flag"}'
    )
    v = _make_validator_for_regen(llm)
    out = v._regenerate_reproducer_with_error(
        "src", "err", "f",
    )
    assert out is not None
    assert out.startswith("// UNREPRODUCIBLE")


def test_regenerate_handles_fenced_markdown():
    """LLM wraps JSON in ```json fences — parser must still extract it."""
    llm = MagicMock()
    llm.complete.return_value = (
        '```json\n'
        '{"reproducer_code": "fixed source"}\n'
        '```'
    )
    v = _make_validator_for_regen(llm)
    out = v._regenerate_reproducer_with_error("s", "e", "f")
    assert out == "fixed source"


def test_regenerate_handles_embedded_json_in_prose():
    """LLM prepends prose before the JSON — embedded-JSON recovery."""
    llm = MagicMock()
    llm.complete.return_value = (
        'Here is the corrected reproducer:\n'
        '{"reproducer_code": "fixed source"}\n'
        'Done.'
    )
    v = _make_validator_for_regen(llm)
    out = v._regenerate_reproducer_with_error("s", "e", "f")
    assert out == "fixed source"


def test_regenerate_returns_none_on_unparseable_response():
    """Malformed LLM response — return None so caller falls through."""
    llm = MagicMock()
    llm.complete.return_value = "this isn't json at all"
    v = _make_validator_for_regen(llm)
    assert v._regenerate_reproducer_with_error("s", "e", "f") is None


def test_regenerate_returns_none_when_empty_code():
    """LLM returns empty reproducer_code → None (caller bails)."""
    llm = MagicMock()
    llm.complete.return_value = '{"reproducer_code": ""}'
    v = _make_validator_for_regen(llm)
    assert v._regenerate_reproducer_with_error("s", "e", "f") is None


def test_regenerate_returns_none_when_llm_errors():
    """LLM exception → None (logged, caller falls through)."""
    from bmc_agent.llm import LLMError
    llm = MagicMock()
    llm.complete.side_effect = LLMError("network timeout")
    v = _make_validator_for_regen(llm)
    assert v._regenerate_reproducer_with_error("s", "e", "f") is None


def test_regenerate_returns_none_when_no_llm_client():
    """When self._llm is None (validator built without LLM, e.g. unit
    tests), regen is a no-op."""
    v = _make_validator_for_regen(llm=None)
    assert v._regenerate_reproducer_with_error("s", "e", "f") is None


# ---------------------------------------------------------------------------
# DynamicValidator constructor wiring
# ---------------------------------------------------------------------------

def test_dynamic_validator_accepts_optional_llm():
    """The new __init__ signature accepts an optional llm; default is None
    (preserves existing call sites)."""
    from bmc_agent.dynamic_validator import DynamicValidator
    from bmc_agent.config import Config
    cfg = Config(llm_api_key="test")
    # Two-arg form still works
    v = DynamicValidator(cfg, harness_gen=MagicMock())
    assert v._llm is None
    # llm kwarg accepted
    llm = MagicMock()
    v2 = DynamicValidator(cfg, harness_gen=MagicMock(), llm=llm)
    assert v2._llm is llm


def test_reproducer_retry_max_honours_env(monkeypatch):
    """``BMC_AGENT_DYN_REPRODUCER_RETRY_MAX`` overrides the default."""
    from bmc_agent.dynamic_validator import DynamicValidator
    from bmc_agent.config import Config
    monkeypatch.setenv("BMC_AGENT_DYN_REPRODUCER_RETRY_MAX", "5")
    v = DynamicValidator(Config(llm_api_key="x"), harness_gen=MagicMock())
    assert v._reproducer_retry_max == 5
