"""Assertion-driven spec synthesis — deterministic helpers."""
from bmc_agent.assert_driven_specs import (
    extract_asserts, called_functions, _failing_asserts,
    callee_lhs_map, attribute_assert, extract_goals, synthesize, SynthResult,
    _resolve_entry, _function_has_goal,
)


class _FakeParsed:
    def __init__(self, bodies): self.function_bodies = bodies


def test_resolve_entry_picks_goal_bearing_function():
    # default entry 'main' doesn't exist; asserts live in foo -> resolve to foo
    p = _FakeParsed({
        "max": "{ if (x>=y) return x; return y; }",
        "foo": "{ int s=max(34,45); //@ assert s==45; }",
    })
    assert _resolve_entry(p, "main") == "foo"
    # explicit entry that already bears a goal is respected
    assert _resolve_entry(p, "foo") == "foo"


def test_resolve_entry_keeps_entry_when_it_has_goals():
    p = _FakeParsed({"g": "{ return x; }",
                     "main": "{ int v=g(3); //@ assert v==3; }"})
    assert _resolve_entry(p, "main") == "main"


def test_resolve_entry_no_switch_when_ambiguous():
    # two goal-bearing functions, entry has none -> don't guess, keep entry
    p = _FakeParsed({"a": "{ //@ assert 1; }", "b": "{ assert(2); }",
                     "main": "{ return 0; }"})
    assert _resolve_entry(p, "main") == "main"


def test_function_has_goal_forms():
    assert _function_has_goal("{ //@ assert x==1; }")
    assert _function_has_goal("{ __VERIFIER_assert(p); }")
    assert _function_has_goal("{ static_assert(q, \"m\"); }")
    assert not _function_has_goal("{ int t = a + b; return t; }")


def test_synthesize_no_specifiable_function_is_na(tmp_path):
    # No verification goal AND nothing to specify (the program is only a driver):
    # there is genuinely nothing to prove, so it must be N/A — NOT a vacuous pass.
    # This path is reached BEFORE any LLM call (the target set is empty), so it runs
    # with llm=None.
    src = "int main(){ int s = 0; return s; }\n"
    f = tmp_path / "driver_only.c"; f.write_text(src)
    from bmc_agent.config import Config
    r = synthesize(str(f), Config.from_env(), llm=None, entry="main")
    assert isinstance(r, SynthResult)
    assert r.no_goals is True
    assert r.ok is False          # never counted as SATISFIED
    assert r.iterations == 0


def test_split_conjuncts_top_level_only():
    # Goal-free contract mining keeps the sound conjuncts and drops over-claims, so
    # the splitter must cut on TOP-LEVEL && only (paren/ternary aware).
    from bmc_agent.assert_driven_specs import _split_conjuncts
    assert _split_conjuncts("result == p*n*r/100 && result >= 0") == \
        ["result == p*n*r/100", "result >= 0"]
    assert _split_conjuncts("a && (b && c) || d") == ["a", "(b && c) || d"]
    assert _split_conjuncts("result == (x && y ? 1 : 0)") == \
        ["result == (x && y ? 1 : 0)"]
    assert _split_conjuncts("  x >= 0  ") == ["x >= 0"]


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


# --- #2 behavioral-strengthening helpers -------------------------------------
from bmc_agent.assert_driven_specs import _clean_expr


def test_clean_expr_strips_code_fence_and_keyword():
    # the model wraps the line in a fence despite the "bare line" instruction
    assert _clean_expr("```c\nresult == (a > b ? a : b)\n```") == "result == (a > b ? a : b)"
    # leading 'ensures' keyword and trailing semicolon are removed
    assert _clean_expr("ensures result >= 0;") == "result >= 0"
    # bare line passes through
    assert _clean_expr("(result == 1) == (x > 0)") == "(result == 1) == (x > 0)"
    # empty / fence-only replies yield empty (caller treats as "no candidate")
    assert _clean_expr("```\n```") == ""
    assert _clean_expr("") == ""


def test_clean_expr_skips_comment_only_first_line():
    assert _clean_expr("// here is the spec\nresult == x") == "result == x"
