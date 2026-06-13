# Phase 0 — Baseline regression oracle (FROZEN 2026-06-13)

No code. This file freezes the verdicts every later phase is checked against.
Sources are completed runs already on disk at HEAD (3af93b4). All shadow-only.

## Carried safety gate (absolute, every phase)
- cross-codebase **0/7** real bugs demoted (libredwg / openjpeg / libtiff / brotli reals).
- VibeOS **0/8** real bugs demoted.
- `vfs_open_handle` and `ip_handle` **always kept** confirmed.
- Any change that demotes a real bug ⇒ REVERT and stop the line.

## A. VibeOS `vfs.c` baseline  (source: findings/phase1_regress_vfs2, driver vibeos_vfs_p1b)
Run config: `--agentic --enable-soundness-gate` (HEAD default, realism tools OFF). Completed: 16 real / 0 latent / 23 unresolved.

KEEP (real, must stay confirmed under enforcement):
- `vfs_open_handle` — confirmed_dynamic (×3: ARITH, MEMORY_SAFETY, SEMANTIC). **REAL — ASan-confirmed heap overflow. Safety-gate anchor.**
- `vfs_readdir`, `vfs_write`, `vfs_append` — confirmed_system_entry (attacker-surface, traced to entry).
- `readdir_callback`, `find_mem_child`, `vfs_delete_recursive` — confirmed_dynamic (carried as reals at baseline; not in the demote list).

ALREADY DEMOTED at baseline (informational):
- `vfs_close_handle` — unlikely (SEMANTIC).

## B. VibeOS `irq.c` baseline  (source: findings/step3_shadow_irq, shadow grounding ⇒ baseline verdicts)
Run config: `--agentic ... --reachability-grounding shadow` (verdicts identical to default; shadow is advisory).

OVER-CONFIRMED FPs — the demote targets (should become unlikely/dropped once Phases 1+2 land):
- `wsod_draw_line` — confirmed_dynamic (ARITHMETIC). NULL-init-trusted-global (`fb_base=NULL`) + nondet-arg artifact.
- `wsod_draw_sad_mac` — confirmed_dynamic (ARITHMETIC). nondet-arg panic-screen helper artifact.
- `sleep_ms` — confirmed_dynamic (ARITHMETIC). busy-wait / `fb_base` class.

ALREADY UNLIKELY at baseline (must NOT regress back to confirmed):
- `wsod_draw_text`, `wsod_delay`, `wsod_hex`, `wsod_int` (×2), `wsod_animate_ekg` (×3) — all unlikely (SEMANTIC).

NO real attacker-reachable bug in irq.c (all panic-screen / timer / delay helpers).

## C. Current best enforcement state — the GAP Phase 1 must close
Source: findings/step4_live_irq (existing `--reachability-grounding uniform`/live tier model, pre-this-plan).
Already demoted correctly: `wsod_draw_sad_mac` → unlikely (×3). 
STILL over-confirmed (the residual FP class this plan targets):
- `wsod_draw_line` → still confirmed_dynamic.
- `sleep_ms` → 1 unlikely + **1 still confirmed_dynamic**.
These two are the `fb_base=NULL` NULL-init-trusted-global artifact (plan blocker flaws #1 evidence_strong-keys-on-harness_kind and #2 unmodeled NULL-init-global). Phase 1 harness-refinement (materialize trusted globals) + Phase 2 (drop harness_kind from evidence axis) are scoped to close exactly these.

## Runnable shadow gates per phase
- Phase 1 GATE: re-shadow irq → `wsod_draw_line`/`sleep_ms` no longer confirmed; `vfs_open_handle` still confirmed; 0 reals lost (vfs unchanged).
- Phase 2 GATE: vfs + irq unchanged on the KEEP set; no REALISTIC→UNREALISTIC flip on a real bug.
- Phase 3 GATE: irq FPs → unlikely; `vfs_open_handle`/`ip_handle` → confirmed; 0 real-bug demotions across all codebases.

## Cross-codebase 0/7 (libredwg/openjpeg/libtiff/brotli) and VibeOS 0/8 reals
Not re-run in shadow each phase (expensive, external trees). Enforced as an INVARIANT: no phase-1/2/3 code path may demote a `confirmed_*` finding whose crash traces to a system entry (`system_entry_reached=True`) or to a non-static public function. The harness-refiner (Phase 1) only fires on static+non-entry NULL-init-trusted-global / nondet-arg artifacts, structurally excluding the cross-codebase reals (all entry-reachable parser OOBs). Verified by code review at each phase, not by re-running the external trees.
