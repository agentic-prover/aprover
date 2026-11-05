#!/usr/bin/env python3
"""Decode a KLEE witness for lean_level_param_harness.cpp (5-way grammar + sigma)."""
import sys
MAX, IB = 4, 12
def decode(buf):
    st = {'b': 0}
    def build(p, lim):
        if st['b'] == 0: return "0", p
        st['b'] -= 1
        c = buf[p] if p < lim and p < len(buf) else 0
        p = p + 1 if p < lim else p
        k = c % 5
        if k == 0: return "0", p
        if k == 1:
            x, p = build(p, lim); return f"succ({x})", p
        if k == 2:
            x, p = build(p, lim); y, p = build(p, lim); return f"max({x},{y})", p
        if k == 3:
            x, p = build(p, lim); y, p = build(p, lim); return f"imax({x},{y})", p
        return ("v" if ((c // 5) & 1) else "u"), p
    st['b'] = MAX; a, _ = build(0, IB // 2)
    st['b'] = MAX; b, _ = build(IB // 2, IB)
    return a, b

def ev(t, su, sv):
    t = t.strip()
    if t == "0": return 0
    if t == "u": return su
    if t == "v": return sv
    if t.startswith("succ("): return ev(t[5:-1], su, sv) + 1
    head, body = t.split("(", 1); body = body[:-1]
    d = 0; i = 0
    for j, ch in enumerate(body):
        if ch == "(": d += 1
        elif ch == ")": d -= 1
        elif ch == "," and d == 0: i = j; break
    x, y = body[:i], body[i+1:]
    if head == "max": return max(ev(x, su, sv), ev(y, su, sv))
    vy = ev(y, su, sv)
    return 0 if vy == 0 else max(ev(x, su, sv), vy)

lvl = bytes.fromhex(sys.argv[1].replace("0x", ""))
sig = bytes.fromhex(sys.argv[2].replace("0x", "")) if len(sys.argv) > 2 else b"\0\0"
su, sv = sig[0] % 3, sig[1] % 3
a, b = decode(lvl)
print(f"a = {a}")
print(f"b = {b}")
print(f"sigma: u={su}, v={sv}")
print(f"eval(a) = {ev(a,su,sv)}   eval(b) = {ev(b,su,sv)}")
