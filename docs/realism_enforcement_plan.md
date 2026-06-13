# Plan: Enforce realism on all artifacts under `--agentic`

Status: **PHASE 0 DONE** (2026-06-13). Baseline oracle frozen in
`findings/autonomous_realism/baseline_oracle.md`. Phase 1 in progress. Resume by reading this
file + the oracle + `git log` for phase commits.

## Goal
Under `--agentic`, make the realism verdict **bite on dynamic findings too** (remove
`confirmed_dynamic` immunity) — but only after realism is trustworthy enough to do so.
Trustworthiness comes from giving an UNREALISTIC verdict a **harness-refinement** outcome (C)
and tool-grounding the judgment. **Shadow-first; no default/immunity flip without explicit user OK.**

## Scope facts (verified in code)
- Config is `--agentic` (`cli.py:158-196`): realism **ON but lightweight** (single LLM call,
  non-tool). Tool-use (`--enable-realism-tools`) and a Claude-Code realism backend are **opt-in**,
  introduced in Phase 2. Also on under `--agentic`: dynamic validation, soundness gate,
  agentic harness-repair, classifier. Triage OFF.
- Realism **runs** on all findings today (`pipeline.py:1861`, `_make_report`), but its verdict is
  **suppressed for dynamic** via the immunity gate (`bug_reporter.py:211-238`):
  `_immune = confidence=="confirmed_dynamic" and not _harness_assertion and not _internal_unreachable`.
  "Enforce realism on all" = make the verdict **bite** on dynamic findings.
- Realism outcomes after a verdict:
  - REALISTIC -> keep `confirmed_*`.
  - UNCERTAIN -> feedback loop / spec-refiner may run; else kept.
  - UNREALISTIC -> one of FOUR: **A** feedback `__CPROVER_assume` clause (`pipeline.py:~2634`),
    **B** soundness-gated spec refiner (`pipeline.py:~2847`), **C** HARNESS refinement (MISSING — Phase 1),
    **D** plain downgrade to `unlikely` (`bug_reporter.py:223-238`).

## The two blocker FP flaws (proven from shadow data)
1. `evidence_strong` keys on `harness_kind=system_entry`, but the system-entry reproducer crashes on
   the SAME uninitialized init-trusted global (`fb_base=NULL`) as the unit harness -> zero reachability
   info -> every wsod FP gets ev=strong -> confirmed. Fix = drop harness_kind, use formal CBMC
   `system_entry_reached` only.
2. NULL-init-trusted-global artifact class is unmodeled (channel-guard sees the `fb_base` write ->
   `internal` -> keep). cf. `b4aa03c` materialized init-trusted NULL globals for CBMC. Fix = harness
   refinement (materialize trusted globals) OR classify boot-init-global NULL-deref as not-reachable.

## Phases (task list mirrors these: Phase 0-4)

### Phase 0 — Baseline lock (no code)
Freeze regression oracle: irq/vfs over-confirm result; cross-codebase 0/7 demoted
(libredwg/openjpeg/libtiff/brotli); VibeOS 0/8 reals demoted; `vfs_open_handle`/`ip_handle`
always kept.

### Phase 1 — Harness-refinement outcome C (KEYSTONE; makes enforcement safe)
- 1a. Branch in the realism-verdict consumer: if `key_concern` names a NULL-init-trusted-global or
  nondet unit-arg artifact -> route to a new `harness_refiner` (not the spec-clause loop).
- 1b. `materialize_trusted_globals()` — init boot-set globals (`fb_base`) in the dynamic harness
  (like `b4aa03c` for CBMC); re-run the dynamic validator.
- 1c. Decide from re-run: refined harness no longer crashes -> artifact -> demote honestly;
  still crashes -> real -> keep `confirmed`.
- GATE (shadow): re-shadow irq/vfs -> `wsod_*` no longer confirmed, `vfs_open_handle` still confirmed,
  0 reals lost.

### Phase 2 — Trustworthy reachability evidence + tool-grounding
- 2a. Drop `harness_kind` from evidence axis in `_maybe_ground_immunity` (pipeline.py):
  `evidence_strong = formal_reach` (CBMC `system_entry_reached` only).
- 2b. Route realism through the tool-enabled path (`check_with_tools_if_enabled`,
  `--enable-realism-tools`) so it reads init/caller code for the NULL-init-global judgment;
  optionally route the `"realism"` role to a capable agentic backend (sonnet-4.5+/Claude-Code,
  NOT the churny subscription path).
- GATE (shadow): cross-codebase 0/7, VibeOS 0/8 unchanged; no REALISTIC->UNREALISTIC flip on a real bug.

### Phase 3 — Enforce realism on dynamic (shadow, end-to-end)
Run uniform on irq + vfs + one OSS target with Phases 1+2 in place; the dynamic verdict now bites.
- GATE: `wsod_*` -> unlikely/dropped; `vfs_open_handle`/`ip_handle`/OSS OOB-readers -> confirmed/likely;
  ZERO real-bug demotions across all five codebases.

### Phase 4 — Decision point (EXPLICIT USER OK REQUIRED)
Only if 1-3 gates green: (a) make enforcement default under `--agentic`, and/or (b) delete the
`confirmed_dynamic` immunity special-case. Do NOT flip either autonomously.

## Carried gates (every phase)
cross-codebase 0/7 demoted · VibeOS 0/8 reals demoted · `vfs_open_handle`/`ip_handle` always kept.
Any real-bug demotion stops the line.

## Key files
- `bmc_agent/pipeline.py` — `_make_report` (realism invoke ~1861), `_maybe_ground_immunity`,
  feedback loop (~2634), spec refiner (~2847).
- `bmc_agent/bug_reporter.py` — immunity gate + downgrade (~211-238).
- `bmc_agent/realism_checker.py` — `check()`, `check_with_tools_if_enabled()` (~584).
- `bmc_agent/reachability_grounding.py` — channel-guard + grounded reachability.
- `bmc_agent/dynamic_validator.py` — harness build + `harness_kind`, `system_entry_reached`.
- `bmc_agent/cli.py` — `--agentic` block (158-196), `--reachability-grounding {off,shadow,live,uniform}`.

## Standing constraints
Do NOT make uniform/enforcement default, do NOT delete immunity, do NOT change `--agentic` default
without explicit user OK. Commit messages end with the Co-Authored-By trailer.
