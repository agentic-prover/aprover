"""
Phase 1 acceptance tests for BMC-Agent Spec Generator.

Tests:
1. Load simple_driver.c, build generation order, verify layer structure.
2. Mock LLM — no real API calls.
3. Run full spec generator with mocked LLM.
4. Assert all functions have specs with non-empty pre/postconditions.
5. Assert callee specs are populated.
6. Test SCC detection on a cycle-containing call graph.
7. Test spec merging (disjunction/conjunction).
8. All tests pass without ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_C = REPO_ROOT / "examples" / "simple_driver.c"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_SPEC_JSON = json.dumps({
    "precondition": "valid(ptr) && ptr != NULL",
    "postcondition": "\\result >= 0",
    "reasoning": "Mock reasoning for test.",
})


def make_mock_llm(response: str = MOCK_SPEC_JSON) -> MagicMock:
    """Create a mock LLMClient whose complete() always returns *response*."""
    mock = MagicMock()
    mock.complete.return_value = response
    return mock


# ---------------------------------------------------------------------------
# 1. Generation order / layer structure
# ---------------------------------------------------------------------------

def test_build_generation_order_simple_driver():
    """
    Verify that the generation order has sensible layers for simple_driver.c.

    dev_open/dev_write/dev_read/dev_close are entry points (no callers).
    rb_init, rb_is_full, rb_is_empty, rb_write, rb_read are internals called
    by the entry points. So layer 1 = entry points, layer 2 = internals.
    """
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec_generator import _build_generation_order

    parsed = parse_c_file(EXAMPLE_C)
    defined = set(parsed.functions.keys())

    # Filter call graph to only defined functions
    filtered_cg: dict[str, set[str]] = {
        fn: parsed.call_graph[fn] & defined
        for fn in defined
    }

    layers = _build_generation_order(filtered_cg)

    print("\nGeneration layers:")
    for i, layer in enumerate(layers, 1):
        print(f"  Layer {i}: {sorted(layer)}")

    assert layers, "Expected at least one layer"
    assert all(isinstance(layer, list) for layer in layers)

    # All defined functions should appear in exactly one layer
    all_in_layers = [fn for layer in layers for fn in layer]
    assert set(all_in_layers) == defined, (
        f"Missing from layers: {defined - set(all_in_layers)}\n"
        f"Extra in layers:   {set(all_in_layers) - defined}"
    )

    # Build a set of callee names for verification
    # Functions that are called by others should appear in later layers
    # than their callers (in general)
    callee_set: set[str] = set()
    for callees in filtered_cg.values():
        callee_set.update(callees)
    callers_only = defined - callee_set  # functions nobody calls

    layer_of: dict[str, int] = {}
    for i, layer in enumerate(layers):
        for fn in layer:
            layer_of[fn] = i

    # Functions that nobody calls should appear in the first layer(s)
    # (they are entry functions in the condensed DAG)
    # This is a structural check; exact layer membership depends on the call graph
    if callers_only:
        min_caller_layer = min(layer_of[fn] for fn in callers_only if fn in layer_of)
        for callee in callee_set & defined:
            # A callee's layer should be >= the layer of at least one of its callers
            callee_layer = layer_of.get(callee, 0)
            # Find any caller for this callee
            for caller, callees in filtered_cg.items():
                if callee in callees:
                    caller_layer = layer_of.get(caller, 0)
                    assert callee_layer >= caller_layer, (
                        f"Callee '{callee}' (layer {callee_layer}) should be >= "
                        f"caller '{caller}' (layer {caller_layer})"
                    )
                    break


def test_build_generation_order_empty():
    """Empty call graph returns empty layers."""
    from bmc_agent.spec_generator import _build_generation_order

    layers = _build_generation_order({})
    assert layers == []


def test_build_generation_order_single_node():
    """Single function with no calls returns one layer."""
    from bmc_agent.spec_generator import _build_generation_order

    layers = _build_generation_order({"foo": set()})
    assert len(layers) == 1
    assert layers[0] == ["foo"]


# ---------------------------------------------------------------------------
# 2. SCC detection with cycles
# ---------------------------------------------------------------------------

def test_scc_detects_cycle():
    """Kosaraju's algorithm must group cyclic functions into one SCC."""
    from bmc_agent.spec_generator import _kosaraju_sccs

    # A -> B -> C -> A  (a cycle)
    graph = {
        "A": {"B"},
        "B": {"C"},
        "C": {"A"},
        "D": {"A"},  # D calls into the cycle
    }
    sccs = _kosaraju_sccs(graph)

    # Find the SCC containing A, B, C
    found_cycle_scc = False
    for scc in sccs:
        if "A" in scc and "B" in scc and "C" in scc:
            found_cycle_scc = True
            break
    assert found_cycle_scc, f"Expected A,B,C in same SCC. Got SCCs: {sccs}"


