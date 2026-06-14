# Plan: Enforce realism on all artifacts under `--agentic`

Status: **PHASE 0 DONE** (2026-06-13). Baseline oracle frozen in
`findings/autonomous_realism/baseline_oracle.md`. Phase 1 in progress. Resume by reading this
file + the oracle + `git log` for phase commits.

## Goal
Under `--agentic`, make the realism verdict **bite on dynamic findings too** (remove
`confirmed_dynamic` immunity) — but only after realism is trustworthy enough to do so.
Trustworthiness comes from giving an UNREALISTIC verdict a **harness-refinement** outcome (C)
and tool-grounding the judgment. **Shadow-first; no default/immunity flip without explicit user OK.**

## DESIGN PRINCIPLE (governs realism, reachability, soundness gate, reproducer)
**Determinism and soundness are orthogonal.** Soundness requires only one thing: *no real bug is
ever silently removed.* Therefore:

> **An agentic (non-deterministic) judgment may RE-TIER a finding's confidence, but only a
> deterministic/formal check or a self-verifying witness may DELETE a sound finding.**

Why: a finding's exclusion is a NARROWING — it can only hide bugs, never invent them, so the two
error directions are asymmetric. A wrong "safe to exclude" hides a real bug (soundness loss); a wrong
"keep it" only adds noise (precision loss). So we arrange every removal to rest on a justification
that cannot wrongly hide a bug:
- **DETERMINISTIC_VERIFIER** — a formal check proves the exclusion (CBMC re-verify under a clause that
  is ALSO shown to hold at every call site by a deterministic caller-check). May DELETE.
- **SELF_VERIFYING_WITNESS** — the artifact is its own proof: a reproducer that compiles and reproduces
  the exact fault, or a materialized-harness re-run. Generation may be non-deterministic; the compile+run
  is deterministic ground truth, so it cannot falsely accept (given the public-API + matching-property
  guards). May DELETE.
- **AGENTIC_JUDGMENT** — any LLM verdict (realism, reachability, spec-soundness). May only RE-TIER
  (lower confidence). MUST NEVER be the sole justification for removing a finding.

Enforced in code by `bmc_agent/soundness_policy.py` (tested). Current compliance: realism UNREALISTIC ->
downgrade to `unlikely` (re-tier, OK); harness-refiner live -> downgrade (re-tier, OK); reproducer ->
self-verifying (OK). **Open item:** the spec-refiner soundness gate currently rests on a non-deterministic
`SoundnessAgent` judgment whose approved clause can EXCLUDE a CEx (effective delete) without a
deterministic caller-check -> must become RE-TIER unless the clause is deterministically caller-checked.
(Wiring tracked as a Phase-2 task; validate against the regression oracle.)

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

> **Finding (2026-06-13, from code+CEx analysis during impl).** The irq residual over-confirms
> (`wsod_draw_line`, 1×`sleep_ms`) are held by blocker-flaw #1 (`evidence_strong` keys on
> `harness_kind`), so their numeric demotion is **Phase 2a's** job, not the harness-refiner's. Most
> irq FPs are nondet-arg signed-overflow CEx (x,y,ms→INT/UINT_MAX), already demoted by the uniform
> reachability tier. The harness-refiner is the **sound empirical demotion channel for the NULL-deref
> artifact class** (e.g. the vfs tree-model FP, b4aa03c): calloc(1,…) is the smallest non-NULL
> object, so a real OOB re-crashes and is kept — it can clean a NULL deref but never mask an overflow.
> Phase 1 gate is therefore read soundly: refiner KEEPS the fb_width-loop FPs (safe) and never
> demotes a real bug; the irq numeric demotion is verified under Phase 2/3.

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
- **2-REPRO. Reproducer agent (do FIRST — it may change 2a).** Today the system-entry reproducer is a
  ONE-SHOT LLM call (`scenario_reproducer.py:199`) that falls open to UNREPRODUCIBLE, losing reals that
  just needed a header/type fix. Build a tool-using `ReproducerAgent` (BaseAgent, pattern of
  `spec_gen_tools.py`): tools = read headers / call-chain / structs; loop compile->run->read-error->fix.
  Constrained by the existing `_reproducer_uses_public_api` guard (cannot fabricate a wrong-reason crash);
  the crash must match the CBMC property. OPT-IN flag. **Synergy:** driving the REAL init path initializes
  trusted globals (e.g. `fb_base`) -> the spurious NULL-deref disappears -> `harness_kind=system_entry`
  becomes reliable evidence again. RE-EVALUATE 2a in light of this (may rescue harness_kind instead of dropping it).
- 2a. Drop `harness_kind` from evidence axis in `_maybe_ground_immunity` (pipeline.py):
  `evidence_strong = formal_reach` (CBMC `system_entry_reached` only). **Conditional on 2-REPRO outcome** —
  if the reproducer agent makes system-entry evidence reliable, KEEP harness_kind but gate it on a
  real-init-path reproducer instead.
- 2c. Route realism through the tool-enabled path (`check_with_tools_if_enabled`,
  `--enable-realism-tools`) so it reads init/caller code for the NULL-init-global judgment;
  optionally route the `"realism"` role to a capable agentic backend (sonnet-4.5+/Claude-Code,
  NOT the churny subscription path).
- GATE (shadow): cross-codebase 0/7, VibeOS 0/8 unchanged; no REALISTIC->UNREALISTIC flip on a real bug.

### Phase 2b — Flag-selector tools-agent (independent quality lever; OPT-IN)
Today `FlagSelector` (`flag_selector.py:328`) + `InliningAdvisor` are SINGLE LLM calls on a 1500-char
truncated body; under `--agentic` they only get an investigation prompt framing, no real tools. Build
opt-in tool-using variants (BaseAgent + tool loop, like `triage_tools.py`) that READ real loop bounds /
array sizes / full callee bodies / headers to choose unwind, per-fn checks, and inline-vs-stub. Gated
like the other `_tools` agents; NOT default (latency cost; keep the paper's "only check-selection is the
agent" framing intact). GATE: no regression in confirmed-bug count or FP rate on the baseline oracle set.

### Phase 3 — Enforce realism on dynamic (shadow, end-to-end)
Run uniform on irq + vfs + one OSS target with Phases 1+2 in place; the dynamic verdict now bites.
- GATE: `wsod_*` -> unlikely/dropped; `vfs_open_handle`/`ip_handle`/OSS OOB-readers -> confirmed/likely;
  ZERO real-bug demotions across all five codebases.

### Phase 4 — Decision point (EXPLICIT USER OK REQUIRED)
Only if 1-3 gates green: (a) make enforcement default under `--agentic`, and/or (b) delete the
`confirmed_dynamic` immunity special-case. Do NOT flip either autonomously.

> **Note (2026-06-14):** Separately from this plan, the user explicitly authorized making the
> **`--agentic` stack itself the default** (`cli.py` `--agentic` is now `BooleanOptionalAction`,
> `default=True`; `--no-agentic` is the escape hatch to the plain core). This is the agentic-PRESET
> default, NOT the realism-enforcement flip: the `confirmed_dynamic` immunity is UNTOUCHED, so (a)/(b)
> above still require their own sign-off. Net effect: the realism-enforcement gates are now the
> default-path quality story rather than an opt-in one.

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
Do NOT make uniform/enforcement default, do NOT delete immunity without explicit user OK.
(The `--agentic` stack being default-ON was explicitly user-authorized 2026-06-14 and is now done;
the immunity/enforcement flip remains gated.) Commit messages end with the Co-Authored-By trailer.
