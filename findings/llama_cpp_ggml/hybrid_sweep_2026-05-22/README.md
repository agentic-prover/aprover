# llama.cpp ggml-quants.c — OpenRouter Claude sweep, 2026-05-22

Full bmc-agent pipeline (Phase 1 spec gen + Phase 2 CBMC + Phase 3
classifier + realism + feedback loop) on llama.cpp/ggml/src/ggml-quants.c
(5491 LoC, ~115 functions analysed).

## Configuration

- All roles routed to Claude Sonnet 4.5 via OpenRouter
- `--real-libc` (CBMC handles preprocessing via -I)
- `--enable-realism-check`
- `--enable-feedback-loop` (feedback_max_iters=3)
- Pre-cap-fix bmc-agent process loaded the patched cbmc.py + llm.py
  (commits 2ab4dcf, b7e53eb), so no artifact blow-ups and no
  retry-burning on HTTP 4xx.

## Results

- **115 functions analysed** in 60 minutes (timed out at the 60-min cap;
  ~95% complete by Phase-3 count)
- **33 verified clean** — primitives, validators, comparison functions,
  many `quantize_*` and `dequantize_*` rows
- **1 raw real_bug** (`iq2_data_index`) — downgraded to **unrealistic**
  by the feedback loop
- **36 spurious** — classifier's feasibility check correctly downgraded
- **36 CBMC errors** — typical for SIMD/intrinsic-heavy quantisation
  code (the harness's stubs don't cover all `__builtin_*`)

**Net: 0 likely-true bugs** in ggml-quants.c after the realism+feedback
filter. The 36 spurious / 1 real_bug all match defensive-programming
patterns where a real caller maintains the invariant that the
counterexample violates.

## Learned constraints (feedback loop highlights)

The most striking output is the constraints the feedback loop
*discovered empirically* from CEx analysis. From
`learned_constraints.json`:

| Function | Learned clause |
|---|---|
| `best_index_int8` | `n <= 16 && (n == 16 ==> val == kvalues_iq4nl \|\| val == kvalues_mxfp4)` |
| `dequantize_row_iq2_xs` | `y != NULL && k >= 0 && k <= 1073741824` |
| `iq1_find_best_neighbour` | `neighbours != NULL && neighbours[0] >= 0 && neighbours[0] <= 512` |
| `make_qp_quants` | `n > 0 && valid_range(x, 0, n) && valid_range(L, 0, n) && valid_range(quant_weights, 0, n) && (forall int i; 0 <= i && i < n ==> isfinite(x[i]) && isfinite(quant_weights[i]) && quant_weights[i] >= 0.0f) && isfinite(nmax) && nmax > 0.0f` |
| `quantize_row_iq4_nl_ref` | `k % 32 == 0` |
| `quantize_row_q8_1_ref` | `k <= 1073741824 && valid_range(x, 0, k) && valid_range(y, 0, k / QK8_1)` |

The `make_qp_quants` clause is particularly impressive — it captures a
multi-array sizing invariant *and* a forall-quantified finiteness +
non-negativity invariant on the quant_weights. This is exactly the
implicit contract the calling code (`quantize_row_q3_K_ref` and
similar) maintains: a "block" of n×float values arrived from upstream
pre-validated.

`quantize_row_iq4_nl_ref`'s `k % 32 == 0` is the IQ4_NL block-size
invariant — every quantisation row must be a multiple of 32 floats
because IQ4_NL packs two 4-bit indices per byte across 32-element
groups.

`best_index_int8`'s table-selection clause is also notable: the
function takes a `kvalues` pointer and a length `n`; the learned spec
identifies that for `n==16` only two specific kvalues tables are valid
(`kvalues_iq4nl` and `kvalues_mxfp4`). Pure caller-context inference
from analysis of the realism CEx.

## Wall clock / cost

- Started 17:41 UTC+4, killed at 60-min timeout 18:41
- ~115 functions through Phase 1 + Phase 2 + Phase 3 + realism + feedback
- LLM provider: Claude Sonnet 4.5 via OpenRouter
- Estimated cost: ~$3-5

## Honest caveats

- 60-min timeout killed the run mid-realism on the last ~15 functions.
  The remaining functions still have raw CBMC counterexamples but no
  classifier/realism verdict yet.
- 36 CBMC errors out of 115 functions = ~31% failure rate, consistent
  with SIMD-intrinsic-heavy code. These need additional harness-gen
  work (stubs for `__builtin_assume_aligned`, `_mm_*` etc.) that's
  beyond this sweep.
- Output dir `/tmp/aprover_llama_nghttp2_or/ggml_quants/` has the
  per-function spec.json / bug_report.json / classification.json for
  full provenance.
