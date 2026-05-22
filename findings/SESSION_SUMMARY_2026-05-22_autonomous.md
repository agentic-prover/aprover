# Session summary — 2026-05-22 (validity/protocol + autonomous tightening)

Companion to `SESSION_SUMMARY_2026-05-22.md` (the morning's AWS Neuron
hybrid sweep work). This document covers the afternoon's
prototype-build + autonomous-mode follow-up: shipping the
validity/protocol split, closing the feedback loop on both PRE and
POST clauses, and going one layer deeper to fix the root-cause
harness modelling issues that produced the relaxable FPs in the
first place.

## What landed

### Validity/protocol prototype (architectural)

- `Spec.pre_validity` / `pre_protocol` structured fields +
  `split_precondition()` (`bmc_agent/spec.py`).
- `classify_precondition()` heuristic: validity (default) vs protocol
  (locked / npid_is_attached / state / refcount / initialized names).
- `_generate_stub` in `harness_generator.py` accepts `spec_mode`:
  `functional` (assume the full PRE; back-compat default) or
  `bug-hunt` (assert validity, assume protocol). Three internal call
  sites threaded.
- `Config.spec_mode` + `BMC_AGENT_SPEC_MODE` env var + CLI
  `--spec-mode={functional,bug-hunt,both}` (both is queued, raises a
  clear NotImplementedError pointing at the two-pass workaround).
- Phase 1 LLM prompt extended with the optional pre_validity /
  pre_protocol JSON fields. Parser captures them and threads to the
  Spec object; back-compat 2-tuple iteration / equality on
  `ParsedSpec`.

### Feedback loop extensions

- `RemediationScope.CALLEE_SPEC_RELAX`: drops over-tight clauses
  from a callee's PRE on the next run. Triggered by
  `<callee>_stub.assertion.<N>` failures. Attached to the *callee*,
  not the FUT.
- `RemediationScope.FUNCTION_POST_RELAX`: symmetric POST-side relax.
  Triggered by `main.assertion.<N>` FUT-post violations that trace
  to an over-tight LLM-emitted POST.
- `LearnedConstraintsStore` gains `callee_relaxations` and
  `function_post_relaxations` slots (schema v1 compatible).
- `HarnessGenerator._callee_relaxations` /
  `_function_post_relaxations` readers; `_generate_stub` and
  `generate_harness` apply `spec.drop_clauses` before
  assert/assume translation. Paren-aware normalisation so a drop
  entry `(x == 0)` matches the post-split clause `x == 0`.

### Root-cause harness fixes (the layer below the feedback loop)

These pre-empt the FPs the feedback loop would have learned to relax,
moving the burden off iterative training:

- **`_kernel_api_return_contract`** now suffix-matches project-local
  wrappers like `neuron_copy_from_user` and emits a signed-cast form
  for unsigned return types. Previously, `result <= 0 && result >= -4095`
  was UNSAT on `unsigned long` (silently `assume(false)`, pruning all
  caller paths through the stub).
- **`_infer_extern_return_contract`** adds a `-4095` lower bound to
  non-positive sibling contracts so very-negative-long returns
  don't wrap to positive int in `int`-typed callers.
- **`_builtin_stub_return_contract`** allocator tables extended with
  the Linux `kmalloc_noprof` / `kzalloc_noprof` / `vmalloc_noprof` /
  `kcalloc_noprof` / `kmalloc_array_noprof` / `devm_k*` family.
  (Currently dormant on Neuron kernel TUs because the parser doesn't
  see calls inside GCC statement-expressions; defensive against
  future codepaths that do.)

### Infrastructure fixes

- `_strip_cpp_linemarkers` now strips the directive prefix even when
  no trailing newline separates it from code (`# 232 "path"static`
  shape in Linux .i files).
- `_generate_real_libc` strips `static` / `extern` / `inline` /
  `register` qualifiers from the local result-var declaration so
  static-function returns don't trip CBMC's "expected constant
  expression" check.

## Empirical: `ncdev_bar_rw` progression

