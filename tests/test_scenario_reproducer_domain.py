"""Scenario reproducer must be domain-aware (Bug A).

The original prompt hardcoded libarchive (archive.h, archive_*_open_memory, ACL
helpers). On any other target the LLM tried to use libarchive and produced
garbage ("not a libarchive symbol"). Now: libarchive targets keep the exact
libarchive prompt; everything else gets a generic "call the function directly"
prompt with no library assumption.
"""

from types import SimpleNamespace

from bmc_agent import scenario_reproducer as sr


def _pf(path="", primary="", pre=""):
    return SimpleNamespace(path=path, primary_source=primary, preprocessed_source=pre)


def test_detects_libarchive_by_path():
    assert sr._is_libarchive_target(_pf(path="/tmp/libarchive_bench/libarchive/archive_acl.c"))


def test_detects_libarchive_by_include():
    assert sr._is_libarchive_target(_pf(path="/x/foo.c", pre="#include <archive.h>\nint f(){}"))


def test_non_libarchive_is_generic():
    assert not sr._is_libarchive_target(_pf(path="examples/vibeos/repo/kernel/vfs.c",
                                            pre='#include "vfs.h"'))
    assert not sr._is_libarchive_target(_pf(path="/proj/net.c"))


def test_libarchive_prompt_unchanged():
    # The load-bearing libarchive prompt must stay byte-for-byte (regression).
    assert "archive_read_open_memory" in sr._SCENARIO_REPRODUCER_PROMPT
    assert "archive_entry_acl_to_text" in sr._SCENARIO_REPRODUCER_PROMPT


def test_generic_prompt_calls_function_directly_no_library():
    g = sr._GENERIC_SCENARIO_REPRODUCER_PROMPT
    assert "{fn_name}" in g and "{fn_signature}" in g and "{fn_body}" in g
    low = g.lower()
    assert "directly" in low and "no public-api wrapper" in low
    # No POSITIVE libarchive API usage (only the 'do NOT' prohibition mentions it).
    assert "archive_read_open_memory" not in g
    assert "archive_entry_acl" not in g


def test_generic_prompt_formats_with_same_placeholders():
    out = sr._GENERIC_SCENARIO_REPRODUCER_PROMPT.format(
        fn_name="vfs_read", fn_signature="void*, int",
        attacker_scenario="oversized length", fn_body="return buf[n];")
    assert "vfs_read" in out and "oversized length" in out