def test_scc_no_cycle():
    """DAG should yield singleton SCCs."""
    from bmc_agent.spec_generator import _kosaraju_sccs

    graph = {
        "A": {"B", "C"},
        "B": {"D"},
        "C": {"D"},
        "D": set(),
    }
    sccs = _kosaraju_sccs(graph)
    # Each node should be its own SCC
    all_nodes = {n for scc in sccs for n in scc}
    assert all_nodes >= {"A", "B", "C", "D"}
    for scc in sccs:
        if len(scc) > 1:
            # Multi-node SCCs should only contain nodes that form a real cycle
            pytest.fail(f"Unexpected multi-node SCC in a DAG: {scc}")


def test_generation_order_with_cycle():
    """
    Functions in the same SCC (cycle) should appear in the same layer.
    """
    from bmc_agent.spec_generator import _build_generation_order

    # A <-> B (mutual recursion), both called by C, C called by nobody
    graph = {
        "C": {"A"},
        "A": {"B"},
        "B": {"A"},  # cycle
    }
    layers = _build_generation_order(graph)
    print(f"\nLayers for cycle graph: {layers}")

    # C should be in an earlier layer than A and B
    layer_of: dict[str, int] = {}
    for i, layer in enumerate(layers):
        for fn in layer:
            layer_of[fn] = i

    assert "C" in layer_of
    assert "A" in layer_of
    assert "B" in layer_of
    assert layer_of["C"] <= layer_of["A"], "C should be in same or earlier layer than A"
    assert layer_of["A"] == layer_of["B"], "A and B (cycle) should be in the same layer"


# ---------------------------------------------------------------------------
# 3. Spec merging
# ---------------------------------------------------------------------------

def test_merge_specs_disjunction_conjunction():
    """merge_specs: preconditions are OR'd, postconditions are AND'd."""
    from bmc_agent.spec import Spec, merge_specs

    s1 = Spec(
        function_name="callee",
        precondition="x > 0",
        postcondition="result >= 0",
    )
    s2 = Spec(
        function_name="callee",
        precondition="x == 0",
        postcondition="result == 0",
    )
    merged = merge_specs([s1, s2])

    assert "x > 0" in merged.precondition
    assert "x == 0" in merged.precondition
    assert "OR" in merged.precondition.upper()

    assert "result >= 0" in merged.postcondition
    assert "result == 0" in merged.postcondition
    assert "AND" in merged.postcondition.upper()


def test_merge_specs_single():
    """merge_specs of one spec returns the spec unchanged."""
    from bmc_agent.spec import Spec, merge_specs

    s = Spec(function_name="f", precondition="x > 0", postcondition="y > 0")
    assert merge_specs([s]) is s


def test_merge_specs_empty_raises():
    from bmc_agent.spec import merge_specs

    with pytest.raises(ValueError):
        merge_specs([])


def test_merge_specs_three_callers():
    """Merging three caller specs should produce correct OR/AND combination."""
    from bmc_agent.spec import Spec, merge_specs

    specs = [
        Spec(function_name="f", precondition="a > 0", postcondition="p"),
        Spec(function_name="f", precondition="b > 0", postcondition="q"),
        Spec(function_name="f", precondition="c > 0", postcondition="r"),
    ]
    merged = merge_specs(specs)

    assert merged.precondition.count("OR") == 2, (
        f"Expected 2 ORs for 3-way merge, got: {merged.precondition}"
    )
    assert merged.postcondition.count("AND") == 2, (
        f"Expected 2 ANDs for 3-way merge, got: {merged.postcondition}"
    )


