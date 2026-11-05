/* Symbolic harness over Lean's own universe-level operations.
 *
 * Target: the Lean-side implementation in stage0/stdlib/Lean/Level.c, i.e. real
 * Lean verification infrastructure compiled from Lean to C. Pure C, so unlike
 * the C++ kernel it needs no bitcode C++ runtime.
 *
 * The properties below are HYPOTHESES about intended behaviour, not contracts
 * Lean documents. A violation is a lead to be replayed and reviewed, never a
 * defect to report as-is. See docs/PROTOCOL.md.
 */

#include <stdint.h>
#include <stdlib.h>
#include <lean/lean.h>

#ifdef KLEE_MODE
#include <klee/klee.h>
#endif

/* Constructors and operations provided by Lean/Level.c */
lean_object *lean_level_mk_zero(lean_object *);
lean_object *lean_level_mk_succ(lean_object *);
lean_object *lean_level_mk_max(lean_object *, lean_object *);
lean_object *lean_level_mk_imax(lean_object *, lean_object *);
uint8_t l_Lean_Level_isEquiv(lean_object *, lean_object *);
uint8_t l_Lean_Level_geq(lean_object *, lean_object *);
lean_object *l_Lean_Level_normalize(lean_object *);

/* External-call models. Each is transcribed or justified against the real
 * implementation rather than invented, and each is recorded per docs/PROTOCOL.md. */
/* `lean_level_eq` is the C++ kernel's structural equality on levels, exported
 * for the Lean side. Our FIRST attempt modelled it by calling Lean's own
 * `Level.beq`. That was wrong, and instructively so: `l_Lean_Level_beq___boxed`
 * is itself a thin wrapper around `lean_level_eq`, so the two called each other
 * without end and every symbolic state died on KLEE's stack limit before any
 * property was evaluated. The symptom looked like an exploration problem and
 * was in fact a harness modelling fault. Recorded per docs/PROTOCOL.md.
 *
 * The model below is transcribed from `operator==` in src/kernel/level.cpp.
 * That function first rejects on differing kind, hash or depth and takes a
 * pointer-equality fast path, then recurses structurally. Hash and depth are
 * functions of structure, so those tests can only reject terms that structural
 * recursion also rejects; the plain recursion below returns the same boolean on
 * every input. Param and MVar are outside this harness's grammar and are
 * compared by identity only, which is recorded as a scope limit.
 *
 * Level constructor tags: zero=0 succ=1 max=2 imax=3 param=4 mvar=5. */
static uint8_t lvl_structeq(lean_object *a, lean_object *b) {
    if (a == b) return 1;                        /* is_eqp fast path */
    unsigned ta = lean_obj_tag(a), tb = lean_obj_tag(b);
    if (ta != tb) return 0;
    switch (ta) {
    case 0: return 1;                            /* zero */
    case 1: return lvl_structeq(lean_ctor_get(a, 0), lean_ctor_get(b, 0));
    case 2:
    case 3: return lvl_structeq(lean_ctor_get(a, 0), lean_ctor_get(b, 0))
                && lvl_structeq(lean_ctor_get(a, 1), lean_ctor_get(b, 1));
    default: return 0;                           /* param/mvar: out of scope */
    }
}

uint8_t lean_level_eq(lean_object *a, lean_object *b) {
    return lvl_structeq(a, b);                   /* borrows, as the kernel does */
}

uint64_t lean_uint64_once_cold(uint64_t *loc, lean_once_cell_t *tok,
                               uint64_t (*init)(void)) {
    (void)tok;
    uint64_t v = init();
    if (loc) *loc = v;
    return v;
}

/* Exact transcription of lean_level_mk_data from src/kernel/level.cpp.
 * Pure bit-packing of hash, depth and the mvar/param flags; copied rather
 * than approximated so the model cannot change observable behaviour. */
uint64_t lean_level_mk_data(uint64_t h, lean_object *depth,
                            uint8_t hasMVar, uint8_t hasParam) {
    size_t d = lean_unbox(depth);
    uint32_t h1 = (uint32_t)h;
    return ((uint64_t)h1) + (((uint64_t)hasMVar) << 32)
         + (((uint64_t)hasParam) << 33) + (((uint64_t)d) << 40);
}

/* Heartbeat accounting drives Lean's own deadline checks. A no-op is safe for
 * a bounded symbolic run and cannot affect the level algebra under test. */
void lean_inc_heartbeat(void) { }

/* Object deallocation is modelled as a leak. `lean_dec_ref_cold` lives in the
 * C++ runtime (object.cpp), which is not available as bitcode. Without a model
 * KLEE calls it natively on interpreter-owned memory; that corrupts the heap
 * and made `l_Lean_Level_geq` diverge. A bounded run never exhausts memory, and
 * declining to free an object cannot change the value any level operation
 * returns. Recorded per docs/PROTOCOL.md. */
void lean_dec_ref_cold(lean_object *o) { (void)o; }