| Run | Relaxations | Failures | Real-bug manifestations | FPs |
|-----|------------|---------|----|-----|
| functional (back-compat, hides the OOB) | n/a | 1 | 0 | 1 |
| bug-hunt no relax | none | **10** | 2 | 8 |
| bug-hunt + 3 PRE relax × 2 stubs | seed | 5 | 2 | 3 |
| bug-hunt + 4 PRE relax | seed | 4 | 2 | 2 |
| bug-hunt + 4 PRE + 1 POST (FUNCTION_POST_RELAX) | seed | 3 | 2 | 1 |
| bug-hunt **after root-cause fixes**, NO relax | none | **3** | 2 | 1 |

The two real-bug manifestations are the same root-cause heap-OOB-read
in `ncdev_bar_rw`'s call to `ncdev_bar_read` / `ncdev_bar_write` (line
1648/1650): `address_count = 1` when `arg.bar != 0`, but `arg.count`
is forwarded as `data_count` — the callee's loop walks past the
1-element allocation.

The 1 remaining FP after root-cause fixes is a harness-modelling
artefact: `kmalloc_noprof`'s call is buried in GCC
statement-expression macros and not detected as a callee, so the
returned buffer has no allocator-size contract.

## Test suite

**725 passing, 0 failing, 2 skipped** under `.venv/bin/python` (the
44 "baseline failures" seen earlier in the day were system python
running the regex parser fallback because tree-sitter wasn't
available there).

New tests in this session (across 5 files):
- `tests/test_spec_validity_protocol_split.py` (+20)
- `tests/test_caller_contract_slip.py` (+5)
- `tests/test_feedback_callee_spec_relax.py` (+9)
- `tests/test_callee_relaxation_consumption.py` (+11)
- `tests/test_function_post_relax.py` (+8)
- `tests/test_unsigned_kernel_api_contract.py` (+7)
- `tests/test_kernel_allocator_contracts.py` (+8)

Total: **+68 tests**.

## Files modified

```
bmc_agent/cli.py               (+21)
bmc_agent/config.py            (+15)
bmc_agent/dsl_to_cbmc.py       (+25)
bmc_agent/feedback_loop.py     (+95)
bmc_agent/harness_generator.py (+165)
bmc_agent/prompts.py           (+22)
bmc_agent/spec.py              (+260)
bmc_agent/spec_generator.py    (+105)
```

Plus 7 new test files and 4 new findings files (this summary +
`empirical_validity_protocol_2026-05-22.md` + 5 CBMC logs).

## What's not done

- **Disclosure-quality PoC.** The `ncdev_bar_read` heap-OOB-read is
  triple-confirmed statically (trivial-spec sweep, functional LLM-spec
  hidden-then-resurfaced, bug-hunt mode). No KASAN PoC executed on a
  Trainium/Inferentia host or QEMU+driver build yet — required for
  AWS disclosure.
- **GCC statement-expression call detection.** Without it,
  `kmalloc_noprof` and similar buried calls don't get allocator
  contracts — the last residual FP class on kernel TUs.
- **`--spec-mode=both`** dual-pass per-function with combined verdict
  (currently raises NotImplementedError pointing at the two-pass
  workaround).
- **End-to-end run of bug-hunt mode through the full pipeline
  (cli.py verify path)** including the LLM-driven realism check and
  the now-extended feedback loop. The unit + integration tests cover
  the mechanics; an LLM-in-the-loop sweep would confirm the
  CALLEE_SPEC_RELAX / FUNCTION_POST_RELAX paths actually fire on
  real driver code.
- **Commits.** The full diff (8 modified + 7 new test files + 4 new
  findings files) is uncommitted on `main`. The user has been
  deliberate about not authorising commits in autonomous mode; the
  diff naturally splits into ~3 logical groups:
  1. Validity/protocol prototype core (spec.py, harness_generator.py,
     dsl_to_cbmc.py, prompts.py, spec_generator.py, config.py, cli.py)
  2. Feedback loop extensions (feedback_loop.py + harness consumption)
  3. Root-cause harness fixes (the unsigned-cast + sibling lower
     bound + kernel allocator additions)
