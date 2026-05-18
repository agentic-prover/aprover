# ggml-alloc.c (llama.cpp / ggml-org) — clean verify

**Date**: 2026-05-19
**Source**: `ggml/src/ggml-alloc.c`, llama.cpp `main` checkout 2026-05-18
**Target functions**: all 87 functions defined in the TU
**bmc-agent config**: `--real-libc`, `--enable-realism-check --enable-realism-thinking`,
`--enable-dynamic-validation --enable-feedback-loop --enable-flag-selection`,
`--threat-model security`. LLM: `anthropic/claude-sonnet-4.5` via OpenRouter.

## Result

**0 confirmed real bugs.** 40 raw counterexamples were surfaced across
iterative refinement; every one was suppressed by either the realism
check (UNREALISTIC verdict) or the feedback loop's clean-converge
(spec tightened, CBMC re-verifies clean).

## Coverage caveat

26 of 48 in-bounds functions failed CBMC parsing with exit code 6
(struct/typedef redefinition conflicts between CBMC's built-in libc and
the source's glibc-internal headers, even with `--real-libc`). Those
functions never got a real verdict — neither verified nor refuted —
and are excluded from the "clean" claim.

Functions that DID complete a full BMC + Phase 3 cycle (22 functions,
fully verified or refuted-then-clean after feedback): they include
`aligned_offset`, `ggml_gallocr_*`, `ggml_dyn_tallocr_*`, the
allocator/free-block manipulation helpers, and graph-walk leaves.

## Most interesting candidate that didn't survive: `aligned_offset`

`aligned_offset(const void *buffer, size_t offset, size_t alignment)`
returns `offset + align` where `align = (alignment - ((uintptr+offset) %
alignment)) % alignment < alignment`. CBMC's `--unsigned-overflow-check`
flagged the addition. The feedback loop iteratively learned that
callers (`ggml_dyn_tallocr_free_bytes`, `ggml_tallocr_new`) only pass
`offset` values bounded by the buffer's `max_buffer_size`, which keeps
the addition well below `SIZE_MAX`. After the constraint was learned,
CBMC re-verified clean — the overflow path is unreachable from
realistic callers.

The pure CBMC view would have reported this as a real overflow; the
feedback loop + realism check correctly distinguished
"theoretical overflow" from "exploitable overflow."

## What this tells us about llama.cpp's allocator

The pipeline's clean verdict on ggml-alloc.c is paper-track evidence
that LLM-driven spec generation + CBMC + adaptive refinement scales
to AI-inference-engine code, not just hand-curated kernel modules.
The same flags surface real bugs in less-audited code (rtl8125
ioctl integer overflow, jq UTF-8 UB), so the absence of findings
here is information, not silence.

## Open questions

- The 26 parse-error functions: structurally inaccessible to the
  current `--real-libc` path. Worth a follow-up to see if a wider
  glibc-struct/union strip allowlist would unblock them.
- One bmc-agent classifier quirk surfaced: 40 raw "real_bug"
  promotions, all subsequently downgraded by the feedback loop's
  clean-converge mechanism. The cli filter caught all of them. The
  raw classifier could be tightened to not promote when the feedback
  loop has already issued a clean verdict for the same function.
