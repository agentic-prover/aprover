"""Tests for ``_replace_function_bodies_with_stubs`` — the source-
transformation helper that powers RetryAction.STUB_CALLEE.

When CBMC times out on a function whose state space is dominated by
inlined callees (the dominant cost in ``--real-libc`` mode where the
whole preprocessed source is included into one TU), this helper
locates selected callee bodies in the source text and replaces them
with nondet stubs. The verification then proceeds with the stubbed
callees treated as returning unconstrained values — same model CBMC
uses for any external function — reducing state space dramatically.

These tests exercise the parser robustness directly (no harness gen,
no CBMC). They are the safety net: if the parser misidentifies a
function (e.g. confuses a call with a definition), or fails to
brace-match a body, the broken transform would silently produce
invalid C that CBMC rejects at the next sweep.
"""

from __future__ import annotations


def test_stubs_simple_void_function():
    """Canonical case — single ``static void`` definition."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
#include <stdio.h>

static void append_id(int *ids, int id) {
    ids[0] = id;
    return;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"append_id"})
    assert stubbed == {"append_id"}
    assert "ids[0] = id" not in out
    assert "AMC stub: append_id" in out
    # Signature is preserved
    assert "static void append_id(int *ids, int id)" in out


def test_stubs_function_returning_int():
    """Non-void return type — stub emits ``int _amc_nondet; return _amc_nondet;``."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
static int compute(int x) {
    int y = x * x;
    return y + 7;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"compute"})
    assert stubbed == {"compute"}
    assert "y + 7" not in out
    assert "int _amc_nondet" in out
    assert "return _amc_nondet" in out


def test_stubs_function_with_pointer_return():
    """Pointer return type (``wchar_t *``) — must preserve the ``*``."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
wchar_t *next_field(wchar_t **wp, wchar_t *start, wchar_t *end) {
    *wp = start;
    return end;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"next_field"})
    assert stubbed == {"next_field"}
    # Verify the stub return type includes the pointer.
    assert "wchar_t * _amc_nondet" in out or "wchar_t *_amc_nondet" in out


def test_does_not_stub_forward_declaration():
    """A forward declaration (``;`` not ``{``) is NOT a definition; the
    transform must leave it alone. Otherwise we'd corrupt declarations
    elsewhere in the file."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
extern int append_id(int *ids, int id);

int caller(void) {
    return append_id(0, 0);
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"append_id"})
    assert stubbed == set()  # no definition found
    assert "extern int append_id(int *ids, int id);" in out
    # The call site is untouched.
    assert "return append_id(0, 0);" in out


def test_does_not_stub_call_site():
    """A call to ``f(...)`` must not be mistaken for a definition. The
    forward check is: after ``)``, the next non-whitespace must be ``{``."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
int main(void) {
    int r = compute(7);  /* this is a CALL, not a def */
    return r;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"compute"})
    assert stubbed == set()
    # The call site is untouched.
    assert "compute(7)" in out


def test_does_not_stub_struct_field_access():
    """``ctx->compute(...)`` or ``ctx.compute(...)`` is a function-pointer
    call, NOT a definition of ``compute``. The backward check (preceding
    char not in ``.>`` or alphanumeric) catches this."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
int caller(struct ops *ctx) {
    return ctx->compute(42);
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"compute"})
    assert stubbed == set()
    assert "ctx->compute(42)" in out


def test_does_not_match_substring_name():
    """``compute2`` should not be touched when the target is ``compute``.
    The backward-check prevents the regex from anchoring on a substring."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
static int compute2(int x) {
    return x + 1;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"compute"})
    assert stubbed == set()
    assert "return x + 1;" in out


def test_brace_count_handles_nested_blocks():
    """Multiple nested ``{}`` blocks in the body — the brace scanner
    must find the OUTER closing brace, not the first inner one."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
