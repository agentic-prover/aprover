"""
Tests for body-evidence handle-validation-contract PRE inference
in SpecGeneratorV2 (replaces the realism + spec_refiner downstream
loop for the dominant caller-contract-slip FP class on libarchive).

Background: every public C library API typically starts with a
"handle validation" macro/function call:

  archive_check_magic(_a, ARCHIVE_MATCH_MAGIC, ...);   // libarchive
  __archive_check_magic((_a), ((0xcad11c9U)), ...);   // preprocessed

When v2 spec-gen treats these as ``true/true`` (the alternative),
BMC explores ``_a == NULL`` and reports a deref CEx — a real
crash, but a caller-contract slip, not a library bug. Encoding
the magic-check pattern as the PRE prevents that entire FP class.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# _looks_like_magic_constant
# ---------------------------------------------------------------------------

def test_looks_like_magic_constant_accepts_uppercase_with_magic():
    from bmc_agent.spec_generator_v2 import _looks_like_magic_constant
    assert _looks_like_magic_constant("ARCHIVE_MATCH_MAGIC") is True
    assert _looks_like_magic_constant("SQLITE_MAGIC_OPEN") is True


def test_looks_like_magic_constant_accepts_hex_literal():
    from bmc_agent.spec_generator_v2 import _looks_like_magic_constant
    # Preprocessed form: 0xcad11c9U
    assert _looks_like_magic_constant("0xcad11c9U") is True
    assert _looks_like_magic_constant("0xCAFEBABE") is True
    assert _looks_like_magic_constant("0X1234") is True


def test_looks_like_magic_constant_accepts_decimal():
    from bmc_agent.spec_generator_v2 import _looks_like_magic_constant
    assert _looks_like_magic_constant("42") is True
    assert _looks_like_magic_constant("1u") is True


def test_looks_like_magic_constant_rejects_uppercase_without_magic():
    from bmc_agent.spec_generator_v2 import _looks_like_magic_constant
    # All-caps but no MAGIC token — could be a flag, not a handle type
    assert _looks_like_magic_constant("ARCHIVE_STATE_NEW") is False
    assert _looks_like_magic_constant("FLAG_X") is False


def test_looks_like_magic_constant_rejects_lowercase_identifier():
    from bmc_agent.spec_generator_v2 import _looks_like_magic_constant
    assert _looks_like_magic_constant("flag") is False
    assert _looks_like_magic_constant("foo_bar") is False


def test_looks_like_magic_constant_rejects_empty():
    from bmc_agent.spec_generator_v2 import _looks_like_magic_constant
    assert _looks_like_magic_constant("") is False


# ---------------------------------------------------------------------------
# _infer_handle_contract_precondition
# ---------------------------------------------------------------------------

def _make_func(name: str, params, body: str):
    """Build a minimal FunctionInfo-shaped object for the inference helper."""
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    sig = FunctionSignature(
        name=name, return_type="int",
        parameters=params,
    )
    return FunctionInfo(
        name=name, signature=sig, body=body, callees=set(),
        source_file=f"/tmp/{name}.c",
    )


def test_infer_symbolic_macro_form():
    """The pre-preprocessor form ``archive_check_magic(_a, ARCHIVE_MATCH_MAGIC, ...)``
    is what bmc-agent sees when ``preprocess=False`` (rare on real OSS but
    happens in unit tests and bare-metal targets)."""
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    body = """\
{
    struct archive_match *a;

    archive_check_magic(_a, ARCHIVE_MATCH_MAGIC, ARCHIVE_STATE_NEW, "fn");
    a = (struct archive_match *)_a;
    return 0;
}
"""
    f = _make_func("fn", [("struct archive *", "_a"), ("int", "x")], body)
    result = _infer_handle_contract_precondition(f)
    assert result == ("_a", "ARCHIVE_MATCH_MAGIC")


def test_infer_preprocessed_expanded_form():
    """The form bmc-agent actually sees on libarchive
    (because preprocess=True): the macro is expanded into the underlying
    function call with parens-wrapped args and a hex magic literal."""
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    body = """\
{
 struct archive_match *a;
 do { int magic_test = __archive_check_magic((_a), ((0xcad11c9U)), (1U), ("archive_match_include_uid")); if (magic_test == (-30)) return (-30); } while (0);
 a = (struct archive_match *)_a;
 return (add_owner_id(a, &(a->inclusion_uids), 0));
}
"""
    f = _make_func(
        "archive_match_include_uid",
        [("struct archive *", "_a"), ("int64_t", "uid")],
        body,
    )
    result = _infer_handle_contract_precondition(f)
    assert result == ("_a", "0xcad11c9U")


def test_infer_returns_none_when_no_magic_check_in_body():
    """A function whose body does NOT start with a handle-validation call —
    e.g. an internal helper that operates on already-validated state —
    should NOT have a contract inferred. The caller falls back to the
    LLM-driven spec_gen path."""
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    body = """\
{
    if (datestr == NULL || *datestr == '\\0') {
        return -1;
    }
    return 0;
}
"""
    f = _make_func(
        "set_timefilter_date",
        [("struct archive_match *", "a"), ("const char *", "datestr")],
        body,
    )
    assert _infer_handle_contract_precondition(f) is None


def test_infer_returns_none_when_first_arg_is_not_a_parameter():
    """A function whose body calls a magic-check function with a LOCAL
    variable (not a parameter) — that's some other state, not the caller
    contract for THIS function."""
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    body = """\
{
    struct archive *local;
    /* not the caller's contract — local is internal */
    archive_check_magic(local, ARCHIVE_MATCH_MAGIC, 0, "x");
    return 0;
}
"""
    f = _make_func("fn", [("int", "x")], body)
    assert _infer_handle_contract_precondition(f) is None


def test_infer_returns_none_when_second_arg_is_not_magic_shaped():
    """A function whose call's second arg is e.g. a flag constant (not a
    magic type tag) — the regex would match the call shape but the
    constant filter rejects it. Avoids over-eager inference on non-magic
    macros that happen to share a similar call shape."""
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    body = """\
{
    /* macro name has 'check' but 2nd arg is a flag, not magic */
    range_check_magic(_a, FLAG_VERBOSE, 5);
    return 0;
}
"""
    f = _make_func("fn", [("int", "_a")], body)
    assert _infer_handle_contract_precondition(f) is None


def test_infer_returns_none_when_macro_name_lacks_magic_token():
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    body = """\
{
    validate_handle(_a, ARCHIVE_MATCH_MAGIC, 0);
    return 0;
}
"""
    f = _make_func("fn", [("int", "_a")], body)
    assert _infer_handle_contract_precondition(f) is None


def test_infer_only_scans_top_of_body():
    """A magic-check call buried 50 lines deep in the body is NOT the
    canonical caller-contract pattern (it'd be a re-validation in a
    different code path). Inference scans only the first ~12 lines to
    avoid promoting deep checks to the PRE."""
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    deep_body = "{\n" + ("    /* filler */\n" * 50) + (
        "    archive_check_magic(_a, ARCHIVE_MATCH_MAGIC, 0, \"x\");\n"
    ) + "}\n"
    f = _make_func("fn", [("int", "_a")], deep_body)
    assert _infer_handle_contract_precondition(f) is None


def test_infer_empty_body_returns_none():
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    f = _make_func("fn", [("int", "x")], "")
    assert _infer_handle_contract_precondition(f) is None


def test_infer_no_parameters_returns_none():
    """A function with no parameters can't have a handle contract."""
    from bmc_agent.spec_generator_v2 import _infer_handle_contract_precondition
    f = _make_func("fn", [], "{\n  archive_check_magic(x, ARCHIVE_MATCH_MAGIC, 0, \"y\");\n}")
    assert _infer_handle_contract_precondition(f) is None


