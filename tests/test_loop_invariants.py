"""Loop-invariant synthesis — deterministic helpers (no LLM/CBMC)."""
import shutil
import pytest
from bmc_agent.loop_invariants import (
    find_loops, _inv_to_cbmc, _inv_to_acsl, _top_implication_to_or,
    insert_loop_invariants, render_loop_invariants_acsl, failing_loopinvs,
    _parse_inv_lines, _guess_unwind, LoopSite, synthesize_loop_invariants,
    modified_vars, build_havoc_abstraction, _has_literal_bound, _inject_no_overflow,
    _has_array_writes, _filter_in_scope, _enclosing_function, _loop_function_callees,
    _is_non_behavioral, _minimize_invariants, _loop_assigns, _enclosing_function_source,
    array_map_contracts, array_map_invariants, array_map_loop_assigns, array_map_specs,
    conditional_array_set_contracts, conditional_array_set_invariants,
    conditional_array_set_loop_assigns, conditional_array_set_specs,
    array_scan_contracts, array_scan_invariants, array_scan_loop_assigns,
    array_scan_specs,
    array_max_contracts, array_max_invariants, array_max_loop_assigns,
    array_max_specs,
    conditional_count_contracts, conditional_count_invariants,
    conditional_count_loop_assigns, conditional_count_specs,
    countdown_counter_contracts, countdown_counter_invariants,
    countdown_counter_loop_assigns, countdown_counter_specs,
)


def test_loop_assigns_includes_for_header_counter():
    # The counter is updated in the for-HEADER (`i++`), not the body — the frame
    # must still list it, or WP's frame is unsound and preservation fails.
    src = "void main(){ int A[2048]; int i;\n for (i = 0; i < 1024; i++) { A[i] = i; } }"
    (lp,) = find_loops(src)
    assert _loop_assigns(lp) == "i, A[..]"


def test_loop_assigns_includes_inline_declared_counter():
    # Frama-C/WP expects the loop-local counter in the frame too; AutoSpec's
    # verified annotations include it.
    src = "void g(){ int A[8]; for (int i = 0; i < 8; i++) { A[i] = i; } }"
    (lp,) = find_loops(src)
    assert _loop_assigns(lp) == "i, A[..]"


def test_loop_assigns_while_counter_in_body():
    src = "void f(int n, int *a){ int s = 0, p = 0; while (p < n) { s += a[p]; p++; } }"
    (lp,) = find_loops(src)
    assert _loop_assigns(lp) == "p, s"


def test_array_map_spec_detects_additive_update():
    src = "void inc(int *a,int n,int c){ for(int i=0;i<n;i++){ a[i]=a[i]+c; } }"
    specs = array_map_specs(src, find_loops(src))
    spec = specs[0]
    assert spec.fn == "inc"
    assert spec.array == "a"
    assert spec.index == "i"
    assert spec.bound == "n"
    assert spec.value_at_k == "\\at(a[k], Pre) + c"
    assert array_map_invariants(spec) == [
        "0 <= i <= n",
        "forall k : 0 <= k < i ==> a[k] == \\at(a[k], Pre) + c",
        "forall k : i <= k < n ==> a[k] == \\at(a[k], Pre)",
    ]
    assert array_map_loop_assigns(spec) == "i, a[0 .. n-1]"
    assert "assigns a[0 .. n-1];" in array_map_contracts(src, find_loops(src), "main")["inc"]


def test_array_map_spec_detects_multiplicative_while_update():
    src = "void dbl(int *a,unsigned n){ int p=0; while(p<n){ a[p] = a[p] * 2; p=p+1; } }"
    specs = array_map_specs(src, find_loops(src))
    spec = specs[0]
    assert spec.fn == "dbl"
    assert spec.index == "p"
    assert spec.bound == "n"
    assert spec.value_at_k == "\\at(a[k], Pre) * 2"


def test_conditional_array_set_detects_branch_update():
    src = (
        "void zero_even(int *a,int n){"
        " for(int i=0;i<n;i++){ if(i%2==0) a[i]=0; } }"
    )
    specs = conditional_array_set_specs(src, find_loops(src))
    spec = specs[0]
    assert spec.fn == "zero_even"
    assert spec.array == "a"
    assert spec.index == "i"
    assert spec.bound == "n"
    assert spec.condition_at_k == "k%2==0"
    assert spec.value_at_k == "0"
    assert conditional_array_set_invariants(spec) == [
        "0 <= i <= n",
        "forall k : 0 <= k < i && (k%2==0) ==> a[k] == 0",
    ]
    assert conditional_array_set_loop_assigns(spec) == "i, a[0 .. n-1]"
    assert "assigns a[0 .. n-1];" in conditional_array_set_contracts(
        src, find_loops(src), "main")["zero_even"]


