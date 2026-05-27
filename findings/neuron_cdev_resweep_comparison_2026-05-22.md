# Neuron-cdev re-sweep comparison — 2026-05-22 (autonomous tick)

Re-ran BMC-Agent on `neuron_cdev.c` after today's harness/feedback
prototype work to see whether the new code surfaces additional real
bugs. Comparison anchored against:
- `findings/aws_neuron_driver/scorecard_neuron_cdev.json` (the prior
  trivial-spec sweep — 118 functions × PRE=POST="true")
- The hybrid_p2 LLM-spec sweep documented in
  `findings/aws_neuron_driver/hybrid_p2_2026-05-22/`

Two re-runs:

1. **Trivial-spec mode** (`Spec.precondition="true"`, same methodology
   as the prior `scorecard_neuron_cdev.json`).
2. **Bug-hunt LLM-spec mode** (`spec_mode="bug-hunt"` with the existing
   spec.json files from `/tmp/aprover_neuron_hybrid_p2/`).

## Trivial-spec re-sweep: no change

| Bucket | Prior | New | Δ |
|---|---:|---:|---:|
| VERIFIED | 54 | 54 | 0 |
| FAIL | 53 | 53 | 0 |
| COMPILE_ERR | 9 | 9 | 0 |
| TIMEOUT | 2 | 2 | 0 |

Bit-identical scorecard, zero per-function verdict changes. Confirmed
with a second file (`neuron_dma.c`, 33/24/2/1 also identical).

The harness *content* changed (`neuron_copy_from_user_stub` now has a
return contract; sibling-inferred contracts have a `-4095` lower
bound). But trivial-spec mode only exercises CBMC's intrinsic
memory-safety properties (`--bounds-check`, `--pointer-check`); it
doesn't have non-trivial POST clauses for the new constraints to
make a verdict difference on. Today's fixes engage in LLM-spec
mode, not trivial.

## Bug-hunt LLM-spec re-sweep: real differences

| Bucket | Trivial baseline | Bug-hunt new | Δ |
|---|---:|---:|---:|
| VERIFIED | 54 | 17 | -37 |
| FAIL | 53 | 36 | -17 |
| COMPILE_ERR | 9 | 62 | **+53** |
| TIMEOUT | 2 | 3 | +1 |

The COMPILE_ERR jump is a pre-existing DSL-translator bug (e.g. the
PRE atom `valid((struct ncdev*)filep->private_data)` translates to
malformed C `((struct ncdev* != NULL` — the cast confuses the
paren-matching). Functional mode dropped these to comments silently;
bug-hunt mode tries to emit asserts and hits the malformed output.
Top error classes (sampled): missing LHS before `>=` (22), before
`!=` (8), before `;` (6).

## Caller-contract-slip candidates surfaced by bug-hunt mode

