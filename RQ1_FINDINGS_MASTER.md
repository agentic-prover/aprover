# RQ1 / bug-finding evaluation — master findings (honest)
(All code changes below are UNCOMMITTED on the working tree at main unless noted. CODEX-only; claude-code disabled by user.)

## 1. CBMC-hard discovery (agent-load-bearing benchmark)
308 SV-COMP tasks (228 bug/80 safe), plain CBMC unwind 64. Bug recall 147/228 (64.5%), 0 false negatives (sound within bound); safe proved 20/80 (25%); 8 FPs (arch). CBMC-HARD-BUG subset = 81 (48 unknown/bound + 33 timeout/scale). Classifier verified faithful.

## 2. Step-2 recall (CORRECTED — this is the key honesty item)
On 81 CBMC-hard bugs (plain CBMC@64 = 0/81): reported bmc-agent/codex 19, CPAchecker 25, ESBMC 15. BUT re-scoring by validation reproducibility: **0/19 codex finds are validation-confirmed** — they were frame_havoc OVER-APPROXIMATION artifacts (dynamic validator does not run on SV-COMP whole-programs -> dynamic_ran=False -> "unreproducible"), counted only because the disposition used the STATIC is_real_bug (havoc-tolerant) and triage was off. So the "19/81 beats ESBMC/CPAchecker" was UNSOUND. Sound confirmed recall on the deep-bug unsafe set = ~0.

## 3. Soundness fix (wired): faithful re-confirmation gate
The frame_havoc->faithful-scope_from_entry re-confirmation existed but was --svcomp-gated. De-hardcoded to STRUCTURAL (fires on frame_havoc + unreach). Now a frame_havoc reach_error candidate is held provisional, faithfully re-checked, counted only if reproduced. Verified: 4/4 deep-bug frame_havoc finds -> dropped (0 confirmed). Sound.

## 4. Loop-contract abstraction (SOUND new capability)
bmc_agent/loop_contracts.py (new): synthesize loop invariant -> __CPROVER_loop_invariant -> goto-instrument --apply-loop-contracts -> cbmc --unwind 1 --unwinding-assertions. PROVES unbounded SAFE loops CBMC fundamentally can't. Two-phase (goal-directed prove / goal-free) + CEx validation. Wired into PlanAgent (loop_contracts strategy + scope_from_entry ladder) + cli dispatch + unwinding-wall fallback. Mixed set: 3/4 safe proved (down, NetBSD, count_up_down-1; count_by_1 unknown = invariant-synth miss). Sound by construction. Bottleneck = invariant-synthesis quality, NOT safe/unsafe (loop abstraction is symmetric — can find bugs too, proven mechanically).

## 5. Deep-bug unsafe — how to improve (current thread)
Diagnosis: the CBMC-HARD "unknown" bugs are genuinely deep (e.g. array[100000] sorts, witness ~1e5 iterations) -> no unrolling reaches them. Sound confirmed recall via BMC = ~0.
- #1 faithful adaptive unwind sweep (no --unwinding-assertions): 8/48 recovered SOUNDLY (loop-acceleration@4096, recursion@100-1000). codex-free. LLM-guided-unwind added (bug_hunt.llm_unwind_bounds) but marginal here (misses are infeasible-depth).
- Concrete-exec witness (LLM guesses triggering input + runs): CEILING 34/40 sound (real crash). USER PIVOTED AWAY from this (prefers LLM-guided symbolic BMC, not guided fuzzing).
- LLM-guided BMC (chosen direction): levers = adaptive unwinding + spec synthesis (loop_contracts) + compositional. Honest TRADEOFF: symbolic recall << 34/40 concrete ceiling, because it needs a synthesizable INDUCTIVE loop summary (hard for full sorts) whereas concrete-exec only needed an input guess.
- bug_hunt.py: faithful_unwind_sweep + hunt_witness (concrete, now UNWIRED from ladders) + llm_unwind_bounds. Deep-bug ladder = [unwind_sweep, loop_contracts].

## KNOWN WIRING GAP (documented, not fixed)
Large programs start at frame_havoc (cost>budget); the new unwind_sweep/loop_contracts levers hang off the scope_from_entry ladder, so they DON'T fire for large programs (frame_havoc->scope_from_entry confirmation has empty ladder -> dead-ends at unknown). To exercise the levers on large programs, the frame_havoc/confirmation path needs its own ladder to [unwind_sweep, loop_contracts]. (bubblesort_2 confirmed this: went frame_havoc->confirm->unknown, never hit the levers.)

## Other (uncommitted): compositional-for-complex-memsafety plan change + ab_memsafety A/B; committed 39dbd0d8f = latent->"unknown" label for reach runs.
## Scheduled task step2-hardbug-score writes per-experiment FINDINGS for step2/ab_memsafety/bound_lc2.
