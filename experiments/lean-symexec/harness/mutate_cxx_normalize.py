#!/usr/bin/env python3
"""Faithful mimic of the Lean Kernel Arena defect `level-imax-normalization`.

The Arena states the mechanism as: a normalization algorithm that "drops an
accumulated successor offset when decomposing imax u (param v)".

In the real kernel (src/kernel/level.cpp) normalize splits a level into a base
and an accumulated successor offset with to_offset, and the IMax case puts that
offset back:

    case level_kind::IMax: {
        auto l1 = normalize(imax_lhs(r));
        auto l2 = normalize(imax_rhs(r));
        return mk_succ(mk_imax(l1, l2), p.second);   // offset restored
    }

The mimic deletes exactly that restoration. Nothing else changes. The incorrect
equivalence of `imax 0 v` and `succ (imax 0 v)` is NOT asserted anywhere: it
emerges from the faulty algorithm, which is what distinguishes a mimic of the
documented defect from an injected rule.
"""
src = "/w/lean4/src/kernel/level.cpp"
dst = "/w/mutant/level_norm_mut.cpp"
s = open(src).read()

old = """    case level_kind::IMax: {
        auto l1 = normalize(imax_lhs(r));
        auto l2 = normalize(imax_rhs(r));
        return mk_succ(mk_imax(l1, l2), p.second);
    }"""
new = """    case level_kind::IMax: {
        auto l1 = normalize(imax_lhs(r));
        auto l2 = normalize(imax_rhs(r));
        /* MIMIC of Lean Kernel Arena `level-imax-normalization`: the accumulated
           successor offset p.second is dropped when decomposing the imax. This
           is the documented mechanism, transcribed; no equivalence is asserted. */
        return mk_imax(l1, l2);
    }"""
assert old in s, "normalize IMax case not found"
open(dst, "w").write(s.replace(old, new, 1))
print("faithful normalize mimic written to", dst)