def test_array_scan_detects_membership_contract():
    src = (
        "int search(int *a,int x,int n){"
        " for(int p=0;p<n;p++){ if(x == a[p]) return 1; } return 0; }"
        "void main(){ int a[3]={1,2,3}; int r=search(a,2,3); //@ assert r == 1; }"
    )
    specs = array_scan_specs(src, find_loops(src))
    spec = specs[0]
    assert spec.fn == "search"
    assert spec.kind == "bool_present"
    assert spec.arrays == ("a",)
    assert spec.condition_at_k == "x == a[k]"
    assert spec.negated_condition_at_k == "x != a[k]"
    assert array_scan_invariants(spec) == [
        "0 <= p <= n",
        "forall k : 0 <= k < p ==> x != a[k]",
    ]
    assert array_scan_loop_assigns(spec) == "p"
    contract = array_scan_contracts(src, find_loops(src), "main")["search"]
    assert "requires \\valid_read(a + (0 .. n-1));" in contract
    assert "assigns \\nothing;" in contract
    assert "\\exists integer k; 0 <= k < n && (x == a[k])" in contract


def test_array_scan_detects_index_find_contract():
    src = (
        "int find(int *arr,int n,int x){ int i=0;"
        " for(i=0;i<n;i++){ if(arr[i] == x){ return i; } } return -1; }"
        "void main(){ int a[3]={1,2,3}; int r=find(a,3,2); //@ assert r == 1; }"
    )
    specs = array_scan_specs(src, find_loops(src))
    spec = specs[0]
    assert spec.kind == "index_find"
    assert spec.condition_at_k == "arr[k] == x"
    assert "arr[\\result] == x" in array_scan_contracts(src, find_loops(src), "main")["find"]


def test_array_scan_uses_fresh_quantifier_to_avoid_capture():
    src = (
        "int find(int *arr,int n,int k){ int i=0;"
        " for(i=0;i<n;i++){ if(arr[i] == k){ return i; } } return -1; }"
        "void main(){ int a[3]={1,2,3}; int r=find(a,3,2); //@ assert r == 1; }"
    )
    spec = array_scan_specs(src, find_loops(src))[0]
    assert spec.qvar != "k"
    assert spec.condition_at_k == f"arr[{spec.qvar}] == k"
    contract = array_scan_contracts(src, find_loops(src), "main")["find"]
    assert f"arr[\\result] == k" in contract
    assert f"integer {spec.qvar}" in contract


def test_array_scan_rejects_compound_condition_negation():
    src = (
        "int both(int *a,int *b,int x,int y,int n){"
        " for(int i=0;i<n;i++){ if(a[i] == x && b[i] == y) return 1; } return 0; }"
    )
    assert array_scan_specs(src, find_loops(src)) == {}


def test_array_scan_rejects_extra_global_side_effect():
    src = (
        "int g;"
        "int search(int *a,int x,int n){"
        " for(int p=0;p<n;p++){ if(x == a[p]) return 1; } g = 1; return 0; }"
    )
    assert array_scan_specs(src, find_loops(src)) == {}


def test_array_scan_detects_all_pass_two_arrays():
    src = (
        "int eq(int *a,int *b,int n){"
        " for(int i=0;i<n;i++){ if(a[i] != b[i]) return 0; } return 1; }"
        "int main(){ int a[2]={1,2}; int b[2]={1,2}; int r=eq(a,b,2);"
        " //@ assert r == 1; }"
    )
    specs = array_scan_specs(src, find_loops(src))
    spec = specs[0]
    assert spec.kind == "bool_all"
    assert spec.arrays == ("a", "b")
    assert spec.negated_condition_at_k == "a[k] == b[k]"
    contract = array_scan_contracts(src, find_loops(src), "main")["eq"]
    assert "requires \\valid_read(a + (0 .. n-1));" in contract
    assert "requires \\valid_read(b + (0 .. n-1));" in contract
    assert "\\forall integer k; 0 <= k < n ==> a[k] == b[k]" in contract


def test_array_max_detects_while_and_for_scan_contracts():
    src = (
        "int m1(int *a,int n){ int i=1; int max=a[0];"
        " while(i<n){ if(max < a[i]) max = a[i]; i=i+1; } return max; }"
        "int m2(int *arr,int n){ int max=arr[0];"
        " for(int i=0;i<n;i++){ if(arr[i] > max){ max=arr[i]; } } return max; }"
        "int main(){ int a[2]={1,2}; int r=m1(a,2); //@ assert r >= a[0]; }"
    )
    specs = array_max_specs(src, find_loops(src))
    assert specs[0].fn == "m1"
    assert specs[0].array == "a"
    assert specs[0].index == "i"
    assert specs[0].max_var == "max"
    assert array_max_invariants(specs[0]) == [
        "0 <= i <= n",
        "forall k : 0 <= k < i ==> max >= a[k]",
    ]
    assert array_max_loop_assigns(specs[0]) == "i, max"
    assert specs[1].fn == "m2"
    assert specs[1].array == "arr"
    contract = array_max_contracts(src, find_loops(src), "main")["m1"]
    assert "requires n > 0;" in contract
    assert "assigns \\nothing;" in contract
    assert "\\result >= a[k]" in contract


