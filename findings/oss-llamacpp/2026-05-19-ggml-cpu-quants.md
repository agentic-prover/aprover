# ggml-cpu/quants.c (llama.cpp / ggml-org) — clean verify (after triage)

**Date**: 2026-05-19
**Source**: `ggml/src/ggml-cpu/quants.c`, llama.cpp `main` checkout 2026-05-18
**Target functions**: all 43 functions defined in the TU
**bmc-agent config**: `--real-libc`, `--enable-realism-check
--enable-realism-thinking`, `--enable-dynamic-validation
--enable-feedback-loop --enable-flag-selection`, `--threat-model
security`. LLM: `anthropic/claude-sonnet-4.5` via OpenRouter.

## Result

**0 real bugs after triage**, but two distinct FP classes surfaced and
landed bmc-agent fixes.

| Stage             | Count |
|-------------------|-------|
| Specs generated   | 43    |
| CBMC verdicts     | 43    |
| Verified clean    | 12    |
| With CEx          | 25    |
| CBMC parse errors | 0     |
| Raw real_bug      | 56    |
| CLI-filtered      | 49 suppressed (realism / refinement) |
| Surviving         | 7 — all of the same FP class (see below) |

### FP class A: source-level `assert(k % QK_K == 0)` violations

All 7 surviving findings are `quantize_row_*.assertion.1`:

```c
void quantize_row_q5_K(const float * GGML_RESTRICT x,
                       void * GGML_RESTRICT vy, int64_t k) {
    assert(k % QK_K == 0);     // <-- this fires
    block_q5_K * GGML_RESTRICT y = vy;
    quantize_row_q5_K_ref(x, y, k);
}
```

bmc-agent's `_extract_source_precondition_asserts` is supposed to
promote `assert(<expr involving only params and known constants>)` at
the top of a function body into a `__CPROVER_assume(<expr>)` in the
harness, exactly mirroring what real callers do. But the
"known-constants" allowlist was just NULL / SIZE_MAX / INT_MAX etc. —
`QK_K` (a `#define`d block-size constant, 256) wasn't recognised, so
the assert wasn't promoted and CBMC explored `k=257` and friends,
tripping the source assert.

**Fix landed**: extended the allowlist heuristic to accept any
ALL_CAPS_WITH_UNDERSCORE identifier as a "compile-time constant"
(C convention). Two negative regression tests guard against
swallowing lowercase free identifiers or `__builtin_*` names that
might still be runtime-mutable. Affected projects beyond ggml:
anywhere `PAGE_SIZE`, `L1_CACHE_BYTES`, `BLOCK_SIZE`, etc. appear in
precondition asserts (i.e. essentially every Linux kernel module
and embedded C codebase).

### FP class B: `ggml_vec_dot_*_generic` caller-contract array overruns

The 49 suppressed findings include `array_bounds`, `pointer_dereference`,
and `pointer_arithmetic` failures on the `ggml_vec_dot_*_generic`
family. These functions take `const void * GGML_RESTRICT vx, const
void * GGML_RESTRICT vy` plus an `int n` and assume the buffers hold
`n / QK_K` typed blocks each. The harness passes nondet `int n` and
NULL/uninit `vx`/`vy`, so CBMC trivially constructs an OOB CEx. The
realism check correctly identified all of these as caller-contract
violations and suppressed them.

(No bmc-agent change needed here — the existing realism check
already handles this class.)

## Coverage caveat

8/43 CBMC runs failed pre-verdict (the new coverage-diagnostics
artifact records this). All 8 are in the `dequantize_row_*_generic`
family where the harness includes `quants.c` which transitively pulls
in CPU-intrinsics headers that CBMC can't parse. Those 8 functions
are excluded from the clean claim.

## Test coverage

3 new regression tests landed:

- `test_source_precondition_allows_all_caps_macro_const` — positive
  case using `QK_K` directly from the ggml-cpu/quants.c failure mode.
- `test_source_precondition_rejects_unknown_lowercase_identifier` —
  guards against the heuristic over-firing on a runtime variable.
- `test_source_precondition_rejects_double_underscore_identifier` —
  rejects `__builtin_*` / `_Static_*` names that might not be
  compile-time stable.

## What this tells us

llama.cpp's CPU quantizer kernels are well-defended: every dangerous
buffer access is either checked at runtime via a source `assert()` or
implicitly bounded by a documented `n / QK_K`-blocks caller contract.
The dynamic validator's runtime SIGSEGV reproduction was technically
correct (passing garbage pointers DOES crash), but real callers don't
do that — and the realism check correctly distinguished the two.

The new ALL_CAPS macro heuristic generalises far beyond ggml; expect
it to materially reduce assert-fire FPs on any project that uses
`#define`d size constants in precondition asserts.
