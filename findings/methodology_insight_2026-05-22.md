# Methodology insight — caller-contract slips in compositional verification

Discovered today (2026-05-22) during the P2 hybrid sweep on
`neuron_cdev.c`. Important enough for the paper's methodology section.

## The observation

Yesterday's trivial-spec sweep flagged `ncdev_bar_read` as a heap-OOB-read
candidate (`findings/aws_neuron_driver/POTENTIAL_BUG_ncdev_bar_read.md`):

- Caller `ncdev_bar_rw` allocates a 1-element `u64 *reg_addresses` when
  `arg.bar != 0`, but then passes `arg.count` (potentially > 1) as the
  `data_count` argument to `ncdev_bar_read`.
- `ncdev_bar_read` iterates `for (i = 0; i < data_count; i++)` reading
  `reg_addresses[i]`, walking off the end of the 1-element buffer.

Today's LLM-spec sweep (P2, full pipeline w/ realism + feedback)
**verifies `ncdev_bar_read` clean** — confidence: 0 real_bug.

## What happened

The Phase 1 spec generator emitted for `ncdev_bar_read`:

```
PRE: ... && valid_range(reg_addresses, 0, data_count) ...
POST: (result == 0 || result < 0) && (result == 0 implies ...)
```

That precondition declares the caller's obligation: pass a
`reg_addresses` array sized to at least `data_count` elements. With
this in the harness's `__CPROVER_assume`, CBMC's bounds-check never
fires because the spec excludes the violating state.

The caller `ncdev_bar_rw` is *also* verified clean. The bmc-agent stubs
`ncdev_bar_read` using its LLM-generated spec (the over-permissive
one), so when ncdev_bar_rw passes a 1-element array with data_count=N,
the stub spec says "any data_count consistent with the size cap is
fine" — the actual mismatch never surfaces.

## Why this matters

This is a fundamental limitation of compositional verification when the
specs are *inferred from code* rather than asserted independently:

- If the auto-generated callee spec is too weak (over-permissive
  precondition), the callee verifies clean *and* every caller is then
  verified against that same over-permissive contract.
- The actual bug — a mismatch between what the caller passes and what
  the callee requires — disappears in both verifications.

Trivial-spec mode catches this because it imposes no preconditions on
the callee; CBMC then sees the full attacker-controlled input space
and finds the OOB. But trivial-spec mode forfeits the precision of
functional postconditions, which is why bmc-agent uses LLM specs by
default.

## Implications for the paper

The AMC architecture's compositional decomposition is sound: each
function verified against its spec, callees stubbed by their specs.
But *correctness of the verification verdict requires soundness of the
specs themselves*.

LLM-generated specs are heuristic: they encode the contract the LLM
*infers* from the code + call sites. When that contract is the
*intended* contract that the buggy caller violates, the bmc-agent
correctly reports the caller as buggy (this is what happened on
yesterday's `jvp_utf8_next` bug-bounty submission). When the LLM
infers a *minimum* contract that the callee body imposes (rather than
the *intended* one that callers were supposed to maintain), the bug
slips through.

**The trivial-spec / FilteringOnly / no-spec ablation is therefore the
recall floor; LLM-spec mode trades recall for precision.** The right
deployment is *both*: run trivial-spec for memory-safety-style bugs
that don't need functional postconditions, and LLM-spec for
functional-correctness verification of well-behaved leaf functions.

For paper section 2 (Architecture) and 5 (Discussion): make this trade
explicit. The hybrid sweep on Neuron driver files this session
demonstrates the case study end-to-end — yesterday's trivial-spec on
neuron_cdev surfaced the ncdev_bar_read OOB, today's LLM-spec on
the same file verifies the function clean *given its inferred
precondition*.

Both verifications are technically correct — they answer different
questions. The user-facing implication: a function "verified clean by
AMC" should be qualified with "given the inferred preconditions" —
which the [[feedback_clean_proof_clarity]] memory already calls out.
