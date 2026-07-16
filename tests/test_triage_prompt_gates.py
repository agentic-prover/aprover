"""Regression tests for the TriageToolsAgent prompt gates (G1–G5).

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

from bmc_agent.agents.triage_tools import _TOOLS_PROMPT_ADDENDUM_FULL as _SYSTEM_PROMPT_TOOLS  # gates now live in the opt-in 'full' scaffold; guard it explicitly


def test_prompt_contains_five_gate_headings():
    """Every gate has a `Gn. <NAME> GATE.` heading."""
    assert "G1. PRIVATE-HEADER REACHABILITY GATE" in _SYSTEM_PROMPT_TOOLS
    assert "G2. ALLOCATION-SITE INVARIANT GATE" in _SYSTEM_PROMPT_TOOLS
    assert "G3. INTRA-FUNCTION LOOP-BOUND INVARIANT GATE" in _SYSTEM_PROMPT_TOOLS
    assert "G4. CALLER-ESTABLISHED PRECONDITION GATE" in _SYSTEM_PROMPT_TOOLS
    assert "G5. STRUCTURAL-INVARIANT GATE" in _SYSTEM_PROMPT_TOOLS


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
    assert "structural-invariant-bound" in _SYSTEM_PROMPT_TOOLS


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


def test_abort_block_lists_all_five_fp_classes():
    """The abort block names every fp_class the gates can vote for
    so the agent emits the right tag."""
    abort_block = _SYSTEM_PROMPT_TOOLS.split(
        "Any of these gates ABORTS the REAL_BUG vote"
    )[-1]
    assert "private-header-gated" in abort_block
    assert "calloc-zero-init-invariant" in abort_block
    assert "intra-function-loop-invariant" in abort_block
    assert "caller-established-precondition" in abort_block
    assert "structural-invariant-bound" in abort_block


def test_g5_documents_both_sub_shapes():
    """G5 covers two structurally-distinct FP classes the realism
    agent kept conflating with G4 or missing entirely: (a) data-
    structure construction bounds (e.g. lookup tables populated by
    loop counters) and (b) allocation-site over-allocation (e.g.
    bit readers peeking past a logical-end pointer into an
    intentionally-sized tail). Both sub-shapes must remain in the
    prompt — dropping either leaves a documented FP class
    uncatchable.
    """
    assert "G5a. DATA-STRUCTURE CONSTRUCTION" in _SYSTEM_PROMPT_TOOLS
    assert "G5b. ALLOCATION-SITE OVER-ALLOCATION" in _SYSTEM_PROMPT_TOOLS


def test_g5_distinguishes_itself_from_g4():
    """G5 looks at write edges and allocation sites; G4 looks at
    call edges. The prompt must call out the difference so the agent
    doesn't conflate the two (the failure mode that produced the
    2026-05-28 misclassifications)."""
    assert "G5 differs" in _SYSTEM_PROMPT_TOOLS or "G5 vs G4" in _SYSTEM_PROMPT_TOOLS
    assert "WRITE edges" in _SYSTEM_PROMPT_TOOLS
    assert "ALLOCATION sites" in _SYSTEM_PROMPT_TOOLS


def test_g5_includes_both_worked_examples():
    """The two libarchive worked examples (decode_code_length and
    read_bits_16) are the canonical illustrations of the two sub-
    shapes. Removing either would leave that sub-shape too abstract
    to apply consistently — both were the realism agent's actual
    misclassifications in the postfix9c Bucket B re-audit."""
    assert "decode_code_length" in _SYSTEM_PROMPT_TOOLS
    assert "read_bits_16" in _SYSTEM_PROMPT_TOOLS
    # The structural constants that bound the values must be quoted
    # so the agent can pattern-match them on similar tables in
    # future sweeps.
    assert "HUFF_NC" in _SYSTEM_PROMPT_TOOLS
    assert "HUFF_RC" in _SYSTEM_PROMPT_TOOLS
    # The +4 tail at the read_ahead site is the load-bearing fact
    # for G5b.
    assert "4 + cur_block_size" in _SYSTEM_PROMPT_TOOLS


def test_g5_documents_when_not_to_apply():
    """G5 has a narrow contract: it does NOT close cases where the
    producer of V is attacker input, where the construction code is
    itself buggy, or where the over-allocation isn't matched to the
    max peek across siblings. The negative-guidance block prevents
    G5 from over-firing on cases that look structural but aren't."""
    assert "G5 does NOT apply" in _SYSTEM_PROMPT_TOOLS
    assert "attacker input" in _SYSTEM_PROMPT_TOOLS
    assert "sibling readers" in _SYSTEM_PROMPT_TOOLS


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
    replacement. Since the base prompt moved to a STRUCTURED judgment
    (the verdict is DERIVED from sub-answers, not named), sanity-check
    that BOTH compose: the addendum's G1-G5 gate vocabulary AND the
    base's structured-judgment schema fields are present."""
    # addendum gate vocabulary (the G1-G5 reachability gates)
    assert "REAL_BUG" in _SYSTEM_PROMPT_TOOLS
    assert "LIKELY_FP" in _SYSTEM_PROMPT_TOOLS
    # base structured-judgment schema (verdict derived from these)
    assert "real_defect" in _SYSTEM_PROMPT_TOOLS
    assert "defect_reachable_in_tree" in _SYSTEM_PROMPT_TOOLS
    assert "witness_reproducible_as_is" in _SYSTEM_PROMPT_TOOLS
    assert "needs_human" in _SYSTEM_PROMPT_TOOLS
