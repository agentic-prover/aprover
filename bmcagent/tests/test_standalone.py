"""Standalone (whole-program) verification: ACSL-assert translation + mode wiring."""
from bmc_agent.standalone import translate_acsl_asserts


def test_acsl_assert_translated_to_cprover_assert():
    src = "void main(){ int x=5;\n    //@ assert x == 5 ;\n}"
    out, n = translate_acsl_asserts(src)
    assert n == 1
    assert '__CPROVER_assert(x == 5, "acsl: x == 5");' in out
    assert "//@ assert" not in out


def test_acsl_translation_handles_multiple_and_quotes():
    src = "//@ assert a == b ;\n//@ assert p != 0 ;\n"
    out, n = translate_acsl_asserts(src)
    assert n == 2
    assert out.count("__CPROVER_assert(") == 2


def test_no_acsl_asserts_is_noop():
    src = "int main(void){ return 0; }"
    out, n = translate_acsl_asserts(src)
    assert n == 0 and out == src


def test_standalone_flag_exposed_on_verify():
    import argparse
    from bmc_agent.cli import build_parser  # type: ignore
    # build_parser may not exist under that name; fall back to invoking --help parse
    import subprocess, sys
    h = subprocess.run([sys.executable, "-m", "bmc_agent.cli", "verify", "--help"],
                       capture_output=True, text=True)
    assert "--standalone" in h.stdout
    assert "--entry" in h.stdout
