# PLAN: BMC-Agent autonomous mode

**Goal:** an `--autonomous` mode that keeps running on a target until
results are useful (or genuine convergence). When the sweep produces
CBMC parse errors, the loop diagnoses + applies fixes (to bmc-agent
itself, where structurally possible) and re-runs. When the sweep
produces false-positive bug findings, the loop refines specs / the
realism prompt / per-function constraints and re-runs.

## What already exists (partial autonomy)

* **`feedback_loop.py`** — given an `UNREALISTIC` realism rejection,
  distills the reason into a learned constraint (callee relaxation,
  function-post relaxation, code-change TODO). Persists to
  `<output>/learned_constraints.json`. Subsequent sweeps consume the
  constraints automatically.
* **`--enable-feedback-loop`** + **`--feedback-max-iters`** —
  in-sweep convergence on a single function: re-run CBMC under a
  tighter precondition until verified clean, REALISTIC, or the same
  CE class repeats.
* **Two-pass call-graph + Phase 3c caller-propagation** — already
  iterates within a sweep.

What's missing for true "keep going until results are good":

1. **CBMC-error autonomy.** When CBMC exits with parse/convert errors
   on ≥50% of functions in a file, the current system flags the file
   as BLOCKED and moves on. That's where the libarchive sweep got
   stuck: 4829 / 4842 functions parse-failed, the system reported
   "0 bugs" instead of trying to fix the harness-gen layer.

2. **Sweep-level convergence.** After a sweep finishes, decide
   whether to run another with adjusted knobs (more strict prompts,
   different threat model, scaled-down arrays, etc.) or stop. No
   loop today.

