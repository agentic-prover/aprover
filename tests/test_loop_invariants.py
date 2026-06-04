"""Loop-invariant synthesis — deterministic helpers (no LLM/CBMC)."""
import shutil
import pytest
from bmc_agent.loop_invariants import (
    find_loops, _inv_to_cbmc, _inv_to_acsl, _top_implication_to_or,
    insert_loop_invariants, render_loop_invariants_acsl, failing_loopinvs,
    _parse_inv_lines, _guess_unwind, LoopSite, synthesize_loop_invariants,
    modified_vars, build_havoc_abstraction, _has_literal_bound, _inject_no_overflow,
    _has_array_writes, _filter_in_scope, _enclosing_function, _loop_function_callees,
    _is_non_behavioral, _minimize_invariants, _loop_assigns,
)


def test_loop_assigns_includes_for_header_counter():
    # The counter is updated in the for-HEADER (`i++`), not the body — the frame
    # must still list it, or WP's frame is unsound and preservation fails.
    src = "void main(){ int A[2048]; int i;\n for (i = 0; i < 1024; i++) { A[i] = i; } }"
    (lp,) = find_loops(src)
    assert _loop_assigns(lp) == "i, A[..]"


def test_loop_assigns_excludes_inline_declared_counter():
    # `for (int i = ...)` declares i loop-locally → it is NOT part of the frame.
    src = "void g(){ int A[8]; for (int i = 0; i < 8; i++) { A[i] = i; } }"
    (lp,) = find_loops(src)
    assert _loop_assigns(lp) == "A[..]"


def test_loop_assigns_while_counter_in_body():
    src = "void f(int n, int *a){ int s = 0, p = 0; while (p < n) { s += a[p]; p++; } }"
    (lp,) = find_loops(src)
    assert _loop_assigns(lp) == "p, s"

