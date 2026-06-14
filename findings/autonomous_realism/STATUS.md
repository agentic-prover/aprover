STATE: RUNNING
Phase: 1 GATE (shadow in flight) + Phase 2 (spec-refiner soundness-policy wiring landed)
Heartbeat: 2026-06-14T10:47:43Z iter-note: Phase-1 irq/vfs shadow runs in flight (~11min, still in
CBMC flag-selection; HARNESS-REFINE adjudication lines come at the report stage). Used the wait window
to land the Phase-2 spec-refiner soundness-policy wiring (committed+pushed b666eed). Polling shadow.

Done since last STATUS:
- Verified Phase-1 wiring + tests green: harness_refiner (32 tests) + soundness_policy all pass.
- Re-launched irq/vfs Phase-1 shadow (detached, via tools/reshadow_phase1.sh) with the NULL-defined-
  trusted-global detection fix (c31b3b5). PIDs 1914837 (irq) / 1914836 (vfs), started 14:35 local.
  Output: findings/phase1_refine_shadow_irq2 + _vfs2. Both still running (~11 min, CBMC stage).
- Phase 2 (STATUS next-item #2) LANDED + pushed (b666eed): wired soundness_policy into the
  spec-refiner accept path. New opt-in flag --enforce-spec-refiner-retier (default OFF, does NOT
  change --agentic default). When ON, an accepted clause that is not deterministically caller-checked
  RE-TIERS the finding ('unlikely' lead) instead of marking it VERIFIED CLEAN (an unsound DELETE on an
  agentic-only SoundnessAgent judgment), and the clause is not persisted. Strictly more conservative:
  can only RESCUE a wrongly-deleted bug, never demote a real one -> safety gate preserved by construction.
  _clause_deterministically_caller_checked() returns False today = the explicit DETERMINISTIC_VERIFIER
  hook for future sound deletion. New test tests/test_spec_refiner_retier.py; 81 tests pass.

Gate results so far:
- Phase 1 shadow: PENDING (no HARNESS-REFINE lines yet; runs in CBMC flag-selection stage).
- Phase 2 unit gate: GREEN (81 passed across refiner/soundness suite; CLI parses new flag; config
  default False + env loader True verified).

Next (in order):
1. POLL findings/phase1_refine_shadow_{irq2,vfs2}/run.log for HARNESS-REFINE adjudication lines.
   GATE to verify: wsod_* NULL-deref -> ARTIFACT (would-demote); vfs_open_handle strcpy -> REAL/kept;
   0 reals lost. Adjudicate when both runs finish (DONE markers in run.log).
2. Validate the Phase-2 retier change against the regression oracle (cross-codebase 0/7, VibeOS 0/8)
   with --enforce-spec-refiner-retier ON: confirm 0 real-bug demotions (RETIER cannot demote by
   construction; verify empirically + measure FP-precision delta).
3. Phase 2-REPRO: tool-using ReproducerAgent (do before the 2a harness_kind decision).

Constraints unchanged: shadow-only; STOP at Phase 4 (needs user OK); never flip --agentic default /
delete confirmed_dynamic immunity. Safety gate absolute: any real-bug demotion -> revert + stop line.
