/* M4b: matmul equivalence specifically targeting the OPTIMIZED path.
 *
 * matmul_forward takes the optimized branch when B*T % LOOP_UNROLL == 0
 * (LOOP_UNROLL = 8 in train_gpt2.c). At SDS=2 the optimized path
 * never fires (B*T in 1..4). At SDS=4 with B*T=8 specifically, we
 * exercise the actual SIMD-ish kernel. This harness pins B*T to 8.
 *
 * Heavier than m4_matmul_equiv.c; expect higher SMT cost.
 */
#include "/tmp/llm.c/train_gpt2.c"

#define SDS 4
#define BUF_ELEMS 128
#define EPS 1e-3f
#define ULP_TOL 32

static float fabsf_pure(float x) { return x < 0.0f ? -x : x; }

int check_matmul_equivalence_optimized_path(void) {
    int B, T, C, OC;
    __CPROVER_assume(B > 0 && B <= SDS);
    __CPROVER_assume(T > 0 && T <= SDS);
    __CPROVER_assume(C > 0 && C <= SDS);
    __CPROVER_assume(OC > 0 && OC <= SDS);
    /* Force the optimized path: B*T must be a multiple of LOOP_UNROLL = 8. */
    __CPROVER_assume(B * T == 8);

    float *inp     = (float *)malloc(sizeof(float) * BUF_ELEMS);
    float *weight  = (float *)malloc(sizeof(float) * BUF_ELEMS);
    float *bias    = (float *)malloc(sizeof(float) * BUF_ELEMS);
    __CPROVER_assume(inp != NULL && weight != NULL && bias != NULL);

    float *out_opt   = (float *)malloc(sizeof(float) * BUF_ELEMS);
    float *out_naive = (float *)malloc(sizeof(float) * BUF_ELEMS);
    __CPROVER_assume(out_opt != NULL && out_naive != NULL);

    for (int i = 0; i < BUF_ELEMS; i++) {
        __CPROVER_assume(!__CPROVER_isnanf(inp[i])    && fabsf_pure(inp[i])    <= 1.0f);
        __CPROVER_assume(!__CPROVER_isnanf(weight[i]) && fabsf_pure(weight[i]) <= 1.0f);
        __CPROVER_assume(!__CPROVER_isnanf(bias[i])   && fabsf_pure(bias[i])   <= 1.0f);
    }

    matmul_forward      (out_opt,   inp, weight, bias, B, T, C, OC);
    matmul_forward_naive(out_naive, inp, weight, bias, B, T, C, OC);

    int i;
    __CPROVER_assume(i >= 0 && i < B * T * OC);
    float diff = out_opt[i] - out_naive[i];
    float abs_diff = fabsf_pure(diff);
    float abs_naive = fabsf_pure(out_naive[i]);
    assert(abs_diff <= EPS * abs_naive + ULP_TOL * 1e-7f);
    return 0;
}
