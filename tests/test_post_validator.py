"""Tests for bmc_agent.post_validator — mechanical revalidation of judge output.

Synthetic fixtures only. No CBMC, no sanitizer runs, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bmc_agent.post_validator import (
    _DEMOTION_LABELS,
    AntipatternHit,
    RevalidationResult,
    extract_property_class,
    lint_reproducer,
    parse_sanitizer_output,
    revalidate_judge_json,
)


# ---------- extract_property_class ------------------------------------------

@pytest.mark.parametrize("prop,expected", [
    ("strcmp.pointer_dereference.1", "pointer_dereference"),
    ("archive_acl_text_len.overflow.3", "overflow"),
    ("main.unwind.0", "unwind"),
    ("add_entry.pointer_dereference.32", "pointer_dereference"),
    ("pm_list.pointer_arithmetic.17", "pointer_arithmetic"),
    ("archive_match.foo.bar.array_bounds.5", "array_bounds"),
    ("", ""),
    ("malformed_no_dots", "malformed_no_dots"),
])
def test_extract_property_class(prop, expected):
    assert extract_property_class(prop) == expected


# ---------- parse_sanitizer_output ------------------------------------------

_STACK_BOF_STDERR = """\
=================================================================
==262061==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x745d26e00048
WRITE of size 1536 at 0x745d26e00048 thread T0
    #0 0x745d2c8fb302 in memcpy ../../../../src/libsanitizer/sanitizer_common/sanitizer_common_interceptors_memintrinsics.inc:115
    #1 0x745d2c78f17d in memory_write /tmp/libarchive_bench/libarchive/libarchive/archive_write_open_memory.c:97
    #2 0x745d2c787e74 in archive_write_client_close /tmp/libarchive_bench/libarchive/libarchive/archive_write.c:534
    #3 0x5b319540279f in main /tmp/something/reproducer.c:34
"""

_LSAN_ONLY_STDERR = """\
=================================================================
==12345==ERROR: LeakSanitizer: detected memory leaks

Direct leak of 64 byte(s) in 1 object(s) allocated from:
    #0 0x7f1234567 in malloc /usr/lib/llvm/...
    #1 0x55abcdef in archive_entry_acl_to_text /tmp/libarchive/libarchive/archive_acl.c:1234
"""

_SIGNED_OVERFLOW_STDERR = """\
/tmp/libarchive_bench/libarchive/libarchive/archive_acl.c:512:24: runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'
    #0 0x7f01234 in archive_acl_text_len /tmp/libarchive_bench/libarchive/libarchive/archive_acl.c:512
    #1 0x5b1234 in main /tmp/repro.c:10
"""

_NO_LIBARCHIVE_FRAME_STDERR = """\
==1==ERROR: AddressSanitizer: SEGV on unknown address
    #0 0x7f0 in __asan_memcpy /libsan/asan_interceptors.cpp:1
    #1 0x7f1 in __libc_memcpy /usr/lib/libc.so.6
    #2 0x5b0 in main /tmp/repro_attempt1.c:42
