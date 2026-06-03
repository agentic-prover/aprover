"""DSL -> ACSL rendering."""
from bmc_agent.acsl import expr_to_acsl, contract_to_acsl, loop_invariants_to_acsl


def test_expr_result_and_forall():
    assert expr_to_acsl("result >= x") == "\\result >= x"
    assert expr_to_acsl("forall k : 0 <= k < i ==> A[k] == k") == \
        "\\forall integer k; 0 <= k < i ==> A[k] == k"


def test_expr_true_false():
    assert expr_to_acsl("true") == "\\true"
    assert expr_to_acsl("") == "\\true"
    assert expr_to_acsl("false") == "\\false"


def test_expr_valid_predicates():
    assert expr_to_acsl("valid(p)") == "\\valid(p)"
    assert expr_to_acsl("valid_range(buf, 0, n)") == "\\valid(buf + (0 .. (n) - 1))"
    # 'result' inside a larger expr, not double-escaped
    assert expr_to_acsl("valid(p) && result == 0") == "\\valid(p) && \\result == 0"


def test_expr_null_predicate():
    # DSL null(p)/!null(p) -> idiomatic ACSL
    assert expr_to_acsl("!null(p) && !null(q)") == "!(p == \\null) && !(q == \\null)"
    assert expr_to_acsl("null(ptr)") == "(ptr == \\null)"


def test_contract_block_drops_vacuous():
    # requires true is dropped; ensures rendered
    block = contract_to_acsl("true", "result >= x && result >= y && (result == x || result == y)")
    assert block.startswith("/*@")
    assert "requires" not in block
    assert "ensures \\result >= x && \\result >= y && (\\result == x || \\result == y);" in block


def test_contract_block_empty_when_vacuous():
    assert contract_to_acsl("true", "true") == ""


def test_loop_invariants_block():
    block = loop_invariants_to_acsl(["i <= 1024", "forall k : (k < i) ==> (A[k] == k)"],
                                    assigns="i, A[0 .. 1023]")
    assert "loop invariant i <= 1024;" in block
    assert "loop invariant \\forall integer k; (k < i) ==> (A[k] == k);" in block
    assert "loop assigns i, A[0 .. 1023];" in block
