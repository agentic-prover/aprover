"""M1 end-to-end pipeline tests: Rust file → SpecGenerator → specs.

These tests verify that the M1 frontend wiring is correct:
  * .rs inputs dispatch through ``parse_source_file`` to the Rust parser.
  * The spec generator selects the Rust-flavoured system prompt
    (different DSL notes for references vs raw pointers).
  * The generator runs the same agentic loop on Rust input as on C
    input — function discovery, layered generation order, per-function
    LLM calls, spec persistence to the artifact store.

The LLM is mocked, so these tests run offline and serve as a regression
gate for the M1 plumbing.  An additional real-LLM smoke test against
CCC's encoding.rs is gated on ANTHROPIC_API_KEY and skipped by default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_MOCK_RUST_SPEC = json.dumps({
    "precondition": "true",
    "postcondition": "\\result == input.len() || \\result == 0",
    "reasoning": "Mock Rust spec for M1 plumbing test.",
})


def _make_mock_llm(response: str = _MOCK_RUST_SPEC) -> MagicMock:
    mock = MagicMock()
    mock.complete.return_value = response
    return mock


_RUST_SOURCE = """\
//! A small Rust module for testing the M1 pipeline wiring.

pub fn entry(buf: &[u8]) -> usize {
    if buf.is_empty() {
        return 0;
    }
    leaf(buf.len())
}

fn leaf(n: usize) -> usize {
    n.saturating_mul(2)
}
"""


def _write_rust_fixture(tmp_path: Path) -> Path:
    f = tmp_path / "fixture.rs"
    f.write_text(_RUST_SOURCE)
    return f


# ---------------------------------------------------------------------------
# Plumbing: source_parser routes .rs through rust_parser
# ---------------------------------------------------------------------------


def test_pipeline_parses_rust_via_source_parser(tmp_path: Path):
    from bmc_agent.rust_parser import ParsedRustFile
    from bmc_agent.source_parser import parse_source_file

    f = _write_rust_fixture(tmp_path)
    parsed = parse_source_file(f)
    assert isinstance(parsed, ParsedRustFile)
    assert {"entry", "leaf"} <= set(parsed.functions)
    assert "leaf" in parsed.call_graph["entry"]


# ---------------------------------------------------------------------------
# SpecGenerator picks the Rust system prompt for .rs inputs
# ---------------------------------------------------------------------------


def test_spec_generator_uses_rust_system_prompt(tmp_path: Path):
    """When generate_specs() is given a .rs file, the system prompt
    forwarded to llm.complete() must be the Rust-flavoured one
    (mentioning slices/refs and wrapping arithmetic) — not the C one."""
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator

    f = _write_rust_fixture(tmp_path)

    config = Config(
        llm_model="mock-model",
        llm_api_key="mock-key",
        artifact_dir=str(tmp_path / "artifacts"),
        max_spec_retries=1,
        batch_size=2,
    )
    store = ArtifactStore(config.artifact_dir)
    mock_llm = _make_mock_llm()

    gen = SpecGenerator(config, mock_llm, store)
    specs = gen.generate_specs(
        source_file=str(f),
        driver_name="rust_fixture",
        domain_knowledge="Small saturating helper module.",
    )

    # Both functions must have specs.
    assert {"entry", "leaf"} == set(specs)
    for s in specs.values():
        assert s.precondition.strip()
        assert s.postcondition.strip()

    # The system prompt argument is the first positional arg to complete().
    assert mock_llm.complete.call_count > 0
    system_prompts_used = {call.args[0] for call in mock_llm.complete.call_args_list}
    # All system-prompt arguments should be identical Rust-flavour strings.
    assert len(system_prompts_used) == 1, (
        f"Expected one stable system prompt across the run, got "
        f"{len(system_prompts_used)} distinct strings"
    )
    sysp = next(iter(system_prompts_used))
    assert "Rust" in sysp, "Expected Rust-aware system prompt, got C-flavour"
    # Spot-check the Rust DSL guidance is present.
    assert "Safe references" in sysp
    assert "wrapping_" in sysp


# ---------------------------------------------------------------------------
# Specs persist to the artifact store under the chosen driver name.
# ---------------------------------------------------------------------------


def test_rust_specs_persist_to_artifact_store(tmp_path: Path):
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.spec_generator import SpecGenerator

    f = _write_rust_fixture(tmp_path)
    config = Config(
        llm_model="mock-model",
        llm_api_key="mock-key",
        artifact_dir=str(tmp_path / "artifacts"),
        max_spec_retries=1,
        batch_size=2,
    )
    store = ArtifactStore(config.artifact_dir)
    gen = SpecGenerator(config, _make_mock_llm(), store)

    gen.generate_specs(
        source_file=str(f),
        driver_name="rust_fixture",
        domain_knowledge="",
    )

    # Round-trip: reload the specs from disk via the store.
    for fn in ("entry", "leaf"):
        loaded = store.load_spec("rust_fixture", fn)
        assert loaded is not None, f"Spec for '{fn}' missing on disk"
        assert loaded.function_name == fn


# ---------------------------------------------------------------------------
# Optional real-LLM smoke test against CCC encoding.rs.
# Skipped unless ANTHROPIC_API_KEY is set AND the CCC sparse checkout
# exists locally.  This is the milestone-M1g end-to-end gate.
# ---------------------------------------------------------------------------

_CCC_ENCODING = Path("/tmp/ccc-peek/src/common/encoding.rs")


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY") or not _CCC_ENCODING.exists(),
    reason="M1g real-LLM smoke test requires ANTHROPIC_API_KEY + /tmp/ccc-peek",
)
def test_rust_pipeline_on_ccc_encoding(tmp_path: Path):
    """Live end-to-end: run AProver's Phase 1 on CCC's encoding.rs.

    Asserts only that the pipeline completes and produces non-trivial
    specs for the pure functions.  The realism of those specs is what
    a manual review would assess — not this test.
    """
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.spec_generator import SpecGenerator

    config = Config(
        llm_model=os.environ.get("BMC_AGENT_LLM_MODEL", "claude-sonnet-4-6"),
        artifact_dir=str(tmp_path / "artifacts"),
        max_spec_retries=1,
        batch_size=2,
    )
    store = ArtifactStore(config.artifact_dir)
    llm = LLMClient(config)
    gen = SpecGenerator(config, llm, store)

    specs = gen.generate_specs(
        source_file=str(_CCC_ENCODING),
        driver_name="ccc_encoding",
        domain_knowledge=(
            "Encoding helpers for non-UTF-8 C source bytes via PUA code "
            "points (U+E080..U+E0FF). bytes_to_string is the encoder; "
            "decode_pua_byte is the inverse."
        ),
    )

    assert {"utf8_sequence_length", "decode_pua_byte"} <= set(specs), (
        f"Expected at least the pure helpers to be specced; got {sorted(specs)}"
    )
    for name, s in specs.items():
        assert s.precondition.strip(), f"Empty precondition for {name}"
        assert s.postcondition.strip(), f"Empty postcondition for {name}"
