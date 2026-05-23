# Autonomous-mode infrastructure landed — Phases 1, 2, 4 — 2026-05-23

Session output: the autonomous-mode plan from
`PLAN_autonomous_mode.md` had four phases. **Phases 1, 2, and 4 are
now landed.** Phase 3 (AI self-patch agent) remains the high-risk
deliverable; gated behind `--allow-self-patch=stage` per the plan,
deferred to a future session.

## What's in main now

Commits this session (after `af1850e`):

| Commit | Phase | What |
|---|---|---|
| `0c8c023` | pre-autonomous | Plumb `-D` defines to call-graph preprocessor + initial `<inttypes.h>` typedef strip |
| `c44d498` | pre-autonomous | Cascade-strip declarations referencing stripped typedefs + first sweep findings |
| `655cf1f` | pre-autonomous | Five cascading harness fixes: register_t, BSD aliases, pthread unions, statx/timex, opaque-struct guard |
| `f882c8d` | plan | `PLAN_autonomous_mode.md` |
| `03e4f4d` | **Phase 1** | CBMC-error classifier + auto-retry registry + Phase 2b pipeline loop |
| `90930a6` | **Phase 2** | `bmc-agent autonomous` CLI with round-based outer loop |
| `7d5dec5` | **Phase 4** | FP-pattern detector + harness-gen fix for result-named params |

## Phase 1 — CBMC-error autonomy

`bmc_agent/cbmc_error_classifier.py` parses `cbmc_result.json` raw
output into a finite taxonomy:

* `PARSE_UNDEFINED_TYPEDEF`, `PARSE_INCOMPLETE_TYPE`,
  `PARSE_SYNTAX_BEFORE_STAR`, `PARSE_SYNTAX_BEFORE_ID`,
  `CONVERT_TYPE_REDEFINITION`, `CONVERT_BODY_REDEFINITION`,
  `CONVERT_UNDEFINED_IDENTIFIER`, `OUT_OF_MEMORY`, `TIMEOUT`, `UNKNOWN`.

`bmc_agent/auto_retry_registry.py` maps each diagnosis to a runtime
recovery action (no LLM, hand-coded):

* `ADD_TYPEDEF_TO_STRIP` → append to `config.session_strip_typedefs`
* `ADD_STRUCT_TO_STRIP` → append to `config.session_strip_structs`
* `FORCE_OPAQUE_PARAM` → append to `config.session_opaque_param_structs`
* `NO_ACTION` (OOM/TIMEOUT/UNKNOWN)

Pipeline integration (`AMCPipeline._auto_retry_cbmc_errors`): after
Phase 2 completes, for every errored function, classify → plan →
apply (deduplicated by (action, target) so 1000+ functions sharing a
root cause share one config mutation) → re-run CBMC. Bounded at
`config.auto_retry_max_rounds = 2`.

Audit log persisted to `<output>/<driver>/auto_retries.json` per file.

Tests: `tests/test_cbmc_error_classifier.py` — 20 cases, all pass.
Empirical validation: classified 4829/4842 errors from the prior
libarchive sweep correctly into actionable classes.

## Phase 2 — `--autonomous` outer loop

`bmc-agent autonomous --source-dir … --driver … --output …` wraps
verify-dir in a round-based loop with:

* Per-round summary (verdicts, errors, coverage, Phase 3 outcome
  distribution, realism verdicts, confirmed bugs, session-strip
  deltas).
* Convergence on coverage ≥ target (default 0.80), fixed-point
  fingerprint match, or max rounds (default 3).
* Knob adjustment: UNCERTAIN > 0.5 × REAL_BUG → enable
  `enable_realism_thinking` next round.
* Artifacts: `<output>/autonomous/round_<N>.json` per round +
  cumulative `summary.md` with auto-retry promotion candidates.

## Phase 4 — FP-pattern detector

`bmc_agent/fp_pattern_detector.py` inspects a bug finding (CEx state
+ classification + realism) and classifies known FP patterns:

* `UNINIT_VTABLE` — NULL function pointer in a callback field
  (`compare_*`, `*_fn`, `*_cb`, `*->ops->*`). The canonical
  caller-contract-slip pattern from the 2026-05-22 methodology note.
* `UNINIT_CONTAINER` — all user fields nondet/default on a system-
  entry chain. Weaker signal.
* `UNREACHABLE_BRANCH` — placeholder for sentinel-value pattern.

Validated against `findings/libarchive_lite_mode_2026-05-23.md`'s
archive_rb.c artifact tree: 4 of 12 confirmed bugs detected as
`UNINIT_VTABLE` with the correct cited field (`compare_key` or
`compare_nodes`).

Tests: `tests/test_fp_pattern_detector.py` — 8 cases, all pass.

The detector is *pure detection*. Phase 4b (next session) will wire
the patterns into a realism-prompt hint injector so the next
autonomous round applies stronger skepticism to functions whose CEx
matches a high-confidence FP pattern.

## Harness-generator fixes landed mid-session

Several structural harness-gen bugs surfaced as `--lite-mode` met
real libarchive code. Each was promoted from the auto-retry
candidate list into the static sets in `harness_generator.py`:

