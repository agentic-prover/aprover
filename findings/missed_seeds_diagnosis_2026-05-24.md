# 9 missed seed-bug diagnosis — 2026-05-24

**Context**: of the 14 mappable seed-bug commits in the
libarchive `b_start..b_end` interval that land in the 7-file corpus,
bmc-agent-lite's N=3 sweep matched 5. This document classifies the 9
misses by their bmc-agent failure mode, so we know which mechanism
fix would unlock each.

All 9 functions DID run through CBMC and DID produce counterexamples
(7–121 CEx each). None were skipped, timed out, or compile-failed.
Every failure is at a later pipeline stage.

## Class A — Path-divergent-unwind filter false-rejects (4 bugs)

`_witness_indicates_path_divergent_unwind` in `realism_checker.py` is a
pre-classifier filter that downgrades `*.unwind.*` violations to
UNREALISTIC when the *exhibited* counterexample witness exits before
reaching the loop the unwind property describes. The logic — "if this
path doesn't loop, the bug isn't along this path" — is sound for
non-unwind bugs but **wrong when the bug itself is an infinite loop or
loop-bound violation on a different path**.

| Commit | Function | Property | Filter message |
|---|---|---|---|
| `d45b5b4b` | `archive_acl_to_text_w` | `.unwind.0` | "witness returns at step 10 before reaching loop-head at step 132" |
| `79a0787b` | `lzx_decode` | `.unwind.0` | "function body has no loop on the exhibited path" |
| `4cbf9582` | `__archive_pathmatch_w` | `.unwind.2` | "witness returns at step 6 before reaching loop-head at step 28" |
| `25d97315` | `do_uncompress_file` | `.unwind.0` | "witness returns at step 6 before reaching loop-head at step 68" — **the seed bug IS an infinite loop** |

**Fix**: weaken the filter. Options:
- A1. Disable it entirely (regresses on real divergent-unwind FPs)
- A2. Skip the filter when the function appears in any in-tree call
  chain that proves loops ARE reachable (more robust; uses parsed
  call-graph)
- A3. Keep classifier rejection but still run realism — let the LLM
  judge whether the unwind hit is a real bug

Recommend **A3**: change the filter from "downgrade to UNREALISTIC"
to "annotate the bug report but let realism decide." The LLM can read
the loop body and the seed-bug-style call chain and judge whether
the bound was hit for cause.

## Class B — Spurious "no caller can produce state" (4 bugs)

`cex_validator` declares spurious when no in-tree caller can produce
the CBMC-supplied input state. This is the harness-state-feasibility
check. It fires on these four because the harness model has fields
the validator cannot satisfy from any direct caller.

| Commit | Function | Property | Reason snippet |
|---|---|---|---|
| `c3cb1c56` | `parse_rockridge` | `parse_rockridge.pointer.1` | "no caller can produce {…, file=`_file_obj!0@1` with all linked fields NULL}" |
| `a9d2cc5e` | `isJolietSVD` | `archive_le32dec.pointer_dereference.5` | byte-swap-helper artifact propagated up the call chain |
| `620bdafa` | `init_unpack` | `init_unpack.pointer_dereference.51` | deeper-index property; caller-set undecidable for unconstrained rar struct |
| `1f545457` | `lzx_huffman_init` | `lzx_huffman_init.precondition_instance.3` | unconstrained bitstream/huffman state |

These are all **vtable-dispatched callees-of-callees** — the
vtable-dispatch detector (which substitutes indirect callers when a
function has no direct callers) finds the OUTER callback but doesn't
recurse to satisfy the inner function's input shape from the outer
callback's body.

**Fix**: either
- B1. Run realism on SPURIOUS-classified findings too (a CEx the
  classifier rejects on caller-feasibility grounds may still be
  reachable via the format-detect → format-dispatch path the LLM
  can recognize)
- B2. Improve the harness for inner callees so the CBMC-supplied
  state is structurally constrained (e.g., `rar->cstate.initialized=1`
  rather than nondet) — this is a harness-generator change
- B3. Add a stronger vtable-recursion in cex_validator's
  caller-feasibility check

Recommend **B1** first (lowest implementation cost, highest coverage)
followed by B2 for the surviving spurious cases.

## Class C — REAL_BUG downgraded by realism (1 bug)

| Commit | Function | Property | Realism verdict |
|---|---|---|---|
| `470379a9` | `archive_match_path_excluded` | `pointer_dereference.5` | unrealistic |

This finding *did* reach `confirmed_system_entry` and went through
realism, which downgraded it. Need to read the realism reasoning
(the prompt got the bug report, source, witness, and active stub
contracts) and decide if the downgrade was:
- correct (witness genuinely impossible) → keep
- wrong (LLM hallucinated a guard or misread the call chain) → tune
  the realism prompt or the artifact-phrase detector

**Fix**: per-finding triage; possibly a prompt tightening.

## Summary

| Class | Count | Single best fix |
|---|---|---|
| A (unwind filter too aggressive) | 4 | Stop pre-classifier downgrade; defer to realism |
| B (spurious from caller-state-infeasibility) | 4 | Run realism on SPURIOUS classifications |
| C (realism over-downgrade) | 1 | Manual triage + prompt tuning |

**Single biggest leverage**: a small change in `pipeline.py` /
`cex_validator.py` to **route SPURIOUS and unwind-filtered findings
through the realism check** instead of suppressing them at the
classifier stage. Realism is the more nuanced gatekeeper, and the
LLM has more context than the heuristic filters. Estimated unlock:
**+3 to +6 of 9 missed seeds** → goal moves from 5/14 to 8–11/14
documented seed matches.
