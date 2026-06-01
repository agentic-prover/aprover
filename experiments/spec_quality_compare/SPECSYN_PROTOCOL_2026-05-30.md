# Spec Quality Comparison Protocol: SpecSyn to BMC-Agent

Date: 2026-05-30

Primary source: SpecSyn, "LLM-based Synthesis and Refinement of Formal
Specifications for Real-world Program Verification", arXiv:2604.21570.

Local artifacts:

- `/mnt/disk7/jw_bmc/papers/specsyn_2604.21570.pdf`
- `/mnt/disk7/jw_bmc/papers/specsyn_2604.21570.txt`

## Decision Question

Can we compare BMC-Agent DSL specs, BMC-Agent ACSL-translated specs, and
ACSL-generating baselines in a way that measures spec quality rather than just
whether a verifier accepts the spec?

Working answer: yes, but the comparison should not use verifier-pass alone.
The transferable core from SpecSyn is mutation/variant discrimination. For
BMC-Agent, we also need an overconstraint metric because harness preconditions
can hide real bug inputs.

## What SpecSyn Measures

SpecSyn uses ACSL specs for C and evaluates them along three useful axes:

1. **Precision**: generated ACSL statements that Frama-C/WP can verify on the
   original program divided by all generated statements. This measures local
   correctness of spec statements.
2. **Recall**: manually written ground-truth ACSL statements covered by the
   generated statements divided by all ground-truth statements. This measures
   semantic strength, but it requires human reference specs.
3. **Variant Discriminative Rate (VDR)**: generate semantic-non-equivalent
   mutants of the original program and count how often the spec rejects the
   mutant while still verifying on the original. This measures whether the spec
   is non-trivial.

SpecSyn also reports downstream utility: how many target properties can be
proved with the generated specs. In Table 3, this is the number of Frama-C/WP
target assertions discharged with each method's specs.

## Why This Cannot Be Copied Blindly

SpecSyn's precision/recall assumes all methods emit ACSL. BMC-Agent currently
emits a harness-oriented DSL, whose native backend is CBMC/Kani-style
assume/assert checking. Directly comparing "ACSL precision" against "CBMC
verified" mixes two verifier semantics.

The ACSL backend pilot in this branch makes a narrow bridge: translate common
function-level `requires` and `ensures` clauses into ACSL and run Frama-C/WP.
This is enough for scalar cases such as `max2`, but it is not yet a complete
translation for loop invariants, frame conditions, quantified properties, or
complex pointer ownership.

Therefore, use two layers:

- **Common ACSL layer** where translation is supported.
- **Native backend layer** where each spec is checked by its intended verifier,
  but scored with the same semantic tests: original acceptance, mutant
  rejection, and bug-witness preservation.

## Metrics for Our Comparison

### 1. Original Validity

Question: does the spec hold on the unmodified program?

- ACSL: Frama-C/WP proves all generated clauses or target assertions.
- DSL: CBMC/Kani harness verifies the generated assumptions/assertions.

This is necessary but weak. A trivial postcondition such as `true` can pass.

### 2. Ground-Truth Coverage

Question: how much of a small human-written reference spec does the generated
spec imply or syntactically cover?

Use this only on a small curated subset. It is expensive and subjective, but it
is the closest analogue to SpecSyn recall.

For the first pilot, score clause-level coverage manually:

- return-value relation
- bounds relation
- null/valid-pointer obligation
- frame/no-modification condition where relevant
- loop invariant only when the benchmark is explicitly loop-centric

### 3. Mutation-Killing / VDR

Question: does the spec reject behavior-changing variants?

This is the main SpecSyn metric to reuse. For each function:

1. Verify the spec on the original program.
2. Create a small set of non-equivalent mutants.
3. Re-run the same spec against each mutant.
4. Count a mutant as killed if verification fails because the mutant violates
   the spec.

Initial mutation set should be small and deterministic:

- relational operator flip: `<` to `<=`, `==` to `!=`
- constant off-by-one: `N` to `N + 1` or `N - 1`
- return expression replacement: `x` to `0`, `x + y` to `x - y`
- removed null/bounds check
- loop bound off-by-one for loop-centric cases

Do not start with 188 operators. SpecSyn uses a large mutation set, but our
first decision is whether this metric distinguishes useful specs in our code.