# ---------------------------------------------------------------------------
# 4. Full spec generator with mocked LLM
# ---------------------------------------------------------------------------

def test_spec_generator_full_run(tmp_path: Path):
    """
    Full spec generation run on simple_driver.c with mocked LLM.
    Verifies all functions get non-trivial specs.
    """
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator

    config = Config(
        llm_model="mock-model",
        llm_api_key="mock-key",
        artifact_dir=str(tmp_path / "artifacts"),
        max_spec_retries=1,
        batch_size=4,
    )
    store = ArtifactStore(config.artifact_dir)
    mock_llm = make_mock_llm(MOCK_SPEC_JSON)

    generator = SpecGenerator(config, mock_llm, store)
    specs = generator.generate_specs(
        source_file=str(EXAMPLE_C),
        driver_name="simple_driver",
        domain_knowledge="Ring buffer implementation for a character device.",
    )

    expected_functions = {
        "rb_init", "rb_write", "rb_read",
        "rb_is_full", "rb_is_empty",
        "dev_open", "dev_close", "dev_write", "dev_read",
    }

    print(f"\nGenerated specs for: {sorted(specs.keys())}")

    # All expected functions should have specs
    for fn in expected_functions:
        assert fn in specs, f"Missing spec for '{fn}'"

    # All specs should have non-empty preconditions and postconditions
    for fn, spec in specs.items():
        assert spec.precondition.strip(), f"Empty precondition for '{fn}'"
        assert spec.postcondition.strip(), f"Empty postcondition for '{fn}'"
        assert spec.function_name == fn, (
            f"Spec function_name mismatch: expected '{fn}', got '{spec.function_name}'"
        )

    # Verify LLM was called (not zero times)
    assert mock_llm.complete.call_count > 0, "LLM was never called"


def test_spec_generator_callee_specs_populated(tmp_path: Path):
    """Callee specs should be attached to the caller's spec."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator
    from bmc_agent.parser import parse_c_file

    config = Config(
        llm_model="mock-model",
        llm_api_key="mock-key",
        artifact_dir=str(tmp_path / "artifacts"),
        max_spec_retries=1,
        batch_size=4,
    )
    store = ArtifactStore(config.artifact_dir)
    mock_llm = make_mock_llm(MOCK_SPEC_JSON)

    generator = SpecGenerator(config, mock_llm, store)
    specs = generator.generate_specs(
        source_file=str(EXAMPLE_C),
        driver_name="simple_driver",
    )

    # Check that callee_specs is populated for functions that have callees
    parsed = parse_c_file(str(EXAMPLE_C))
    defined = set(parsed.functions.keys())

    for fn_name, spec in specs.items():
        raw_callees = parsed.call_graph.get(fn_name, set())
        defined_callees = raw_callees & defined
        for callee in defined_callees:
            assert callee in spec.callee_specs, (
                f"Expected callee spec for '{callee}' in spec of '{fn_name}'"
            )


def test_spec_generator_specs_saved_to_disk(tmp_path: Path):
    """Specs must be persisted to the artifact store."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator

    config = Config(
        llm_model="mock-model",
        llm_api_key="mock-key",
        artifact_dir=str(tmp_path / "artifacts"),
        max_spec_retries=1,
    )
    store = ArtifactStore(config.artifact_dir)
    mock_llm = make_mock_llm(MOCK_SPEC_JSON)

    generator = SpecGenerator(config, mock_llm, store)
    specs = generator.generate_specs(
        source_file=str(EXAMPLE_C),
        driver_name="simple_driver",
    )

    for fn_name in specs:
        loaded = store.load_spec("simple_driver", fn_name)
        assert loaded is not None, f"Spec for '{fn_name}' not found on disk"
        assert loaded.function_name == fn_name


# ---------------------------------------------------------------------------
# 5. Fallback behavior
# ---------------------------------------------------------------------------

