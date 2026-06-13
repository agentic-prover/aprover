STATE: RUNNING
Phase: 1 (harness-refinement outcome C) — IMPLEMENTED + WIRED; shadow runs in flight.

## Done so far (committed + pushed to main)
- Phase 0: baseline regression oracle frozen (`baseline_oracle.md`).
- Phase 1 core: `bmc_agent/harness_refiner.py` (detect undefined boot-init-trusted externs in a
  sibling .c, materialize pointer = calloc(1,sizeof)); 11 unit tests green.
- Phase 1 wiring: `DynamicValidator.refine_and_revalidate()` + `pipeline._maybe_refine_harness()`
  behind `--harness-refinement {off,shadow,live}` (DEFAULT OFF = zero change to existing runs).
  Synthetic GCC end-to-end tests prove soundness: NULL deref cleaned after materialization
  (artifact), real OOB still faults on the 1-element buffer (kept). 14 refiner tests +
  355 touched-area tests green.

## In flight (background, --harness-refinement shadow, log-only)
- irq: findings/phase1_refine_shadow_irq/run.log  (pid 1311000)
- vfs: findings/phase1_refine_shadow_vfs/run.log  (pid 1312004)  ← SAFETY-critical (vfs_open_handle)
Both started ~23:21Z, expected ~20-30 min. Watching for `HARNESS-REFINE [shadow]` lines and the
final verdict table.

## Phase 1 GATE being checked (soundly scoped)
- vfs_open_handle: refined harness must STILL crash (strcpy overflow survives materialization) =>
  KEPT confirmed. (Absolute safety anchor.)
- No real bug WOULD-demoted anywhere (every HARNESS-REFINE "ARTIFACT" must be a genuine FP).
- irq: observe which NULL-default FPs the refiner WOULD demote (the fb_width-loop FPs are expected
  to be KEPT by the refiner — they re-crash; their numeric demotion is Phase 2a's job, per the
  recorded attribution finding).

## Next
1. Read both shadow results; confirm gate. If any real bug would-demote → that's a bug in the
   refiner → fix before proceeding (safety gate).
2. Phase 2a: in `_maybe_ground_immunity`, `evidence_strong = formal_reach` (drop the
   `harness_kind=="system_entry"` axis = blocker-flaw #1). Shadow-test on irq.

## Decisions for the user
- None blocking. Stop at Phase 4 for explicit OK.
- FYI: pre-existing stale test `test_agentic_keeps_classifier_off_realism_triage_keeps_dynval`
  asserts realism OFF under --agentic; realism is now ON (matches the plan's scope facts). Not
  touched by me; flagging in case you want it updated.
