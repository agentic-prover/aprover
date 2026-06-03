"""Assertion-driven spec synthesis — deterministic helpers."""
from bmc_agent.assert_driven_specs import (
    extract_asserts, called_functions, _failing_asserts,
    callee_lhs_map, attribute_assert, extract_goals,
)


def test_extract_goals_all_forms():
    src = (
        "int main(){\n"
        "  assert(x >= y);\n"
        "  static_assert(y >= 1, \"y positive\");\n"
        "  __VERIFIER_assert(A[1023] == 1023);\n"
        "  //@ assert z == 0 ;\n"
        "}\n"
    )
    goals = extract_goals(src)
    assert "x >= y" in goals
    assert "y >= 1" in goals                 # static_assert message dropped
    assert "A[1023] == 1023" in goals
    assert "z == 0" in goals                 # ACSL comment form


def test_extract_goals_nested_parens_and_dedup():
    src = ("int main(){ assert(f(a, b) == g(c)); assert(f(a, b) == g(c)); "
           "__VERIFIER_assert(p && (q || r)); }")
    goals = extract_goals(src)
    assert goals.count("f(a, b) == g(c)") == 1      # de-duplicated
    assert "p && (q || r)" in goals                  # balanced parens, no truncation


def test_extract_goals_static_assert_without_message():
    # no trailing string literal -> keep the whole condition
    assert "n > 0" in extract_goals("_Static_assert(n > 0);")


def test_extract_asserts():
    src = ("int main(){ int x=add();\n"
           "//@ assert x == a + b ;\n"
           "//@ assert x == 56 ;\n}")
    assert extract_asserts(src) == ["x == a + b", "x == 56"]


def test_called_functions_detects_real_calls_not_defs():
    src = ("int add(int*p,int*q){ return *p+*q; }\n"
           "int main(){ int x = add(&a,&b); return 0; }\n")
    # 'add' is both defined and called; 'main' defined but here we ask for callees
    got = called_functions(src, ["add", "main", "unused"])
    assert "add" in got
    assert "unused" not in got


def test_failing_asserts_recovers_expr_from_description():
    class CE:
        def __init__(self, prop, desc):
            self.failing_property, self.description = prop, desc
            self.variable_assignments, self.trace, self.failure_location = {}, [], {}
    class R:
        counterexamples = [CE("main.assertion.1", "assert: x == 57"),
                           CE("main.bounds.1", "array bounds")]
    assert _failing_asserts(R()) == ["x == 57"]


def test_callee_lhs_map_traces_assignments():
    entry = ("int main(){ int s = sum(a,n); int m = maxv(a,n);\n"
             "  s = sum(b,n);  /* second call site, same lhs */\n"
             "  //@ assert m >= s ; }")
    m = callee_lhs_map(entry, ["sum", "maxv", "noret"])
    assert m["sum"] == ["s"]          # de-duped across both call sites
    assert m["maxv"] == ["m"]
    assert "noret" not in m           # never assigned -> absent


def test_attribute_assert_implicated_callee_first():
    lhs_map = {"sum": ["s"], "maxv": ["m"]}
    callees = ["sum", "maxv"]
    # assert mentions only `m` -> maxv implicated first, sum as fallback
    assert attribute_assert("m >= s_unrelated_token", lhs_map, callees) == ["maxv", "sum"]
    # assert mentions `s` -> sum first
    assert attribute_assert("s == a + b", lhs_map, callees) == ["sum", "maxv"]


def test_attribute_assert_falls_back_to_all_when_no_match():
    lhs_map = {"sum": ["s"], "maxv": ["m"]}
    # nothing matches -> preserve source order so refinement still progresses
    assert attribute_assert("z == 0", lhs_map, ["sum", "maxv"]) == ["sum", "maxv"]


def test_attribute_assert_substring_not_falsely_matched():
    # lhs 's' must match as a whole identifier, not inside 'sum' or 'samples'
    lhs_map = {"sum": ["s"]}
    assert attribute_assert("samples == sum_total", lhs_map, ["sum"]) == ["sum"]  # fallback, no real hit
    assert attribute_assert("s == 0", lhs_map, ["sum"]) == ["sum"]               # real hit
