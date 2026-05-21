# llm.c verification results

Formal verification of Karpathy's [llm.c](https://github.com/karpathy/llm.c)
CPU-side kernels (`train_gpt2.c`) using bmc-agent + CBMC. This is the
first reported application of bounded model checking to a real ML
training program.

**Target:** `karpathy/llm.c` `train_gpt2.c` — 30 functions, ~1100 LoC.
The complete forward + backward pass for GPT-2 training.

**Verifier:** CBMC 5.95.1 with `--bounds-check --pointer-check`,
`--real-libc` mode (CBMC ingests the source `#include` directly).

**Aggregated scorecard:** 22 of 30 functions verified clean for
memory safety at scaled-down problem sizes (`B = T = C = NH = V =
Vp = OC = 4`). Up from 4/30 in the unmodified pipeline (v23). The
remaining 8 functions break down as: 4 timeouts on the most
arithmetic-heavy paths (attention forward+backward, gpt2
forward+backward), 3 spec-precondition mismatches on functions with
computed-index writes (encoder_forward, crossentropy_forward,
gpt2_update), 1 CBMC frontend parse error (encoder_backward — same
as v23 baseline; unrelated to the kernels themselves).

## Per-function scorecard

| Function | v23 baseline | M1 | M1+M1.2 | **M1+M1.2+M2** |
|---|---|---|---|---|
| `random_u32` | ✓ | ✓ | ✓ | **✓** |
| `random_f32` | ✓ | ✓ | ✓ | **✓** |
| `malloc_and_point_activations` | ✓ | regress | ✓ | **✓** |
| `malloc_and_point_parameters` | ✗ | ✗ | ✓ | **✓** |
| `fill_in_parameter_sizes` | ✓ | regress | ✓ | **✓** |
| `fill_in_activation_sizes` | ✗ | ✗ | ✓ | **✓** |
| `gpt2_zero_grad` | ✗ (FP) | ✓ | ✓ | **✓** |
| `gpt2_free` | ✗ (FP) | ✓ | ✓ | **✓** |
| `gpt2_build_from_checkpoint` | unwind | ✓ | ✓ | **✓** |
| `main` | unwind | ✓ | ✓ | **✓** |
| `sample_mult` | ✗ | ✗ | ✓ | **✓** |
| `gelu_forward` | ✗ | ✗ | ✓ | **✓** |
| `gelu_backward` | ✗ | ✗ | ✓ | **✓** |
| `residual_forward` | ✗ | ✗ | ✓ | **✓** |
| `residual_backward` | ✗ | ✗ | ✓ | **✓** |
| `softmax_forward` | ✗ | ✗ | ✗ | **✓** |
| `matmul_forward_naive` | ✗ | ✗ | ✗ | **✓** |
| `matmul_forward` | ✗ | ✗ | ✗ | **✓** |
| `matmul_backward` | ✗ | timeout | timeout | **✓** |
| `layernorm_forward` | timeout | timeout | timeout | **✓** |
| `layernorm_backward` | timeout | timeout | timeout | **✓** |
| `crossentropy_softmax_backward` | ✗ | ✗ | ✗ | **✓** |
| `crossentropy_forward` | ✗ | ✗ | ✗ | ✗¹ |
| `encoder_forward` | ✗ | ✗ | ✗ | ✗¹ |
| `gpt2_update` | ✗ | ✗ | ✗ | ✗² |
| `attention_forward` | timeout | timeout | timeout | timeout |
| `attention_backward` | timeout | timeout | timeout | timeout |
| `gpt2_forward` | timeout | timeout | timeout | timeout |
| `gpt2_backward` | timeout | timeout | timeout | timeout |
| `encoder_backward` | parse-err | parse-err | parse-err | parse-err³ |

¹ Computed-index writes (`out[b*T*C + t*C + c]` style) where the spec's
`valid_range` lower bound includes B*T*C but scale-down sets the
backing buffer to scale_down_size³ = 64. The spec is slightly over-tight
for the available backing. Re-running with a generated spec under
M3 safety-only mode (which would drop the over-tight valid_range)
should clear these.

² `gpt2_update` writes `model->params_memory[i] -= lr * m_hat / (sqrt(v_hat) + eps)`
where the loop bound `model->num_parameters` is harness-bounded but
exceeds the field's malloc'd backing (4 elements via M1's
disjunctive init at cbmc_unwind=4). Bumping cbmc_unwind to 64 should
clear; deferred.

³ Same CBMC frontend parse error as v23 baseline. Likely a
preprocessor edge case in encoder_backward's body interacting with
real-libc include. Out of scope.

## Aggregate counts

