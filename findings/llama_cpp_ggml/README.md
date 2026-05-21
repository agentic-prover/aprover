# llama.cpp ggml C-side verification results

Application of bmc-agent's M1 + M1.2 + M2 stack to llama.cpp's
`ggml/src/ggml-alloc.c`. This is a follow-on to the llm.c work in
`../llm_c/` — the question being asked is whether the infrastructure
improvements that gave 22/30 clean on llm.c compound into bug-finding
leverage on a real ML inference codebase with active CVE history.

**Target:** `ggerganov/llama.cpp` (commit `45b455e`),
`ggml/src/ggml-alloc.c` — 48 functions, ~1250 LoC. The graph
allocator and dynamic memory management for ggml tensors.

**Verifier:** CBMC 5.95.1, `--bounds-check --pointer-check`,
`--unwind 4`, 60-second per-function budget. Real-libc mode.

**Configuration:** `infer_field_validity=True`,
`infer_array_param_bounds=True`, `scale_down=True`, `scale_down_size=4`,
`cbmc_real_libc=True`. Trivial specs (`precondition="true"`,
`postcondition="true"`) — checks ONLY memory safety, no LLM
spec generation.

## Aggregate scorecard

| Verdict | Count | % |
|---|---:|---:|
| VERIFIED | 14 | 29% |
| FAIL (harness FP) | 23 | 48% |
| TIMEOUT | 6 | 13% |
| COMPILE_ERR | 5 | 10% |
| **Real bugs found** | **0** | **0%** |

## Triage: classification of the 23 FAILs

Every FAIL was inspected. They sort into four classes, all
**harness-induced false positives**, none real bugs:

### Class A — Top-level handle NULL deref (most common)

Functions take an opaque handle (`ggml_gallocr_t`, `struct vbuffer *`,
`struct ggml_dyn_tallocr *`) and dereference it without a NULL
check. Examples:

- `ggml_gallocr_needs_realloc.pointer_dereference.1`:
  `pointer NULL in galloc->n_nodes` at line 1009.
- `ggml_gallocr_hash_get.pointer_dereference.1`:
  `pointer NULL in galloc->hash_values` at line 585.
- `ggml_gallocr_get_buffer_size.pointer_dereference.1`:
  same shape.
- `ggml_gallocr_needs_realloc.pointer_dereference.1`,
  `ggml_dyn_tallocr_free.pointer_dereference.{1,3}`,
  `ggml_dyn_tallocr_max_size.pointer_dereference.13`,
  `ggml_dyn_tallocr_reset.pointer_dereference.3`,
  `free_buffers.precondition_instance.2` (free on nondet handle),
  `ggml_vbuffer_alloc.pointer_dereference.13`.

**Verdict:** defensive-programming gaps, not bugs. The C library
tradition is to assume caller-provided handles are non-NULL by
convention. The public API documentation (`ggml-alloc.h`) does NOT
explicitly forbid passing NULL, but `ggml_gallocr_new_n` (the only
documented way to obtain a `ggml_gallocr_t`) uses
`GGML_ASSERT(galloc != NULL)` to abort on allocation failure. A
caller who *would* see NULL is either:
(a) using a stale / freed pointer (use-after-free)
(b) intentionally passing NULL (caller error)

Same severity class as Karpathy's llm.c issue #10 (`calloc` NULL not
handled). Maintainers typically close these as "don't pass NULL."

### Class B — Precondition-propagation gap

`aligned_offset.assertion.1` fires inside `aligned_offset`'s body
(`assert(alignment && !(alignment & (alignment - 1)))`) because
nondet `alignment` can be 0 or non-power-of-2. Affected callers:

- `ggml_dyn_tallocr_free_bytes`
- `ggml_gallocr_allocate_node`
- `ggml_gallocr_free_extra_space`
- `ggml_tallocr_new`

Real callers always pass `alloc->alignment` which the constructor
guarantees is power-of-2 by `assert(align && !(align & (align - 1)))`.
bmc-agent doesn't propagate constructor invariants through callees.

**Verdict:** harness gap; cross-function precondition propagation
would fix it. Research-grade (M5+).

### Class C — Struct-pointer-field harness gap

M1 currently handles **primitive-pointer** struct fields (`float *`,
`int *`, ...) with NULL-or-malloc'd disjunctive init. It does NOT
handle struct-pointer or pointer-to-pointer fields. Failures:

- `ggml_gallocr_hash_get.pointer_dereference.1` — `galloc->hash_values`
  is `struct hash_node *`
- `ggml_dyn_tallocr_reset.array_bounds.1` — `alloc->chunks` is
  `struct tallocr_chunk **`
