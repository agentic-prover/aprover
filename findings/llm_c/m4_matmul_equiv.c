/* M4 equivalence harness: matmul_forward ≡ matmul_forward_naive
 *
 * Verifies that the optimized matmul agrees with the reference impl
 * on every output element up to a ulp tolerance, at bounded sizes
 * (B = T = C = OC = 2). Both implementations are pulled in from
 * train_gpt2.c via real-libc mode.
 */
#include "/tmp/llm.c/train_gpt2.c"

#define SDS 2          /* scale_down_size */
#define BUF_ELEMS 16   /* SDS^3 * 2 -- room for B*T*OC at max sizes */
#define EPS 1e-4f
#define ULP_TOL 8

static float fabsf_pure(float x) { return x < 0.0f ? -x : x; }

int check_matmul_equivalence(void) {
    /* Bounded sizes shared by both calls. */
    int B, T, C, OC;
    __CPROVER_assume(B > 0 && B <= SDS);
    __CPROVER_assume(T > 0 && T <= SDS);
    __CPROVER_assume(C > 0 && C <= SDS);
    __CPROVER_assume(OC > 0 && OC <= SDS);

    /* Shared nondet inputs. Bound input magnitudes so the kernels'
     * float arithmetic stays in a tractable range and no NaN/Inf
     * is produced. */
    float *inp     = (float *)malloc(sizeof(float) * BUF_ELEMS);
    float *weight  = (float *)malloc(sizeof(float) * BUF_ELEMS);
    float *bias    = (float *)malloc(sizeof(float) * BUF_ELEMS);
    __CPROVER_assume(inp != NULL && weight != NULL && bias != NULL);

    /* Two output buffers -- one per impl. */
    float *out_opt   = (float *)malloc(sizeof(float) * BUF_ELEMS);
    float *out_naive = (float *)malloc(sizeof(float) * BUF_ELEMS);
    __CPROVER_assume(out_opt != NULL && out_naive != NULL);

    /* Inputs bounded and finite. */
    for (int i = 0; i < BUF_ELEMS; i++) {
        __CPROVER_assume(!__CPROVER_isnanf(inp[i])    && fabsf_pure(inp[i])    <= 1.0f);
        __CPROVER_assume(!__CPROVER_isnanf(weight[i]) && fabsf_pure(weight[i]) <= 1.0f);
        __CPROVER_assume(!__CPROVER_isnanf(bias[i])   && fabsf_pure(bias[i])   <= 1.0f);
    }

    /* Run both impls with the SAME inputs. */
    matmul_forward      (out_opt,   inp, weight, bias, B, T, C, OC);
    matmul_forward_naive(out_naive, inp, weight, bias, B, T, C, OC);

    /* Per-element equivalence assertion, on a nondet index in range. */
    int i;
    __CPROVER_assume(i >= 0 && i < B * T * OC);
    float diff = out_opt[i] - out_naive[i];
    float abs_diff = fabsf_pure(diff);
    float abs_naive = fabsf_pure(out_naive[i]);
    /* |opt - naive| <= eps * |naive| + ulp_tol * smallest_normal */
    assert(abs_diff <= EPS * abs_naive + ULP_TOL * 1e-7f);

    return 0;
}
