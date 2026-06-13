STATE: RUNNING
Phase: 1 (harness-refinement outcome C) — Phase 0 DONE.

## Just did (iter 1)
- Phase 0 baseline lock (no code). Froze the regression oracle in
  `findings/autonomous_realism/baseline_oracle.md` from completed runs already on disk:
  - vfs (phase1_regress_vfs2): `vfs_open_handle` confirmed_dynamic ×3 = REAL keep-anchor; reals listed.
  - irq (step3_shadow_irq = baseline verdicts): FPs over-confirmed = `wsod_draw_line`,
    `wsod_draw_sad_mac`, `sleep_ms`; the wsod SEMANTIC set already `unlikely`.
  - Identified the residual GAP from step4_live_irq: existing uniform/live tier model already
    demotes `wsod_draw_sad_mac`, but `wsod_draw_line` + one `sleep_ms` are STILL confirmed —
    that is the `fb_base=NULL` NULL-init-trusted-global class Phase 1+2 must close.
- Committed Phase 0 (oracle + plan status + this file) to main and pushed.

## Test/gate results
- Phase 0 GATE: oracle frozen and self-consistent against on-disk completed runs. PASS (no code).
- Safety anchors confirmed present in baseline: `vfs_open_handle` confirmed_dynamic. OK.

## Next (Phase 1)
- 1a. Branch in the realism-verdict consumer (bug_reporter.py / pipeline.py): when key_concern names
  a NULL-init-trusted-global or nondet unit-arg artifact, route to a NEW `harness_refiner`
  (shadow/logging first — no verdict change), gated behind a flag.
- 1b. `materialize_trusted_globals()` — init boot-set globals (`fb_base`) in the dynamic harness
  (mirror b4aa03c which did this for CBMC); re-run the dynamic validator.
- 1c. Decide from re-run; GATE = re-shadow irq so `wsod_draw_line`/`sleep_ms` drop, vfs_open_handle kept.

## Decisions the user must make
- None yet. Will STOP at Phase 4 (default/immunity flip) for explicit OK.
