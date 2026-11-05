#!/usr/bin/env python3
"""Decode a KLEE witness for lean_level_c_harness.c into the two level terms.

Usage:  decode_witness.py <hex-bytes> [--nomax] [--max-nodes N] [--input-bytes N]
Mirrors build() in lean_level_c_harness.c exactly.
"""
import sys

def decode(buf, max_nodes, input_bytes, nomax):
    state = {"budget": 0}
    def build(p, lim):
        if state["budget"] == 0:
            return "0", p
        state["budget"] -= 1
        c = (buf[p] if p < lim and p < len(buf) else 0) & 3
        p = p + 1 if p < lim else p
        if c == 0:
            return "0", p
        if c == 1:
            x, p = build(p, lim); return f"succ({x})", p
        if c == 2 and not nomax:
            x, p = build(p, lim); y, p = build(p, lim); return f"max({x},{y})", p
        x, p = build(p, lim); y, p = build(p, lim); return f"imax({x},{y})", p
    state["budget"] = max_nodes
    a, _ = build(0, input_bytes // 2)
    state["budget"] = max_nodes
    b, _ = build(input_bytes // 2, input_bytes)
    return a, b

if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    nomax = "--nomax" in sys.argv
    mn = 4; ib = 16
    for i, x in enumerate(sys.argv):
        if x == "--max-nodes": mn = int(sys.argv[i+1])
        if x == "--input-bytes": ib = int(sys.argv[i+1])
    raw = args[0].replace("0x", "").replace("\\x", "").replace(" ", "")
    buf = bytes.fromhex(raw)
    a, b = decode(buf, mn, ib, nomax)
    print(f"a = {a}")
    print(f"b = {b}")