# ---------------------------------------------------------------------------
# _spec_from_handle_contract
# ---------------------------------------------------------------------------

def test_spec_from_handle_contract_shape():
    from bmc_agent.spec_generator_v2 import _spec_from_handle_contract
    from bmc_agent.spec import SpecStatus
    spec = _spec_from_handle_contract("foo", "_a", "ARCHIVE_MATCH_MAGIC")
    assert spec.function_name == "foo"
    assert spec.precondition == "_a != NULL && _a->magic == ARCHIVE_MATCH_MAGIC"
    assert spec.postcondition == "true"
    assert spec.status == SpecStatus.GENERATED
    assert spec.pre_validity == "_a != NULL && _a->magic == ARCHIVE_MATCH_MAGIC"
    # evidence tagged for downstream filtering / audit
    assert any(
        "caller_contract:magic_check" in tags
        for tags in spec.evidence.values()
    )


def test_spec_from_handle_contract_preprocessed_magic():
    """Hex-literal magic from the preprocessed form is preserved verbatim
    in the PRE — CBMC compares it directly against the field at verification
    time, so we must NOT translate it (e.g. drop the ``U`` suffix)."""
    from bmc_agent.spec_generator_v2 import _spec_from_handle_contract
    spec = _spec_from_handle_contract("foo", "_a", "0xcad11c9U")
    assert "0xcad11c9U" in spec.precondition
