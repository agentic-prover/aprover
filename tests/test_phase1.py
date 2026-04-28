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
