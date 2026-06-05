"""DSL -> ACSL rendering."""
from bmc_agent.acsl import (
    expr_to_acsl,
    contract_to_acsl,
    loop_invariants_to_acsl,
    condition_to_acsl_clauses,
)


def test_expr_result_and_forall():
    assert expr_to_acsl("result >= x") == "\\result >= x"
    # boolean structure is fully parenthesised (precedence-independent render)
    assert expr_to_acsl("forall k : 0 <= k < i ==> A[k] == k") == \
        "\\forall integer k; (0 <= k < i) ==> (A[k] == k)"
    assert expr_to_acsl("forall k, 0 <= k < i => A[k] == k") == \
        "\\forall integer k; (0 <= k < i) ==> (A[k] == k)"


def test_expr_true_false():
    assert expr_to_acsl("true") == "\\true"
    assert expr_to_acsl("") == "\\true"
    assert expr_to_acsl("false") == "\\false"


def test_expr_valid_predicates():
    assert expr_to_acsl("valid(p)") == "\\valid(p)"
    assert expr_to_acsl("valid_range(buf, 0, n)") == "\\valid(buf + (0 .. (n) - 1))"
    # 'result' inside a larger expr, not double-escaped
    assert expr_to_acsl("valid(p) && result == 0") == "(\\valid(p)) && (\\result == 0)"


def test_expr_null_predicate():
    # DSL null(p)/!null(p) -> idiomatic ACSL
    assert expr_to_acsl("!null(p) && !null(q)") == "(!(p == \\null)) && (!(q == \\null))"
    assert expr_to_acsl("null(ptr)") == "(ptr == \\null)"


def test_contract_block_drops_vacuous():
    # requires true is dropped; ensures rendered
    block = contract_to_acsl("true", "result >= x && result >= y && (result == x || result == y)")
    assert block.startswith("/*@")
    assert "requires" not in block
    assert "ensures \\result >= x;" in block
    assert "ensures \\result >= y;" in block
    assert "ensures ((\\result == x) || (\\result == y));" in block


def test_contract_splits_conjuncts_and_drops_prose():
    block = contract_to_acsl(
        "true",
        "*a <= *b && the multiset of outputs equals the original values && *b <= *c",
    )
    assert "ensures *a <= *b;" in block
    assert "ensures *b <= *c;" in block
    assert "multiset" not in block


def test_condition_to_acsl_clauses_keeps_comma_forall():
    assert condition_to_acsl_clauses("forall i, 0 <= i < n => a[i] == 0") == [
        "\\forall integer i; (0 <= i < n) ==> (a[i] == 0)"
    ]


def test_nested_forall_and_guard_only_implication_repair():
    assert expr_to_acsl("(forall k : 0 <= k < n) ==> a[k] == 0") == \
        "\\forall integer k; (0 <= k < n) ==> (a[k] == 0)"
    assert expr_to_acsl("(i == n) ==> ((forall k : 0 <= k < n) ==> (a[k] == 0))") == \
        "(i == n) ==> (\\forall integer k; (0 <= k < n) ==> (a[k] == 0))"
    assert expr_to_acsl(
        "forall j : 0 <= j < i ==> (forall k : i <= k < n) ==> a[j] <= a[k]"
    ) == (
        "\\forall integer j; (0 <= j < i) ==> "
        "(\\forall integer k; (i <= k < n) ==> (a[j] <= a[k]))"
    )


def test_contract_block_empty_when_vacuous():
    assert contract_to_acsl("true", "true") == ""


def test_loop_invariants_block():
    block = loop_invariants_to_acsl(["i <= 1024", "forall k : (k < i) ==> (A[k] == k)"],
                                    assigns="i, A[0 .. 1023]")
    assert "loop invariant i <= 1024;" in block
    assert "loop invariant \\forall integer k; (k < i) ==> (A[k] == k);" in block
    assert "loop assigns i, A[0 .. 1023];" in block