### 4. Overconstraint / Witness Preservation

Question: does the spec exclude real caller states or known bug witnesses?

This metric is BMC-Agent-specific and should be reported separately from VDR.
It catches the failure mode where an LLM spec adds a precondition strong enough
to make CBMC clean while declaring away the bug.

Concrete example from the private findings repo:

- `ncdev_bar_read` trivial-spec mode exposed a heap OOB candidate.
- A later LLM-generated spec added `valid_range(reg_addresses, 0, data_count)`.
- That precondition made the callee verify clean, but it excluded the caller
  state where the buffer has one element and `data_count > 1`.

For each known-bug case, the spec must preserve at least one known bug witness
or documented violating caller state. If it rejects that state via `assume` or
`requires`, mark it as overconstrained.

### 5. Downstream Utility

Question: does the spec help prove or find the target property that matters?

Report this separately by benchmark type:

- ACSL verification benchmark: target assertions proved.
- BMC bug-finding benchmark: known bug rediscovered, false positives filtered,
  timeout/error.

Do not collapse these into one "accuracy" number unless the benchmark has a
common oracle.

## Proposed Minimal Pilot

The first pilot should answer whether the metric stack is useful, not whether
one method wins globally.

### Case A: Scalar Functional Spec

Use `experiments/acsl_backend_pilot/max2.c`.

Purpose:

- tests DSL-to-ACSL translation
- has simple human reference behavior
- mutation score should distinguish `ensures \result >= x && \result >= y`
  from `ensures \true`

Expected runnable checks:

- ACSL original validity with Frama-C/WP
- DSL original validity with CBMC if a matching harness exists
- 3-5 return/comparison mutants

### Case B: Bounds / Pointer Spec

Use a small extracted C function with pointer validity and length bounds before
loop-heavy cases. Candidate sources:

- a simple SV-COMP reachability program translated to C/assertions
- a small AutoRocq SV-COMP-origin case after assertion recovery

Purpose:

- tests whether generated specs capture memory-safety preconditions without
  becoming vacuous
- introduces a non-trivial `requires`/`assume` boundary

Stop if Frama-C needs loop invariants that are not present; that would answer
the first decision and should not become a proof-engineering sweep.

### Case C: Known Overconstraint Bug

Use `ncdev_bar_read` from the private findings repo as a design case, not a
full KASAN reproduction.

Purpose:

- checks whether a generated spec preserves the violating caller state
- demonstrates why BMC-Agent needs an overconstraint metric in addition to
  SpecSyn-style VDR

Minimal check:

- encode the caller-state witness:
  `bar != 0`, allocated address count is `1`, and `data_count > 1`
- evaluate whether the generated precondition accepts or rejects it
- compare trivial/no-spec mode vs LLM-spec mode

## Recommended Reporting Table

Use one row per method/spec per case:

| case | method | spec form | original valid | reference coverage | mutants killed / total | overconstraint | downstream target | notes |
|---|---|---|---:|---:|---:|---|---|---|

Do not add rows for methods that cannot emit or be translated into a meaningful
spec for the case. Record "not applicable" with the reason instead.

## Implementation Plan

1. Add a small mutation harness runner under `experiments/spec_quality_compare/`.
2. Reuse `bmc_agent/spec_quality.py` for native DSL mutation scoring where
   possible, but treat its current string mutations as a smoke mechanism rather
   than final VDR.
3. Extend the ACSL pilot path to run the same translated contract on mutated
   C sources.
4. Add a witness-preservation checker for known-bug cases. Start with
   handwritten predicates instead of asking an LLM to judge them.
5. Run only the three pilot cases above. Expand only if the metrics separate
   weak/trivial specs from useful specs.

## Immediate Recommendation

For the paper comparison, frame SpecSyn as providing the best evaluation idea:
spec strength should be measured by mutant/variant rejection. Then argue that
BMC-Agent needs one extra axis, overconstraint/witness preservation, because
BMC-style specs are executable assumptions as well as assertions.

The first executable experiment should be:

1. `max2`: demonstrate common ACSL-layer comparison.
2. one small bounds/pointer case: demonstrate memory-safety precondition
   handling.
3. `ncdev_bar_read`: demonstrate overconstraint detection.

This is enough to decide whether a larger ACSL-vs-DSL comparison is worth
running.