"""


def test_parse_stack_buffer_overflow_in_memory_write():
    info = parse_sanitizer_output(_STACK_BOF_STDERR)
    assert info.family == "stack-buffer-overflow"
    assert info.has_real_crash is True
    assert info.has_lsan_leak is False
    assert info.top_libarchive_frame is not None
    func, path = info.top_libarchive_frame
    # The asan memcpy frame must NOT be picked — it's the sanitizer runtime.
    # The first libarchive/libarchive/ frame is memory_write.
    assert func == "memory_write"
    assert "archive_write_open_memory.c" in path


def test_parse_lsan_only():
    info = parse_sanitizer_output(_LSAN_ONLY_STDERR)
    assert info.has_lsan_leak is True
    assert info.has_real_crash is False
    # family field documents the dominant signal; lsan-leak is acceptable here.
    assert info.family == "lsan-leak"


def test_parse_signed_overflow_picks_libarchive_frame():
    info = parse_sanitizer_output(_SIGNED_OVERFLOW_STDERR)
    assert info.family == "signed-overflow"
    assert info.has_real_crash is True
    assert info.top_libarchive_frame is not None
    assert info.top_libarchive_frame[0] == "archive_acl_text_len"


def test_parse_no_libarchive_frame_when_crash_outside_lib():
    info = parse_sanitizer_output(_NO_LIBARCHIVE_FRAME_STDERR)
    assert info.family == "SEGV"
    assert info.has_real_crash is True
    assert info.top_libarchive_frame is None  # nothing in libarchive/libarchive/


def test_parse_empty_string():
    info = parse_sanitizer_output("")
    assert info.family is None
    assert info.has_real_crash is False
    assert info.has_lsan_leak is False
    assert info.top_libarchive_frame is None


# ---------- antipattern lint -------------------------------------------------

def _write(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(src)
    return p


def test_antipattern_size_by_pointer_matches_real_v7_repro(tmp_path):
    """The exact bad call from the v7 cmp_key_mbs reproducer."""
    src = """
    int main(void) {
        const void *buff;
        size_t size;
        struct archive *a = archive_write_new();
        archive_write_set_format_pax(a);
        archive_write_open_memory(a, (void **)&buff, &size, NULL);
        return 0;
    }
    """
    f = _write(tmp_path, "repro.c", src)
    hits = lint_reproducer(f)
    names = {h.name for h in hits}
    assert "write_open_memory_size_by_pointer" in names


def test_antipattern_size_aliasing(tmp_path):
    """archive_write_open_memory(..., &cap, &cap) — same address for buffSize and used."""
    src = """
    int main(void) {
        size_t cap = 4096;
        char buf[4096];
        archive_write_open_memory(a, buf, &cap, &cap);
        return 0;
    }
    """
    f = _write(tmp_path, "repro.c", src)
    names = {h.name for h in lint_reproducer(f)}
    # size_by_pointer also fires here (3rd arg is &cap); that's a true positive too.
    assert "write_open_memory_size_aliasing" in names


def test_antipattern_acl_to_text_leak(tmp_path):
    src = """
    int main(void) {
        struct archive_entry *e = archive_entry_new();
        char *t = archive_entry_acl_to_text(e, NULL, 0);
        printf("%s\\n", t);
        archive_entry_free(e);
        return 0;
    }
    """
    f = _write(tmp_path, "repro.c", src)
    names = {h.name for h in lint_reproducer(f)}
    assert "acl_to_text_leak" in names


def test_antipattern_acl_to_text_with_free_is_clean(tmp_path):
    """Same code but with a free() in the same statement — should NOT fire."""
    src = """
    int main(void) {
        char *t = archive_entry_acl_to_text(e, NULL, 0); free(t);
        return 0;
    }
    """
    f = _write(tmp_path, "repro.c", src)
    names = {h.name for h in lint_reproducer(f)}
    assert "acl_to_text_leak" not in names


def test_antipattern_clean_write_open_memory_no_hits(tmp_path):
    """The canonical correct call from the prompt — must produce zero hits."""
    src = """
    int main(void) {
        char buf[4096];
        size_t used = 0;
        archive_write_open_memory(a, buf, sizeof(buf), &used);
        return 0;
    }
    """
    f = _write(tmp_path, "repro.c", src)
    assert lint_reproducer(f) == []


def test_lint_reproducer_missing_file_returns_empty(tmp_path):
    assert lint_reproducer(tmp_path / "does_not_exist.c") == []


# ---------- revalidate_judge_json -------------------------------------------

def _write_judge(tmp_path: Path, *, failing_property: str, verdict: str,
                 dyn: dict | None, repro_src: str | None = None) -> Path:
    """Construct a minimal judge_<property>.json + optional reproducer next to it."""
    fn_dir = tmp_path / "target_fn"
    fn_dir.mkdir(exist_ok=True)
    if repro_src is not None and dyn is not None:
        rp = fn_dir / "reproducer.c"
        rp.write_text(repro_src)
        dyn = {**dyn, "harness_path": str(rp)}
    payload = {
        "failing_property": failing_property,
        "judge": {"verdict": verdict},
        "primary_dynamic_validation": dyn,
    }
    p = fn_dir / f"judge_{failing_property}.json"
    p.write_text(json.dumps(payload))
    return p


def test_revalidate_no_dyn_run_stays_candidate(tmp_path):
    jp = _write_judge(
        tmp_path,
        failing_property="strcmp.pointer_dereference.1",
        verdict="realistic",
        dyn={"outcome": "not_triggered", "stderr": ""},
    )
    r = revalidate_judge_json(jp, "target_fn")
    assert r.revised_label == "candidate"
    assert r.original_verdict == "realistic"


def test_revalidate_lsan_only_demotes_pointer_deref(tmp_path):
    jp = _write_judge(
        tmp_path,
        failing_property="strlen.pointer_dereference.1",
        verdict="realistic",
        dyn={"outcome": "confirmed_dynamic", "stderr": _LSAN_ONLY_STDERR},
    )
    r = revalidate_judge_json(jp, "target_fn")
    assert r.revised_label == "fp_leak_only"


def test_revalidate_wrong_class_demotes(tmp_path):
    """signed-overflow doesn't confirm a pointer_dereference claim."""
    jp = _write_judge(
        tmp_path,
        failing_property="strcmp.pointer_dereference.1",
        verdict="realistic",
        dyn={"outcome": "confirmed_dynamic", "stderr": _SIGNED_OVERFLOW_STDERR},
    )
    r = revalidate_judge_json(jp, "target_fn")
    assert r.revised_label == "fp_wrong_sanitizer_class"


