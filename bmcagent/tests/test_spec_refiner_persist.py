"""In-sweep persistence of the soundness-gated spec_refiner clause.

When _spec_refine_iterate accepts a clause (it excluded the realism-rejected
counterexample), that clause must be persisted to the learned-constraints
store as a FUNCTION_SPEC remediation -- so the function's other CExs /
re-verifications pick it up via harness Step 1.7 in the same sweep (and it
carries across sweeps). Previously the gated clause was used for one re-verify
and discarded.
"""

import tempfile
from types import SimpleNamespace

from bmc_agent.config import Config
from bmc_agent.pipeline import AMCPipeline
from bmc_agent.feedback_loop import LearnedConstraintsStore


def _shim(artifact_dir, *, feedback_loop=True, soundness_gate=False):
    cfg = Config()
    cfg.artifact_dir = artifact_dir
    cfg.enable_feedback_loop = feedback_loop
    cfg.enable_soundness_gate = soundness_gate
    # AMCPipeline._persist_gated_refiner_clause only touches self.config.
    return SimpleNamespace(config=cfg)


def _proposal(clause, rationale="excludes nondet phnum"):
    return SimpleNamespace(added_clause=clause, rationale=rationale)


def _validation(prop="elf_load.memcpy.1"):
    return SimpleNamespace(counterexample=SimpleNamespace(failing_property=prop))


def test_accepted_clause_is_persisted_as_function_clause():
    with tempfile.TemporaryDirectory() as d:
        shim = _shim(d)
        func = SimpleNamespace(name="elf_load")
        AMCPipeline._persist_gated_refiner_clause(
            shim, func, _proposal("e_phnum <= 65535"), _validation())
        store = LearnedConstraintsStore(d)
        assert "e_phnum <= 65535" in store.function_clauses("elf_load")
        # Not leaked into project scope.
        assert "e_phnum <= 65535" not in store.project_clauses()


def test_disabled_feedback_loop_skips_persist():
    with tempfile.TemporaryDirectory() as d:
        shim = _shim(d, feedback_loop=False)
        func = SimpleNamespace(name="elf_load")
        AMCPipeline._persist_gated_refiner_clause(
            shim, func, _proposal("e_phnum <= 65535"), _validation())
        store = LearnedConstraintsStore(d)
        assert store.function_clauses("elf_load") == []


def test_empty_clause_is_noop():
    with tempfile.TemporaryDirectory() as d:
        shim = _shim(d)
        func = SimpleNamespace(name="elf_load")
        AMCPipeline._persist_gated_refiner_clause(
            shim, func, _proposal("   "), _validation())
        store = LearnedConstraintsStore(d)
        assert store.function_clauses("elf_load") == []


def test_persisted_clause_is_read_back_for_harness_step17():
    # The whole point: a persisted function clause is what harness Step 1.7
    # emits as __CPROVER_assume on the next harness for this function.
    with tempfile.TemporaryDirectory() as d:
        shim = _shim(d)
        func = SimpleNamespace(name="vfs_read")
        AMCPipeline._persist_gated_refiner_clause(
            shim, func, _proposal("n >= 0"), _validation("vfs_read.bounds.1"))
        # Fresh store instance reads the persisted file from disk.
        assert "n >= 0" in LearnedConstraintsStore(d).function_clauses("vfs_read")
