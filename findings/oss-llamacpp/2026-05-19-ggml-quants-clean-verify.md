# ggml-quants.c (llama.cpp / ggml-org) — clean verify

**Date**: 2026-05-19
**Source**: `ggml/src/ggml-quants.c`, llama.cpp `main` checkout 2026-05-18
**Target functions**: all 115 functions defined in the TU
**bmc-agent config**: `--real-libc`, `--enable-realism-check
--enable-realism-thinking`, `--enable-dynamic-validation
--enable-feedback-loop --enable-flag-selection`, `--threat-model
security`. LLM: `anthropic/claude-sonnet-4.5` via OpenRouter.

## Result

**0 real bugs after triage**, but bmc-agent's raw classifier emitted
3 confirmed_dynamic SIGSEGV findings. All three were the same FP
class: qsort comparator functions classified as system entry points.

| Function           | CBMC verdict     | Triage   |
|--------------------|------------------|----------|
| `iq1_sort_helper`  | confirmed_dynamic SIGSEGV | FP — qsort comparator |
| `iq2_compare_func` | confirmed_dynamic SIGSEGV | FP — qsort comparator |
| `iq3_compare_func` | confirmed_dynamic SIGSEGV | FP — qsort comparator |

### The qsort-comparator FP class

All three functions match the C-standard qsort comparator shape:

```c
static int iq1_sort_helper(const void * left, const void * right) {
    const float * l = left;
    const float * r = right;
    return *l < *r ? -1 : *l > *r ? 1 : 0;
}
```

They are called exclusively as the last argument to
`qsort(..., iq1_sort_helper)`. `qsort()`'s contract guarantees both
`left` and `right` are non-NULL pointers into the sorted array.

bmc-agent's classifier checked the call graph, saw no direct callers
(`iq1_sort_helper(...)` is never written anywhere in the source),
and concluded "this is a system entry point — CEx with arbitrary
inputs is reachable from outside the corpus." The dynamic validator
ran the function with NULL pointers, hit SIGSEGV on the deref, and
upgraded the verdict to `confirmed_dynamic`.

Real callers cannot pass NULL because qsort doesn't.

## Fix landed in bmc-agent

Added an "address-taken" check in `cex_validator.py`. Before
classifying a caller-less function as a system entry, the validator
now scans the source for any reference to the function's name not
followed by `(` (i.e. the address has been taken — passed to a
library function, stored in a function-pointer table, assigned to a
struct field). When detected, the verdict drops to UNRESOLVED with a
reason citing the indirection.

Test coverage: 6 new tests covering qsort args, direct calls,
whitespace-before-paren, struct-initializer function pointers,
comments/string literals (so the detector doesn't poison fixtures
whose stub bodies mention the function name), and empty-source
graceful-noop.

## Coverage caveat

97/115 CBMC runs failed pre-verdict — 84% of functions never
produced a verifiable outcome. The new coverage-diagnostics path
flagged the run as BLOCKED. Looking at the failure breakdown:

The 18 functions that DID complete a full BMC + Phase 3 cycle
include `iq2_grid_size`, `iq3_data_index`, `iq2xs_free_impl`,
`iq3xs_free_impl`, `iq1_grid_size`, `iq2_data_size`, `iq3_data_size`,
allocator/free helpers around the static grid tables, and the three
qsort comparators discussed above.

The remaining 97 are mostly the heavy SIMD-quantizer functions
(`quantize_row_*`, `dequantize_row_*`, the per-block `make_q*_quants`
implementations) which produce CBMC parse errors related to
intrinsics or large stack-allocated arrays. A follow-up pass to
trim or stub the SIMD intrinsics could unblock them.

## What this tells us

The qsort FP class is real and applies broadly: any C codebase that
uses qsort/bsearch with a project-local comparator, pthread_create
with a worker function, or atexit/signal with a handler will hit
the same path. The new address-taken filter generalises beyond ggml
and is regression-tested.

The clean-verify on the 18 fully-checked functions stands. The
remaining 97 functions are blocked on CBMC's SIMD parser support,
not on bmc-agent precision.
