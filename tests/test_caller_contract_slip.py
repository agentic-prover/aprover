"""Caller-contract-slip integration test.

The bug we are fixing (see findings/methodology_insight_2026-05-22.md):
when a caller passes args that violate the callee's PRE, the historical
``functional`` spec mode assumed the PRE inside the callee stub, which
silently pruned the offending caller path. The new ``bug-hunt`` mode
should assert validity clauses at the top of the stub so CBMC reports
the violation at the caller's call site.

This test uses a miniature ``ncdev_bar_read`` callee modelled on the
real Neuron driver function. We don't run CBMC — we just inspect the
stub source emitted by ``_generate_stub`` to verify the assert / assume
split is wired correctly. CBMC will pick it up automatically in the
empirical re-run (Phase 5).
"""

from bmc_agent.harness_generator import _generate_stub
from bmc_agent.parser import FunctionSignature, ParsedCFile
from bmc_agent.spec import Spec


def _make_ncdev_bar_read_parsed_file() -> ParsedCFile:
    """A ParsedCFile containing just the ``ncdev_bar_read`` signature.

    Real prototype (paraphrased):
        int ncdev_bar_read(struct neuron_device *nd,
                           u64 *reg_addresses,
                           u32 data_count,
                           u32 *data);
    """
    sig = FunctionSignature(
        name="ncdev_bar_read",
        return_type="int",
        parameters=[
            ("struct neuron_device *", "nd"),
            ("u64 *", "reg_addresses"),
            ("u32", "data_count"),
            ("u32 *", "data"),
        ],
    )
    return ParsedCFile(
        path="ncdev.c",
        functions={"ncdev_bar_read": sig},
        call_graph={},
        function_bodies={},
    )


def _make_ncdev_bar_read_spec() -> Spec:
    """Spec mirroring what bmc-agent produced for ncdev_bar_read on
    the 2026-05-22 Neuron P2 sweep. The validity clause
    ``valid_range(reg_addresses, 0, data_count)`` is the caller
    obligation; the buggy real caller (``ncdev_bar_rw``) violates it
    when ``data_count`` exceeds the size of its on-stack
    ``reg_addresses`` buffer.
    """
    return Spec(
        function_name="ncdev_bar_read",
        precondition=(
            "valid(nd) && valid_range(reg_addresses, 0, data_count) && "
            "valid_range(data, 0, data_count)"
        ),
        postcondition="result == 0 || result < 0",
    )


# ---------------------------------------------------------------------------
# functional mode (default, back-compat)
# ---------------------------------------------------------------------------


def test_functional_mode_assumes_full_precondition():
    """Default behaviour: every PRE clause is ``__CPROVER_assume``-d
    inside the stub. This is the historical mode — it must keep
    working for back-compat with the 612-test baseline."""
    parsed = _make_ncdev_bar_read_parsed_file()
    spec = _make_ncdev_bar_read_spec()
    stub = _generate_stub(
        "ncdev_bar_read", spec, parsed, spec_mode="functional"
    )
    # Validity-style clauses still appear, but as assumes — so any
    # call site that violates them is silently pruned.
    assert "__CPROVER_assume" in stub
    # The bug-hunt-specific comment must NOT be present.
    assert "bug-hunt: assert validity" not in stub
    # No ``assert(`` should target the precondition in functional mode.
    # (``assert(`` could still legitimately appear from postcondition
    # translation, so we look for the validity clause specifically.)
    assert "assert(reg_addresses" not in stub


# ---------------------------------------------------------------------------
# bug-hunt mode — the fix for the caller-contract slip
# ---------------------------------------------------------------------------


def test_bug_hunt_mode_asserts_validity_clauses_at_stub_top():
    """In bug-hunt mode, validity clauses become ``assert(...)`` at
    the top of the stub. CBMC binds the formal parameters to the
    caller's actual argument expressions; if the caller passes args
    that violate the assertion, CBMC reports the failure at the
    call site — surfacing the caller-contract slip."""
    parsed = _make_ncdev_bar_read_parsed_file()
    spec = _make_ncdev_bar_read_spec()
    stub = _generate_stub(
        "ncdev_bar_read", spec, parsed, spec_mode="bug-hunt"
    )
    assert "bug-hunt: assert validity" in stub
    # The validity clauses must be emitted as asserts, not assumes.
    # ``precond_to_assert`` wraps clauses in ``assert(...)``; the
    # legacy ``__CPROVER_assume`` should not also wrap the same
    # validity content.
    # Heuristic: at least one ``assert(`` mentioning ``reg_addresses``
    # (the validity clause's pointer) should appear.
    assert "assert(" in stub
    # The pointer that the caller-contract slip hinges on must appear
    # inside an assert — that's how the bug surfaces.
    assert any(
        line.lstrip().startswith("assert(") and "reg_addresses" in line
        for line in stub.splitlines()
    ), stub


def test_bug_hunt_mode_skips_protocol_assert_when_pre_is_pure_validity():
    """All PRE clauses here are validity → no protocol assume section."""
    parsed = _make_ncdev_bar_read_parsed_file()
    spec = _make_ncdev_bar_read_spec()
    stub = _generate_stub(
        "ncdev_bar_read", spec, parsed, spec_mode="bug-hunt"
    )
    assert "bug-hunt: assume protocol" not in stub


def test_bug_hunt_mode_uses_structured_split_when_present():
    """If the LLM (or the user) populated pre_validity / pre_protocol
    directly, ``split_precondition`` returns them as-is — bypassing the
    classifier. Validity goes to assert, protocol to assume."""
    parsed = _make_ncdev_bar_read_parsed_file()
    spec = Spec(
        function_name="ncdev_bar_read",
        precondition="valid_range(reg_addresses, 0, data_count) && locked(&nd->lock)",
        postcondition="true",
        pre_validity="valid_range(reg_addresses, 0, data_count)",
        pre_protocol="locked(&nd->lock)",
    )
    stub = _generate_stub(
        "ncdev_bar_read", spec, parsed, spec_mode="bug-hunt"
    )
    assert "bug-hunt: assert validity" in stub
    # ``locked(...)`` is sanitised in dsl_to_cbmc — it doesn't produce a
    # statement (ghost state). What matters here is that the validity
    # clause emitted an assert; the protocol section can be empty.
    assert any(
        line.lstrip().startswith("assert(") and "reg_addresses" in line
        for line in stub.splitlines()
    ), stub


def test_bug_hunt_mode_protocol_clause_uses_assume_not_assert():
    """A protocol clause that DOES translate to a C condition (a bare
    comparison naming a state-flag) must be assumed, never asserted."""
    parsed = _make_ncdev_bar_read_parsed_file()
    spec = Spec(
        function_name="ncdev_bar_read",
        precondition="valid(nd) && nd->state == 1",
        postcondition="true",
        pre_validity="valid(nd)",
        pre_protocol="nd->state == 1",
    )
    stub = _generate_stub(
        "ncdev_bar_read", spec, parsed, spec_mode="bug-hunt"
    )
    assert "bug-hunt: assume protocol" in stub
    # The state predicate must appear inside an assume, never an assert.
    assert any(
        "__CPROVER_assume(" in line and "state" in line
        for line in stub.splitlines()
    ), stub
    assert not any(
        line.lstrip().startswith("assert(") and "state" in line
        for line in stub.splitlines()
    ), stub