def test_spec_generator_llm_failure_fallback(tmp_path: Path):
    """When LLM raises LLMError, generator should fall back to weak spec."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMError
    from bmc_agent.spec_generator import SpecGenerator

    config = Config(
        llm_model="mock-model",
        llm_api_key="mock-key",
        artifact_dir=str(tmp_path / "artifacts"),
        max_spec_retries=1,
    )
    store = ArtifactStore(config.artifact_dir)

    mock_llm = MagicMock()
    mock_llm.complete.side_effect = LLMError("Simulated LLM failure")

    generator = SpecGenerator(config, mock_llm, store)
    specs = generator.generate_specs(
        source_file=str(EXAMPLE_C),
        driver_name="simple_driver",
    )

    # Every function should still have a spec (fallback)
    assert len(specs) > 0
    for fn_name, spec in specs.items():
        # Fallback specs use "true" for both pre and post
        assert spec.precondition == "true", (
            f"Expected fallback precondition 'true' for '{fn_name}', got: {spec.precondition!r}"
        )
        assert spec.postcondition == "true", (
            f"Expected fallback postcondition 'true' for '{fn_name}', got: {spec.postcondition!r}"
        )


def test_spec_generator_unparseable_response_fallback(tmp_path: Path):
    """When LLM returns garbage JSON, generator should fall back gracefully."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator

    config = Config(
        llm_model="mock-model",
        llm_api_key="mock-key",
        artifact_dir=str(tmp_path / "artifacts"),
        max_spec_retries=1,
    )
    store = ArtifactStore(config.artifact_dir)
    mock_llm = make_mock_llm("this is not JSON at all!!!")

    generator = SpecGenerator(config, mock_llm, store)
    specs = generator.generate_specs(
        source_file=str(EXAMPLE_C),
        driver_name="simple_driver",
    )

    # All functions should still have specs (either fallback or merged)
    assert len(specs) > 0
    for fn_name, spec in specs.items():
        assert spec.precondition.strip(), f"Empty precondition for '{fn_name}'"
        assert spec.postcondition.strip(), f"Empty postcondition for '{fn_name}'"


# ---------------------------------------------------------------------------
# 6. No real API calls
# ---------------------------------------------------------------------------

def test_no_api_key_needed(tmp_path: Path):
    """Spec generation with mocked LLM must not require ANTHROPIC_API_KEY."""
    import os
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator

    # Ensure no API key is set
    env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        config = Config(
            llm_model="mock-model",
            llm_api_key="",  # no key
            artifact_dir=str(tmp_path / "artifacts"),
            max_spec_retries=1,
        )
        store = ArtifactStore(config.artifact_dir)
        mock_llm = make_mock_llm(MOCK_SPEC_JSON)

        generator = SpecGenerator(config, mock_llm, store)
        specs = generator.generate_specs(
            source_file=str(EXAMPLE_C),
            driver_name="simple_driver",
        )

        assert len(specs) > 0, "Expected specs even without API key (using mock)"
    finally:
        if env_backup is not None:
            os.environ["ANTHROPIC_API_KEY"] = env_backup


# ---------------------------------------------------------------------------
# 7. FunctionInfo dataclass
# ---------------------------------------------------------------------------

def test_function_info_from_parsed():
    """ParsedCFile.get_function_info should return a correct FunctionInfo."""
    from bmc_agent.parser import parse_c_file, FunctionInfo

    parsed = parse_c_file(EXAMPLE_C)
    info = parsed.get_function_info("rb_write")

    assert info is not None
    assert isinstance(info, FunctionInfo)
    assert info.name == "rb_write"
    assert info.body.strip(), "Expected non-empty body"
    assert info.source_file == str(EXAMPLE_C)
    assert isinstance(info.callees, set)


def test_all_function_infos():
    """ParsedCFile.all_function_infos should return all functions."""
    from bmc_agent.parser import parse_c_file, FunctionInfo

    parsed = parse_c_file(EXAMPLE_C)
    infos = parsed.all_function_infos()

    assert len(infos) == len(parsed.functions)
    for info in infos:
        assert isinstance(info, FunctionInfo)
        assert info.name in parsed.functions


# ---------------------------------------------------------------------------
# 8. Prompts module
# ---------------------------------------------------------------------------

