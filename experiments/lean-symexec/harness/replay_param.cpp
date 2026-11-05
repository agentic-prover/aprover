// Native replay of the SECOND experiment's witness, under the SEMANTIC oracle.
//
// Links against a native build of Lean's real runtime and real C++ kernel. No
// KLEE, no bitcode libc++, none of the single-threaded header shims. Takes the
// solver's witness bytes and parameter assignment on the command line and
// reports the decoded terms, both semantic values, and what the kernel's
// is_equivalent actually returns.
//
//   ./replay_param <lvl-hex> <sigma-hex>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include "kernel/level.h"
#include "kernel/init_module.h"
#include "util/name.h"
#include "runtime/stackinfo.h"

namespace lean {
void initialize_alloc(); void initialize_debug(); void initialize_object();
void initialize_thread(); void initialize_mutex(); void initialize_name();
void initialize_options();
}
using namespace lean;

static const unsigned MAX_NODES = 4;
static const unsigned INPUT_BYTES = 12;

struct decoder { const unsigned char *buf; unsigned len, pos, budget; 
                 unsigned char next() { return pos < len ? buf[pos++] : 0; } };

static char shown[4096]; static int sp;

static level build(decoder &d, level const &pu, level const &pv) {
    if (d.budget == 0) { sp += sprintf(shown+sp, "0"); return mk_level_zero(); }
    d.budget--;
    unsigned char c = d.next();
    switch (c % 5) {
    case 0: sp += sprintf(shown+sp, "0"); return mk_level_zero();
    case 1: { sp += sprintf(shown+sp, "succ("); level x = build(d,pu,pv); sp += sprintf(shown+sp, ")"); return mk_succ(x); }
    case 2: { sp += sprintf(shown+sp, "max("); level a = build(d,pu,pv); sp += sprintf(shown+sp, ","); level b = build(d,pu,pv); sp += sprintf(shown+sp, ")"); return mk_max_core(a,b); }
    case 3: { sp += sprintf(shown+sp, "imax("); level a = build(d,pu,pv); sp += sprintf(shown+sp, ","); level b = build(d,pu,pv); sp += sprintf(shown+sp, ")"); return mk_imax_core(a,b); }
    default: { bool isv = ((c/5)&1); sp += sprintf(shown+sp, isv?"v":"u"); return isv ? pv : pu; }
    }
}

// Reference semantics, identical to the one the symbolic harness uses.
static unsigned eval_level(level const &l, unsigned su, unsigned sv,
                           level const &pu, level const &pv) {
    if (is_zero(l)) return 0;
    if (is_succ(l)) return eval_level(succ_of(l), su, sv, pu, pv) + 1;
    if (is_max(l)) { unsigned x = eval_level(max_lhs(l),su,sv,pu,pv), y = eval_level(max_rhs(l),su,sv,pu,pv); return x>y?x:y; }
    if (is_imax(l)) { unsigned y = eval_level(imax_rhs(l),su,sv,pu,pv); if (y==0) return 0;
                      unsigned x = eval_level(imax_lhs(l),su,sv,pu,pv); return x>y?x:y; }
    return (l == pv) ? sv : su;
}

static int hexval(char c){ if(c>='0'&&c<='9')return c-'0'; if(c>='a'&&c<='f')return c-'a'+10;
                           if(c>='A'&&c<='F')return c-'A'+10; return -1; }

int main(int argc, char **argv) {
    if (argc < 3) { std::fprintf(stderr, "usage: replay_param <lvl-hex> <sigma-hex>\n"); return 2; }
    save_stack_info(true);
    initialize_alloc(); initialize_debug(); initialize_object();
    initialize_thread(); initialize_mutex(); initialize_name();
    initialize_options(); initialize_level();

    unsigned char input[INPUT_BYTES]; std::memset(input, 0, sizeof input);
    const char *h = argv[1]; if (!std::strncmp(h,"0x",2)) h += 2;
    for (unsigned i = 0; i < INPUT_BYTES && hexval(h[2*i])>=0 && hexval(h[2*i+1])>=0; i++)
        input[i] = (unsigned char)(hexval(h[2*i])*16 + hexval(h[2*i+1]));
    unsigned char sg[2] = {0,0};
    const char *g = argv[2]; if (!std::strncmp(g,"0x",2)) g += 2;
    for (unsigned i = 0; i < 2 && hexval(g[2*i])>=0 && hexval(g[2*i+1])>=0; i++)
        sg[i] = (unsigned char)(hexval(g[2*i])*16 + hexval(g[2*i+1]));
    unsigned su = sg[0] % 3, sv = sg[1] % 3;

    level pu = mk_univ_param(name("u")), pv = mk_univ_param(name("v"));

    sp = 0; decoder d1{ input, INPUT_BYTES/2, 0, MAX_NODES };
    level a = build(d1, pu, pv); shown[sp] = 0;
    char sa[4096]; std::strcpy(sa, shown);
    sp = 0; decoder d2{ input + INPUT_BYTES/2, INPUT_BYTES/2, 0, MAX_NODES };
    level b = build(d2, pu, pv); shown[sp] = 0;

    unsigned ea = eval_level(a, su, sv, pu, pv);
    unsigned eb = eval_level(b, su, sv, pu, pv);
    bool eq = is_equivalent(a, b);

    std::printf("a = %s\nb = %s\n", sa, shown);
    std::printf("sigma: u=%u v=%u\n", su, sv);
    std::printf("eval(a) = %u\neval(b) = %u\n", ea, eb);
    std::printf("kernel is_equivalent(a,b) = %s\n", eq ? "true" : "false");
    bool violated = eq && (ea != eb);
    std::printf("ORACLE (equivalent => same denotation): %s\n",
                violated ? "VIOLATED" : "holds");
    return violated ? 1 : 0;
}
