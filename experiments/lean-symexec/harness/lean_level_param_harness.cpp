// Symbolic harness over the C++ kernel's universe-level algebra, WITH universe
// parameters, for the faithful mimic of Lean Kernel Arena
// `level-imax-normalization`.
//
// Two things distinguish this from the earlier mutation harness.
//
//   1. The grammar includes `param`. The Arena states the defect in terms of
//      decomposing `imax u (param v)`, so a harness over closed terms cannot
//      exercise the documented mechanism at all.
//   2. Terms are built with the RAW constructors (mk_imax_core, mk_max_core).
//      The smart constructor mk_imax rewrites `imax 0 v` to `v` before the node
//      exists, which forecloses the entire family. This is recorded in
//      the README as a measured result.
//
// The properties below are hypotheses about intended behaviour, not contracts
// Lean documents. A violation is a lead to be replayed, never a defect as-is.

#include <cstdint>
#include <cstddef>

#include "kernel/level.h"
#include "kernel/init_module.h"
#include "util/name.h"
#include "runtime/stackinfo.h"

namespace lean {
void initialize_alloc();
void initialize_debug();
void initialize_object();
void initialize_thread();
void initialize_mutex();
void initialize_name();
void initialize_options();
}

#ifdef KLEE_MODE
#include <klee/klee.h>
#define ASSERT(c, msg) do { if (!(c)) klee_report_error(__FILE__, __LINE__, msg, "contract"); } while (0)
#else
#include <cstdio>
#define ASSERT(c, msg) do { if (!(c)) { std::printf("VIOLATION: %s\n", msg); } } while (0)
#endif

using namespace lean;

static const unsigned MAX_NODES   = 4;
static const unsigned INPUT_BYTES = 12;

namespace {

struct decoder {
    const unsigned char *buf;
    unsigned len, pos, budget;
    unsigned char next() { return pos < len ? buf[pos++] : 0; }
};

// Five constructors: zero, succ, max, imax, param. Universe parameters are
// drawn from a two-name pool, which is enough to express `imax u (param v)`
// and to let the solver choose whether two terms share a parameter.
level build(decoder &d, level const &pu, level const &pv) {
    if (d.budget == 0)
        return mk_level_zero();
    d.budget--;
    unsigned char c = d.next();
    switch (c % 5) {
    case 0:
        return mk_level_zero();
    case 1:
        return mk_succ(build(d, pu, pv));
    case 2: {
        level a = build(d, pu, pv);
        level b = build(d, pu, pv);
        return mk_max_core(a, b);
    }
    case 3: {
        level a = build(d, pu, pv);
        level b = build(d, pu, pv);
        return mk_imax_core(a, b);
    }
    default:
        return ((c / 5) & 1) ? pv : pu;
    }
}

// Denotational evaluation of a level under an assignment of the two universe
// parameters to naturals. This is OUR reference semantics, written from the
// definition of the level algebra, and it shares no code with Lean's normalize
// or is_geq. That independence is the point: is_geq is defined as
// is_geq_core(normalize(l1), normalize(l2)), so any property built from is_geq
// cannot detect a fault inside normalize. The Arena states the defect in
// exactly these terms, that imax 0 v and succ (imax 0 v) "evaluate to 0 and 1
// respectively when v = 0", so evaluation is the oracle the defect is defined
// against.
unsigned eval_level(level const &l, unsigned su, unsigned sv,
                    level const &pu, level const &pv) {
    if (is_zero(l))  return 0;
    if (is_succ(l))  return eval_level(succ_of(l), su, sv, pu, pv) + 1;
    if (is_max(l)) {
        unsigned x = eval_level(max_lhs(l), su, sv, pu, pv);
        unsigned y = eval_level(max_rhs(l), su, sv, pu, pv);
        return x > y ? x : y;
    }
    if (is_imax(l)) {
        unsigned y = eval_level(imax_rhs(l), su, sv, pu, pv);
        if (y == 0) return 0;                  // imax u 0 = 0
        unsigned x = eval_level(imax_lhs(l), su, sv, pu, pv);
        return x > y ? x : y;
    }
    return (l == pv) ? sv : su;                // param
}

} // namespace

int main() {
    save_stack_info(true);
    initialize_alloc();
    initialize_debug();
    initialize_object();
    initialize_thread();
    initialize_mutex();
    initialize_name();
    initialize_options();
    initialize_level();

    level pu = mk_univ_param(name("u"));
    level pv = mk_univ_param(name("v"));

    unsigned char input[INPUT_BYTES];
    unsigned char assign[2];
#ifdef KLEE_MODE
    klee_make_symbolic(input, sizeof(input), "lvl");
    klee_make_symbolic(assign, sizeof(assign), "sigma");
#ifdef FOCUS
    // Family focus: only the OUTERMOST constructor of each term is fixed, to
    // imax and to succ. Everything beneath stays symbolic, including whether a
    // parameter appears and which one. Recorded as an input-surface decision.
    klee_assume((input[0] % 5) == 3);
    klee_assume((input[INPUT_BYTES / 2] % 5) == 1);
#endif
#else
    for (unsigned i = 0; i < INPUT_BYTES; i++) input[i] = (unsigned char)(i * 7 + 3);
    assign[0] = 0; assign[1] = 0;
#endif
    // Parameter assignments are bounded; the v = 0 case the Arena names is in range.
    unsigned su = assign[0] % 3;
    unsigned sv = assign[1] % 3;

    decoder d1{ input, INPUT_BYTES / 2, 0, MAX_NODES };
    level a = build(d1, pu, pv);
    decoder d2{ input + INPUT_BYTES / 2, INPUT_BYTES / 2, 0, MAX_NODES };
    level b = build(d2, pu, pv);

    // P1, the oracle for this defect. If the kernel calls two levels
    // equivalent, they must denote the same universe under every assignment of
    // the universe parameters. The solver chooses the assignment as well as the
    // two terms, so it is free to find the assignment that separates them.
    if (is_equivalent(a, b)) {
        ASSERT(eval_level(a, su, sv, pu, pv) == eval_level(b, su, sv, pu, pv),
               "levels called equivalent denote different universes");
    }

    // P2: equivalence is reflexive.
    ASSERT(is_equivalent(a, a), "is_equivalent is not reflexive");

    return 0;
}
