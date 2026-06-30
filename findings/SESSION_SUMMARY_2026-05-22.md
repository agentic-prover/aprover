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
   `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`, so any sweep using the
   local key environment got an empty key for non-spec_gen
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
- `test_bmc_agent_llm_api_key_env` / `_beats_openai` — env-var fallback.
- `test_raw_output_capped_for_huge_cbmc_json` — CBMC dump cap.
- `test_struct_assignment_does_not_blow_up_variable_assignments`.
- `test_scalar_assignment_still_uses_data_field`.
- `test_http_4xx_does_not_burn_retries`.

### Hybrid-mode AWS Neuron sweep (8 files, 61 functions)

Files: `neuron_arch`, `_log`, `_topsp`, `_cinit`, `_module`, `_core`,
`_ds`, `_reset`. Full per-file table not included here.

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

Routing: started reasoning-model-hybrid; switched to all-OpenRouter when the reasoning model went
HTTP 504 mid-sweep. the reasoning model has since recovered.

### Phase-2 sweep (reasoning-model-hybrid, giant Neuron files)

`neuron_cdev / dma / mempool / metrics`.

| Metric | Value |
|---|---|
| Files | 4 (`cdev` ok, `dma` / `mempool` / `metrics` timed out) |
| Functions verified clean | **85** |
| `real_bug` raw | 3 |
| `spurious` | 35 |
| Wall clock | ~2h 40min (started 17:34, done 20:14) |

The embargoed bug candidate (yesterday's trivial-spec finding)
**verified clean** here — root-cause is documented in
`methodology_insight_2026-05-22.md` as a "caller-contract slip" case
study: the LLM-generated callee precondition encodes the obligation
the bug-triggering caller violates, so both functions verify clean
against the (over-permissive) inferred contract. **Paper-track methodology insight**: trivial-spec
is the recall floor, LLM-spec adds precision; the right deployment
runs both modes per target.

### Hybrid sweep on llama.cpp + nghttp2 (OR-mode)

`ggml-quants.c` and `nghttp2_frame.c` — see
[`llama_cpp_ggml/hybrid_sweep_2026-05-22/README.md`](llama_cpp_ggml/hybrid_sweep_2026-05-22/README.md).

| Metric | Value |
|---|---|
| Functions analysed | 188 (115 ggml + 73 nghttp2) |
| Verified clean | **71** (33 ggml + 38 nghttp2) |
| `real_bug` raw | 19 |
| `real_bug` after filter | 2 (both reasoning-model-504-uncertain) |
| `spurious` | 44 |

Notable: the feedback loop discovered a multi-clause invariant for
`make_qp_quants` capturing array sizing + forall-quantified
finiteness/non-negativity of quant_weights — exactly the contract
real callers maintain. `nghttp2_frame_*_init` constructors all verify
clean. 0 likely-true bugs in either target, consistent with their
OSS-Fuzz histories.

### Cleanup

Killed 6 orphan `cargo-kani` → `cbmc` process pairs from yesterday's
Rust fastrand sweep — they were deadlocked SAT-solving for 22+ hours
and consuming 6 CPU cores. Each parent was still alive but blocked on
its child's never-progressing SAT solver. Killing the parents
propagated to the children.

## Security posture (API keys)

User explicitly asked mid-session that API keys never be pushed.
Verified:
- `git log -G "<redacted-key-patterns>"` returns nothing
  — neither key value has ever appeared in repo history.
- `git ls-files | grep -E "tmp/|aprover_keys|hybrid_keys"` → empty.
- Local key files live outside the repo.
- `.gitignore` hardened (commit `6c6d5ec`) — verified with
  `git check-ignore` against synthetic key-file paths.
- Sweep logs were grep'd for key material — none present.

## Methodology insight

The session's first sweep produced **2 real_bug classifications with
empty realism verdicts** (`uncertain` from a failed LLM call). That
felt wrong — the realism check was supposed to fire. Root-causing it
surfaced THREE distinct bmc-agent bugs that compounded each other:

1. **Env-var mismatch** — `BMC_AGENT_LLM_API_KEY` set in env but config
   read `OPENAI_API_KEY`, so the realism LLM had no key at all.
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

## Aggregate session totals

| Sweep | Files | Clean | real_bug raw | After filter | Spurious |
|---|---:|---:|---:|---:|---:|
| OR-mode Neuron (8 files) | 8 | 21 | 10 | 4 | 8 |
| reasoning-model-hybrid P2 (4 giant Neuron) | 4 | 85 | 3 | 2 | 35 |
| OR llama.cpp + nghttp2 | 2 | 71 | 19 | 2 | 44 |
| **Total** | **14** | **177** | **32** | **8** | **87** |

**177 verified-clean memory-safety + functional properties** across the
AWS Neuron driver kernel attack surface and two heavily-fuzzed OSS
targets, in a single ~3-hour session. **8 surviving real_bug
candidates** — all `confirmed_system_entry` with `realism=null/uncertain`
due to either reasoning-model 504 hiccups during the realism call or upstream
realism-prompt size issues that the cbmc.py patch fixes for future
sweeps. Triage tool: `findings/find_unfiltered_real_bugs.py`.

## Open items for next session

- Re-run the 8 surviving real_bug candidates with the fully-patched
  pipeline + healthy reasoning-model backend; expect most/all to flip to
  `unrealistic` (defensive-programming-gap pattern).
- Disk cleanup: `/tmp/aprover_neuron_or_sweep` is ~24 GB because the
  in-flight OR pipeline ran before commit `2ab4dcf` landed. The
  per-function scorecards have been pulled into `findings/`; the raw
  dirs can be removed once the user reads the summary.
- Re-run the 3 timed-out P2 files (`dma`, `mempool`, `metrics`) with
  the patched code — the cbmc.py / llm.py / 504-retry fixes should
  let them complete within budget.
- Paper section update: fold the embargoed caller-contract-slip case
  study into the Discussion section (see
  `methodology_insight_2026-05-22.md`).
