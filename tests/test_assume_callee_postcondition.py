"""#2: flag-gated functional-postcondition propagation in callee stubs."""
from bmc_agent.harness_generator import _c_expressible_postcondition, _generate_stub
from bmc_agent.source_parser import parse_source_file
from bmc_agent.spec import Spec


def test_c_expressible_accepts_clean_and_rejects_prose_acsl():
    assert _c_expressible_postcondition("result == *p + *q", ["p", "q"]) == "result == *p + *q"
    assert _c_expressible_postcondition("\\result == *p", ["p"]) == "result == *p"
    # prose (unbound word 'sum') and ACSL (\old) rejected
    assert _c_expressible_postcondition("result == (sum of a[0] through a[n-1])", ["a", "n"]) is None
    assert _c_expressible_postcondition("result == *p && *p == \\old(*p)", ["p"]) is None


def _stub(flag):
    src = "int add(int *p, int *q){ return *p + *q; }\n"
    parsed = parse_source_file("/tmp/t.c", source_text=src)
    spec = Spec(function_name="add", precondition="valid(p) && valid(q)",
                postcondition="result == *p + *q")
    return _generate_stub("add", spec, parsed, assume_postcondition=flag)


def test_flag_on_emits_assume():
    s = _stub(True)
    assert "__CPROVER_assume(result == *p + *q);" in s


def test_flag_off_keeps_comment_unchanged():
    s = _stub(False)
    assert "__CPROVER_assume(result == *p + *q);" not in s
    assert "/* condition: result == *p + *q */" in s
