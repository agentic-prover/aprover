# Session summary — 2026-05-22 (autonomous mode)

User authorised an overnight autonomous session targeting "interesting
things with bmc-agent" after the 2026-05-21 sweep work. Four durable
infrastructure fixes landed and the bmc-agent hybrid pipeline ran across
the AWS Neuron driver — surfacing the bugs that drove three of the four
fixes in the first place.

## Headline deliverables

### bmc-agent infrastructure fixes (4 commits)

1. **Config: recognise `BMC_AGENT_LLM_API_KEY` env var** (`3a03126`).
   The hybrid env-file convention sets `BMC_AGENT_LLM_API_KEY` alongside
   `_BASE_URL` / `_MODEL` / `_PROVIDER`. Config only honoured
   `K2THINK_API_KEY` / `ANTHROPIC_API_KEY`, so any sweep that sourced
   `/tmp/aprover_hybrid_keys.env` got an empty key for non-spec_gen
   roles. The realism check (the feature that downgrades
   defensive-programming gaps from `real_bug` → `unrealistic`) was
   failing silently on every CEx with "No API key for OpenAI-compatible
   provider". Recognise the canonical name in both `resolved_api_key()`
   and `from_env()`. +2 tests.

2. **CBMC: cap raw_output and summarise struct-valued trace assignments**
   (`2ab4dcf`). Two pathological-blow-up paths surfaced while sweeping
   AWS Neuron preprocessed kernel TUs:
   - `CBMCResult.raw_output` stored the *full* `cbmc --json-ui` dump
     verbatim. On a kernel TU (preprocessed ~3MB, `struct neuron_device`
     with 28 fields, unwind=4), CBMC's trace JSON hit **9.18 GB**. That
     blob got serialised into `bug_report.json` (which grew to **264 MB**)
     and `classification.json` (211 MB). Downstream LLM prompts pulled
     from this state and overflowed OpenRouter's 8 MB request cap,
     silently failing the realism + reproducer LLM calls.
   - `_extract_counterexamples` used `str(rhs_value)` as the fallback
     when a CBMC trace step's value had no top-level `data` field. For
     struct assignments (`{'members': [{...}, ...]}`) this recursively
     stringified the nested state — megabytes per assignment for kernel
     structs. Same blow-up amplification.

   Fix: cap raw_output at 64 KB head + 4 KB tail with an elision marker;
   summarise struct/array values as `<struct: N members>` /
   `<array: N elements>`; last-ditch cap of 512 chars on unknown shapes.
   +3 tests.

3. **`.gitignore`: broader env / key / secret-file patterns** (`6c6d5ec`).
   Defense-in-depth so secrets can't accidentally enter the index even
   under non-`.env` filenames. Adds `.env.*`, `*.env`, `*_keys.env`,
   `*_secret*`, `*.secret`, `**/secrets.json`, `**/credentials.json`.
   Triggered by user request; history audit confirmed no key material
   had ever been committed.

4. **LLM: skip retry on HTTP 4xx** (`b7e53eb`). The retry classifier
   treated every LLMError as potentially transient. HTTP 4xx (bad-request,
   auth, request-too-large) is a permanent client error — retrying just
   burns 3 × exponential-backoff seconds. Concrete motivator: OpenRouter
   rejected oversized realism prompts (pre-`2ab4dcf`) with HTTP 400 and
   bmc-agent burned ~90s/CEx × 3 attempts. Now: an `is_4xx` check
   short-circuits the transient classifier, raising immediately. +1 test.

### Test suite

**684 passing, 2 skipped** (was 678 baseline). +6 net new tests:
- `test_bmc_agent_llm_api_key_env` / `_beats_k2think` — env-var fallback.
- `test_raw_output_capped_for_huge_cbmc_json` — CBMC dump cap.
- `test_struct_assignment_does_not_blow_up_variable_assignments`.
- `test_scalar_assignment_still_uses_data_field`.
- `test_http_4xx_does_not_burn_retries`.

### Hybrid-mode AWS Neuron sweep (8 files, 61 functions)

Files: `neuron_arch`, `_log`, `_topsp`, `_cinit`, `_module`, `_core`,
`_ds`, `_reset`. Full per-file table in
[`findings/aws_neuron_driver/hybrid_sweep_2026-05-22/README.md`](aws_neuron_driver/hybrid_sweep_2026-05-22/README.md).