def test_prompts_module_constants():
    """All prompt template constants should exist and contain key substrings."""
    from bmc_agent import prompts

    assert hasattr(prompts, "DSL_GRAMMAR")
    assert hasattr(prompts, "ENTRY_SPEC_PROMPT")
    assert hasattr(prompts, "INTERNAL_SPEC_PROMPT")
    assert hasattr(prompts, "EXPECTED_SPEC_PROMPT")

    assert "precondition" in prompts.DSL_GRAMMAR.lower()
    assert "{signature}" in prompts.ENTRY_SPEC_PROMPT
    assert "{body}" in prompts.ENTRY_SPEC_PROMPT
    assert "{expected_specs}" in prompts.INTERNAL_SPEC_PROMPT
    assert "{callee_name}" in prompts.EXPECTED_SPEC_PROMPT


# ---------------------------------------------------------------------------
# 9. CLI smoke test
# ---------------------------------------------------------------------------

def test_cli_help():
    """CLI should print help without error."""
    from bmc_agent.cli import build_parser

    parser = build_parser()
    # Should not raise
    assert parser is not None


def test_cli_generate_with_mock(tmp_path: Path):
    """CLI generate command should work end-to-end with a mocked LLM."""
    from unittest.mock import patch
    from bmc_agent.cli import main

    with patch("bmc_agent.spec_generator.SpecGenerator.generate_specs") as mock_gen:
        from bmc_agent.spec import Spec, SpecStatus
        mock_gen.return_value = {
            "rb_write": Spec(
                function_name="rb_write",
                precondition="valid(rb)",
                postcondition="\\result <= len",
                status=SpecStatus.GENERATED,
            )
        }
        ret = main([
            "generate",
            "--source", str(EXAMPLE_C),
            "--driver", "test_driver",
            "--output", str(tmp_path / "artifacts"),
        ])
        assert ret == 0


# ---------------------------------------------------------------------------
# 10. Struct context extraction
# ---------------------------------------------------------------------------

_STRUCT_C = """\
typedef struct {
    int size;
    unsigned char *data;
} my_buf_t;

typedef struct {
    int count;
    int capacity;
} ring_t;

my_buf_t make_buf(int sz) {
    my_buf_t b;
    b.size = sz;
    return b;
}

void process(my_buf_t buf, int idx) {
    (void)buf;
    (void)idx;
}

void other(ring_t r) {
    (void)r;
}
"""


def test_extract_struct_names_identifies_struct_params(tmp_path: Path):
    """_extract_struct_names should return type names that are not basic C types."""
    from bmc_agent.parser import parse_c_file

    src = tmp_path / "struct_test.c"
    src.write_text(_STRUCT_C)

    parsed = parse_c_file(str(src))

    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.spec_generator import SpecGenerator
    from unittest.mock import MagicMock

    config = Config(llm_api_key="test")
    mock_llm = MagicMock()
    store = ArtifactStore(str(tmp_path / "artifacts"))
    gen = SpecGenerator(config, mock_llm, store)

    process_info = parsed.get_function_info("process")
    assert process_info is not None

    names = gen._extract_struct_names(process_info)
    assert "my_buf_t" in names, f"Expected my_buf_t in {names}"
    # int should NOT appear — it's a basic type
    assert "int" not in names


def test_extract_struct_context_includes_typedef(tmp_path: Path):
    """_extract_struct_context should find typedef struct definitions."""
    from bmc_agent.parser import parse_c_file

    src = tmp_path / "struct_test.c"
    src.write_text(_STRUCT_C)
    parsed = parse_c_file(str(src))
    # Simulate preprocessed_source being set
    parsed.preprocessed_source = _STRUCT_C

    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator
    from unittest.mock import MagicMock

    config = Config(llm_api_key="test")
    mock_llm = MagicMock()
    store = ArtifactStore(str(tmp_path / "artifacts"))
    gen = SpecGenerator(config, mock_llm, store)

    process_info = parsed.get_function_info("process")
    assert process_info is not None

    ctx = gen._extract_struct_context(process_info, parsed)
    assert "my_buf_t" in ctx, "Struct definition should appear in context"
    assert "size" in ctx or "data" in ctx, "Struct fields should appear in context"