| Function | stub-PRE failures | Classification |
|---|---:|---|
| (embargoed caller function A) | 9 | **Real bug** — the known embargoed OOB, now flagged at the *caller* call site |
| (embargoed sibling-callee function) | 6 | **Same root-cause bug** — caller → sibling-callee → write-data chains the same misallocated address-array to one more callee |
| `ncdev_create_device_node` | 2 | **Defensive-coding gap** — `&devnodes[minor]` accessed without bounds check on `minor = ndev->device_index`; in practice PCI subsystem bounds it, so not exploitable |
| `ncdev_post_metric` | 2 | FP — `copy_from_user` PRE the over-tight class (LLM emits `valid(from)` for a userspace pointer that doesn't require kernel-readability) |
| `ncdev_read_hw_counters` | 2 | FP — same `copy_from_user` PRE class as above |

## Headline answer to "any new real bugs?"

**No new distinct real bugs in `neuron_cdev.c`.** The bug-hunt mode
surfaces 5 caller-contract candidates, but careful triage gives:

- **1 root-cause real bug** (the embargoed OOB candidate) — now
  visible at **3 vantage points** (the original internal flag, the
  new caller flag, and the sibling-callee chained flag). Better
  localisation for disclosure, no new disclosure target.
- **1 defensive-coding gap** in `ncdev_create_device_node` (worth
  noting to AWS but not crash-class).
- **3 known-FP class** caller-contract slips (over-tight LLM PREs
  that the existing CALLEE_SPEC_RELAX feedback path is designed
  to learn away).

## What this confirms about today's session work

- The validity/protocol split is *functionally correct* (it does
  surface the caller-contract slip, as predicted by the
  methodology insight).
- It does NOT find dramatically more bugs on this codebase — the
  prior pipeline's single real-bug candidate was the right answer.
  The architectural value is in *localisation* and *disclosure
  narrative*, not in absolute bug count.
- The DSL translator's paren-handling bug on cast expressions is a
  pre-existing blocker for full-file LLM-spec runs; needs fixing
  before bug-hunt mode can be useful at sweep scale. Currently
  62/118 functions fail to compile, so coverage is poor.
- For a cleaner empirical story on the paper, the path forward is
  either: (a) fix the DSL paren handling so all 118 functions
  compile, then re-run bug-hunt mode; or (b) accept that the headline
  result is "found exactly the bug it was designed to find" with the
  embargoed caller walkthrough as the demonstration.

## Update: paren-fix landed, sweep re-run (same autonomous session)

Path (a) was taken. The `_match_call` balanced-paren helper replaced
the six `[^)]+` regexes in `bmc_agent/dsl_to_cbmc.py`. +14 regression
tests; 739 passing, 0 failing under venv python.

Re-run sweep with the fix, then again after also adding kernel-macro
defaults (PAGE_SIZE / EFAULT / EINVAL / ENOMEM / EAGAIN / EIO /
ENODEV) to the harness preamble:

| Bucket | v1 (original) | v2 (paren) | v3 (+ macros) |
|---|---:|---:|---:|
| VERIFIED | 17 | 25 | 26 |
| FAIL | 36 | 55 | 60 |
| COMPILE_ERR | 62 | 35 | **29** |
| TIMEOUT | 3 | 3 | 3 |

Net coverage in {VERIFIED ∪ FAIL}: 53 → 80 → **86 of 118** (73%).

The caller-contract-slip headline list is unchanged across all three
sweeps (still 5 candidates, same triage). The newly-compiling
functions surface as either VERIFIED or FUT-body-assertion FAIL —
not new caller-contract slips.

The remaining 29 compile errors trace to LLM-emitted spec atoms
referencing names not in harness scope: loop-quantifier variables
(`i`), function-body locals (`arg`, `param`, `buffer`), and obscure
NEURON_IOCTL_* constants. Fixing these requires clause-level
filtering during translation, not preamble additions — larger
refactor than today's autonomous scope. Raw v3 log persisted at
`bug_hunt_sweep_neuron_cdev_v3_2026-05-22.log`.

## Update: clause-level filtering shipped (continued autonomous tick)

Took on the "larger refactor" anyway. Two follow-on fixes landed:

- **Strip `const` from struct-pointer backing storage.** Harnesses for
  ``const struct X *`` parameters declared ``const struct X _obj;``
  then assigned to its fields — CONVERSION ERROR. Drop the const on
  the backing decl; keep it on the pointer type.
- **Unbound-identifier filter** with per-conjunct application.
  ``_condition_to_stmts`` now splits on top-level ``&&`` (in addition
  to ``AND``/newline/semicolon), then drops only the conjuncts that
  reference identifiers outside the function's parameter list and the
  always-bound common set (C keywords, kernel macros, CBMC
  intrinsics, ``assert``, ``result``). Type tags inside C casts
  (``(struct ncdev *)p``) are stripped from the scan buffer so they
  don't get flagged.

After these fixes:

| Version | VERIFIED | FAIL | COMPILE_ERR | Caller-contract candidates |
|---|---:|---:|---:|---:|
| v1 (original) | 17 | 36 | 62 | 5 |
| v3 (paren + macros) | 26 | 60 | 29 | 5 |
| v7 (+ const + unbound + &&-split) | 26 | 87 | **2** | **10** |

**60 → 2 compile errors** (97% reduction). Coverage in {VERIFIED ∪
FAIL} now 113/118 = 96%. 5 new caller-contract slip candidates
surfaced beyond the original 5 — these are functions that previously
didn't compile so the prototype couldn't even see them. Triage of
the new 5:

- `ncdev_create_device_node` — defensive `minor` bounds (already
  triaged: not exploitable)
- `ncdev_ioctl` (60 stub failures) and `ncdev_misc_ioctl` (12) —
  central IOCTL dispatchers; massive volume because every sub-handler
  contract is checked; mostly the same `param != NULL` FP repeated
- `ncdev_program_engine` / `ncdev_program_engine_nc` — copy_from_user
  FP class (over-tight `valid(from)`)
- `ncdev_printk` — copy_from_user FP class

**Same headline real-bug count.** No new distinct disclosure-quality
bug. The prototype now sees ~all of the file and the answer is still
"1 root-cause bug (embargoed OOB candidate)". v7 log persisted at
`bug_hunt_sweep_neuron_cdev_v7_2026-05-22.log`.

**Final follow-on (v8): parser fix for `void*name` shape.**
`_extract_param_ts` previously returned `(type="void*param", name="")`
when no whitespace separated the pointer star from the identifier;
the harness then synthesised an undeclared symbol `_`. Added a
regex-based fallback. +2 regression tests in test_phase2.py.

v8 sweep: **1 compile error remaining** (down from 62, 98% reduction).
The lone holdout is `ncdev_open` (kernel intrinsic `iminor` undeclared
— different fix pattern requiring a stub-decl addition). v8 log
persisted at `bug_hunt_sweep_neuron_cdev_v8_2026-05-22.log`.

**iminor fix (post-v8):** added `iminor` and `imajor` declarations to
the kernel-intrinsic-stub preamble. The `iminor`-shaped error is
resolved for ncdev_open (verified by isolated CBMC run). A different
undeclared symbol (`get_current()`) then surfaces — the kernel
intrinsic universe is large and stubbing each is whack-a-mole.
ncdev_open will likely need either a sweeping kernel-intrinsic
catalogue or explicit per-target stubs. Out of scope for this thread;
the headline 60 → 1 compile-error reduction stands.
