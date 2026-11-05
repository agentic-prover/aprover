// Naive symbolic harness over Lean's universe-level operations.
//
// Why this target: universe levels are the one kernel family whose operations
// are pure over self-contained objects, so a harness can drive them without
// constructing a Lean `environment`. It is also a family with a history of real
// kernel defects: the Lean Kernel Arena carries `level-imax-normalization`,
// `level-imax-leq`, `constlevels` and `closure-level-subst` as bug tests.
//
// IMPORTANT, and this is the methodological point of docs/PROTOCOL.md: the properties
// asserted below are *our hypotheses* about the intended contract of these
// operations, not contracts Lean documents. A violation is a LEAD, not a defect.
// It must be replayed natively, minimised, and reviewed against the intended
// semantics before it is called anything at all. An agent inventing an assertion
// and reporting its failure as a bug is precisely the failure mode this work
// commits to avoiding.
//
// Build (inside klee/klee:3.1, from lean4/src):
//   clang++ -std=c++20 -include mutex -emit-llvm -c -I. -Iinclude -I<gen> \
//           -DKLEE_MODE -O0 -w -o harness.bc <this file>
// Link with the kernel + runtime bitcode, then run under KLEE.

#include <cstdint>
#include <cstddef>

#include "kernel/level.h"
#include "kernel/init_module.h"
#include "util/name.h"
#include "runtime/stackinfo.h"

// initialize_util_module() calls initialize_runtime_module(), which lives in
// runtime/init_module.cpp. That unit does not lower to bitcode because it pulls
// in libuv and OpenSSL. We therefore initialise the subset that does lower,
// skipping the I/O and event-loop initialisers the harness never reaches.
// This is an external-call model and is recorded
// as such: the omitted initialisers are initialize_io and initialize_libuv.
namespace lean {
void initialize_alloc();
void initialize_debug();
void initialize_object();
void initialize_thread();
void initialize_mutex();
void initialize_process();
void initialize_stack_overflow();
void initialize_name();
void initialize_name_generator();
void initialize_options();
}

#ifdef KLEE_MODE
#include <klee/klee.h>
#define ASSERT(c, msg) do { if (!(c)) klee_report_error(__FILE__, __LINE__, msg, "contract"); } while (0)
#else
#include <cassert>
#include <cstdio>
#define ASSERT(c, msg) do { if (!(c)) { std::printf("VIOLATION: %s\n", msg); } } while (0)
#endif

using namespace lean;

// Bound the shape of generated levels. Small on purpose: the point of the
// first run is to learn whether exploration is tractable at all, not to be
// exhaustive.
static const unsigned MAX_NODES = 4;

// Build through the raw constructors (as the Lean side does) or through the
// kernel's simplifying smart constructors. See build() below.
#ifdef RAW_CONSTRUCTORS
static const bool RAW_CTORS = true;
#else
static const bool RAW_CTORS = false;
#endif
static const unsigned INPUT_BYTES = 12;

namespace {

struct decoder {
    const unsigned char *buf;
    unsigned len;
    unsigned pos;
    unsigned budget;

    unsigned char next() { return pos < len ? buf[pos++] : 0; }
};

// Decode a bounded level tree in prefix form. Every byte consumed is symbolic,
// so the solver chooses the tree shape as well as the leaves.
level build(decoder &d) {
    if (d.budget == 0)
        return mk_level_zero();
    d.budget--;

    // Universe parameters are omitted: constructing a `name` reaches
    // lean_name_mk_string, which is implemented on the Lean side and is not in
    // the C++ bitcode. Ground levels still exercise the imax normalisation path.
    // Recorded as a scope limit, not a silent choice.
    switch (d.next() % 4) {
    case 0:
        return mk_level_zero();
    case 1:
        return mk_succ(build(d));
    case 2: {
        level a = build(d);
        level b = build(d);
        return RAW_CTORS ? mk_max_core(a, b) : mk_max(a, b);
    }
    default: {
        level a = build(d);
        level b = build(d);
        // The smart constructor mk_imax rewrites `imax 0 v` to `v` before the
        // node is ever built (level.cpp: `is_zero(l1) -> return l2`). Terms of
        // that shape are therefore not representable through it, and no budget
        // makes them reachable. mk_imax_core is the raw constructor the Lean
        // side uses via lean_level_mk_imax. Which of the two a harness builds
        // through decides whether a whole defect family can be reached at all.
        return RAW_CTORS ? mk_imax_core(a, b) : mk_imax(a, b);
    }
    }
}

} // namespace

int main() {
    // Record this thread's stack bounds. Without it check_stack() reads
    // uninitialised bounds, wrongly concludes the stack is exhausted, and
    // throws from inside is_equivalent. That is a harness artifact, not a
    // kernel defect, and is exactly the confusion docs/PROTOCOL.md warns about.
    save_stack_info(true);

    initialize_alloc();
    initialize_debug();
    initialize_object();
    initialize_thread();
    initialize_mutex();
    // initialize_process() and initialize_stack_overflow() are deliberately
    // skipped: the latter calls sigaltstack(), which KLEE cannot model, and
    // neither is reachable from level operations. Recorded as an external-call
    // model, per docs/PROTOCOL.md.
    // Only the level module is initialised. initialize_kernel_module() also
    // runs initialize_expr/declaration/type_checker/trace, which reach
    // lean_name_mk_string, a Lean-implemented function absent from the C++
    // bitcode. initialize_level() itself is self-contained.
    initialize_level();

    unsigned char input[INPUT_BYTES];
#ifdef KLEE_MODE
    klee_make_symbolic(input, sizeof(input), "level_input");
#ifdef FOCUS
    // Family focus. Only the OUTERMOST constructor of each term is constrained,
    // to imax and to succ respectively. Everything beneath stays symbolic: the
    // solver still chooses whether the inner level is an imax, whether its left
    // argument is zero, and whether the two terms share structure. This is the
    // input-surface decision AGOLIC's planner is meant to make, recorded here
    // rather than hidden.
    klee_assume((input[0] % 4) == 3);
    klee_assume((input[INPUT_BYTES / 2] % 4) == 1);
#endif
#else
    for (unsigned i = 0; i < INPUT_BYTES; i++) input[i] = static_cast<unsigned char>(i * 7 + 3);
#endif

    decoder d1{ input, INPUT_BYTES / 2, 0, MAX_NODES };
    level a = build(d1);
    decoder d2{ input + INPUT_BYTES / 2, INPUT_BYTES / 2, 0, MAX_NODES };
    level b = build(d2);

    // Hypothesis 1: normalisation is idempotent.
    level na  = normalize(a);
    level nna = normalize(na);
    ASSERT(is_equivalent(na, nna), "normalize is not idempotent");

    // Hypothesis 2: equivalence is reflexive.
    ASSERT(is_equivalent(a, a), "is_equivalent is not reflexive");

    // Hypothesis 3: the order is reflexive.
    ASSERT(is_geq(a, a), "is_geq is not reflexive");

    // Hypothesis 4: equivalent levels are mutually greater-or-equal. This is
    // the one most likely to be a genuine contract and the one that touches
    // the imax handling where the Arena's level bugs have lived.
    if (is_equivalent(a, b)) {
        ASSERT(is_geq(a, b) && is_geq(b, a),
               "equivalent levels are not mutually >=");
    }

    // Hypothesis 5: normalisation preserves equivalence.
    ASSERT(is_equivalent(a, na), "normalize does not preserve equivalence");

    return 0;
}