def test_extract_struct_context_includes_constructor(tmp_path: Path):
    """_extract_struct_context should find factory functions returning the struct type."""
    from bmc_agent.parser import parse_c_file

    src = tmp_path / "struct_test.c"
    src.write_text(_STRUCT_C)
    parsed = parse_c_file(str(src))
    parsed.preprocessed_source = _STRUCT_C

    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator
    from unittest.mock import MagicMock

    config = Config(llm_api_key="test")
    mock_llm = MagicMock()
    store = ArtifactStore(str(tmp_path / "artifacts"))
    gen = SpecGenerator(config, mock_llm, store)

    process_info = parsed.get_function_info("process")
    assert process_info is not None

    ctx = gen._extract_struct_context(process_info, parsed)
    # make_buf returns my_buf_t — should appear as constructor
    assert "make_buf" in ctx, "Constructor function should appear in struct context"


def test_extract_struct_context_empty_for_basic_types(tmp_path: Path):
    """_extract_struct_context should return empty string when all params are basic types."""
    c_src = "int add(int a, int b) { return a + b; }\n"

    src = tmp_path / "basic.c"
    src.write_text(c_src)

    from bmc_agent.parser import parse_c_file

    parsed = parse_c_file(str(src))
    parsed.preprocessed_source = c_src

    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator
    from unittest.mock import MagicMock

    config = Config(llm_api_key="test")
    mock_llm = MagicMock()
    store = ArtifactStore(str(tmp_path / "artifacts"))
    gen = SpecGenerator(config, mock_llm, store)

    add_info = parsed.get_function_info("add")
    assert add_info is not None

    ctx = gen._extract_struct_context(add_info, parsed)
    assert ctx == "", f"Expected empty context for basic types, got: {ctx!r}"


def test_spec_system_prompt_contains_dsl_grammar():
    """SPEC_SYSTEM_PROMPT should embed the full DSL grammar for prompt caching."""
    from bmc_agent import prompts

    assert hasattr(prompts, "SPEC_SYSTEM_PROMPT")
    # DSL grammar content should be present in the system prompt
    assert "requires" in prompts.SPEC_SYSTEM_PROMPT
    assert "ensures" in prompts.SPEC_SYSTEM_PROMPT
    assert "valid(ptr)" in prompts.SPEC_SYSTEM_PROMPT
    # The system prompt should NOT contain prompt template placeholders
    assert "{dsl_grammar}" not in prompts.SPEC_SYSTEM_PROMPT


def test_safety_only_appends_postcondition_clause():
    """``spec_system_prompt_for(language='c', safety_only=True)`` returns
    the base prompt with the SAFETY_ONLY_POSTCOND_CLAUSE appended. The
    clause must forbid functional/algebraic postconditions and permit
    only safety/range/no-NaN claims."""
    from bmc_agent.prompts import (
        spec_system_prompt_for, SAFETY_ONLY_POSTCOND_CLAUSE,
        SPEC_SYSTEM_PROMPT,
    )

    out_off = spec_system_prompt_for("c", safety_only=False)
    out_on  = spec_system_prompt_for("c", safety_only=True)
    assert out_off == SPEC_SYSTEM_PROMPT
    assert out_on  == SPEC_SYSTEM_PROMPT + SAFETY_ONLY_POSTCOND_CLAUSE

    # The clause itself must enumerate the allowed safety predicates
    # and forbid functional/algebraic ones.
    assert "!isnan(result)" in SAFETY_ONLY_POSTCOND_CLAUSE
    assert "!isinf(result)" in SAFETY_ONLY_POSTCOND_CLAUSE
    assert "FORBIDDEN" in SAFETY_ONLY_POSTCOND_CLAUSE
    # Examples of disallowed shapes are explicitly called out.
    assert "compute_reference_value" in SAFETY_ONLY_POSTCOND_CLAUSE


def test_safety_only_combines_with_strict_dsl():
    """When BOTH strict_dsl and safety_only are set, both grammars
    apply: the strict-DSL base prompt plus the safety-only postcondition
    clause."""
    from bmc_agent.prompts import (
        spec_system_prompt_for, SAFETY_ONLY_POSTCOND_CLAUSE,
        STRICT_SPEC_SYSTEM_PROMPT,
    )

    out = spec_system_prompt_for("c", strict=True, safety_only=True)
    assert out == STRICT_SPEC_SYSTEM_PROMPT + SAFETY_ONLY_POSTCOND_CLAUSE