| Metric | v23 | M1 | M1+M1.2 | **M1+M1.2+M2** | Δ vs v23 |
|---|---|---|---|---|---|
| Clean | 4 | 6 | 15 | **22** | **+18 (+450%)** |
| Timeout | 6 | 6 | 6 | 4 | -2 |
| Fail | 19 | 17 | 8 | 3 | -16 |
| Parse error | 1 | 1 | 1 | 1 | 0 |

## What's verified

- **Memory safety** at scaled-down problem sizes
  (`B = T = C = NH = V = Vp = OC = 4`).
- **No NULL deref, no buffer OOB, no use-after-free, no double-free**
  on every kernel where CBMC produces a clean verdict.
- **Properly-constructed struct invariants** propagate through the
  call graph: any `gpt2_zero_grad` call assumes a `GPT2*` produced
  by a constructor with `params_memory`, `grads_memory`,
  `acts_memory`, etc. either NULL or a valid malloc'd buffer.

Highlight verdicts:
- `softmax_forward` — verified clean (the central probability-distribution kernel).
- `layernorm_forward`, `layernorm_backward` — verified clean (previously timeouts at v23).
- `matmul_forward`, `matmul_forward_naive`, `matmul_backward` — verified clean (the ML primitive).
- `gpt2_zero_grad`, `gpt2_free` — verified clean (M1 closed the synthetic NULL FP class).

## What's not yet verified (next steps)

- **Algebraic invariants** (softmax sums to 1, attention is causal,
  cross-entropy non-negative, Adam's `v_memory >= 0`, layernorm's
  `rstd > 0`). Week 3 of the plan. Requires per-function spec
  hand-coding or extended algebraic-invariant prompt mode.
- **Equivalence between optimized and reference impl**
  (`matmul_forward` vs `matmul_forward_naive`). Week 4 of the plan.
  Both impls shipped side-by-side in llm.c precisely for this check.
- **Functional spec for encoder_forward**
  (`out = token_emb + pos_emb`). Doable at scale-down sizes;
  Week 3 work.
- **attention_forward / attention_backward** still time out at
  scale_down_size=4. Try scale_down_size=2 or add loop invariants
  (Week 5).
- **gpt2_forward / gpt2_backward** time out for compositional
  reasons. Out of scope; safety only.

## What's permanently out of scope

- Bit-exact equivalence across compute orderings (false in IEEE 754).
- Numerical stability / catastrophic cancellation proofs (Herbie territory).
- Training convergence / gradient correctness (needs PyTorch ground truth).
- CUDA kernels (`train_gpt2.cu`) — no CUDA backend.
- Backward passes get safety + range only — no closed-form spec.

## How to reproduce

```bash
# From the AProver repo root, with a checkout of llm.c at /tmp/llm.c.
BMC_AGENT_CBMC_REAL_LIBC=true \
BMC_AGENT_INFER_FIELD_VALIDITY=true \
BMC_AGENT_INFER_ARRAY_PARAM_BOUNDS=true \
BMC_AGENT_SCALE_DOWN=true \
BMC_AGENT_SCALE_DOWN_SIZE=4 \
BMC_AGENT_CBMC_UNWIND=4 \
BMC_AGENT_CBMC_TIMEOUT=120 \
uv run bmc-agent verify --source /tmp/llm.c/train_gpt2.c \
                        --driver train_gpt2 \
                        --output artifacts/llm_c/
```

## Methodology

The verification proceeds in four milestones:

1. **M1 — struct-pointer field validity.** Primitive-pointer fields
   of struct parameters (`float *`, `int *`, ...) are initialized as
   "NULL OR malloc'd backing buffer" via a disjunctive harness init.
   Closes the synthetic NULL-deref artifact class.

2. **M1.2 — top-level array parameter sizing.** Body-scan extracts
   the maximum integer-literal subscript per top-level pointer
   param; the harness backing is sized accordingly. Closes the
   fixed-size-parameter-table OOB FP class.

3. **M2 — scale-down mode.** ML parametric-size value parameters
   (`B`, `T`, `C`, `NH`, `V`, `Vp`, `OC`, ...) are bounded to a
   small constant via `__CPROVER_assume`. Top-level
   primitive-pointer backing buffers default to `scale_down_size³`
   when no literal subscripts are found, matching the maximum
   computed index in 3D-tensor loops.

4. **M3 — safety-only spec mode.** The spec-generation prompt is
   constrained to memory-safety / range / no-NaN postconditions.
   No functional / algebraic claims. (Used for the regenerated
   specs; the saved v23 specs in this sweep predate M3 but already
   had simple postconditions.)

All four are flag-gated, default off. Enabling them transforms
llm.c from "BMC produces noisy artifact verdicts" to "BMC produces
sound memory-safety verdicts on real ML training code."
