from pathlib import Path

from bmc_agent.acsl import (
    FramaCResult,
    build_acsl_source,
    recover_plain_asserts_to_acsl,
    translate_spec_to_acsl,
)
from bmc_agent.parser import FunctionSignature, parse_c_file
from bmc_agent.spec import Spec


def test_translate_max2_postcondition_to_acsl() -> None:
    sig = FunctionSignature(
        name="max2",
        return_type="int",
        parameters=[("int", "x"), ("int", "y")],
    )
    spec = Spec(
        function_name="max2",
        precondition="true",
        postcondition=(
            "result >= x && result >= y && (result == x || result == y) && "
            "((x >= y && result == x) || (y > x && result == y))"
        ),
    )

    contract = translate_spec_to_acsl(spec, sig)

    assert "ensures \\result >= x;" in contract.text
    assert "ensures \\result >= y;" in contract.text
    assert "ensures (\\result == x) || (\\result == y);" in contract.text
    assert "((x >= y) && (\\result == x)) || ((y > x) && (\\result == y))" in contract.text
    assert not contract.unsupported


def test_recover_plain_asserts_skips_comments_and_strings() -> None:
    source = (
        "// assert(comment_only);\n"
        'const char *s = "assert(string_only)";\n'
        "void f(int x) {\n"
        "  assert(x > 0);\n"
        "}\n"
    )

    recovered, count = recover_plain_asserts_to_acsl(source)

    assert count == 1
    assert "// assert(comment_only);" in recovered
    assert '"assert(string_only)"' in recovered
    assert "  //@ assert x > 0;" in recovered
    assert "assert(x > 0);" not in recovered


def test_build_acsl_source_inserts_contract_before_function() -> None:
    source = "int max2(int x, int y) {\n  return x >= y ? x : y;\n}\n"
    parsed = parse_c_file(Path("max2.c"), source_text=source)
    spec = Spec(
        function_name="max2",
        precondition="true",
        postcondition="result >= x && result >= y",
    )

    build = build_acsl_source(
        source,
        parsed,
        {"max2": spec},
        add_assigns_nothing=True,
    )

    assert build.inserted_functions == ["max2"]
    assert build.source_text.startswith("/*@")
    assert "ensures \\result >= x;" in build.source_text
    assert "assigns \\nothing;" in build.source_text
    assert "*/\nint max2" in build.source_text


def test_build_acsl_source_can_recover_asserts_before_insertion() -> None:
    source = "#include <assert.h>\nint f(int x) {\n  assert(x > 0);\n  return x;\n}\n"
    parsed = parse_c_file(Path("f.c"), source_text=source)
    spec = Spec(function_name="f", precondition="true", postcondition="result == x")

    build = build_acsl_source(
        source,
        parsed,
        {"f": spec},
        recover_asserts=True,
    )

    assert build.recovered_asserts == 1
    assert build.inserted_functions == ["f"]
    assert "ensures \\result == x;" in build.source_text
    assert "//@ assert x > 0;" in build.source_text


def test_unsupported_dsl_primitive_is_reported_not_rendered() -> None:
    sig = FunctionSignature(
        name="f",
        return_type="int",
        parameters=[("int", "x")],
    )
    spec = Spec(
        function_name="f",
        precondition="no_overflow(x + 1)",
        postcondition="true",
    )

    contract = translate_spec_to_acsl(spec, sig)

    assert contract.text == ""
    assert len(contract.unsupported) == 1
    assert contract.unsupported[0].reason == "unsupported DSL primitive: no_overflow"


def test_frama_c_result_classifies_annotation_errors() -> None:
    result = FramaCResult(
        command=["frama-c"],
        returncode=1,
        runtime_s=0.1,
        stdout="[kernel:annot-error] invalid user input",
        stderr="",
    )

    assert result.status == "annotation_error"
