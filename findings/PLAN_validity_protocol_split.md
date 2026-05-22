# Plan — validity / protocol split in spec_gen

**Created:** 2026-05-22, end of session.
**Status:** plan only, no code changes yet. Pick up here next session.
**Motivation:** the `ncdev_bar_read` "caller-contract slip" documented in
`findings/methodology_insight_2026-05-22.md`. LLM-spec mode verifies the
bug clean because the LLM-emitted PRE (`valid_range(reg_addresses, 0,
data_count)`) is the very contract the buggy caller violates, and
bmc-agent stubs the callee by *assuming* that PRE — so the mismatch
disappears from both verifications.

## Problem statement

A single PRE predicate plays two roles in Hoare logic:

- **Assumption** when verifying the callee body in isolation.
- **Obligation** when verifying a caller (must hold at every call site).

Current bmc-agent collapses them: callee stubs in
`harness_generator._emit_callee_stub` use `precond_to_assume(...)` —
i.e., they ASSUME the PRE inside the stub body. That means a caller
that passes garbage matching the stub's assumed PRE is verified clean,
even though the same caller in reality violates the callee's contract.

The fix is to **distinguish two clause classes inside the PRE**:

| Class | Asserted at call site? | Assumed for callee verification? | Examples |
|---|---|---|---|
| **validity** (internal soundness) | YES | YES | `valid_range(p, 0, n)`, `n <= alloc_size(p)`, `p != NULL`, `i < len` |
| **protocol** (caller cooperation) | NO (default) / YES (paranoid mode) | YES | `device_initialized`, `lock_held`, `state == READY`, `ref_count > 0` |

Validity clauses describe what the callee body literally requires for
memory safety — they MUST be discharged by every caller. Protocol
clauses encode higher-level invariants callers maintain.

## Architecture decision

Introduce **two-clause-class specs** end-to-end:

1. `Spec.precondition` stays as-is for backwards compatibility (full PRE).
2. Add `Spec.pre_validity: str` and `Spec.pre_protocol: str` as
   structured sub-fields. `pre_validity ∧ pre_protocol ≡ precondition`.
3. Default at parse time: if the LLM doesn't supply the split, run a
   classifier pass that bucket clauses into validity / protocol.
4. Two verification modes (`--spec-mode=bug-hunt` / `--spec-mode=functional`,
   plus existing `--strict-dsl` trivial-spec mode):
   - **bug-hunt**: at call sites, ASSERT `pre_validity` (caller's
     obligation), ASSUME `pre_protocol`. POST reduced to memory-safety
     witness only.
   - **functional**: at call sites, ASSUME both (current behaviour),
     ASSERT full POST.
   - Trivial-spec mode (`--strict-dsl`): unchanged — PRE = `true`,
     CBMC sees full input space.
5. Recommended deployment: run **both** bug-hunt and functional per
   target. The trivial-spec mode is still useful as a sanity floor.

## Concrete code changes

### Phase 1 — data model

- **`bmc_agent/spec.py`**: extend `Spec` dataclass:
  ```python
  pre_validity: str = ""    # asserted at call sites
  pre_protocol: str = ""    # assumed for callee verification
  ```
  Update `to_dict` / `from_dict` (backwards-compatible: missing fields
  default empty; if both empty, fall back to `precondition` treated as
  all-validity for safety).
- **Tests:** round-trip `to_dict`/`from_dict` with split fields;
  back-compat for old JSON missing the split.

### Phase 2 — LLM emission

- **`bmc_agent/prompts.py`**: extend the spec-gen system prompt to
  emit a structured split. Add a "Classify each PRE clause" instruction
  with these guidelines:
  - validity: pointer in-bounds, index in range, NULL guards for
    dereferenced pointers, integer ranges required for the body to
    avoid UB.
  - protocol: object initialization status, lock held, ref counts,
    state-machine state, global handler tables populated.
- **`bmc_agent/spec_generator.py`**: parse the structured response into
  `pre_validity` + `pre_protocol`. Fall back to classifying a flat PRE
  by a regex/keyword heuristic when the LLM emits the old single-PRE
  format.

### Phase 3 — harness emission (the actual fix)

