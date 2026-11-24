"""The confirmed-bug report must not hardcode libarchive for non-library
targets (VibeOS kernel etc.) — wrong Project label + clone instructions
mislead triage. libarchive targets keep their snapshot/clone boilerplate."""
import tempfile

import bmc_agent.report_generator as rg

_REPORT = {
    "function_name": "dns_resolve",
    "violated_property": "htons.overflow.1",
    "call_chain": ["dns_resolve"],
    "confidence": "unlikely",
    "counterexample": {"variable_assignments": {}},
}
_REALISM = {"verdict": "realistic", "reasoning": "x", "key_concern": "y",
            "llm_confidence": "medium"}


def _render(file_stem, driver):
    return rg._format_report("br.json", _REPORT, _REALISM, file_stem,
                             tempfile.mkdtemp(), "rerun", driver=driver)


# ---- detection --------------------------------------------------------

def test_detect_vibeos_is_not_libarchive():
    assert rg._looks_like_libarchive("net", "examples/vibeos/repo/kernel", "vibeos_net") is False


def test_detect_libarchive_by_driver():
    assert rg._looks_like_libarchive("archive_acl", None, "libarchive_sweep") is True


def test_detect_libarchive_by_source_root():
    assert rg._looks_like_libarchive("foo", "/tmp/libarchive/libarchive", "drv") is True


# ---- rendering --------------------------------------------------------

def test_vibeos_report_has_no_libarchive():
    md = _render("net", "vibeos_net")
    assert "libarchive" not in md.lower()
    assert "git clone" not in md
    assert "- **Project**: vibeos_net" in md


def test_libarchive_report_keeps_boilerplate():
    md = _render("archive_acl", "libarchive")
    assert "git clone https://github.com/libarchive/libarchive" in md
    assert "**Project**: libarchive" in md


def test_generic_repro_uses_committed_harness():
    md = _render("net", "vibeos_net")
    assert "harness.c" in md
    assert "-larchive" not in md
