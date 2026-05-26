"""Tests for bmc_agent.spec_generator_v2 — the v2 orchestrator + parsers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bmc_agent.spec import Spec, SpecStatus
from bmc_agent.spec_generator_v2 import (
    SpecGeneratorV2,
    _build_bottom_up_layers,
    _build_spec_from_validated,
    _extract_json_object,
    _spec_from_seed_only,
    _trivial_spec,
    _validate_and_extract,
)


# ---------- JSON extraction --------------------------------------------------


def test_extract_clean_json():
    text = '{"a": 1, "b": [2, 3]}'
    assert _extract_json_object(text) == {"a": 1, "b": [2, 3]}


def test_extract_json_with_prose_wrapper():
    text = 'Here is the spec:\n{"verdict": "yes"}\nThat should work.'
    assert _extract_json_object(text) == {"verdict": "yes"}


def test_extract_json_with_code_fence():
    text = '```json\n{"x": 42}\n```'
    assert _extract_json_object(text) == {"x": 42}


def test_extract_json_with_bare_fence():
    text = '```\n{"x": 42}\n```'
    assert _extract_json_object(text) == {"x": 42}


def test_extract_invalid_returns_none():
    assert _extract_json_object("not json at all") is None
    assert _extract_json_object("") is None
    assert _extract_json_object(None) is None  # type: ignore[arg-type]


def test_extract_truncated_recovers_balanced_prefix():
    """If the LLM emitted trailing garbage, extract the balanced {...} prefix."""
    text = '{"a": 1}\nextra garbage } that breaks json'
    obj = _extract_json_object(text)
    assert obj == {"a": 1}


# ---------- schema validation -----------------------------------------------


def test_validate_accepts_minimal_valid_output():
    payload = {
        "pre_validity": [{"clause": "!null(p)", "evidence": ["caller_site_1"]}],
        "pre_protocol": [],
        "postcondition": [],
        "loop_invariants": [],
        "spec_disagreement": False,
        "uncertainty_notes": "",
    }
    out = _validate_and_extract(payload, "fn")
    assert out is not None
    pv, pp, post, loops, disagreement, notes = out
    assert pv == [{"clause": "!null(p)", "evidence": ["caller_site_1"]}]
    assert pp == []
    assert post == []
    assert loops == []
    assert disagreement is False
    assert notes == ""


def test_validate_rejects_untagged_clause():
    """Rule 1: every clause needs ≥1 evidence tag."""
    bad = {
        "pre_validity": [{"clause": "!null(p)", "evidence": []}],
        "pre_protocol": [],
        "postcondition": [],
    }
    assert _validate_and_extract(bad, "fn") is None


def test_validate_rejects_missing_evidence_key():
    bad = {
        "pre_validity": [{"clause": "!null(p)"}],
        "pre_protocol": [],
        "postcondition": [],
    }
    assert _validate_and_extract(bad, "fn") is None


def test_validate_rejects_empty_clause():
    bad = {
        "pre_validity": [{"clause": "  ", "evidence": ["body:L1"]}],
        "pre_protocol": [],
        "postcondition": [],
    }
    assert _validate_and_extract(bad, "fn") is None


def test_validate_rejects_non_list_evidence():
    bad = {
        "pre_validity": [{"clause": "!null(p)", "evidence": "caller_site_1"}],
        "pre_protocol": [],
        "postcondition": [],
    }
    assert _validate_and_extract(bad, "fn") is None


def test_validate_rejects_non_string_tag():
    bad = {
        "pre_validity": [{"clause": "!null(p)", "evidence": [42]}],
        "pre_protocol": [],
        "postcondition": [],
    }
    assert _validate_and_extract(bad, "fn") is None


def test_validate_loop_invariants_defaults_to_empty_on_bad_type():
    payload = {
        "pre_validity": [],
        "pre_protocol": [],
        "postcondition": [],
        "loop_invariants": "not a list",
    }
    out = _validate_and_extract(payload, "fn")
    assert out is not None
    assert out[3] == []  # loops field defaulted


def test_validate_rejects_non_object_input():
    assert _validate_and_extract("hello", "fn") is None  # type: ignore[arg-type]
    assert _validate_and_extract([1, 2, 3], "fn") is None  # type: ignore[arg-type]


# ---------- spec assembly ---------------------------------------------------


def test_build_spec_from_validated_combines_clauses():
    pv = [{"clause": "!null(p)", "evidence": ["caller_site_1"]}]
    pp = [{"clause": "locked(&mu)", "evidence": ["header_comment"]}]
    post = [{"clause": "result >= 0", "evidence": ["body:L42"]}]
    spec = _build_spec_from_validated("fn", pv, pp, post, [], False)
    assert spec.function_name == "fn"
    assert spec.pre_validity == "!null(p)"
    assert spec.pre_protocol == "locked(&mu)"
    assert spec.precondition == "!null(p) && locked(&mu)"
    assert spec.postcondition == "result >= 0"
    assert spec.evidence == {
        "!null(p)": ["caller_site_1"],
        "locked(&mu)": ["header_comment"],
        "result >= 0": ["body:L42"],
    }
    assert spec.status == SpecStatus.GENERATED


def test_build_spec_empty_clauses_yield_trivial_strings():
    spec = _build_spec_from_validated("fn", [], [], [], [], False)
    assert spec.precondition == "true"
    assert spec.postcondition == "true"
    assert spec.pre_validity == ""
    assert spec.pre_protocol == ""
    assert spec.evidence == {}


def test_build_spec_disagreement_propagates():
    spec = _build_spec_from_validated("fn", [], [], [], [], True)
    assert spec.spec_disagreement is True


# ---------- fallback specs ---------------------------------------------------


def test_trivial_spec_external_boundary():
    spec = _trivial_spec("fn", "external_boundary")
    assert spec.precondition == "true"
    assert spec.postcondition == "true"
    assert spec.evidence == {"true": ["external_boundary"]}
    assert spec.status == SpecStatus.GENERATED


def test_trivial_spec_failed_parse_marks_failed():
    spec = _trivial_spec("fn", "failed_parse")
    assert spec.status == SpecStatus.FAILED


def test_seed_only_spec_includes_pattern_evidence():
    from bmc_agent.spec_evidence import SeedClause
    seeds = [
        SeedClause(clause="start <= end", pattern_name="paired_pointers"),
        SeedClause(clause="len <= 4", pattern_name="length_bound"),
    ]
    spec = _spec_from_seed_only("fn", seeds, "test")
    assert spec.pre_validity == "start <= end && len <= 4"
    assert spec.postcondition == "true"
    assert spec.evidence == {
        "start <= end": ["signature_pattern"],
        "len <= 4": ["signature_pattern"],
    }
    assert spec.status == SpecStatus.FAILED


def test_seed_only_spec_no_seeds_emits_trivial_pre():
    spec = _spec_from_seed_only("fn", [], "test")
    assert spec.precondition == "true"


# ---------- topological layering --------------------------------------------


def test_layers_leaves_first():
    g = {"a": {"b", "c"}, "b": {"c"}, "c": set(), "d": {"a"}}
    layers = _build_bottom_up_layers(g)
    # c has no callees → layer 0; b depends on c → layer 1; a depends on b,c → layer 2; d depends on a → layer 3
    assert layers[0] == ["c"]
    assert layers[1] == ["b"]
    assert layers[2] == ["a"]
    assert layers[3] == ["d"]


def test_layers_handles_disconnected_nodes():
    g = {"a": set(), "b": set(), "c": set()}
    layers = _build_bottom_up_layers(g)
    assert len(layers) == 1
    assert set(layers[0]) == {"a", "b", "c"}


def test_layers_handles_cycles():
    g = {"a": {"b"}, "b": {"a"}, "c": set()}
    layers = _build_bottom_up_layers(g)
    # c is a leaf; a-b cycle goes in the cycle-breaking final layer.
    assert layers[0] == ["c"]
    assert set(layers[-1]) == {"a", "b"}


def test_layers_empty_graph():
    assert _build_bottom_up_layers({}) == []


# ---------- SpecGeneratorV2 with mocked LLM ---------------------------------


def _minimal_parsed_file(fn_name: str, sig):
    """Build a minimal ParsedCFile-like object for orchestrator tests.

    Has just enough attributes that gather_evidence_bundle + parse_doc_annotations
    don't crash on MagicMock comparisons.
    """
    class _P:
        path = "/tmp/_test_synthetic.c"
        functions = {fn_name: sig}
        function_definitions = {}        # empty → parse_doc_annotations short-circuits
        function_bodies = {fn_name: ""}
        call_graph = {fn_name: set()}
        struct_definitions = {}
        preprocessed_source = None
    return _P()


def _mock_pipeline_env(tmp_path):
    """Build a minimal Config + LLM + ArtifactStore for orchestrator tests."""
    from bmc_agent.config import Config

    cfg = Config(artifact_dir=str(tmp_path / "artifacts"))
    cfg.cbmc_unwind = 4

    llm = MagicMock()
    store = MagicMock()
    store.init_driver = MagicMock()
    store.save_spec = MagicMock()
    return cfg, llm, store


def test_orchestrator_canonical_short_circuit(tmp_path, monkeypatch):
    """A function in universal_stub_contracts skips the LLM entirely."""
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    gen = SpecGeneratorV2(cfg, llm, store)

    # Patch canonical_signature to return a non-None for our test fn.
    from bmc_agent import universal_stub_contracts as usc
    monkeypatch.setattr(
        usc, "canonical_signature",
        lambda name: ("int", [("char *", "p"), ("size_t", "n")]) if name == "memcpy_like" else None,
    )

    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="memcpy_like",
        signature=FunctionSignature(
            name="memcpy_like", return_type="int",
            parameters=[("char *", "p"), ("size_t", "n")],
        ),
        body="int memcpy_like(char *p, size_t n) { return 0; }",
        callees=set(),
        source_file="",
    )
    spec = gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("memcpy_like", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    # Canonical-contract evidence tag; LLM never called.
    assert spec.evidence == {"true": ["canonical_contract"]}
    assert llm.complete.call_count == 0


def test_orchestrator_boundary_short_circuit(tmp_path):
    """Boundary functions get trivial specs without invoking the LLM."""
    from bmc_agent.boundary_detector import BoundaryDetector

    cfg, llm, store = _mock_pipeline_env(tmp_path)
    bd = BoundaryDetector(public_names=frozenset({"public_api"}))
    gen = SpecGeneratorV2(cfg, llm, store, boundary_detector=bd)

    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="public_api",
        signature=FunctionSignature(
            name="public_api", return_type="int", parameters=[("int", "x")],
        ),
        body="int public_api(int x) { return x; }",
        callees=set(),
        source_file="",
    )
    spec = gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("public_api", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    assert spec.evidence == {"true": ["external_boundary"]}
    assert llm.complete.call_count == 0


def test_orchestrator_happy_path_with_valid_llm_response(tmp_path):
    """End-to-end happy path: LLM returns valid JSON → spec is well-formed."""
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    llm.complete.return_value = """
    {
      "pre_validity": [
        {"clause": "!null(p)", "evidence": ["caller_site_1", "body:L3"]}
      ],
      "pre_protocol": [],
      "postcondition": [
        {"clause": "result >= 0", "evidence": ["body:L8"]}
      ],
      "loop_invariants": [],
      "spec_disagreement": false,
      "uncertainty_notes": ""
    }
    """
    gen = SpecGeneratorV2(cfg, llm, store)

    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="internal_fn",
        signature=FunctionSignature(
            name="internal_fn", return_type="int", parameters=[("int *", "p")],
        ),
        body="int internal_fn(int *p) { if (!p) return -1; return *p; }",
        callees=set(),
        source_file="",
    )
    spec = gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("internal_fn", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    assert spec.pre_validity == "!null(p)"
    assert spec.postcondition == "result >= 0"
    assert spec.evidence["!null(p)"] == ["caller_site_1", "body:L3"]
    assert spec.status == SpecStatus.GENERATED
    assert llm.complete.call_count == 1


def test_orchestrator_falls_back_to_seed_on_parse_failure(tmp_path):
    """LLM returns garbage twice → fall back to seed-only spec."""
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    llm.complete.return_value = "totally not json"
    gen = SpecGeneratorV2(cfg, llm, store)

    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="scan",
        signature=FunctionSignature(
            name="scan", return_type="int",
            parameters=[("const char *", "start"), ("const char *", "end")],
        ),
        body="int scan(const char *start, const char *end) { return 0; }",
        callees=set(),
        source_file="",
    )
    spec = gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("scan", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    # Should fall back to seed-only (paired-pointer pattern fires for start/end).
    assert spec.status == SpecStatus.FAILED
    assert "start" in spec.pre_validity and "end" in spec.pre_validity
    # Seed clauses carry signature_pattern evidence.
    assert any(tags == ["signature_pattern"] for tags in spec.evidence.values())


def test_orchestrator_falls_back_to_seedless_when_no_seeds(tmp_path):
    """LLM fails AND no universal-pattern matches → trivial pre, FAILED status."""
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    llm.complete.return_value = "garbage"
    gen = SpecGeneratorV2(cfg, llm, store)

    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="trivial",
        signature=FunctionSignature(
            name="trivial", return_type="int", parameters=[("int", "x")],
        ),
        body="int trivial(int x) { return x; }",
        callees=set(),
        source_file="",
    )
    spec = gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("trivial", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    assert spec.precondition == "true"
    assert spec.status == SpecStatus.FAILED


# ---------- v2.2 tool-use trigger gate -------------------------------------


def _mock_tu_result(text, error=""):
    """Build a ToolUseResult mock matching the dataclass shape."""
    from bmc_agent.llm import ToolUseResult
    return ToolUseResult(
        text=text, iterations=2, tool_calls_made=1,
        messages=[], error=error,
    )


def test_v22_does_not_fire_when_disabled(tmp_path):
    """enable_spec_gen_tools=False → tool-use branch is skipped."""
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    cfg.enable_spec_gen_tools = False
    # Base v2 returns a spec with spec_disagreement=True — would trigger
    # if the flag were on.
    llm.complete.return_value = (
        '{"pre_validity": [{"clause": "!null(p)", "evidence": ["body:L1"]}],'
        ' "pre_protocol": [], "postcondition": [],'
        ' "loop_invariants": [], "spec_disagreement": true,'
        ' "uncertainty_notes": "body and callers disagree"}'
    )
    gen = SpecGeneratorV2(cfg, llm, store)
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="fn",
        signature=FunctionSignature(name="fn", return_type="int",
                                    parameters=[("int *", "p")]),
        body="int fn(int *p) { return *p; }",
        callees=set(), source_file="",
    )
    gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("fn", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    # complete_with_tools must NOT have been called.
    assert not hasattr(llm.complete_with_tools, "call_count") or \
        llm.complete_with_tools.call_count == 0


def test_v22_fires_on_spec_disagreement(tmp_path):
    """enable_spec_gen_tools=True + spec_disagreement=True → tool-use fires."""
    from unittest.mock import MagicMock
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    cfg.enable_spec_gen_tools = True
    # Base v2 flags disagreement.
    llm.complete.return_value = (
        '{"pre_validity": [{"clause": "!null(p)", "evidence": ["body:L1"]}],'
        ' "pre_protocol": [], "postcondition": [],'
        ' "loop_invariants": [], "spec_disagreement": true,'
        ' "uncertainty_notes": "ambiguous"}'
    )
    # complete_with_tools returns a refined spec.
    llm.complete_with_tools = MagicMock(return_value=_mock_tu_result(
        '{"pre_validity": ['
        '{"clause": "!null(p)", "evidence": ["body:L1"]},'
        '{"clause": "!null(p->next)", "evidence": ["body:L3"]}'
        '],'
        ' "pre_protocol": [], "postcondition": [],'
        ' "loop_invariants": [], "spec_disagreement": false,'
        ' "uncertainty_notes": ""}'
    ))
    gen = SpecGeneratorV2(cfg, llm, store)
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="fn",
        signature=FunctionSignature(name="fn", return_type="int",
                                    parameters=[("struct foo *", "p")]),
        body="int fn(struct foo *p) { return p->next->val; }",
        callees=set(), source_file="",
    )
    spec = gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("fn", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    assert llm.complete_with_tools.call_count == 1
    # Refined spec has the additional field-level clause.
    assert "!null(p->next)" in spec.pre_validity


def test_v22_fires_on_no_caller_evidence(tmp_path):
    """Empty callers + empty address_taken_sites → tool-use fires."""
    from unittest.mock import MagicMock
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    cfg.enable_spec_gen_tools = True
    llm.complete.return_value = (
        '{"pre_validity": [{"clause": "!null(p)", "evidence": ["body:L1"]}],'
        ' "pre_protocol": [], "postcondition": [],'
        ' "loop_invariants": [], "spec_disagreement": false,'
        ' "uncertainty_notes": "no caller evidence"}'
    )
    llm.complete_with_tools = MagicMock(return_value=_mock_tu_result(
        '{"pre_validity": [{"clause": "!null(p)", "evidence": ["body:L1"]}],'
        ' "pre_protocol": [], "postcondition": [],'
        ' "loop_invariants": [], "spec_disagreement": false,'
        ' "uncertainty_notes": ""}'
    ))
    gen = SpecGeneratorV2(cfg, llm, store)
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="orphan",
        signature=FunctionSignature(name="orphan", return_type="int",
                                    parameters=[("int *", "p")]),
        body="int orphan(int *p) { return *p; }",
        callees=set(), source_file="",
    )
    gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("orphan", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],  # empty corpus → no callers → no address-taken
    )
    assert llm.complete_with_tools.call_count == 1


def test_v22_falls_back_to_base_when_tool_use_fails(tmp_path):
    """Tool-use call raises → caller returns the base v2 spec."""
    from unittest.mock import MagicMock
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    cfg.enable_spec_gen_tools = True
    llm.complete.return_value = (
        '{"pre_validity": [{"clause": "!null(p)", "evidence": ["body:L1"]}],'
        ' "pre_protocol": [], "postcondition": [],'
        ' "loop_invariants": [], "spec_disagreement": true,'
        ' "uncertainty_notes": ""}'
    )
    llm.complete_with_tools = MagicMock(side_effect=RuntimeError("tool-use crashed"))
    gen = SpecGeneratorV2(cfg, llm, store)
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="fn",
        signature=FunctionSignature(name="fn", return_type="int",
                                    parameters=[("int *", "p")]),
        body="int fn(int *p) { return *p; }",
        callees=set(), source_file="",
    )
    spec = gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("fn", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    # We got the base v2 spec back (single !null(p) clause).
    assert spec.pre_validity == "!null(p)"


def test_v22_falls_back_to_base_when_tool_use_returns_error(tmp_path):
    """ToolUseResult with .error set → caller returns the base spec."""
    from unittest.mock import MagicMock
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    cfg.enable_spec_gen_tools = True
    llm.complete.return_value = (
        '{"pre_validity": [{"clause": "!null(p)", "evidence": ["body:L1"]}],'
        ' "pre_protocol": [], "postcondition": [],'
        ' "loop_invariants": [], "spec_disagreement": true,'
        ' "uncertainty_notes": ""}'
    )
    llm.complete_with_tools = MagicMock(return_value=_mock_tu_result(
        text="", error="max_iterations exceeded",
    ))
    gen = SpecGeneratorV2(cfg, llm, store)
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    fi = FunctionInfo(
        name="fn",
        signature=FunctionSignature(name="fn", return_type="int",
                                    parameters=[("int *", "p")]),
        body="int fn(int *p) { return *p; }",
        callees=set(), source_file="",
    )
    spec = gen._generate_one(
        func_info=fi,
        parsed=_minimal_parsed_file("fn", fi.signature),
        all_specs_so_far={},
        corpus_paths=[],
    )
    assert spec.pre_validity == "!null(p)"


def test_v22_does_not_fire_when_base_spec_is_clean(tmp_path):
    """No disagreement + callers present → tool-use SKIPPED (cost saver)."""
    from unittest.mock import MagicMock
    cfg, llm, store = _mock_pipeline_env(tmp_path)
    cfg.enable_spec_gen_tools = True
    llm.complete.return_value = (
        '{"pre_validity": [{"clause": "!null(p)", "evidence": ["caller_site_1"]}],'
        ' "pre_protocol": [], "postcondition": [],'
        ' "loop_invariants": [], "spec_disagreement": false,'
        ' "uncertainty_notes": ""}'
    )
    llm.complete_with_tools = MagicMock()  # would crash test if called
    gen = SpecGeneratorV2(cfg, llm, store)
    # Build a parsed file where the function has a real caller (so
    # bundle.callers is non-empty), avoiding the no-caller-evidence trigger.
    from bmc_agent.parser import FunctionInfo, FunctionSignature
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".c",
                                      delete=False) as tmp:
        tmp.write("int caller(int *p) { return target(p); }\n"
                  "int target(int *p) { return *p; }\n")
        tmp_path_c = tmp.name
    fi = FunctionInfo(
        name="target",
        signature=FunctionSignature(name="target", return_type="int",
                                    parameters=[("int *", "p")]),
        body="int target(int *p) { return *p; }",
        callees=set(), source_file=tmp_path_c,
    )
    parsed = _minimal_parsed_file("target", fi.signature)
    parsed.path = tmp_path_c
    gen._generate_one(
        func_info=fi,
        parsed=parsed,
        all_specs_so_far={},
        corpus_paths=[Path(tmp_path_c)],
    )
    # No disagreement, has callers → tool-use must NOT fire.
    assert llm.complete_with_tools.call_count == 0