def test_revalidate_crash_in_other_libarchive_func_demotes(tmp_path):
    """The cmp_key_mbs case: stack-buffer-overflow in memory_write, not target_fn."""
    jp = _write_judge(
        tmp_path,
        failing_property="strcmp.pointer_dereference.1",
        verdict="realistic",
        dyn={"outcome": "confirmed_dynamic", "stderr": _STACK_BOF_STDERR},
        repro_src="/* clean */",
    )
    r = revalidate_judge_json(jp, "cmp_key_mbs")
    assert r.revised_label == "fp_wrong_crash_site"
    assert "memory_write" in r.reasons[0]


def test_revalidate_crash_in_target_passes(tmp_path):
    """Top libarchive frame matches the target_function → confirmed_clean."""
    jp = _write_judge(
        tmp_path,
        failing_property="archive_acl_text_len.overflow.3",
        verdict="realistic",
        dyn={"outcome": "confirmed_dynamic", "stderr": _SIGNED_OVERFLOW_STDERR},
        repro_src="/* nothing antipatterny */",
    )
    r = revalidate_judge_json(jp, "archive_acl_text_len")
    assert r.revised_label == "confirmed_clean", r.reasons


def test_revalidate_static_callee_relaxation(tmp_path):
    """If the top libarchive frame is a static callee of the target, crash-site still matches."""
    jp = _write_judge(
        tmp_path,
        failing_property="strcmp.pointer_dereference.1",
        verdict="realistic",
        dyn={"outcome": "confirmed_dynamic", "stderr": _STACK_BOF_STDERR},
        repro_src="/* clean */",
    )
    r = revalidate_judge_json(jp, "cmp_key_mbs", static_callees={"memory_write"})
    assert r.revised_label == "confirmed_clean"


def test_revalidate_no_libarchive_frame_demotes(tmp_path):
    jp = _write_judge(
        tmp_path,
        failing_property="strlen.pointer_dereference.1",
        verdict="realistic",
        dyn={"outcome": "confirmed_dynamic", "stderr": _NO_LIBARCHIVE_FRAME_STDERR},
        repro_src="/* clean */",
    )
    r = revalidate_judge_json(jp, "target_fn")
    assert r.revised_label == "fp_no_libarchive_frame"


def test_revalidate_antipattern_demotes_even_with_matching_crash(tmp_path):
    """Crash site matches target, but reproducer has a known FP-inducing pattern."""
    bad_src = """
    int main(void) {
        const void *buff;
        size_t size;
        archive_write_open_memory(a, (void**)&buff, &size, NULL);
        return 0;
    }
    """
    # Use an stderr where top libarchive frame == target_fn (simulate the match).
    stderr = """\
==1==ERROR: AddressSanitizer: stack-buffer-overflow
    #0 0x1 in target_fn /tmp/libarchive_bench/libarchive/libarchive/foo.c:10
    #1 0x2 in main /tmp/r.c:1
"""
    jp = _write_judge(
        tmp_path,
        failing_property="foo.pointer_dereference.1",
        verdict="realistic",
        dyn={"outcome": "confirmed_dynamic", "stderr": stderr},
        repro_src=bad_src,
    )
    r = revalidate_judge_json(jp, "target_fn")
    assert r.revised_label == "fp_reproducer_antipattern"
    assert "write_open_memory_size_by_pointer" in r.antipatterns


# ---------- demotion-label invariants ---------------------------------------

def test_demotion_labels_match_module_constant():
    """If post_validator grows a new fp_ label, _DEMOTION_LABELS must include it."""
    # This guards against the CLI under-reporting flips when a new check is added.
    expected = {
        "fp_leak_only",
        "fp_wrong_sanitizer_class",
        "fp_no_libarchive_frame",
        "fp_wrong_crash_site",
        "fp_reproducer_antipattern",
        "revalidate_error",
    }
    assert _DEMOTION_LABELS == expected
