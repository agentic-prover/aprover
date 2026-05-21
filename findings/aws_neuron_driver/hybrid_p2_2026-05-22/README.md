# AWS Neuron Driver — P2 K2-hybrid sweep, 2026-05-22

Phase 2 of today's Neuron driver sweep: the four giant attack-surface
files that were deferred from the morning OR-mode sweep to keep budget
bounded. K2-hybrid mode (K2 default + Claude/OpenRouter for spec_gen +
feedback_distill) — uses the patched cbmc.py + llm.py so no artifact
blow-ups and no retry-burning on HTTP 4xx.

## Targets

| File | LoC (preprocessed) | Functions | Status |
|---|---:|---:|---|
| neuron_cdev.c   | 72709 | ~140 | ✓ ok (4200s) |
| neuron_dma.c    | 65040 | ~60  | timeout @ 4800s |
| neuron_mempool.c| 63829 | ~30  | timeout @ 4800s |
| neuron_metrics.c| 64244 | ~26  | timeout @ 4800s |

(LoC is post-cpp; original .c sizes are 1.7-2.0k LoC each.)

## Results

| File | Verified clean | real_bug raw | spurious | After filter | CBMC errors |
|---|---:|---:|---:|---:|---:|
| neuron_cdev | **47** | 0 | 5 | 0 | ~90 |
| neuron_dma | 19 | 1 | 8 | 1 (uncertain) | ~33 |
| neuron_mempool | 12 | 0 | 5 | 0 | ~13 |
| neuron_metrics | 7 | 2 | 17 | 1 (uncertain) | ~0 |
| **TOTAL** | **85** | **3** | **35** | **2** | |

### Notable findings

**neuron_cdev `ncdev_bar_read` verified clean** — directly contradicts
yesterday's trivial-spec sweep that flagged this function as a
heap-OOB-read candidate. Root cause documented in
[`findings/methodology_insight_2026-05-22.md`](../../methodology_insight_2026-05-22.md):
the LLM-generated precondition encodes
`valid_range(reg_addresses, 0, data_count)` as a caller obligation;
CBMC trusts it and verifies clean. The actual bug is in the caller
`ncdev_bar_rw` violating that contract, but the LLM-spec stub mode
verifies the caller clean against the over-permissive callee spec
too. **Important paper-track methodology finding**: trivial-spec is
the recall floor, LLM-spec adds precision; right deployment runs
both.

**neuron_metrics 2 real_bug survivors** —
`nmetric_set_performance_profile` and one other (
`nmetric_*`). All confirmed_system_entry with realism=uncertain due
to K2 504 hiccups; need triage but match the defensive-programming
pattern.

**neuron_dma 1 real_bug survivor** — `ndma_zerocopy_supported`,
same pattern.

## Wall clock / cost

- Started 17:34, finished 20:14 = ~2h 40min total wall clock
- 4 files in parallel pairs (parallel=2)
- 3 of 4 hit per-file 80-min timeout — Phase 3 LLM-bound on K2-hybrid
  with intermittent K2 HTTP 504s slowing per-call latency
- Estimated cost: ~$2-3 (K2 covers most LLM volume; Claude only for
  spec_gen + feedback_distill)

## What the timeouts cost us

The three timed-out files (dma, mempool, metrics) had their Phase 3
classifier interrupted mid-stream. Across all three, ~46 functions
have raw CBMC verdicts (verified or FAIL with CEx) but no
classification/realism — they're not counted in any column above.
With the cbmc.py + llm.py patches landed today, a re-run with the
80-min timeout should now complete all four files (the artifact
blow-up was burning significant time on serialization), but that's
budget for a future session.

## Files

`/tmp/aprover_neuron_hybrid_p2/<stem>/<stem>_p2/<fn>/` contains
spec.json, harness.c, bug_report.json, classification.json for each
processed function. Per-file run.log has the full Phase 1+2+3 trace.

## Compared to yesterday's trivial-spec sweep on the same files

| File | Yesterday (trivial-spec) | Today (LLM-spec hybrid) |
|---|---|---|
| neuron_cdev (M1.3) | 54/118 clean, 1 real_bug | 47/?? clean, 0 real_bug |
| neuron_dma | 28/60 clean, 0 real_bug | 19/?? clean, 1 real_bug |
| neuron_mempool | (not separately reported) | 12/?? clean, 0 real_bug |
| neuron_metrics | (not separately reported) | 7/?? clean, 2 real_bug |

The LLM-spec mode produced fewer clean verdicts on cdev because it
imposes precondition assumptions that CBMC must clear, but ALSO
verifies functional postconditions (the bmc-agent value-add). The
ncdev_bar_read divergence is the paper-track methodology insight
documented above.
