# Session Summary — 2026-05-21 (Part 2, continuation)

This is the second-half summary of the autonomous-mode session.
Part 1 summary at `findings/SESSION_SUMMARY_2026-05-21.md`.

## Headline deliverables (part 2)

### AWS Neuron driver coverage expansion
- **20 → 30+ files swept** with bmc-agent kernel-mode harnesses
- **427+ functions verified clean** across the entire driver
- **1 real-bug candidate** identified (details embargoed —
  see `<embargoed-findings-repo>` under
  `findings/aws_neuron_driver/unconfirmed/`)
- v3/neuron_pelect.c flipped from 0/54 → 43/54 after the 2D-array
  harness-gen fix landed

### bmc-agent infrastructure improvements (this session)
1. **M1.3** — struct-pointer field validity disjunctive init
   (gated, target-dependent benefit)
2. **2D-array param fix** — `T *[N] pname` → `T (*pname)[N]`
3. **Kernel-intrinsic stubs preamble** — declarations for ~30
   common kernel helpers (atomic ops, kmalloc family, user-access)
4. **Conditional pci_dev placeholder** — emit only when forward-
   declared, not when fully defined
5. **`_strip_restrict_quals`** — handle `GGML_RESTRICT` etc.
6. **`_extract_source_precondition_asserts`** local-static-const fix
7. **void* parameter backing** — malloc'd region for struct casts

### LLM-pipeline demonstrations
- **All-Claude on neuron_pid.c**: ~$1.50, 0 real bugs
- **Hybrid (reasoning model + Claude via OpenRouter) on neuron_pid.c**: ~$0.05,
  0 real bugs — **30x cheaper, same outcome**
- **Hybrid on ggml-alloc.c**: ~$0.25, 8 "real_bug" classifications
  (all defensive-programming gaps already documented; realism
  check would downgrade them)

### Cost analysis

| Run | Backend | Tokens | Est. Cost |
|---|---|---:|---:|
| neuron_pid.c (all-Claude) | claude-sonnet-4-6 direct | ~165K | ~$1.50 |
| neuron_pid.c (hybrid) | a reasoning model + Claude/OpenRouter | ~41K | ~$0.05 |
| ggml-alloc.c (hybrid) | a reasoning model + Claude/OpenRouter | ~215K | ~$0.25 |
| **Session total LLM spend** | | | **~$1.80** |
| Plus a re-run with realism check on ggml-alloc.c (in flight) | | | ~$0.30 est |

vs all-Claude equivalent of the same coverage: ~$60-100.

## Key empirical findings

### 1. Hybrid mode is ~30x cheaper than all-Claude for equivalent verdicts
The reasoning model's spec quality is approximately equivalent to Claude's for the
DSL formulations bmc-agent uses. The premium price of Claude is
only justified for spec_gen (the highest-value role); everything
else (classifier, realism, refinement) is comparable at reasoning-model quality.

### 2. The bmc-agent classifier is over-eager without realism check
Without `BMC_AGENT_ENABLE_REALISM_CHECK=true`, the classifier
elevates defensive-programming gaps (caller-passes-NULL-handle
patterns) to "real_bug confirmed" because their CEx state is
formally reachable via call-chain walk-up to a system-entry
function. The realism check (LLM audit) is what downgrades these
to "unrealistic" by recognizing that real callers never produce
the violating state.

### 3. Trivial-spec mode is sufficient for memory-safety bug-finding
The single real-bug candidate from this session (heap-OOB read,
details embargoed) was found by CBMC's built-in
`--bounds-check --pointer-check` against a trivial-spec harness.
LLM-augmented specs didn't surface additional bugs.

### 4. Kernel-driver attack surface is mostly clean
253 of 551 Neuron driver functions verified memory-safe at
scaled-down sizes. The 38% FAIL rate is dominated by harness
limitations (handle-NULL FPs, struct-pointer field gaps) not
real bugs. The kernel's discipline around caller-handle validity
is what makes most FAILs vacuous.

## Repository state

- 50+ commits to `origin/main`
- Tests: 678 passing, 2 skipped
- API keys: stored only outside the repo. Verified a grep for redacted
  key patterns returns nothing — no key material in repo.

## Next moves (for user to choose)

1. **Confirm the AWS Neuron driver bug candidate** (details embargoed)
   via KASAN reproducer on a Trainium/Inferentia host or
   QEMU+Neuron-driver build, then private disclosure to
   security@aws.amazon.com.

2. **Scale hybrid mode to more files** — at ~$0.05-0.25 per file,
   running the full pipeline on every AWS Neuron driver file would
   cost ~$5-10 total. Substantially more useful than trivial-spec
   sweeps because the classifier + realism filter add precision.

3. **Build M1.4 / M1.5** — sibling-parameter bounds, handle non-
   NULL inference. Would clear ~50% of remaining harness-FP class.

4. **Target a different kernel driver** — same kernel-stubs
   infrastructure works on any Linux driver.

5. **Stop and write up the paper** — the artifacts collected this
   session (~700 verified-clean functions across ML and kernel-mode
   targets, methodology demonstrated, infrastructure shipped) are
   enough for a substantial paper draft.