- `ggml_gallocr_init_tensor.assertion.1` + `pointer_dereference.19`
  — `galloc->bufts` is `ggml_backend_buffer_type_t *` (an opaque
  typedef'd pointer)

**Verdict:** addressable as **M1.3** — extend the disjunctive init
to struct-pointer and pointer-to-pointer fields. Likely 1-2 days of
work. Would clear about half of the FAIL set.

### Class D — Unbounded-index parameter

`ggml_vbuffer_chunk_size(struct vbuffer *buf, int chunk)` does
`buf->chunks[chunk]` without validating `chunk in [0,
GGML_VBUFFER_MAX_CHUNKS)`. Affected:

- `ggml_vbuffer_chunk_size.array_bounds.1`
- `ggml_vbuffer_tensor_alloc.array_bounds.1`
- `ggml_gallocr_node_needs_realloc.pointer_dereference.19`
- `get_node_buffer_id.pointer_dereference.5`
- `ggml_tallocr_alloc.assertion.1`

**Verdict:** caller-maintained invariant ("index is in range relative
to a sibling parameter") that the harness doesn't infer. Same shape
as Class B but on indices instead of values. Addressable via
**M1.4** — index-vs-bound sibling-parameter detection.

## Net conclusion

**The M1+M1.2+M2 stack ported from llm.c is NOT sufficient for
ggml-alloc.c.** The codebase uses more complex pointer structures
(opaque typedefs over struct pointers, pointer-to-pointer fields,
caller-maintained handle-validity invariants) than llm.c's
straightforward float-array kernels.

The 14 clean verdicts demonstrate the infrastructure works on this
codebase; the 23 FAILs are all harness-shape false positives, none
real bugs.

**No GHSA-worthy finding. Result: 0 actionable findings.**

## What would unlock real bug-finding here

1. **M1.3 (struct-pointer field validity).** Same shape as M1 but for
   struct-pointer fields. Clears ~8 of the 23 FAILs. Estimated 1-2 days.

2. **M1.4 (sibling-parameter index bounds).** Detect patterns like
   `f(T *buf, int i)` where `i` indexes `buf->something[i]`. Constrain
   `i` to a bound derived from the struct's array field size. Clears
   ~5 more. Estimated 2-3 days.

3. **M1.5 (caller-handle non-NULL inference).** Detect that an opaque
   handle parameter (typedef'd struct pointer) is universally
   non-NULL in callers. Emit `__CPROVER_assume(handle != NULL)`.
   Clears ~9 more. Estimated 1-2 days. **However**, this would mask
   any real defensive-programming bugs the library DOES have, so
   it's a tradeoff — opt-in via flag.

After all three, the expected verdict on ggml-alloc.c would be
roughly 30-35 clean, 2-3 timeout, 5 compile-err. **At which point
the remaining failures, if any, would be candidates for real
triage.** Today, the signal-to-noise is too poor to identify a real
bug.

## What's permanently out of scope for this target

- C++ files (`ggml-backend.cpp`, `ggml.cpp`, the GGUF parser, the
  vocab loader, the server) — no C++ frontend in bmc-agent.
- CUDA / Metal / Vulkan backends — no backend.
- Functions that depend on `ggml-cpu` SIMD intrinsics — CBMC's
  SIMD model is incomplete.

## Honest framing

The expected outcome of this exercise was either:
1. Real bug found (10-20% probability)
2. Clean negative result confirming ggml C-side is solid (70%)
3. Infrastructure-blocked (10-20%)

The actual outcome is #3: bmc-agent is infrastructure-blocked on
this codebase. We hit harness gaps M1 didn't address. **This is a
useful negative result** in that it tells us EXACTLY what bmc-agent
improvements are needed for ggml-class targets to be approachable.

The next move is M1.3 (struct-pointer field validity) — same code
shape as M1, generalizes the disjunctive-init pattern from
primitive-pointer fields to struct-pointer fields.

## Files in this directory

- `README.md` — this file
- `scorecard_ggml_alloc.json` — per-function verdict JSON
- `sweep_ggml_alloc.log` — raw sweep log with verdicts and elapsed
  times for each of the 48 functions

## Dedup against published llama.cpp GHSAs

Cross-checked against the 13 published advisories on llama.cpp.
Closest match: **GHSA-mqp6-7pv6-fqjf** (`global-buffer-overflow in
ggml_type_size`, 2024-08, medium). That bug is in the RPC backend's
unsafe `type` field, not in `ggml-alloc.c`. **No overlap with our
findings (which is moot since we have no findings).**
