# claudes-c-compiler common/encoding.rs — 1 real bug

**Date**: 2026-05-19
**Source**: anthropics/claudes-c-compiler `master` checkout 2026-05-19,
`src/common/encoding.rs`
**Target functions**: 4 declared, 3 verdicts produced
**bmc-agent config**: Kani backend, `--enable-realism-check --enable-realism-thinking --enable-feedback-loop --enable-flag-selection`, `--threat-model security`. LLM: `anthropic/claude-sonnet-4.5` via OpenRouter.

## Result

**1 real Rust panic confirmed in `decode_pua_byte`.** The function indexes
`input[pos]` without verifying `pos < input.len()`. All in-tree callers
guard the precondition via surrounding loop invariants, so the panic
isn't triggered by current code paths; cargo-fuzz or any future caller
that doesn't maintain the invariant will crash.

### Bug: `decode_pua_byte` — slice OOB on `input[pos]`

`&[u8]` indexing panics with `slice_index_fail` when `pos >= input.len()`.
The function is `pub` and accepts `pos: usize` with no constraint.

**CBMC property hit**:
`core::slice::index::slice_index_fail::do_panic::runtime.assertion.N`

**Caller guards (in-tree)**: every call site is inside a `for pos in
0..input.len()` style loop or guarded by an explicit length check.

**Classification**: REAL_BUG under `--threat-model security` (PUA byte
streams from external sources can drive `pos >= len`). LATENT under
safety/functional.

**Fix sketch**: take `&[u8]` + a typed `Index` newtype, or return
`Option<...>` so callers must handle the out-of-range case.

## bmc-agent improvement landed

None specific to encoding.rs — bug surfaced through the standard
defensive spec workflow. The `_param_init_block` slice-bound exploration
of the `&[u8]` parameter with adversarial `pos` was sufficient.
