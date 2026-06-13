STATE: RUNNING
Phase: 1 (harness-refinement outcome C) — core landed, wiring next.

## Done so far
- Phase 0 (committed): froze baseline regression oracle in `baseline_oracle.md`.
  Keep-anchor `vfs_open_handle` confirmed_dynamic; irq FPs = wsod_draw_line / wsod_draw_sad_mac /
  sleep_ms; residual still-confirmed under current uniform tier = wsod_draw_line + 1×sleep_ms.
- Phase 1 core (committed, UNWIRED ⇒ zero behavior risk): `bmc_agent/harness_refiner.py` +
  `tests/test_harness_refiner.py` (11 green). Detects undefined boot-init-trusted EXTERN globals
  defined in a sibling .c (NULL/0 init, assigned only in an *_init fn), materializes them
  (pointer → calloc(1,sizeof) — b4aa03c model) so a confirmed_dynamic finding can be re-run:
  clean ⇒ NULL-default artifact ⇒ demote; still crashes ⇒ keep.

## Important honest finding (drives the rest of the plan)
Deeper code+CEx analysis refines the plan's attribution:
- The irq FPs are mostly **nondet-arg signed-overflow** CEx (x,y,ms driven to INT/UINT_MAX),
  NOT pure fb_base NULL-derefs. wsod_draw_sad_mac is ALREADY demoted by the existing uniform
  reachability tier (step4_live_irq). The residual wsod_draw_line + 1×sleep_ms stay confirmed
  because `evidence_strong` keys on `harness_kind=="system_entry"` (plan blocker-flaw #1).
- ⇒ The precise fix for the irq residual is **Phase 2a** (drop harness_kind from the evidence
  axis; use formal CBMC reachability only). The harness-refiner is the SOUND empirical demotion
  channel for the NULL-deref artifact class (e.g. the vfs tree-model FP from b4aa03c); by design
  (calloc(1,...)) it conservatively KEEPS the fb_width-loop FPs — safe, never masks a real OOB.
- Net: harness-refiner = trustworthiness keystone (can't demote a real bug); Phase 2a does the
  irq numeric demotion. Both shadow-gated. Plan file to be annotated with this.

## Next (this line, in order)
1. Wire the refiner: `DynamicValidator.refine_and_revalidate(...)` (clean-recompile to find undefined
   externs → materialize → re-run) + a SHADOW pipeline hook behind a new
   `--harness-refinement {off,shadow,live}` flag (default off). Shadow logs WOULD-demote/keep only.
2. Shadow-run irq + vfs; verify `vfs_open_handle` re-crashes on the materialized buffer (KEPT) and
   no real bug is demoted. (This is the Phase 1 GATE, soundly scoped.)
3. Then Phase 2a (harness_kind → formal_reach) in shadow; re-shadow irq for the numeric demotion.

## Decisions for the user
- None blocking. NOTE for the morning: the plan's Phase 1 gate ("re-shadow irq → wsod_* no longer
  confirmed") is partly Phase-2a's job, not the harness-refiner's, per the finding above. I am
  proceeding soundly (refiner = safe NULL-deref channel; 2a = irq numeric demotion) and will keep
  both shadow-only. Will STOP at Phase 4 for explicit OK.
