# claudes-c-compiler frontend/preprocessor/utils.rs — 4 real bugs

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

**4 real Rust panics confirmed across 8 `pub` byte-index helpers.**
All four functions share the same AI-generated-code shape: a public
`fn helper(bytes: &[u8], start: usize, ...)` that indexes
`bytes[start]` and arithmetics on `start` with no bounds check
and no precondition documented. Every in-tree caller maintains
`start <= bytes.len()` through surrounding parser invariants, so
none are currently exploited — but the functions are `pub` API
surface and any future caller (a test, a fuzzer, a new pass) will
trip them.

The four findings landed only after this session's `&mut`-parameter
support in the Kani harness generator went in; the v1 run with the
older code surfaced only 2 of the 4 (the two functions whose
signatures used `&[u8]` rather than `&mut Vec<u8>` / `&mut String`).

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

### Bug 3: `copy_literal_bytes_raw` — `bytes[start]` OOB

```rust
pub fn copy_literal_bytes_raw(bytes: &[u8], start: usize, quote: u8,
                              result: &mut Vec<u8>) -> usize {
    let len = bytes.len();
    result.push(bytes[start]); // opening quote     <-- OOB if start >= len
    let mut i = start + 1;                          <-- overflow on usize::MAX
    while i < len { ... }
}
```

CBMC property: `copy_literal_bytes_raw.assertion.1` —
"index out of bounds: the length is less than or equal to the
given index". Same `bytes[start]` panic pattern as `bytes_to_str`
plus the same `start + 1` overflow as `skip_literal_bytes`. The
function combines both v1-discovered bug classes in one body.

### Bug 4: `copy_literal_bytes_to_string` — multiple

```rust
pub fn copy_literal_bytes_to_string(bytes: &[u8], start: usize,
                                    quote: u8, result: &mut String) -> usize {
    let len = bytes.len();
    let mut i = start + 1; // skip opening quote     <-- overflow
    while i < len {
        if bytes[i] == b'\\' && i + 1 < len { i += 2; }   <-- i+1 overflow
        else if bytes[i] == quote {
            i += 1;
            let slice = std::str::from_utf8(&bytes[start..i])   <-- slice OOB
                .expect("literal copy produced non-UTF8");
            ...
        }
        ...
    }
    let slice = std::str::from_utf8(&bytes[start..i])           <-- slice OOB
        .expect(...);
    ...
}
```

CBMC fired three distinct property failures on this function:
- `core::slice::index::slice_index_fail.assertion.1`
- `core::slice::index::slice_index_fail.assertion.2`
- `copy_literal_bytes_to_string.assertion.1` ("attempt to add with overflow")

Multiple latent panics in one body. Same family.

## bmc-agent improvement landed

This run also delivered an &mut-parameter capability to the Kani
harness generator. v1 (before the fix) found only 2 of these 4
bugs because the two `&mut`-taking functions were skipped with:

> `copy_literal_bytes_raw`: &mut references in Kani harnesses are not yet supported

Added support for three concrete &mut shapes in
`_param_init_block`:

- `&mut Vec<T>` — backing `Vec::new()` + `&mut backing`
- `&mut String` — backing `String::new()` + `&mut backing`
- `&mut <primitive>` — nondet scalar + `&mut backing`

Anything else (`&mut SomeUserStruct`) still falls through to the
NotImplementedError path. 3 regression tests; 539 passing.

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
