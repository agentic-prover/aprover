# claudes-c-compiler frontend/preprocessor/utils.rs — 2 real bugs

**Date**: 2026-05-19
**Source**: anthropics/claudes-c-compiler `master` checkout 2026-05-19,
`src/frontend/preprocessor/utils.rs`
**Target functions**: 8 declared, 6 verdicts produced (2 blocked by
`&mut` parameter — see "bmc-agent limitations" below)
**bmc-agent config**: Kani backend, `--enable-realism-check
--enable-realism-thinking --enable-feedback-loop --enable-flag-selection`,
`--threat-model security`. LLM: `anthropic/claude-sonnet-4.5` via
OpenRouter.

## Result

**2 real Rust panics confirmed.** Both functions are `pub`, both
have no documented preconditions, both crash on inputs that are
syntactically reachable from any external caller. All in-tree call
sites guard their arguments, so neither is currently exploited —
but they are latent panic-condition footguns added by Claude
during the AI-generated C-compiler build, exactly the bug class
bmc-agent was pivoted toward this session.

### Bug 1: `bytes_to_str` — slice-index panic on `start > end` or `end > len`

```rust
#[inline(always)]
pub fn bytes_to_str(bytes: &[u8], start: usize, end: usize) -> &str {
    std::str::from_utf8(&bytes[start..end])
        .expect("bytes_to_str: input is not valid UTF-8")
}
```

`&bytes[start..end]` panics with `slice_index_fail` when:
- `start > end`, OR
- `end > bytes.len()`, OR
- `start > bytes.len()`

`bytes_to_str` is `pub`. Every one of the ~9 in-tree callers (in
`frontend/preprocessor/expr_eval.rs`) tracks `start <= i <= bytes.len()`
through the surrounding parser loop, so none currently trip the
panic. But a future caller — a new preprocessor pass, a test, a
fuzzer harness — that passes user-derived offsets without that
invariant would crash CCC's preprocessor at runtime.

CBMC property hit:
`core::slice::index::slice_index_fail::do_panic::runtime.assertion.2`

### Bug 2: `skip_literal_bytes` — integer overflow on `start == usize::MAX`

```rust
pub fn skip_literal_bytes(bytes: &[u8], start: usize, quote: u8) -> usize {
    let len = bytes.len();
    let mut i = start + 1; // skip opening quote     // <-- overflows
    while i < len {
        ...
    }
}
```

`start + 1` overflows when `start == usize::MAX`. In debug builds
(Rust default for `cargo build`): panic with "attempt to add with
overflow". In release builds (`cargo build --release`, what CCC
ships): silently wraps to 0 and the loop re-reads `bytes[0]` —
quietly wrong, not a panic. The caller continues with a wrong
"position after closing quote" return value.

Both modes are bugs: the debug-mode panic is a hard crash; the
release-mode wraparound silently corrupts the preprocessor's
position tracking. Saturating-add (`start.saturating_add(1)`) or a
precondition `assert!(start < usize::MAX)` would fix it.

CBMC property hit: `skip_literal_bytes.assertion.1` — Kani's
overflow check. Description: "attempt to add with overflow".

## bmc-agent limitations surfaced

This run hit a recorded limitation:

> `copy_literal_bytes_raw`: &mut references in Kani harnesses are not yet supported

The other two functions in this file —
`copy_literal_bytes_raw(bytes, start, quote, result: &mut Vec<u8>)`
and `copy_literal_bytes_to_string(... result: &mut String)` — both
take `&mut` accumulator parameters. The current Kani backend rejects
`&mut` params with a clean error in `_param_init_block`.

Adding `&mut` support means: (a) allocate a backing slot for the
referent, (b) take a mutable borrow at the call site, (c) make sure
the postcondition can still reference the borrow after the call.
Tractable but non-trivial. Filed as next-up improvement.

## What this tells us about claudes-c-compiler

The 6 verdicts split 4 verified-clean / 2 real-bug. The clean four
are the `is_ident_*` family — straight char-class predicates that
Claude got right on the first try. The two buggy ones are exactly
the byte-index-manipulation helpers, which is the canonical place
LLMs get C-style code subtly wrong (off-by-one, missing precondition,
unhandled-edge-case-on-MAX-input). Claude wrote the entire compiler
front-to-back; the preprocessor utils having two such bugs after
~2.7k stars and presumably some manual testing speaks to the
strengths of bounded model checking over pure code review for
AI-generated systems code.

## Test coverage / commits this session enabling the run

To get to a working CCC verification, bmc-agent needed five
Rust-pipeline fixes (committed and pushed to
`test/bmc-action-selftest-1`):

- `26f9521` — auto-retry Kani timeouts with shrinking `slice_bound`
- `7dc9c5a` — tighten retry wall-clock to 60s
- `9e31a9f` — strip `use crate::*` + non-target sibling fns
- `5a1df60` — separate retry on "loop unwind bound exhausted"
  (bumps unwind to 16, distinct from timeout path)
- `9bfc231` — also strip impl blocks from the harness

After all five, 536 tests pass; this run was the first to produce
real-bug findings from an AI-generated Rust crate.