- **`bmc_agent/harness_generator.py`** `_emit_callee_stub` (line ~502):
  - Currently: `precond_to_assume(callee_spec.precondition, ...)` →
    emits `__CPROVER_assume(...)` for entire PRE.
  - New: at the **call site** (not inside the stub body), emit
    `__CPROVER_assert(pre_validity)` to make the caller discharge it.
    Inside the stub body, only `__CPROVER_assume(pre_protocol)`.
  - This is the call-site rewrite — the stub stays nearly the same,
    but the CALL gets prefixed with assertions on the actual arguments.
  - Translate `pre_validity` clauses by substituting formal parameter
    names with the actual call-site argument expressions
    (`reg_addresses` → caller's `reg_addresses` local, etc.). Re-use
    the existing `precond_to_assume` machinery but emit `__CPROVER_assert`
    instead, and run argument substitution.
- **Mode flag:** wire a `spec_mode` arg through `pipeline.py` →
  `harness_generator.py`. Default `bug-hunt` for the new flag;
  pre-existing tests use `functional` to keep current behaviour.

### Phase 4 — CLI / pipeline

- **`bmc_agent/cli.py`**: add `--spec-mode={bug-hunt,functional,both}`.
  When `both`, run two passes per function and emit a combined verdict
  (`clean` only if both pass; `real_bug` if bug-hunt finds one;
  `functional_bug` if functional finds one).
- **`bmc_agent/pipeline.py`**: thread the mode through Phase 2 harness
  emission and Phase 3 classification.

### Phase 5 — validation

- **Regression:** existing test suite (684 currently passing) must
  remain green when `spec_mode=functional` (default for back-compat in
  tests).
- **Caller-contract slip:** add an integration test using a
  miniaturised `ncdev_bar_rw` / `ncdev_bar_read` pair. Assert that in
  `bug-hunt` mode bmc-agent flags the caller (CBMC reports the PRE
  assertion failure at the call site). In `functional` mode it
  remains clean.
- **Empirical:** re-run the 2026-05-22 Neuron P2 sweep on
  `neuron_cdev.c` with `--spec-mode=both`. Expect `ncdev_bar_read`
  call site in `ncdev_bar_rw` to surface as `real_bug` again, while
  the rest of the file's clean verdicts (47 functions) stay clean.
  Compare to the trivial-spec sweep's 54/118 — bug-hunt mode should
  recover or exceed that bug while keeping the LLM-spec precision
  gains.

## Files touched (rough estimate)

- `bmc_agent/spec.py` — +20 lines
- `bmc_agent/prompts.py` — +30 lines (new instructions)
- `bmc_agent/spec_generator.py` — +40 lines (parse + heuristic fallback)
- `bmc_agent/harness_generator.py` — +60 lines (call-site assertion,
  argument substitution)
- `bmc_agent/cli.py` — +10 lines
- `bmc_agent/pipeline.py` — +15 lines (thread the mode)
- `tests/test_spec_validity_protocol_split.py` — new file, +200 lines
- `tests/test_caller_contract_slip.py` — new file, +100 lines

## Risk / gotchas

1. **Argument substitution is fragile.** A PRE like
   `valid_range(reg_addresses, 0, data_count)` uses callee formal
   parameter names. At the call site, those become the caller's actual
   argument expressions. We need a robust substitution pass — re-use
   the source_parser machinery or build a small AST-level substitution
   for the DSL.
2. **DSL coverage.** `valid_range`, `__CPROVER_r_ok`, `__CPROVER_w_ok`
   already exist. Make sure the validity-clause classifier doesn't
   miss any DSL terms in current LLM output. Sample 20 specs from
   `findings/aws_neuron_driver/hybrid_p2_2026-05-22/` first to
   inventory the clause vocabulary.
3. **Heuristic fallback quality.** When the LLM emits a flat PRE
   (back-compat), the regex classifier needs to be conservative —
   "if in doubt, classify as validity" (asserting too much is
   discoverable as new FPs; assuming too much hides bugs, which is the
   problem we're fixing).
4. **POST handling.** The plan focuses on PRE, but POST has a dual
   issue: if the callee's POST is asserted in functional mode but
   doesn't reflect actual side effects, the caller verifies clean
   against fictional postconditions. Out of scope for this plan but
   worth a follow-up.
5. **Existing real-bug findings.** Any function currently flagged as
   `real_bug` in `findings/bounty/REAL_BUGS_FOUND.md` (specifically
   `jvp_utf8_next`) should still flag in bug-hunt mode. Re-run as a
   smoke test.

## Order of operations next session

1. Inventory clause vocabulary from `/tmp/aprover_neuron_hybrid_p2/`
   spec.json files (1h).
2. Implement Phase 1 (data model + tests) (1h).
3. Implement Phase 3 (harness emission, the actual behavioural change)
   — this is the bit that can be tested in isolation with a hand-written
   spec, before touching the LLM emission path (3-4h).
4. Add the caller-contract-slip integration test using `ncdev_bar_*`
   miniature (2h).
5. Verify the existing 684 tests stay green at `spec_mode=functional`.
6. Implement Phase 2 (LLM prompt + parser) (2-3h).
7. Implement Phase 4 (CLI wiring) (1h).
8. Empirical Phase 5 on `neuron_cdev.c` with `--spec-mode=both`.

Total estimate: 1-1.5 dev days for a working prototype, +0.5 day for
the empirical re-run.

## Paper-track payoff

This converts the methodology insight in
`methodology_insight_2026-05-22.md` from a "limitation we discovered"
into an "architectural feature" of bmc-agent:

> Compositional verification with inferred specs requires distinguishing
> obligation clauses from assumption clauses. Trivial-spec is the
> degenerate case where all clauses are dropped (no precision);
> LLM-spec without classification collapses both into assumption
> (precision but blind to caller-contract slips); the validity/protocol
> split recovers both.

Promote from Discussion section (current placement) to a concrete
Method-section contribution backed by the `ncdev_bar_read` case study
showing all three modes — trivial-spec catches it, LLM-spec misses it,
validity/protocol split catches it AND keeps the functional-correctness
checks on the other 84 clean functions.