def test_conditional_count_detects_output_relation_contract():
    src = (
        "int count_matches(int *a,int n,int x,int *out){ int p=0; int c=0; *out=0;"
        " while(p<n){ if(a[p] == x){ c = c + 1; *out = *out + x; } p=p+1; }"
        " return c; }"
        "int main(){ int a[2]={1,2}; int out=0; int c=count_matches(a,2,2,&out);"
        " //@ assert out == c*2; }"
    )
    specs = conditional_count_specs(src, find_loops(src))
    spec = specs[0]
    assert spec.array == "a"
    assert spec.count_var == "c"
    assert spec.out_ptr == "out"
    assert spec.addend == "x"
    assert conditional_count_invariants(spec) == [
        "0 <= p <= n",
        "0 <= c <= p",
        "*out == c * x",
    ]
    assert conditional_count_loop_assigns(spec) == "p, c, *out"
    contract = conditional_count_contracts(src, find_loops(src), "main")["count_matches"]
    assert "requires \\valid(out);" in contract
    assert "assigns *out;" in contract
    assert "ensures *out == \\result * x;" in contract


def test_conditional_count_rejects_non_invariant_addend():
    src = (
        "int count_matches(int *a,int n,int x,int *out){ int p=0; int c=0; *out=0;"
        " while(p<n){ if(a[p] == x){ c = c + 1; *out = *out + a[p]; } p=p+1; }"
        " return c; }"
    )
    assert conditional_count_specs(src, find_loops(src)) == {}


def test_conditional_count_rejects_extra_output_side_effect():
    src = (
        "int count_matches(int *a,int n,int x,int *out,int *other){ int p=0; int c=0;"
        " *out=0; *other=0;"
        " while(p<n){ if(a[p] == x){ c = c + 1; *out = *out + x; } p=p+1; }"
        " return c; }"
    )
    assert conditional_count_specs(src, find_loops(src)) == {}


def test_countdown_counter_detects_returned_iteration_count():
    src = (
        "int count_down(int x){ int a=x; int y=0;"
        " while(a != 0){ y = y + 1; a = a - 1; } return y; }"
        "int main(){ int r=count_down(3); //@ assert r == 3; }"
    )
    specs = countdown_counter_specs(src, find_loops(src))
    spec = specs[0]
    assert spec.counter == "a"
    assert spec.result_var == "y"
    assert spec.input_var == "x"
    assert countdown_counter_invariants(spec) == [
        "0 <= a",
        "y + a == x",
    ]
    assert countdown_counter_loop_assigns(spec) == "a, y"
    contract = countdown_counter_contracts(src, find_loops(src), "main")["count_down"]
    assert "requires x >= 0;" in contract
    assert "ensures \\result == x;" in contract

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


def test_filter_in_scope_drops_pointer_as_integer():
    src = "int fun(int x, int y, int *r){ while(*r >= y){ *r = *r - y; } return 0; }"
    kept = _filter_in_scope([
        "r == 1",
        "r + y == 1",
        "*r >= 0",
        "\\valid(r)",
    ], src)
    assert kept == ["*r >= 0", "\\valid(r)"]


def test_filter_in_scope_drops_unsupported_logic_calls():
    src = (
        "/*@ axiomatic A { logic integer Acc(int *a, integer m, integer k); } */\n"
        "int f(int *a, int n){ while(n > 0){ n--; } return n; }"
    )
    kept = _filter_in_scope([
        "power(n) == 1",
        "Acc(a, 0, n) >= 0",
    ], src)
    assert kept == ["Acc(a, 0, n) >= 0"]


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


def test_enclosing_function_source_excludes_caller_locals():
    src = ("void reverse(int *a, int n){ int i=0; while(i<n){ a[i]=i; i++; } }\n"
           "void main(){ int arr[5]={1,2,3,4,5}; reverse(arr,5); /*@ assert arr[4]==4; */ }\n")
    lp = find_loops(src)[0]
    fn_src = _enclosing_function_source(src, lp.start_offset)
    assert "void reverse" in fn_src
    assert "int *a" in fn_src
    assert "arr[5]" not in fn_src
    assert _filter_in_scope(["a[0] == 0", "arr[4] == 4"], fn_src) == ["a[0] == 0"]


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