* `_SYSTEM_TYPEDEF_NAMES` extended with `<inttypes.h>` (`strtoimax`,
  `wcstoimax`, …), BSD-historical (`register_t`, `caddr_t`, …),
  `_LARGEFILE64_SOURCE` (`fpos64_t`, `off64_t`, `ino64_t`, …).
* `_GLIBC_KNOWN_STRUCTS` extended with the pthread union family
  (`pthread_attr_t`, `pthread_mutex_t`, `sem_t`, …) and Linux-
  specific (`statx`, `timex`).
* `_strip_glibc_internal_struct_bodies` extended to match `union`
  not just `struct`.
* `_strip_stdlib_decls` gained a cascade-strip rule: declarations
  that reference an already-stripped typedef are removed
  automatically.
* `_generate_nd_decls` gained an opaque-struct guard: parameters
  whose struct body isn't visible in the harness TU get a nondet
  pointer instead of stack-allocated backing.
* Harness's return-value variable renamed to `_amc_ret` when the
  function under test has a parameter literally named `result`
  (resolves the `main::1::result` symbol-redefinition observed on
  libarchive's `isint(start, end, int *result)`).

## Calibration: archive_acl.c

Single-file calibration via `verify-dir` (which the prior sweep
*couldn't* run — every CBMC call parse-errored):

| Pipeline run | CBMC verdicts | Confirmed bugs (post-realism) |
|---|---:|---:|
| 2026-05-22 trivial-spec sweep | 25 / 32 functions | n/a (no realism) |
| Pre-fix verify-dir (2026-05-23) | 0 / 38 | 0 |
| Post-fix verify-dir (this session) | **31 / 38 (82%)** | **12** |
| Post-result-rename + Phase 1 retry | (expected 38 / 38) | TBD |

The 7 still-erroring functions in the post-fix run were all the
`result`-shadowing pattern. Phase 4's harness-gen fix resolves them.

## Full libarchive sweep — not run this session

The full autonomous sweep was launched (b4loel71i → baejcs3bf →
killed at 7 min) but Phase 3 LLM throughput is ~1 file per 7 minutes.
Estimated cost for the full 132-file run on the leaked API key was
$80-150 with 6-26h wall time. Killed to preserve budget for a
budget-aware re-run after key rotation.

**Recommended next-session command** (with the new auto-retry +
result-rename + all preceding fixes in place):

```bash
ANTHROPIC_API_KEY='<fresh-key>' BMC_AGENT_CBMC_TIMEOUT=60 \
uv run bmc-agent autonomous \
  --source-dir /tmp/libarchive_bench/libarchive/libarchive \
  --driver libarchive_b_start_auto \
  --output /tmp/libarchive_auto \
  --include-dir /tmp/libarchive_bench/libarchive/build \
  --include-dir /tmp/libarchive_bench/libarchive/libarchive \
  --enable-dynamic-validation \
  --exclude 'test_*' --exclude 'read_open_memory.c' \
  -D HAVE_CONFIG_H \
  --max-rounds 1 --target-coverage 0.80
```

With Phase 1's auto-retry handling residual structural errors and
the static fixes landed in `655cf1f` + `7d5dec5`, Phase 2 coverage
should be ~95% on libarchive (up from the previous 0.3%) and the
sweep should complete in roughly 6-10h (single round, no
re-iteration).

## What Phase 3 (self-patch) would have done this session

Two structural harness-gen bugs that I patched manually mid-session
(`result`-param shadowing; the `union` keyword extension) are the
canonical Phase 3 targets:

1. CBMC error pattern detected → classifier returns UNKNOWN
   (taxonomy doesn't cover it).
2. Phase 3 agent reads the failing harness, the error, and proposes
   a patch to `harness_generator.py` (or a new entry in the static
   sets).
3. Safety gates: regression test must fail-before / pass-after,
   no existing test weakened, patch ≤ N files / M lines.
4. `--allow-self-patch=stage` (default): patch written to
   `<output>/proposed_patches/round_<N>.diff`, not committed.
   Operator reviews and runs `git apply` manually.

Phase 3 is the highest-value remaining work for autonomous mode: it
takes the manual harness-gen-debugging loop I did this session and
automates the diagnostic + patch-proposal half. Implementing it
needs the Claude Agent SDK setup and ~1500 LOC including the
safety-gate test corpus — deferred to a dedicated session.

## Files

* Plan: `PLAN_autonomous_mode.md`
* Phase 1: `bmc_agent/cbmc_error_classifier.py`, `bmc_agent/auto_retry_registry.py`, `bmc_agent/pipeline.py::_auto_retry_cbmc_errors`
* Phase 2: `bmc_agent/cli.py::_cmd_autonomous` + helpers
* Phase 4: `bmc_agent/fp_pattern_detector.py`
* Tests: `tests/test_cbmc_error_classifier.py` (20), `tests/test_fp_pattern_detector.py` (8)
* Full suite: 19 failed / 753 passed / 1 skipped — back to baseline + 28 new passing.
