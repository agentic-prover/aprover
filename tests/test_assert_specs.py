"""Assertion-driven spec synthesis — deterministic helpers."""
from bmc_agent.assert_driven_specs import extract_asserts, called_functions, _failing_asserts


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
