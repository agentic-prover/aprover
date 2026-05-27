"""Regression tests for the TriageToolsAgent prompt gates (G1, G2, G3).

These tests pin the gate identifiers and fp_class strings into the
prompt so that a future refactor of ``_TOOLS_PROMPT_ADDENDUM`` cannot
silently drop a gate. They do not exercise the LLM — they assert on
the *prompt content* the agent sends.

If you intentionally rename, remove, or refactor a gate, update the
test to match. The test is here to catch *accidental* loss, not to
freeze the wording.
"""

from __future__ import annotations

import pytest

from bmc_agent.agents.triage_tools import _SYSTEM_PROMPT_TOOLS


def test_prompt_contains_four_gate_headings():
    """Every gate has a `Gn. <NAME> GATE.` heading."""
    assert "G1. PRIVATE-HEADER REACHABILITY GATE" in _SYSTEM_PROMPT_TOOLS
    assert "G2. ALLOCATION-SITE INVARIANT GATE" in _SYSTEM_PROMPT_TOOLS
    assert "G3. INTRA-FUNCTION LOOP-BOUND INVARIANT GATE" in _SYSTEM_PROMPT_TOOLS
    assert "G4. CALLER-ESTABLISHED PRECONDITION GATE" in _SYSTEM_PROMPT_TOOLS


def test_prompt_documents_each_fp_class_string():
    """The abort instruction lists every fp_class the gates can vote.

    The downstream verdict pipeline relies on these exact strings to
    count how often each gate fires. Renaming a class without
    updating downstream comparators would silently break aggregation.
    """
    assert "private-header-gated" in _SYSTEM_PROMPT_TOOLS
    assert "calloc-zero-init-invariant" in _SYSTEM_PROMPT_TOOLS
    assert "intra-function-loop-invariant" in _SYSTEM_PROMPT_TOOLS
    assert "caller-established-precondition" in _SYSTEM_PROMPT_TOOLS


def test_g3_explains_the_procedure():
    """G3's body must describe the procedure the agent runs."""
    assert "CONTINUATION SET" in _SYSTEM_PROMPT_TOOLS
    # Mentions the structural invariant — preceding loop establishes
    # a constraint that the current loop's continuation cannot satisfy.
    assert "preceding loop" in _SYSTEM_PROMPT_TOOLS.lower()


def test_g3_includes_a_worked_example():
    """The next_field_w worked example is the canonical illustration
    of the gate's structural shape. Removing it would leave the gate
    too abstract to apply consistently."""
    assert "next_field_w" in _SYSTEM_PROMPT_TOOLS
    # Both loops in the worked example must be quoted so the agent
    # can pattern-match.
    assert "Leading skip" in _SYSTEM_PROMPT_TOOLS
    assert "Trim loop" in _SYSTEM_PROMPT_TOOLS


def test_abort_block_lists_all_four_fp_classes():
    """The abort block names every fp_class the gates can vote for
    so the agent emits the right tag."""
    abort_block = _SYSTEM_PROMPT_TOOLS.split(
        "Any of these gates ABORTS the REAL_BUG vote"
    )[-1]
    assert "private-header-gated" in abort_block
    assert "calloc-zero-init-invariant" in abort_block
    assert "intra-function-loop-invariant" in abort_block
    assert "caller-established-precondition" in abort_block


def test_g4_documents_the_procedure_and_example():
    """G4 must spell out the in-tree-caller audit procedure and the
    next_field worked example so the agent can apply it consistently."""
    assert "next_field" in _SYSTEM_PROMPT_TOOLS
    assert "in-tree caller" in _SYSTEM_PROMPT_TOOLS
    assert "100% of in-tree callers" in _SYSTEM_PROMPT_TOOLS
    # The negative guidance — G4 doesn't fire on "harness over-
    # permissive" alone — must be present so the gate doesn't
    # over-fire on cases where some real-world caller violates P.
    assert "harness is over-permissive" in _SYSTEM_PROMPT_TOOLS
    assert "necessary but not sufficient" in _SYSTEM_PROMPT_TOOLS


def test_prompt_addendum_appended_to_base_prompt():
    """The TriageToolsAgent prompt is BASE + addendum, not a
    replacement. Sanity-check that the base prompt's REAL_BUG /
    LIKELY_FP / NEEDS_HUMAN vocabulary is still present after the
    addendum loads."""
    assert "REAL_BUG" in _SYSTEM_PROMPT_TOOLS
    assert "LIKELY_FP" in _SYSTEM_PROMPT_TOOLS
    assert "NEEDS_HUMAN" in _SYSTEM_PROMPT_TOOLS
