"""Tests for balanced-paren argument extraction in DSL atoms.

Regression: the old `[^)]+` regex pattern stopped at the FIRST `)`,
which for atoms with C casts inside the argument captured the cast's
inner close paren instead of the call's outer close paren. The
translator then emitted malformed C that wouldn't compile. Observed
on sweep of neuron_cdev.c: 62/118 functions failed to
compile due to this issue.
"""

from __future__ import annotations

from bmc_agent.dsl_to_cbmc import (
    _atom_to_expr,
    _match_call,
    translate_atom,
)


# ---------------------------------------------------------------------------
# Low-level matcher
# ---------------------------------------------------------------------------


def test_match_call_simple_arg():
    out = _match_call("valid(p)", "valid")
    assert out is not None
    start, end, args = out
    assert args == ["p"]
    assert start == 0
    assert end == len("valid(p)")


def test_match_call_with_cast_arg():
    """The motivating case — argument contains a C cast with its own
    parens. The old regex captured ``(struct ncdev*`` and broke."""
    atom = "valid((struct ncdev*)filep->private_data)"
    out = _match_call(atom, "valid")
    assert out is not None
    _, _, args = out
    assert args == ["(struct ncdev*)filep->private_data"]


def test_match_call_with_sizeof_arg():
    """Nested ``sizeof(struct X)`` inside an arg."""
    atom = "valid_range(buf, 0, sizeof(struct neuron_ioctl_bar_rw))"
    out = _match_call(atom, "valid_range")
    assert out is not None
    _, _, args = out
    assert args == ["buf", "0", "sizeof(struct neuron_ioctl_bar_rw)"]


def test_match_call_multi_arg_with_nested_calls():
    """Multiple args, multiple nested calls — both kinds of paren
    balancing exercised together."""
    atom = "valid_range((char*)dest, 0, sizeof(struct foo))"
    out = _match_call(atom, "valid_range")
    assert out is not None
    _, _, args = out
    assert args == ["(char*)dest", "0", "sizeof(struct foo)"]


def test_match_call_returns_none_on_unbalanced():
    """Truncated input — no matching close paren."""
    assert _match_call("valid(p", "valid") is None


def test_match_call_returns_none_when_name_absent():
    assert _match_call("valid_range(buf, 0, n)", "in_bounds") is None


def test_match_call_preserves_start_offset():
    """``start`` is used downstream for negation detection (looks at
    chars before the match)."""
    atom = "x != null(p)"
    out = _match_call(atom, "null")
    assert out is not None
    start, _, _ = out
    assert atom[start - 1] == " "
    assert atom[start - 2] == "="


# ---------------------------------------------------------------------------
# End-to-end: malformed C is no longer emitted
# ---------------------------------------------------------------------------


def test_valid_with_cast_translates_correctly():
    out = translate_atom(
        "valid((struct ncdev*)filep->private_data)", context="assume"
    )
    assert out is not None
    # No malformed cast — the closing ``)`` of the cast should be
    # present immediately before ``filep`` (i.e., the full cast token
    # appears intact, then is followed by the operand).
    assert "(struct ncdev*)filep->private_data != NULL" in out
    # The old buggy output had ``((struct ncdev* != NULL`` — make sure
    # that shape doesn't sneak through.
    assert "((struct ncdev* !=" not in out


def test_valid_range_with_sizeof_translates_correctly():
    out = translate_atom(
        "valid_range(buf, 0, sizeof(struct foo))", context="assume"
    )
    assert out is not None
    # All three args should be intact; the sizeof shouldn't be split.
    assert "sizeof(struct foo)" in out
    # The lower-bound and upper-bound terms should be properly emitted.
    assert "0 >= 0" in out
    assert "sizeof(struct foo) >= 0" in out


def test_valid_range_with_cast_in_first_arg_translates_correctly():
    out = translate_atom(
        "valid_range((char*)dest, 0, sizeof(struct foo))", context="assume"
    )
    assert out is not None
    assert "(char*)dest != NULL" in out


def test_atom_to_expr_handles_cast_in_valid():
    out = _atom_to_expr("valid((struct ncdev*)x)")
    assert out is not None
    assert "(struct ncdev*)x != NULL" in out
    assert "((struct ncdev* !=" not in out


def test_null_with_cast_arg_translates_correctly():
    """``null((void*)p)`` — same paren hazard, different predicate."""
    out = translate_atom("null((void*)p)", context="assume")
    assert out is not None
    assert "(void*)p == NULL" in out


def test_in_bounds_with_sizeof_index_translates_correctly():
    """sizeof can be hiding inside ``idx`` too."""
    out = translate_atom("in_bounds(arr, sizeof(struct foo))", context="assume")
    assert out is not None
    # idx side should retain the full sizeof.
    assert "sizeof(struct foo)" in out


def test_owns_with_cast_arg_uses_last_arg_as_pointer():
    """Two-arg owns with a cast in the pointer position."""
    out = translate_atom("owns(ctx, (T*)p)", context="assume")
    assert out is not None
    assert "(T*)p != NULL" in out
    # The scope arg should not appear in the emitted condition.
    assert "ctx !=" not in out


# ---------------------------------------------------------------------------
# fully_parenthesize: pin &&/||/==>/<==> grouping so the rendered C/ACSL does
# not depend on operator precedence (and CBMC and Frama-C agree).
# ---------------------------------------------------------------------------
from bmc_agent.dsl_to_cbmc import fully_parenthesize, postcond_to_assert


def test_fully_parenthesize_biconditional_and_or_mix():
    # The shape that motivated the fix: (r==1)==cond && r==0 || r==1.
    # C precedence groups it ((==-bicond && r==0) || r==1); make that explicit.
    expr = "(result == 1) == ((a + b > c) && (b + c > a)) && result == 0 || result == 1"
    assert fully_parenthesize(expr) == (
        "(((result == 1) == ((a + b > c) && (b + c > a))) && (result == 0)) "
        "|| (result == 1)"
    )


def test_fully_parenthesize_precedence_and_idempotent():
    assert fully_parenthesize("a || b && c") == "a || (b && c)"
    assert fully_parenthesize("a && b || c") == "(a && b) || c"
    # ==> binds looser than &&  (ACSL)
    assert fully_parenthesize("p ==> q && r") == "p ==> (q && r)"
    # <==> binds loosest of all
    assert fully_parenthesize("p <==> q || r") == "p <==> (q || r)"
    # primaries and single comparisons are left bare
    assert fully_parenthesize("result == 1") == "result == 1"
    assert fully_parenthesize("a[i]") == "a[i]"
    # applying twice changes nothing
    for e in ("a || b && c", "(x==1)==c && y==0 || z", "p ==> q && r"):
        assert fully_parenthesize(fully_parenthesize(e)) == fully_parenthesize(e)


def test_fully_parenthesize_does_not_split_quantifier_or_comment():
    # a /* ... */ operand is left untouched (already-dropped clause)
    assert "/* x */" in fully_parenthesize("a && /* x */")


def test_postcond_to_assert_emits_single_grouped_assert():
    # The whole biconditional must become ONE correctly-grouped assert, not be
    # mis-split into independent && conjuncts (which would change the meaning).
    stmts = postcond_to_assert(
        "(result == 1) == ((a + b > c) && (b + c > a)) && result == 0 || result == 1",
        ["a", "b", "c"],
    )
    asserts = [s for s in stmts if s.strip().startswith("assert(")]
    assert len(asserts) == 1
    assert asserts[0] == (
        "assert((((result == 1) == ((a + b > c) && (b + c > a))) "
        "&& (result == 0)) || (result == 1));"
    )
