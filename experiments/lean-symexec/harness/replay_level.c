/* Native replay of a solver-generated witness.
 *
 * Same decoder as lean_level_c_harness.c, but linked against Lean's REAL
 * runtime and stdlib rather than KLEE's interpreter and our four models.
 * Its purpose is to check that a witness KLEE produced reproduces the same
 * verdicts outside the symbolic engine, which is what docs/PROTOCOL.md requires
 * before anything is called a finding.
 *
 *   ./replay <hex-bytes> [--nomax]
 */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdlib.h>
#include <lean/lean.h>

lean_object *lean_level_mk_zero(lean_object *);
lean_object *lean_level_mk_succ(lean_object *);
lean_object *lean_level_mk_max(lean_object *, lean_object *);
lean_object *lean_level_mk_imax(lean_object *, lean_object *);
uint8_t l_Lean_Level_isEquiv(lean_object *, lean_object *);
uint8_t l_Lean_Level_geq(lean_object *, lean_object *);

#define MAX_NODES 4
#define INPUT_BYTES 16

static unsigned budget, nomax;
static unsigned char buf[INPUT_BYTES];

static unsigned char nextb(unsigned *p, unsigned lim) {
    return (*p < lim) ? buf[(*p)++] : 0;
}

static lean_object *build(unsigned *p, unsigned lim, char *out, int *n) {
    if (budget == 0) { *n += sprintf(out + *n, "0"); return lean_level_mk_zero(lean_box(0)); }
    budget--;
    unsigned char c = nextb(p, lim) & 3;
    if (c == 0) { *n += sprintf(out + *n, "0"); return lean_level_mk_zero(lean_box(0)); }
    if (c == 1) {
        *n += sprintf(out + *n, "succ(");
        lean_object *x = build(p, lim, out, n);
        *n += sprintf(out + *n, ")");
        return lean_level_mk_succ(x);
    }
    if (c == 2 && !nomax) {
        *n += sprintf(out + *n, "max(");
        lean_object *x = build(p, lim, out, n); *n += sprintf(out + *n, ",");
        lean_object *y = build(p, lim, out, n); *n += sprintf(out + *n, ")");
        return lean_level_mk_max(x, y);
    }
    *n += sprintf(out + *n, "imax(");
    lean_object *x = build(p, lim, out, n); *n += sprintf(out + *n, ",");
    lean_object *y = build(p, lim, out, n); *n += sprintf(out + *n, ")");
    return lean_level_mk_imax(x, y);
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    fprintf(stderr, "[mark] entered main\n");
    if (argc < 2) { fprintf(stderr, "usage: replay <hex> [--nomax]\n"); return 2; }
    for (int i = 2; i < argc; i++) if (!strcmp(argv[i], "--nomax")) nomax = 1;

    const char *h = argv[1];
    if (!strncmp(h, "0x", 2)) h += 2;
    memset(buf, 0, sizeof buf);
    for (unsigned i = 0; i < INPUT_BYTES && hexval(h[2*i]) >= 0 && hexval(h[2*i+1]) >= 0; i++)
        buf[i] = (unsigned char)(hexval(h[2*i]) * 16 + hexval(h[2*i+1]));

#ifdef INIT_RT
    /* Not used by default: initialising the full runtime module starts libuv's
     * event loop, which never returns for a batch program. Level operations are
     * self-contained and need no module initialisation. */
    lean_initialize_runtime_module();
#endif

    fprintf(stderr, "[mark] about to build\n");
    char sa[512] = {0}, sb[512] = {0}; int na = 0, nb = 0;
    unsigned pa = 0, pb = INPUT_BYTES / 2;
    budget = MAX_NODES; lean_object *a = build(&pa, INPUT_BYTES / 2, sa, &na);
    budget = MAX_NODES; lean_object *b = build(&pb, INPUT_BYTES,     sb, &nb);

    fprintf(stderr, "[mark] built\n");
    printf("a = %s\nb = %s\n", sa, sb);

    lean_inc(a); lean_inc(b);
    uint8_t eq = l_Lean_Level_isEquiv(a, b);
    fprintf(stderr, "[mark] isEquiv done\n");
    lean_inc(a); lean_inc(b);
    uint8_t ab = l_Lean_Level_geq(a, b);
    lean_inc(a); lean_inc(b);
    uint8_t ba = l_Lean_Level_geq(b, a);
    fprintf(stderr, "[mark] geq done\n");

    printf("isEquiv(a,b) = %u\ngeq(a,b)     = %u\ngeq(b,a)     = %u\n", eq, ab, ba);
    int violated = eq && !(ab && ba);
    printf("H3 (equivalent levels mutually geq): %s\n",
           violated ? "VIOLATED" : "holds");
    return violated ? 1 : 0;
}
