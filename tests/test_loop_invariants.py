"""Loop-invariant synthesis — deterministic helpers (no LLM/CBMC)."""
from bmc_agent.loop_invariants import (
    find_loops, _inv_to_cbmc, _inv_to_acsl, _top_implication_to_or,
    insert_loop_invariants, render_loop_invariants_acsl, failing_loopinvs,
)

SRC = (
    "int main(void){\n"
    "  int A[1024]; unsigned i;\n"
    "  for (i = 0; i < 1024; i++) {\n"
    "    A[i] = i;\n"
    "  }\n"
    "  return 0;\n"
    "}\n"
)


def test_find_loops_locates_for_loop_head():
    loops = find_loops(SRC)
    assert len(loops) == 1
    lp = loops[0]
    assert lp.kind == "for"
    assert "i < 1024" in lp.guard
    assert "A[i] = i;" in lp.body
    # head_offset is just inside the body brace
    assert SRC[lp.head_offset - 1] == "{"


def test_find_loops_nested():
    src = "void f(){ for(;;){ while(g){ x++; } } }"
    loops = find_loops(src)
    assert [l.kind for l in loops] == ["for", "while"]


def test_inv_to_cbmc_forall():
    got = _inv_to_cbmc("forall k : (k < i) ==> (A[k] == k)")
    assert got == "__CPROVER_forall { int k; ((k < i) ==> (A[k] == k)) }"


def test_inv_to_cbmc_plain_passthrough():
    assert _inv_to_cbmc("i <= 1024") == "i <= 1024"


def test_top_implication_rewrite():
    assert _top_implication_to_or("a ==> b") == "(!(a) || (b))"
    # nested implication on the rhs is rewritten too
    assert _top_implication_to_or("a ==> b ==> c") == "(!(a) || ((!(b) || (c))))"
    # no top-level implication -> unchanged
    assert _top_implication_to_or("x && y") == "x && y"


def test_inv_to_acsl_forall_and_result():
    assert _inv_to_acsl("forall k : 0 <= k < i ==> A[k] == k") == \
        "\\forall integer k; 0 <= k < i ==> A[k] == k"
    assert _inv_to_acsl("result >= x") == "\\result >= x"


def test_insert_loop_invariants_at_head():
    out = insert_loop_invariants(SRC, {0: ["i <= 1024", "forall k : (k < i) ==> (A[k] == k)"]})
    # both asserts inserted, tagged, before the body statement
    assert '__CPROVER_assert(i <= 1024, "loopinv_0_0");' in out
    assert '__CPROVER_forall { int k; ((k < i) ==> (A[k] == k)) }, "loopinv_0_1"' in out
    assert out.index("loopinv_0_0") < out.index("A[i] = i;")


def test_render_acsl_block():
    acsl = render_loop_invariants_acsl({0: ["i <= 1024", "forall k : (k < i) ==> (A[k] == k)"]})
    assert "loop invariant i <= 1024;" in acsl
    assert "loop invariant \\forall integer k; (k < i) ==> (A[k] == k);" in acsl


def test_failing_loopinvs_parsing():
    class CE:
        def __init__(self, d): self.description, self.failing_property = d, ""
    class R:
        counterexamples = [CE("loopinv_0_1"), CE("loopinv_2_0"), CE("GOAL")]
    assert failing_loopinvs(R()) == [(0, 1), (2, 0)]
