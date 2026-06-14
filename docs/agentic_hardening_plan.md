# --agentic hardening — plan & resume state

Resume anchor for the `--agentic` work. Branch: **`reproducer-agent-merge`**
(NOT pushed, NOT merged to main — this repo works linearly on main).

## Done this session (committed on the branch)

```
5f2700b docs: agent telemetry + soundness gate usage and next steps
41dbcc8 soundness: standing tiering-logic guard + adjudication checker
b46fd28 telemetry: per-agent runtime instrumentation
3cfb465 fix: guard reproducer-agent output against non-str leaking into dyn-val
8f5cfa6 agentic harness: default-ON repair + AgenticHarnessGen as harness_gen BaseAgent
295451a pipeline: add off-switch for Phase 3d oracle-disagreement diagnosis (default OFF)
ed6616a agents: merge DynamicReproAgent into the tool-using ReproducerAgent
feadb99 bmc-config + reproducer agents: default ON with --no- toggles
f885ee9 realism-enforcement: Phase 3 DONE — GATE GREEN, enforcement stays default-ON
```

Current default-on `--agentic` agents (10): spec_gen (+ split pass-2), bmc_config
(cbmc_driver), classifier, refinement, soundness (rides refinement), feedback_distill,
realism, reproducer (dynamic_repro), harness_gen. OFF by default: disagreement_diagnose,
dynval_triage, realism-tools, triage, agentic-harness-primary.

## Validation discipline (use for EVERY change)
- Full suite: `python3 -m pytest tests/ -q -p no:cacheprovider` — baseline is **54 failures**
  (all pre-existing/unrelated: rust-parser ModuleNotFoundError, cache_prefix threat-model
  drift, phase/kani env). A change is clean iff `comm -13 baseline now` is empty.
- ALSO run `tests/test_phase3.py` ALONE — full-suite ordering masks regressions (this is how
  the reproducer non-str leak slipped through once). Isolated baseline = 3 failures.

## Plan (re-sequenced: budget-free first, since the live sweep is on hold)

### Budget-free track — NO LLM/CBMC, do autonomously
1. **Agent registry (IN PROGRESS — see below).** Collapse the 3 hand-synced role lists into
   one source. DRAFT MODULE ALREADY ADDED: `bmc_agent/agent_registry.py` (AGENT_ROLES +
   REGISTRY + label_for). It is NOT yet wired — nothing imports it. Remaining steps:
   - `config.py`: replace the literal role tuple in the env-routing loop
     (`for role in ( "spec_gen", ... "harness_gen" ):`) with `for role in AGENT_ROLES:`,
     add `from bmc_agent.agent_registry import AGENT_ROLES`.
   - `cli.py`: replace the `ALL_AGENT_ROLES = ( ... )` literal in `_apply_provider_args`
     with `ALL_AGENT_ROLES = AGENT_ROLES` (import at top).
   - Add `tests/test_agent_registry.py` pinning AGENT_ROLES to the exact historical 11-role
     set {spec_gen, feedback_distill, refinement, realism, classifier, disagreement_diagnose,
     triage, dynamic_repro, dynval_triage, cbmc_driver, harness_gen} so accidental drift fails.
   - Optionally fold the AI-layers printout labels onto `label_for` (lower priority; printout
     is keyed on enable_* flags, not roles).
2. **Token plumbing into telemetry.** Thread `usage` (prompt/completion tokens) out of
   `LLMClient.complete()` / `complete_with_tools()` (llm.py logs it at ~117/389 but doesn't
   return it) into `agent_telemetry` (the `tokens` field is reserved, currently 0). Turns
   duration into $/finding. Deterministic; testable with mocks.
3. **Centralize output-contract validation in `BaseAgent`.** Make `run()`/`parse` enforce the
   declared output type and return None/error on violation, so the non-str-leak class (the
   reproducer regression, fixed reactively in 3cfb465) can't recur.
4. **Test fidelity.** Add randomized test order (pytest-randomly) in CI + faithful agent
   test-doubles, so a default-flip can't silently regress behind ordering.

### Budget-gated track — needs a live --agentic sweep (ON HOLD pending user go)
0. **Live baseline (Phase 0).** `verify-dir --agentic` on a known-oracle fixture (recommend
   VibeOS vfs — reals vfs_readdir/vfs_write, FPs vfs_append/vfs_delete_recursive). Capture
   `<artifact_dir>/agent_telemetry.json` + run
   `tools/check_soundness_gate.py <findings_dir> --reals vfs_readdir,vfs_write --fps vfs_append,vfs_delete_recursive`.
   Exit: a $/role baseline + GREEN gate.
5. **Measured efficiency (Phase 2).** One change at a time: route low-judgment/high-volume
   roles to a cheaper model via `BMC_AGENT_LLM_<ROLE>_PROVIDER` (no code change), and flatten
   tool-loop agents where tools don't move recall/precision. After each: re-sweep, diff
   telemetry (cost) + soundness gate (recall GREEN) + FP rate. Keep wins, revert duds.
   (flat-vs-agentic and cheap-vs-expensive are ORTHOGONAL knobs.)

### Land it (Phase 4)
Run the empirical gate once more, decide branch/merge strategy, land on main.

## Loose ends
- Branch `reproducer-agent-merge` is unpushed / unmerged.
- Repo `git gc` / "too many unreachable loose objects" warning — a one-time `git gc` clears it.
- Permissions: full bypass is set in the LOCAL Claude Code `.claude/settings.local.json`
  (`permissions.defaultMode = bypassPermissions`, `skipDangerousModePermissionPrompt = true`).

## Tooling reference
- Telemetry: `bmc_agent/agent_telemetry.py`; per-run dump at `<artifact_dir>/agent_telemetry.json`.
- Soundness: `tests/test_soundness_corpus.py` (deterministic), `tools/check_soundness_gate.py`
  (empirical, over a real findings dir). See `docs/agent_telemetry_and_soundness.md`.