static int parse(const char *s) {
    if (*s) {
        while (*s++) {
            if (*s == ';') {
                break;
            }
        }
    }
    return 0;
}
static int next(void) {
    return 1;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"parse"})
    assert stubbed == {"parse"}
    # The next function (``next``) is untouched — i.e., we found the
    # right closing brace and didn't accidentally extend past it.
    assert "static int next(void) {\n    return 1;\n}" in out
    # parse's body is gone.
    assert "while (*s++)" not in out
    assert "AMC stub: parse" in out


def test_brace_inside_string_literal_does_not_confuse_scanner():
    """A ``}`` inside ``"..."`` must NOT close the body. Without
    literal-awareness, we'd close the body too early and the splice
    would produce broken C."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
static const char *describe(int x) {
    if (x) return "} fake brace";
    return "ok";
}
static int real(void) {
    return 42;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"describe"})
    assert stubbed == {"describe"}
    # The ``real`` function is intact — proves the scanner found the
    # true closing brace, not the one inside the string.
    assert "static int real(void) {\n    return 42;\n}" in out


def test_brace_inside_comment_does_not_confuse_scanner():
    """Same idea but with ``}`` inside a ``/* */`` comment."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
static int weird(int x) {
    /* trailing brace } in this comment */
    return x;
}
static int next_one(void) {
    return 1;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"weird"})
    assert stubbed == {"weird"}
    assert "static int next_one(void) {\n    return 1;\n}" in out


def test_stubs_multiple_functions_in_one_call():
    """Common case: the retry handler wants to stub several callees at
    once. Verify both get stubbed, neither corrupts the other."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
static int append_id(int *ids, int id) {
    ids[0] = id;
    return 0;
}

static void next_field(char **wp) {
    *wp += 1;
}

static int unrelated(void) {
    return 9;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(
        src, {"append_id", "next_field"}
    )
    assert stubbed == {"append_id", "next_field"}
    assert "ids[0] = id" not in out
    assert "*wp += 1" not in out
    # Unrelated function is preserved verbatim.
    assert "static int unrelated(void) {\n    return 9;\n}" in out


def test_missing_function_silently_returns_unmodified():
    """When the target name has no definition (e.g. it's declared in a
    header included by reference but the body is in a different TU),
    the helper should leave the text unchanged and report it in the
    skipped set."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = "int x = 1;\n"
    out, stubbed = _replace_function_bodies_with_stubs(src, {"missing"})
    assert stubbed == set()
    assert out == src


def test_empty_fn_names_is_noop():
    """Defensive: empty input set returns the text verbatim. No regex
    work, no surprises."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = "void f(void) { return; }\n"
    out, stubbed = _replace_function_bodies_with_stubs(src, set())
    assert out == src
    assert stubbed == set()


def test_stub_emits_assume_zero_for_attribute_decorated_rettype():
    """When the return-type extraction is uncertain (e.g. contains
    ``__attribute__`` syntax that we don't want to ad-hoc reconstruct),
    the helper falls back to ``__CPROVER_assume(0)`` so CBMC treats the
    stub as unreachable — strictly safer than emitting broken C."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
static __attribute__((pure)) int hard_to_parse(int x) {
    return x;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"hard_to_parse"})
    assert stubbed == {"hard_to_parse"}
    assert "__CPROVER_assume(0)" in out


def test_stubbing_preserves_signature_for_compile():
    """The stubbed source must still compile — the function's signature
    (including param list) is preserved verbatim, only the body
    changes. This is what allows the rest of the TU's call sites to
    still type-check."""
    from bmc_agent.harness_generator import _replace_function_bodies_with_stubs
    src = """\
static int validate(const char *s, size_t n) {
    return s != NULL && n > 0;
}
"""
    out, stubbed = _replace_function_bodies_with_stubs(src, {"validate"})
    assert stubbed == {"validate"}
    # Signature unchanged
    assert "static int validate(const char *s, size_t n)" in out
    # Original body gone
    assert "s != NULL && n > 0" not in out