def test_safety_only_applies_to_rust_prompt_too():
    """Rust spec prompt is already strict-formal; safety-only still
    appends the postcondition clause on top."""
    from bmc_agent.prompts import (
        spec_system_prompt_for, SAFETY_ONLY_POSTCOND_CLAUSE,
        RUST_SPEC_SYSTEM_PROMPT,
    )

    out = spec_system_prompt_for("rust", safety_only=True)
    assert out == RUST_SPEC_SYSTEM_PROMPT + SAFETY_ONLY_POSTCOND_CLAUSE


def test_relax_postcondition_permits_null_when_body_returns_null():
    """Soften `result != NULL` when body has explicit `return NULL;`.

    Regression: this session's bounty runs surfaced many CEs where the
    LLM generated assertions like `result != NULL` while the function
    body had an explicit `return NULL;` error path. CBMC then flagged
    every error-path execution as a postcondition violation.
    """
    from bmc_agent.spec_generator import _relax_postcondition_for_error_paths

    body = "if (x == NULL) return NULL;\nreturn malloc(sizeof(*p));"
    post = "result != NULL"
    softened = _relax_postcondition_for_error_paths(post, body, "f")
    assert "result == NULL" in softened
    assert "result != NULL" in softened


def test_relax_postcondition_permits_negative_when_body_returns_neg1():
    """Soften `result > 0` when body has `return -1;`."""
    from bmc_agent.spec_generator import _relax_postcondition_for_error_paths

    body = "if (bad) return -1;\nreturn n;"
    post = "result > 0"
    softened = _relax_postcondition_for_error_paths(post, body, "f")
    assert "result < 0" in softened
    assert "result > 0" in softened


def test_relax_postcondition_permits_error_enums():
    """Append UPPER_SNAKE error enums to postcondition disjunction."""
    from bmc_agent.spec_generator import _relax_postcondition_for_error_paths

    body = "if (bad) return CURLUE_OUT_OF_MEMORY;\nreturn CURLUE_OK;"
    post = "result == CURLUE_OK"
    softened = _relax_postcondition_for_error_paths(post, body, "f")
    assert "CURLUE_OUT_OF_MEMORY" in softened


def test_relax_postcondition_no_change_without_error_returns():
    """Postcondition unchanged when body has no error-sentinel returns."""
    from bmc_agent.spec_generator import _relax_postcondition_for_error_paths

    body = "return 42;"
    post = "result == 42"
    assert _relax_postcondition_for_error_paths(post, body, "f") == post


def test_relax_postcondition_idempotent_when_clause_already_present():
    """If postcondition already permits the error sentinel, no change."""
    from bmc_agent.spec_generator import _relax_postcondition_for_error_paths

    body = "if (x) return NULL;\nreturn p;"
    post = "result == NULL || result->ok > 0"
    softened = _relax_postcondition_for_error_paths(post, body, "f")
    # Should not double-wrap since result == NULL is already in the clause.
    assert softened == post


def test_spec_prompts_no_dsl_grammar_placeholder():
    """Spec prompts should not have {dsl_grammar} placeholder after refactoring."""
    from bmc_agent import prompts

    for name in (
        "ENTRY_SPEC_PROMPT",
        "INTERNAL_SPEC_PROMPT",
        "EXPECTED_SPEC_PROMPT",
        "CALLER_HEAVY_SPEC_PROMPT",
        "IMPL_HEAVY_SPEC_PROMPT",
    ):
        prompt = getattr(prompts, name)
        assert "{dsl_grammar}" not in prompt, (
            f"{name} still contains {{dsl_grammar}} — should be in system prompt"
        )


# ---------------------------------------------------------------------------
# Vacuous-spec critique pass (openai provider)
# ---------------------------------------------------------------------------

