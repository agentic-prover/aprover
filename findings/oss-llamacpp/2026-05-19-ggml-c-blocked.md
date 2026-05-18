# ggml.c (llama.cpp / ggml-org) — run blocked, no verdict

**Date**: 2026-05-19
**Source**: `ggml/src/ggml.c`, llama.cpp `main` checkout 2026-05-18
**Target functions**: all 439 functions defined in the TU
**bmc-agent config**: `--real-libc`, `--enable-realism-check
--enable-realism-thinking`, `--enable-dynamic-validation
--enable-feedback-loop --enable-flag-selection`, `--threat-model
security`. LLM: `anthropic/claude-sonnet-4.5` via OpenRouter.

## Result

**No verdicts.** 0 functions completed CBMC verification. Phase 1
generated 439 specs and Phase 2 wrote 439 harnesses, but every single
CBMC invocation failed at the conversion stage. Two distinct failure
modes:

| Failures | Class                                                |
|----------|------------------------------------------------------|
|    403   | CBMC: `failed to find symbol 'GGML_VERSION'` / `'GGML_COMMIT'` |
|     34   | bmc-agent harness bug: `assert(ctx, expr)` — 2-arg `assert` |
|      2   | bmc-agent harness bug: `ggml_tensor * x = ...` — no `struct` keyword for non-typedef'd struct |
|      0   | actually verified                                    |

## Root causes

1. **Build-time macros.** `ggml.c:515` is `return GGML_VERSION;`,
   `ggml.c:519` is `return GGML_COMMIT;`. Both are defined by CMake
   via `-DGGML_VERSION="${GGML_INSTALL_VERSION}" -DGGML_COMMIT="${GGML_BUILD_COMMIT}"`
   (`ggml/CMakeLists.txt:410-411`). With `--real-libc` mode CBMC
   parses the source `ggml.c` directly, sees the unresolved
   identifiers, and aborts conversion. Every harness `#include`s
   the same `ggml.c`, so every harness fails identically.

2. **`assert(ctx, ...)` arity.** The LLM-generated postconditions
   call C `assert()` with two arguments — `assert(ctx, result != NULL)` —
   which the glibc macro rejects. Likely the model is producing
   pseudo-code with the context as a debugging tag. Spec prompt
   should explicitly forbid multi-arg `assert`.

3. **`struct ggml_tensor` is not a typedef.** Harness emits
   `ggml_tensor * result = ggml_cont_1d(...);` whereas the C declaration
   requires `struct ggml_tensor *`. The harness generator must check
   whether a type name has an actual `typedef` before omitting the
   `struct` keyword.

## What this tells us about bmc-agent

Failure mode #1 is high-leverage to fix because it kills entire runs
silently. Two reasonable fixes:
- **Auto-stub**: parse CBMC error log, find `failed to find symbol
  'X'`, restart that function with `-D X='"undef"'` (or `-D X=0`
  depending on use context). Bounded retry.
- **Fail-fast**: if the same undefined symbol blocks > N functions
  in a row, surface a single clear `--define` recommendation at the
  end of Phase 2 and exit non-zero. Today this is buried in 439 DEBUG
  lines that read as "feedback loop chose to skip".

Failure modes #2 and #3 are isolated harness bugs but each
represents a small handful of functions across any C codebase.

## Coverage caveat

This is not a "verified clean" result. ggml.c is the largest single
TU in llama.cpp and the run produced no information about it. The
clean-verify on the sister file `ggml-alloc.c` stands; ggml.c
needs a re-run with `-D GGML_VERSION='"dev"' -D GGML_COMMIT='"dev"'`
plus the harness fixes above before any conclusion can be drawn.