| Metric | Value |
|---|---|
| Functions analysed | 61 |
| Verified clean | **21** |
| `real_bug` raw classifications | 10 |
| `spurious` (downgraded by classifier feasibility check) | 8 |
| `real_bug` after realism + feedback-loop filter | **4** |
| CBMC errors | 19 |

Of the 10 raw `real_bug` classifications, **6 were downgraded to
`unrealistic` by the in-sweep feedback loop** (the architecture from
`bmc_agent_session_2026-05-13.md`): bmc-agent distilled a learned
precondition constraint, re-ran CBMC with it applied, and the function
verified clean.

The 4 surviving candidates — `ts_nq_destroy`, `neuron_log_rec_add`,
`neuron_ds_release_pid`, `nr_stop_thread` — are all cleanup / lifecycle
functions where the LLM realism check failed due to the 8 MB OpenRouter
prompt-size cap (the cbmc.py blow-up was contributing). Manual
inspection of caller sites in `/tmp/aws-neuron-driver/` confirms they
follow the same defensive-programming-gap pattern as yesterday's
`ggml-alloc.c` sweep. **Net: 0 likely-true bugs in this sweep.**

Routing: started K2-hybrid; switched to all-OpenRouter when K2 went
HTTP 504 mid-sweep. K2 has since recovered.

### Phase-2 sweep launched (in progress)

K2-hybrid sweep on the four giant Neuron files (`cdev`, `dma`, `mempool`,
`metrics`) launched at 17:34 in the background. These benefit from
commits `2ab4dcf` (no GB-scale artifacts) and `b7e53eb` (no wasted retry
on the 8 MB cap). Results pending — will land in tomorrow morning's
state.

### Cleanup

Killed 6 orphan `cargo-kani` → `cbmc` process pairs from yesterday's
Rust fastrand sweep — they were deadlocked SAT-solving for 22+ hours
and consuming 6 CPU cores. Each parent was still alive but blocked on
its child's never-progressing SAT solver. Killing the parents
propagated to the children.

## Security posture (API keys)

User explicitly asked mid-session that API keys never be pushed.
Verified:
- `git log -G "sk-or-v1-7f73b577|IFM-iXqtRmi5M4B1thw4"` returns nothing
  — neither key value has ever appeared in repo history.
- `git ls-files | grep -E "tmp/|aprover_keys|hybrid_keys"` → empty.
- Both key files (`/tmp/aprover_hybrid_keys.env`,
  `/tmp/aprover_or_keys.env`) live outside the repo.
- `.gitignore` hardened (commit `6c6d5ec`) — verified with
  `git check-ignore` against synthetic key-file paths.
- Sweep logs in `/tmp/aprover_neuron_or_sweep/*/run.log` grep'd for
  key material — none present.

## Methodology insight

The session's first sweep produced **2 real_bug classifications with
empty realism verdicts** (`uncertain` from a failed LLM call). That
felt wrong — the realism check was supposed to fire. Root-causing it
surfaced THREE distinct bmc-agent bugs that compounded each other:

1. **Env-var mismatch** — `BMC_AGENT_LLM_API_KEY` set in env but config
   read `K2THINK_API_KEY`, so the realism LLM had no key at all.
2. **CBMC raw_output / struct-stringify blow-up** — bug_report.json hit
   264 MB on `ts_nq_destroy` because CBMC's full trace dump and a struct
   value's recursive `str()` ballooned the in-memory state.
3. **HTTP 4xx retry-burning** — when the resulting oversized prompt
   tripped OpenRouter's 8 MB limit, the retry loop burned 3×backoff
   before giving up.

Each fix was small (1-line / 10-line) but all three were necessary
to make the realism check actually run. This is exactly the
"fix bmc-agent in-flight when patterns emerge" workflow from
`bmc_agent_session_2026-05-13.md` — three durable infrastructure wins
from one round of empirical sweeping.

## Open items for next session

- Wait for the Phase 2 sweep (giant Neuron files) to finish, triage
  its real_bug list with the fully-patched pipeline.
- Llama.cpp `ggml-quants.c` + nghttp2 `nghttp2_frame.c` hybrid sweep
  (both previously trivial-spec only).
- Disk cleanup: `/tmp/aprover_neuron_or_sweep` is ~24 GB because the
  in-flight pipeline ran before commit `2ab4dcf` landed. The per-function
  scorecards have been pulled into `findings/`; the raw dirs can be
  removed once the user reads the summary.
- Re-run the 4 surviving real_bug candidates with the patched pipeline
  to get a definitive realism verdict.