# --- general (update-shape-agnostic) relational candidates --------------------
from bmc_agent.loop_invariants import relational_equality_candidates


def test_relational_candidates_cover_lockstep_that_syntactic_misses():
    # lockstep i=i+1; j=j+1 keeps i==j, but the RHS differ syntactically — the
    # syntactic detector misses it; the pairwise candidate generator includes it.
    src = "void m(){int i=0,j=0; while(u()){ i=i+1; j=j+1; } }"
    lp = find_loops(src)[0]
    assert equal_update_invariants(lp) == []                 # syntactic: miss
    assert "i == j" in relational_equality_candidates(lp)    # general: proposed


def test_relational_candidates_all_pairs_likely_first():
    src = "void m(){int a=0,b=0,c=0; while(u()){int s=a; a=s; b=s; c=c+1;} }"
    cands = relational_equality_candidates(find_loops(src)[0])
    assert set(cands) == {"a == b", "a == c", "b == c"}      # every pair
    assert cands[0] == "a == b"                              # shared-RHS pair first


def test_relational_candidates_capped_and_bounded():
    # < 2 scalars → nothing; honors max_scalars cap
    src1 = "void m(){int x=0; while(u()){ x=x+1; } }"
    assert relational_equality_candidates(find_loops(src1)[0]) == []
    src2 = "void m(){int a=0,b=0,c=0; while(u()){a=a+1;b=b+1;c=c+1;} }"
    assert relational_equality_candidates(find_loops(src2)[0], max_scalars=2) == []


class _Chk:
    """Mock check_fn: a clause set 'verifies' iff it includes every clause in
    `required` (simulates load-bearing clauses for the goal)."""
    def __init__(self, required): self.required = set(required)
    def __call__(self, ann):
        clauses = {c for v in ann.values() for c in v}
        class R:  # minimal LoopCheck-like
            pass
        r = R(); r.verified = self.required <= clauses
        return r


def test_dedup_keeps_independent_drops_redundant(monkeypatch):
    import bmc_agent.loop_invariants as li
    # stub entailment: only `y >= 1` is entailed by the rest; `y <= 100000` is not
    def fake_entails(rest, clause, config):
        return clause.strip() == "y >= 1" and {"x >= 1", "x == y"} <= set(rest)
    monkeypatch.setattr(li, "_entails", fake_entails)
    ann = {0: ["x >= 1", "x == y", "y >= 1", "y <= 100000"]}
    chk = _Chk(["x >= 1", "x == y"])          # goal needs only these
    out = li._dedup_invariants(ann, chk, [], object(), li.logger)
    assert "y >= 1" not in out[0]             # redundant -> dropped
    assert "y <= 100000" in out[0]            # independent sound fact -> KEPT (not minimized away)
    assert "x == y" in out[0] and "x >= 1" in out[0]


def test_generality_gate_drops_removable_flags_loadbearing():
    import bmc_agent.loop_invariants as li
    # n==5 is removable (goal still proves); a[0]==1 is load-bearing here
    ann = {0: ["0 <= i", "n == 5", "a[0] == 1", "x == y"]}
    chk = _Chk(["0 <= i", "a[0] == 1", "x == y"])   # proof needs a[0]==1 but NOT n==5
    out, flagged = li._generality_gate(ann, chk, [], li.logger)
    assert "n == 5" not in out[0]             # removable caller-specific -> dropped
    assert "a[0] == 1" in out[0]              # load-bearing caller-specific -> kept...
    assert "a[0] == 1" in flagged             # ...and FLAGGED as non-behavioral
    assert "x == y" in out[0] and "x == y" not in flagged   # behavioral, untouched


def test_reinject_pairs_dropped_clause_with_auxiliary():
    from bmc_agent.loop_invariants import _reinject
    reinj = set()
    # refinement proposed the auxiliary `x>=1` but dropped the goal-relevant `x>=y`;
    # re-injection restores `x>=y` so the pair lands in the set together.
    out = _reinject(["y >= 0", "y <= 100000", "x >= 1"], ["x >= y"], reinj)
    assert "x >= y" in out and "x >= 1" in out
    assert reinj == {"x >= y"}             # tracked, so a 2nd failure can give up on it
    # no dropped clauses → identity, nothing tracked
    r2 = set()
    assert _reinject(["x >= 1"], [], r2) == ["x >= 1"] and r2 == set()
    # already present → not duplicated, not tracked
    r3 = set()
    assert _reinject(["x >= y"], ["x >= y"], r3) == ["x >= y"] and r3 == set()