IC3 = (
    "void main(){\n"
    "  int x = 1; int y = 1;\n"
    "  while (unknown1()) {\n"
    "    int t1 = x; int t2 = y;\n"
    "    x = t1 + t2; y = t1 + t2;\n"
    "  }\n"
    "  static_assert(y >= 1);\n"
    "}\n"
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


def test_inv_to_cbmc_expands_chained_comparison():
    # math-style `0 <= k < i` (valid ACSL, INVALID C) must be split for CBMC
    got = _inv_to_cbmc("forall k : 0 <= k < i ==> A[k] == k")
    assert got == "__CPROVER_forall { int k; (((0 <= k) && (k < i)) ==> A[k] == k) }"


def test_top_implication_rewrite():
    assert _top_implication_to_or("a ==> b") == "(!(a) || (b))"
    # nested implication on the rhs is rewritten too
    assert _top_implication_to_or("a ==> b ==> c") == "(!(a) || ((!(b) || (c))))"
    # no top-level implication -> unchanged
    assert _top_implication_to_or("x && y") == "x && y"


def test_inv_to_acsl_forall_and_result():
    assert _inv_to_acsl("forall k : 0 <= k < i ==> A[k] == k") == \
        "\\forall integer k; (0 <= k < i) ==> (A[k] == k)"
    assert _inv_to_acsl("result >= x") == "\\result >= x"


def test_insert_loop_invariants_at_head():
    out = insert_loop_invariants(SRC, {0: ["i <= 1024", "forall k : (k < i) ==> (A[k] == k)"]})
    # plain invariant -> direct assert; quantified -> single-nondet-WITNESS form (O(1)/iter)
    assert '__CPROVER_assert(i <= 1024, "loopinv_0_0");' in out
    assert 'int k = __VERIFIER_nondet_int();' in out
    assert '__CPROVER_assume((k < i));' in out
    assert '__CPROVER_assert((A[k] == k), "loopinv_0_1");' in out
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


def test_parse_inv_lines_strips_noise():
    txt = ("```\n"
           "- loop invariant i <= 1024;\n"
           "forall k : (k < i) ==> (A[k] == k)\n"
           "// a comment\n"
           "```")
    assert _parse_inv_lines(txt) == ["i <= 1024", "forall k : (k < i) ==> (A[k] == k)"]


def test_guess_unwind_from_bound():
    loops = [LoopSite("for", "i = 0; i < 1024; i++", 0, "", 0)]
    assert _guess_unwind(loops, 64) == 1026          # bound+2
    assert _guess_unwind([LoopSite("while", "x", 0, "", 0)], 64) == 64   # no literal -> default


def test_modified_vars_excludes_body_locals():
    lp = find_loops(IC3)[0]
    scalars, arrays = modified_vars(lp.body)
    assert scalars == ["x", "y"]      # t1, t2 are body-local -> excluded
    assert arrays == []


def test_has_literal_bound():
    assert _has_literal_bound(find_loops("void f(){ for(i=0;i<64;i++){x=1;} }")) is True
    assert _has_literal_bound(find_loops(IC3)) is False   # while(unknown1()) -> abstract


def test_build_havoc_abstraction_structure():
    lp = find_loops(IC3)[0]
    out = build_havoc_abstraction(IC3, lp, ["x == y", "x >= 1"], math_ints=False)
    assert "abstracted by its invariant" in out
    assert '__CPROVER_assert(x == y, "loopinv_0_0");' in out        # base
    assert "__CPROVER_assume((x == y) && (x >= 1));" in out          # assume inv
    assert "if (unknown1()) {" in out                                # guard
    assert "__CPROVER_assume(0);" in out                             # cut
    assert "while (unknown1())" not in out                           # loop replaced


def test_has_array_writes_dispatch_signal():
    assert _has_array_writes(find_loops("void f(){ for(i=0;i<64;i++){A[i]=i;} }")) is True
    assert _has_array_writes(find_loops(IC3)) is False   # scalar -> havoc mode


def test_filter_in_scope_drops_hallucinated_vars():
    src = "void main(){ int x=1; int y=0; while(y<100000){ x=x+y; y=y+1; } }"
    # 'i' is not in the program -> dropped; x/y clauses kept; forall var exempt
    kept = _filter_in_scope(["x >= y", "i <= y", "y >= 0",
                             "forall k : (k < y) ==> (x >= k)"], src)
    assert kept == ["x >= y", "y >= 0", "forall k : (k < y) ==> (x >= k)"]


def test_math_ints_injects_no_overflow():
    body = " x = t1 + t2; "
    got = _inject_no_overflow(body)
    assert "(long long)(t1) + (long long)(t2) <= 2147483647LL" in got
    assert "x = t1 + t2;" in got       # original assignment preserved


@pytest.mark.skipif(not shutil.which("cbmc"), reason="cbmc not installed")
def test_synthesize_end_to_end_with_mock_llm(tmp_path):
    """Whole driver: propose (mocked) → real CBMC validity+adequacy → ACSL out.
    The mock returns the behavioral invariant the engine would synthesize."""
    from bmc_agent.config import Config
    src = (
        "int main(void){\n"
        "  int A[8]; unsigned i;\n"
        "  for (i = 0; i < 8; i++) { A[i] = i; }\n"
        "  __VERIFIER_assert(A[7] == 7);\n"
        "  return 0;\n"
        "}\n"
    )
    f = tmp_path / "bench.c"; f.write_text(src)

    class MockLLM:
        def complete(self, system, prompt, max_tokens=0, role=""):
            # fully-bounded antecedent (0 <= k): the witness form needs the lower
            # bound, else A[k] for k<0 is an OOB read.
            return "i <= 8\nforall k : 0 <= k < i ==> (A[k] == (int)k)"

    r = synthesize_loop_invariants(str(f), Config.from_env(), MockLLM(),
                                   entry="main", unwind=10, timeout=90)
    assert r.ok, r.note
    assert r.annotations[0]
    assert "loop invariant \\forall integer k;" in r.acsl
    assert "A[7] == 7" in r.goals


# --- Frama-C oracle: inline callee loops so a caller-resident goal discharges ----

def test_enclosing_function_identifies_callee():
    src = ("int f(int *a, int n){ int s=0; while(n>0){ s+=a[--n]; } return s; }\n"
           "void main(){ int x[3]={1,2,3}; int s=f(x,3); /*@ assert s==6; */ }\n")
    lp = find_loops(src)[0]
    assert _enclosing_function(src, lp.start_offset) == "f"


def test_enclosing_function_skips_control_keywords():
    # a loop nested inside an `if` block of a callee must resolve to the callee,
    # not the `if` (which also matches the name(...){ shape).
    src = ("int g(int n){ int s=0; if(n>0){ for(int i=0;i<n;i++){ s+=i; } } return s; }\n"
           "void main(){ /*@ assert 1; */ }\n")
    lp = find_loops(src)[0]
    assert _enclosing_function(src, lp.start_offset) == "g"


def test_loop_function_callees_inlines_callee_not_entry():
    # loop in a callee -> inline it; loop directly in the entry -> nothing to inline.
    callee = ("int f(int *a, int n){ int s=0; while(n>0){ s+=a[--n]; } return s; }\n"
              "void main(){ int x[3]={1,2,3}; int s=f(x,3); /*@ assert s==6; */ }\n")
    assert _loop_function_callees(callee, "main") == ["f"]
    colocated = ("void main(){ int s=0,i=0; while(i<10){ s+=i; i++; } /*@ assert s>=0; */ }\n")
    assert _loop_function_callees(colocated, "main") == []


# --- minimal behavioral invariants: minimization + non-behavioral heuristic ------

def test_non_behavioral_heuristic():
    # value-pinning clauses (caller/input constants) are non-behavioral
    for c in ("n == 5", "a[0] == 1", "len == 1024", "x == -43"):
        assert _is_non_behavioral(c), c
    # relationships / bounds / quantified facts are behavioral
    for c in ("p <= n", "p >= 0", "sum == (p>=1?a[0]:0)",
              "forall k : 0 <= k < i ==> A[k]==k", "x == y"):
        assert not _is_non_behavioral(c), c


def test_minimize_drops_redundant_keeps_behavioral_core():
    # needed = bound + behavioral summary; redundant = input constants.
    ann = {0: ["n == 5", "a[0] == 1", "a[1] == 2", "p <= n", "sum == S", "p >= 0"]}
    needed = {"p <= n", "sum == S", "p >= 0"}

    class _Chk:
        def __init__(self, v): self.verified = v; self.result = None

    def check(a):                       # "verifies" iff the load-bearing core is present
        return _Chk(needed.issubset(set(a[0])))

    import logging
    out = _minimize_invariants(ann, check, None, logging.getLogger("t"))
    assert set(out[0]) == needed


def test_minimize_never_empties_a_loop():
    ann = {0: ["n == 5", "a[0] == 1"]}

    class _Chk:
        def __init__(self, v): self.verified = v; self.result = None

    import logging
    # everything "verifies" (goal proved for free) -> must still keep >= 1 clause
    out = _minimize_invariants(ann, lambda a: _Chk(True), None, logging.getLogger("t"))
    assert len(out[0]) == 1


def test_parse_inv_lines_drops_reasoning_prose():
    from bmc_agent.loop_invariants import _parse_inv_lines
    reply = ("Let me think.\n"
             "p <= n\n"
             "Wait, I need a behavioral invariant:\n"
             "forall k : 0 <= k < p ==> A[k] == k\n"
             "The most direct way:")
    assert _parse_inv_lines(reply) == ["p <= n", "forall k : 0 <= k < p ==> A[k] == k"]


def test_parse_inv_lines_normalizes_acsl_quantifier():
    from bmc_agent.loop_invariants import _parse_inv_lines
    # ACSL-native `\forall <type> v; body` -> DSL `forall v : body`
    assert _parse_inv_lines("\\forall int i; 0 <= i < n ==> a[i] == i + 1") == \
        ["forall i : 0 <= i < n ==> a[i] == i + 1"]
    assert _parse_inv_lines("\\forall integer k; (0 <= k < p) ==> (A[k] == k)") == \
        ["forall k : (0 <= k < p) ==> (A[k] == k)"]


def test_acsl_quantifier_survives_scope_filter():
    # the regression: an ACSL-form quantifier was dropped because the bound var
    # wasn't recognized -> _filter_in_scope flagged it out-of-scope. Now it's kept.
    from bmc_agent.loop_invariants import _parse_inv_lines, _filter_in_scope
    src = "int f(int *a, int n){ int p=0,s=0; while(p<n){s=s+a[p];p++;} return s; }"
    inv = _parse_inv_lines("\\forall int i; 0 <= i && i < n ==> a[i] == i + 1")
    assert _filter_in_scope(inv, src) == inv


def test_wp_failing_invariant_indices_maps_to_clause():
    from bmc_agent.loop_invariants import _wp_failing_invariant_indices, LoopSite
    loops = [LoopSite("for", "i=99;i>=0;i--", 0, "", 0)]
    ann = {0: ["i >= -1", "i <= 99", "forall k : i<k<=99 ==> A[k]==k",
               "forall k : 0<=k<=i ==> A[k]==k"]}
    # WP names the failing invariant 1-based, source-order
    assert _wp_failing_invariant_indices(
        ["typed_main_loop_invariant_4_established"], ann, loops) == [(0, 3)]
    assert _wp_failing_invariant_indices(
        ["typed_main_loop_invariant_1_preserved",
         "typed_main_loop_invariant_3_preserved"], ann, loops) == [(0, 0), (0, 2)]
    # an assert-only (goal) failure yields no invariant indices
    assert _wp_failing_invariant_indices(["typed_main_assert"], ann, loops) == []


def test_brace_braceless_loops_normalizes_and_preserves():
    from bmc_agent.loop_invariants import brace_braceless_loops, find_loops
    # already-braced is byte-for-byte unchanged
    braced = "void f(){ for(i=0;i<8;i++){ A[i]=i; } }"
    assert brace_braceless_loops(braced) == braced
    # simple brace-less for -> braced + now detectable
    bl = "void main(){ int A[64],i;\n for (i=0;i<64;i++)\n   A[i]=7;\n }"
    assert len(find_loops(bl)) == 0
    assert len(find_loops(brace_braceless_loops(bl))) == 1
    # brace-less while
    assert len(find_loops(brace_braceless_loops("void g(){int i=0; while(i<9) i++;}"))) == 1
    # do-while condition is not mistaken for a brace-less body (no crash, unchanged)
    dw = "void h(){ int i=0; do { i++; } while(i<5); }"
    assert brace_braceless_loops(dw) == dw


def test_detect_accumulator_sum_and_product():
    from bmc_agent.loop_invariants import (
        find_loops, brace_braceless_loops, detect_accumulator, accumulator_invariants,
        accumulator_axiomatic)
    # sum fold: acc = acc + a[p]
    src = "int s(int *a,int n){int p=0,sum=0; while(p<n){sum=sum+a[p]; p++;} return sum;}"
    (lp,) = find_loops(src)
    spec = detect_accumulator(lp, src)
    assert spec is not None
    assert (spec.kind, spec.acc, spec.array, spec.index, spec.elem_type, spec.bound) == \
        ("sum", "sum", "a", "p", "int", "n")
    assert accumulator_invariants(spec) == [
        "0 <= p", "p <= n", "sum == AccFold_sum_sum(a, 0, p)"]
    ax = accumulator_axiomatic(spec)
    assert "logic integer AccFold_sum_sum(int *a, integer m, integer k) reads a[m .. k-1]" in ax
    assert "m >= k ==> AccFold_sum_sum(a, m, k) == 0" in ax            # sum identity
    assert "AccFold_sum_sum(a, m, k-1) + a[k-1]" in ax                 # sum step

    # product fold via compound assignment: pr *= a[i]
    psrc = "int pr(int *a,int n){int i=0,prod=1; while(i<n){prod*=a[i]; i++;} return prod;}"
    (plp,) = find_loops(psrc)
    pspec = detect_accumulator(plp, psrc)
    assert pspec is not None and pspec.kind == "product"
    pax = accumulator_axiomatic(pspec)
    assert "== 1" in pax                                               # product identity
    assert "* a[k-1]" in pax                                           # product step


def test_detect_accumulator_declines_non_folds():
    from bmc_agent.loop_invariants import find_loops, detect_accumulator
    # array fill (writes the array, not a scalar fold) -> not an accumulator
    fill = "void f(int *A,int n){int i=0; while(i<n){A[i]=i; i++;}}"
    assert detect_accumulator(find_loops(fill)[0], fill) is None
    # scalar recurrence with no array index (IC3-style) -> not an accumulator
    ic3 = "void f(){int x=1,y=2; while(x<100){x=x+y;}}"
    assert detect_accumulator(find_loops(ic3)[0], ic3) is None
    # fold whose counter has no 0-initializer reaching the loop -> declines (unsound otherwise)
    noinit = "int s(int *a,int n,int p){int sum=0; while(p<n){sum=sum+a[p]; p++;} return sum;}"
    assert detect_accumulator(find_loops(noinit)[0], noinit) is None


def test_accumulator_function_contract():
    from bmc_agent.loop_invariants import find_loops, accumulator_contracts
    src = ("int sumArray(int *a, int n){int p=0,sum=0; while(p<n){sum=sum+a[p]; p++;} return sum;}"
           "\nvoid main(){int arr[5]={1,2,3,4,5}; int s=sumArray(arr,5); /*@ assert s==15; */}")
    contracts = accumulator_contracts(src, find_loops(src), entry="main")
    assert set(contracts) == {"sumArray"}
    block = contracts["sumArray"]
    assert "requires n >= 0;" in block
    assert "requires \\valid_read(a + (0 .. n-1));" in block
    assert "assigns \\nothing;" in block
    assert "ensures \\result == AccFold_sum_sum(a, 0, n);" in block
    # the entry function (loop in main, no caller) gets NO contract even when the
    # fold is fully detectable — a contract bridges a caller to a callee, and the
    # entry has no caller.
    from bmc_agent.loop_invariants import detect_accumulator
    inmain = ("void main(){int A[8]; int i=0,s=0; while(i<8){s=s+A[i]; i++;} /*@ assert s==s; */}")
    assert detect_accumulator(find_loops(inmain)[0], inmain) is not None   # fold IS detected
    assert accumulator_contracts(inmain, find_loops(inmain), entry="main") == {}


# --- behavioral strengthening: relational equality invariants -----------------
from bmc_agent.loop_invariants import equal_update_invariants


def test_equal_update_invariants_detects_parallel_assignment():
    src = ("void main(){int x=1,y=1; while(unknown1()){"
           "int t1=x; int t2=y; x=t1+t2; y=t1+t2;} }")
    loops = find_loops(src)
    assert equal_update_invariants(loops[0]) == ["x == y"]


def test_equal_update_invariants_none_for_distinct_rhs():
    src = "void main(){int x=0,y=0; while(unknown1()){ x=x+1; y=y+2; } }"
    assert equal_update_invariants(find_loops(src)[0]) == []


def test_equal_update_invariants_skips_accumulator_fold():
    # sum=sum+a[p] / p++ has no two scalars sharing an RHS → no spurious equality
    src = ("int s(int*a,int n){int p=0,sum=0; while(p<n){ sum=sum+a[p]; p++; }"
           " return sum;}")
    assert equal_update_invariants(find_loops(src)[0]) == []


def test_equal_update_invariants_ignores_declaration_initializers():
    # `int t1=x; int t2=x;` are declarations, not loop-carried scalar updates
    src = "void main(){int x=0; while(unknown1()){ int t1=x; int t2=x; x=x+1; } }"
    assert equal_update_invariants(find_loops(src)[0]) == []