def _make_generator_with_mock(tmp_path, responses, provider="openai"):
    """Build a SpecGenerator whose LLMClient yields *responses* in order."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator
    config = Config(
        llm_model="mock",
        llm_api_key="mock",
        llm_provider=provider,
        artifact_dir=str(tmp_path / "art"),
        max_spec_retries=1,
        batch_size=1,
    )
    store = ArtifactStore(config.artifact_dir)
    mock_llm = MagicMock()
    mock_llm.complete = MagicMock(side_effect=list(responses))
    gen = SpecGenerator(config, mock_llm, store)
    return gen, mock_llm


def _func_stub(name="f", body=None):
    """A FunctionInfo-shaped stub with a non-trivial body."""
    if body is None:
        body = "{\n    let r = a + b;\n    if r > 100 { return 0; }\n    r\n}"
    class _Sig:
        is_pub = True
        is_static = False
        parameters = [("u32", "a"), ("u32", "b")]
        return_type = "u32"
        modifiers = []
        type_parameters = ""
        where_clause = ""
    class _F:
        def __init__(self):
            self.name = name
            self.body = body
            self.signature = _Sig()
            self.callees = set()
    return _F()


def test_vacuous_critique_skips_anthropic_provider(tmp_path):
    """Anthropic path: never run the second critique call."""
    initial = json.dumps({"precondition": "true", "postcondition": "true"})
    gen, mock = _make_generator_with_mock(tmp_path, [initial], provider="anthropic")
    result = gen._complete_with_vacuous_critique("prompt", _func_stub())
    assert result == ("true", "true")
    # Critique pass would have requested a second call -- prove it didn't.
    assert mock.complete.call_count == 1


def test_vacuous_critique_runs_on_openai_provider(tmp_path):
    """openai path: second call fires when first is vacuous on non-trivial body."""
    initial = json.dumps({"precondition": "true", "postcondition": "true"})
    upgraded = json.dumps({
        "precondition": "true",
        "postcondition": "result <= a.wrapping_add(b)",
    })
    gen, mock = _make_generator_with_mock(tmp_path, [initial, upgraded], provider="openai")
    result = gen._complete_with_vacuous_critique("prompt", _func_stub())
    assert mock.complete.call_count == 2
    assert result == ("true", "result <= a.wrapping_add(b)")


def test_vacuous_critique_keeps_first_when_second_is_also_vacuous(tmp_path):
    """If the model insists on true/true twice, take that as a signal not to churn further."""
    initial = json.dumps({"precondition": "true", "postcondition": "true"})
    again = json.dumps({"precondition": "true", "postcondition": "true"})
    gen, mock = _make_generator_with_mock(tmp_path, [initial, again], provider="openai")
    result = gen._complete_with_vacuous_critique("prompt", _func_stub())
    assert mock.complete.call_count == 2
    assert result == ("true", "true")


def test_vacuous_critique_skips_trivial_body(tmp_path):
    """A genuinely trivial wrapper (1 line) is allowed to be true/true."""
    initial = json.dumps({"precondition": "true", "postcondition": "true"})
    gen, mock = _make_generator_with_mock(tmp_path, [initial], provider="openai")
    triv = _func_stub(name="wrap", body="{ a }")
    result = gen._complete_with_vacuous_critique("prompt", triv)
    assert mock.complete.call_count == 1
    assert result == ("true", "true")


def test_vacuous_critique_skips_when_first_is_already_rich(tmp_path):
    """No second call when the first spec is already non-vacuous."""
    rich = json.dumps({"precondition": "a > 0", "postcondition": "result == a + b"})
    gen, mock = _make_generator_with_mock(tmp_path, [rich], provider="openai")
    result = gen._complete_with_vacuous_critique("prompt", _func_stub())
    assert mock.complete.call_count == 1
    assert result == ("a > 0", "result == a + b")


def test_vacuous_critique_handles_unparseable_critique(tmp_path):
    """If the critique response is gibberish, fall back to the first spec."""
    initial = json.dumps({"precondition": "true", "postcondition": "true"})
    junk = "lalala not json at all"
    gen, mock = _make_generator_with_mock(tmp_path, [initial, junk], provider="openai")
    result = gen._complete_with_vacuous_critique("prompt", _func_stub())
    assert mock.complete.call_count == 2
    assert result == ("true", "true")