/* `lean_array_push` lives in the C++ runtime (object.cpp) and is not available
 * as bitcode; `Level.normalize` reaches it when collecting the arguments of a
 * `max`. Lean's implementation pushes in place when the array is exclusive and
 * copies otherwise. Always copying is observationally identical, so this model
 * is faithful. Recorded per docs/PROTOCOL.md. */
/* `lean_alloc_object` is the runtime's object allocator, reached from
 * `lean_alloc_array`. With Lean's small allocator disabled its own definition
 * is exactly a malloc, so this model is the implementation, not a stand-in. */
lean_object *lean_alloc_object(size_t sz) { return (lean_object *)malloc(sz); }

lean_object *lean_array_push(lean_object *a, lean_object *v) {
    size_t sz = lean_array_size(a);
    lean_object *r = lean_alloc_array(sz + 1, sz + 1);
    lean_object **src = lean_array_cptr(a);
    lean_object **dst = lean_array_cptr(r);
    for (size_t i = 0; i < sz; i++) { lean_inc(src[i]); dst[i] = src[i]; }
    dst[sz] = v;
    lean_dec(a);
    return r;
}



#define MAX_NODES 4
#define INPUT_BYTES 16

static unsigned pos_a, pos_b, budget;
static unsigned char buf[INPUT_BYTES];

static unsigned char nextb(unsigned *p, unsigned lim) {
    return (*p < lim) ? buf[(*p)++] : 0;
}

static lean_object *build(unsigned *p, unsigned lim) {
    if (budget == 0)
        return lean_level_mk_zero(lean_box(0));
    budget--;
    switch (nextb(p, lim) & 3) {
    case 0:
        return lean_level_mk_zero(lean_box(0));
    case 1:
        return lean_level_mk_succ(build(p, lim));
#ifndef NOMAX
    case 2: {
        lean_object *x = build(p, lim);
        lean_object *y = build(p, lim);
        return lean_level_mk_max(x, y);
    }
#endif
    default: {
        lean_object *x = build(p, lim);
        lean_object *y = build(p, lim);
        return lean_level_mk_imax(x, y);
    }
    }
}

int main(void) {
#ifdef KLEE_MODE
    klee_make_symbolic(buf, sizeof(buf), "lv");
#ifdef FOCUS
    /* Family focus. Only the TOP constructor of each level is constrained, to
     * imax and to succ respectively. Everything below stays symbolic: whether
     * the inner level is an imax, whether its left argument is zero, and
     * whether the two levels share structure are all left to the solver. This
     * is the kind of input-surface selection AGOLIC's planner performs, and it
     * is recorded here rather than hidden. */
    klee_assume((buf[0] & 3) == 3);
    klee_assume((buf[INPUT_BYTES / 2] & 3) == 1);
#endif
#else
    for (unsigned i = 0; i < INPUT_BYTES; i++) buf[i] = (unsigned char)(i * 5 + 1);
#endif

    /* Both levels have a FULLY SYMBOLIC shape: the solver chooses each
     * constructor as well as each leaf. Nothing about imax is hard-coded, so
     * any witness the solver produces is synthesised, not supplied by us. */
    pos_a = 0;
    budget = MAX_NODES;
    lean_object *a = build(&pos_a, INPUT_BYTES / 2);

    pos_b = INPUT_BYTES / 2;
    budget = MAX_NODES;
    lean_object *b = build(&pos_b, INPUT_BYTES);

    lean_inc(a); lean_inc(a);
    lean_inc(b); lean_inc(b);

#ifdef TRACE
    klee_warning("TRACE built");
#endif
    /* H1: equivalence is reflexive. */
    if (!l_Lean_Level_isEquiv(a, a))
        klee_report_error(__FILE__, __LINE__, "isEquiv not reflexive", "contract");

    /* H2: the order is reflexive. */
    lean_inc(a); lean_inc(a);
    if (!l_Lean_Level_geq(a, a))
        klee_report_error(__FILE__, __LINE__, "geq not reflexive", "contract");

    /* H3: equivalent levels are mutually >=. This is the property that the
     * Arena's level-imax bugs violate in the checkers that got them wrong. */
#ifdef TRACE
    klee_warning("TRACE reached H3");
#endif
    lean_inc(a); lean_inc(b);
    if (l_Lean_Level_isEquiv(a, b)) {
#ifdef TRACE
        klee_warning("TRACE isEquiv(a,b) TRUE");
#endif
        lean_inc(a); lean_inc(b);
        uint8_t ab = l_Lean_Level_geq(a, b);
        lean_inc(a); lean_inc(b);
        uint8_t ba = l_Lean_Level_geq(b, a);
        if (!(ab && ba))
            klee_report_error(__FILE__, __LINE__,
                              "equivalent levels not mutually geq", "contract");
#ifdef TRACE
        else klee_warning("TRACE mutually geq held");
#endif
    }

#ifdef TRACE
    klee_warning("TRACE END");
#endif
    return 0;
}
