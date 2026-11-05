#include <stdint.h>
#include <lean/lean.h>
#ifdef KLEE_MODE
#include <klee/klee.h>
#endif
lean_object *lean_level_mk_zero(lean_object *);
lean_object *lean_level_mk_succ(lean_object *);
lean_object *lean_level_mk_imax(lean_object *, lean_object *);
uint8_t l_Lean_Level_isEquiv(lean_object *, lean_object *);
uint8_t l_Lean_Level_geq(lean_object *, lean_object *);
static uint8_t lvl_structeq(lean_object *a, lean_object *b) {
    if (a == b) return 1;
    unsigned ta = lean_obj_tag(a), tb = lean_obj_tag(b);
    if (ta != tb) return 0;
    switch (ta) {
    case 0: return 1;
    case 1: return lvl_structeq(lean_ctor_get(a,0), lean_ctor_get(b,0));
    case 2:
    case 3: return lvl_structeq(lean_ctor_get(a,0), lean_ctor_get(b,0))
                && lvl_structeq(lean_ctor_get(a,1), lean_ctor_get(b,1));
    default: return 0;
    }
}
uint8_t lean_level_eq(lean_object *a, lean_object *b) { return lvl_structeq(a,b); }
uint64_t lean_uint64_once_cold(uint64_t *loc, lean_once_cell_t *tok, uint64_t (*init)(void)) {
    (void)tok; uint64_t v = init(); if (loc) *loc = v; return v;
}
uint64_t lean_level_mk_data(uint64_t h, lean_object *depth, uint8_t hasMVar, uint8_t hasParam) {
    size_t d = lean_unbox(depth); uint32_t h1 = (uint32_t)h;
    return ((uint64_t)h1) + (((uint64_t)hasMVar)<<32) + (((uint64_t)hasParam)<<33) + (((uint64_t)d)<<40);
}
void lean_inc_heartbeat(void) { }

/* Object deallocation is modelled as a leak. `lean_dec_ref_cold` lives in the
 * C++ runtime (object.cpp), which is not available as bitcode. Without a model
 * KLEE calls it natively on interpreter-owned memory; that corrupts the heap
 * and made `l_Lean_Level_geq` diverge. A bounded run never exhausts memory, and
 * declining to free an object cannot change the value any level operation
 * returns. Recorded per docs/PROTOCOL.md. */
void lean_dec_ref_cold(lean_object *o) { (void)o; }


int main(void) {
#if STEP >= 1
    lean_object *z = lean_level_mk_zero(lean_box(0));
    klee_warning("STEP1 mk_zero ok");
#endif
#if STEP >= 2
    lean_object *i = lean_level_mk_imax(z, lean_level_mk_zero(lean_box(0)));
    klee_warning("STEP2 mk_imax ok");
#endif
#if STEP >= 3
    lean_object *s = lean_level_mk_succ(i);
    klee_warning("STEP3 mk_succ ok");
#endif
#if STEP >= 4
    lean_inc(i); lean_inc(i);
    uint8_t r = l_Lean_Level_isEquiv(i, i);
    klee_warning("STEP4 isEquiv ok");
#endif
#if STEP >= 5
    lean_inc(i); lean_inc(s);
    uint8_t q = l_Lean_Level_geq(i, s);
    klee_warning("STEP5 geq ok");
#endif
#if STEP >= 6
    /* The exact witness the Arena test describes:
     *    a = imax 0 0        b = succ (imax 0 0)
     * They denote 0 and 1, so they are NOT equivalent. */
    lean_object *wa = lean_level_mk_imax(lean_level_mk_zero(lean_box(0)),
                                         lean_level_mk_zero(lean_box(0)));
    lean_object *wb = lean_level_mk_succ(
                        lean_level_mk_imax(lean_level_mk_zero(lean_box(0)),
                                           lean_level_mk_zero(lean_box(0))));
    lean_inc(wa); lean_inc(wb);
    uint8_t e = l_Lean_Level_isEquiv(wa, wb);
    klee_warning(e ? "W: isEquiv(imax 0 0, succ(imax 0 0)) = TRUE"
                   : "W: isEquiv(imax 0 0, succ(imax 0 0)) = false");
    lean_inc(wa); lean_inc(wb);
    uint8_t g1 = l_Lean_Level_geq(wa, wb);
    lean_inc(wa); lean_inc(wb);
    uint8_t g2 = l_Lean_Level_geq(wb, wa);
    klee_warning(g1 ? "W: geq(a,b) = true" : "W: geq(a,b) = FALSE");
    klee_warning(g2 ? "W: geq(b,a) = true" : "W: geq(b,a) = FALSE");
    if (e && !(g1 && g2)) klee_warning("W: H3 WOULD FIRE");
#endif
    klee_warning("REACHED END");
    return 0;
}
