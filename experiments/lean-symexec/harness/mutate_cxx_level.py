#!/usr/bin/env python3
"""Inject the documented imax/succ incorrect equivalence into the C++ kernel.

Mirrors the mutation applied to the Lean-generated C, so the same oracle can be
run against the actual C++ kernel. Only is_equivalent changes.
"""
src = "/w/lean4/src/kernel/level.cpp"
dst = "/w/mutant/level_mut.cpp"
s = open(src).read()

old = """bool is_equivalent(level const & lhs, level const & rhs) {
    check_system("level constraints");
    return lhs == rhs || normalize(lhs) == normalize(rhs);
}"""
new = """/* MUTATION: the Lean Kernel Arena's level-imax-normalization
   failure pattern, injected into the C++ kernel. A checker with this fault
   treats imax 0 v and succ (imax 0 v) as equivalent, although at v = 0 they
   denote 0 and 1. Nothing else in this file is changed. */
static bool mut_succ_of_imax_zero(level const & s, level const & other) {
    if (!is_succ(s)) return false;
    level const & inner = succ_of(s);
    if (!is_imax(inner)) return false;
    if (!is_zero(imax_lhs(inner))) return false;
    return inner == other;
}

bool is_equivalent(level const & lhs, level const & rhs) {
    check_system("level constraints");
    if (mut_succ_of_imax_zero(rhs, lhs) || mut_succ_of_imax_zero(lhs, rhs))
        return true;
    return lhs == rhs || normalize(lhs) == normalize(rhs);
}"""
assert old in s, "is_equivalent body not found"
open(dst, "w").write(s.replace(old, new, 1))
print("mutated C++ kernel written to", dst)