3. **Self-patching.** When the diagnostic narrows a CBMC error to a
   structural harness-gen bug (e.g. "stripped typedef X has
   declarations Y, Z, W still referencing it"), the system should
   propose a code patch to the harness-gen. Today this requires a
   human (me, this session) to read the error and edit the file.

## Proposed phases

### Phase 1: CBMC-error classifier + auto-retry registry (LOW RISK)

**Scope:** structural fixes for *known* error patterns, no AI
self-patching.

* Add `bmc_agent/cbmc_error_classifier.py` — parses
  `cbmc_result.json` raw output, classifies into a finite taxonomy:
  - `parse_undefined_typedef` (extract identifier)
  - `parse_incomplete_type` (extract struct/union tag)
  - `convert_type_redefinition` (extract type name)
  - `convert_body_redefinition` (extract struct/union tag)
  - `convert_undefined_identifier`
  - `out_of_memory`
  - `unknown`

* Add `bmc_agent/auto_retry_registry.py` — for each error class, a
  *known recovery action* implemented as a flag-change or
  harness-regen with a different strategy:
  - `parse_incomplete_type X` → regen harness with X-param as nondet
    pointer (already the new default after 655cf1f; this becomes the
    fallback when the bug recurs on a future codebase).
  - `convert_body_redefinition Y` → add Y to a *runtime* known-glibc
    set, regen.
  - `parse_undefined_typedef Z` → check if Z is in
    `_SYSTEM_TYPEDEF_NAMES`; if not, add to a session-local strip
    set, regen.

* Pipeline change: after Phase 2, if a function CBMC-errored AND its
  error class is in the registry, regen harness with the registered
  workaround and re-run CBMC. Bounded retry: max 2 attempts per
  function.

**Why low risk:** every recovery action is a hand-coded transformation,
not LLM-generated code. The registry grows by human-authored entries
(or by AI proposing entries that a human reviews — Phase 3).

**Stopping condition:** sweep ends when every function has either a
verdict or has exhausted retry attempts.

**Estimated effort:** ~400 LOC + tests. Could land in 2-3 commits.

### Phase 2: Continuous-mode CLI (`--autonomous`) (MEDIUM RISK)

**Scope:** outer loop around verify-dir.

* New CLI: `bmc-agent autonomous --source-dir … --max-rounds N
  --budget-usd X --target-coverage Y`
* Each round:
  1. Run verify-dir with current knobs.
  2. Summarize: per-file CBMC success rate, REAL_BUG count after
     realism, UNCERTAIN count, UNREALISTIC count.
  3. Check convergence:
     - Coverage ≥ `target_coverage`? → done.
     - Same #bugs and #errors as previous round? → converged, done.
     - Out of API budget? → done.
     - Out of rounds? → done.
  4. Otherwise, adjust knobs for next round:
     - If many parse errors and Phase 1 retries are exhausted:
       escalate (Phase 3 self-patch loop, if enabled).
     - If many UNCERTAIN realism verdicts: bump
       `--enable-realism-thinking`.
     - If many REAL_BUGs that look like caller-contract slips
       (detected by witness-pattern: NULL function pointer in
       `*->ops->fn`): inject a session-local hint into the realism
       prompt template.
* Output: `<output>/autonomous/round_<N>.json` per round + a
  cumulative `summary.md`.

**Why medium risk:** the loop is straightforward, but the knob-
adjustment heuristics need empirical calibration on real targets.

**Estimated effort:** ~700 LOC + tests + a calibration sweep on 2-3
codebases.

### Phase 3: AI self-patch loop (HIGH RISK)

**Scope:** when the CBMC error is structural and not in the
auto-retry registry, an LLM agent proposes a patch to
`bmc_agent/harness_generator.py` (or `preprocessor.py`), and the loop
runs the test suite, applies the patch on a branch, re-runs the
sweep, and commits the patch only if (a) tests pass, (b) sweep
coverage improves by ≥ a configurable threshold.

* New module: `bmc_agent/self_patch_agent.py` — wraps the Claude
  Agent SDK. Tools available to the agent:
  - `read_file(path)`, `grep(pattern)`, `read_cbmc_error(func)`.
  - `propose_patch(path, old, new)` — produces a unified diff.
  - `run_tests()` — runs the project's pytest.
  - `run_sweep_subset(files)` — re-runs a subset under the proposed
    patch and reports coverage delta.
  - `commit_and_push()` — gated on a configurable
    `--allow-self-patch=auto|stage|deny` flag.

* Safety gates:
  - Patch must touch ≤ N files, ≤ M lines (configurable).
  - Patch must come with a regression test that fails before and
    passes after.
  - Patch must not weaken any existing test.
  - Patches outside `bmc_agent/harness_generator.py` and
    `bmc_agent/preprocessor.py` require explicit allow-list.
  - `--allow-self-patch=stage` (default) writes patches to
    `<output>/proposed_patches/round_<N>.diff` without applying;
    `=auto` applies + commits; `=deny` reports without proposing.

**Why high risk:** AI editing the tool that produces the verification
verdicts can introduce subtle correctness bugs (e.g. a "fix" that
strips a typedef the verification depends on, producing false
negatives instead of false positives). Mitigations: the safety gates
above, and a separate `--soundness-tripwire` mode that re-runs the
existing test corpus to detect regressions.

**Estimated effort:** ~1500 LOC + extensive testing on a curated
"known-good" sweep corpus.

### Phase 4: FP-driven prompt evolution (MEDIUM-HIGH RISK)

**Scope:** when post-realism FPs share a common pattern (e.g. the
caller-contract-slip pattern from the libarchive RB-tree findings),
inject a learned hint into the realism prompt for the next round.

* New module: `bmc_agent/realism_prompt_evolver.py`.
* Inputs: prior round's REAL_BUG findings whose CEx state matches a
  known FP pattern (NULL function pointer in `*->ops->*`,
  uninitialized container, etc.).
* Output: a 1-2 paragraph "additional skepticism hint" appended to
  the realism prompt for the next round, persisted to
  `<output>/learned_realism_hints.md`.
* The hint format is constrained (no free-form additions to the
  prompt's decision rules) so the soundness of the prompt's
  REALISTIC/UNREALISTIC/UNCERTAIN decision logic is preserved.

**Estimated effort:** ~500 LOC + tests + a calibration pass.

## Convergence / stopping criteria

The autonomous loop stops when ANY of:
1. Per-file CBMC coverage ≥ target (default 80%).
2. Two consecutive rounds produce the same {confirmed bugs, error
   count, FP count} fingerprint (true fixed-point).
3. API budget exhausted (counted via per-call usage).
4. Wall-clock cap reached.
5. Max rounds reached (default 5).

## Open questions (before implementing)

* **Q1: Should self-patching (Phase 3) be in scope?** The libarchive
  session needed exactly that — when bmc-agent's harness-gen has a
  bug, a human needs to read the CBMC error and patch
  `harness_generator.py`. Phase 3 automates that with all the safety
  gates above, but it's the highest-risk piece.

* **Q2: Default mode = stage or auto?** Even if Phase 3 lands,
  default behaviour should probably be `stage` (write proposed patches
  to disk, don't apply) so a human reviews periodically. `auto` would
  be opt-in for trusted-target sweeps.

* **Q3: Budget enforcement granularity.** Per-round? Per-sweep?
  Mid-LLM-call (abort if budget exhausted)? The leaked-key incident
  showed budget concerns matter; the autonomous loop needs a hard
  ceiling.

## Implementation order

If approved, suggested order:
1. **Phase 1** (CBMC-error classifier + retry registry) — lands the
   most coverage delta with the least risk. Validates the
   classification taxonomy on the libarchive sweep before any LLM
   work.
2. **Phase 2** (`--autonomous` outer loop) — once Phase 1 is solid,
   the outer loop is mostly orchestration.
3. **Phase 4** (FP prompt evolution) — bigger structural impact than
   Phase 3 with much lower correctness risk.
4. **Phase 3** (self-patch agent) — last, behind `stage` default,
   only after Phases 1+2+4 land.

Estimated total: ~3000 LOC + thorough testing. Realistic delivery
in 4-6 phased commits.
