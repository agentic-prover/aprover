# BUG-25 — `stbtt__handle_clipped_edge` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(scanline, 0, x + 1) && valid(e) && x >= 0 && (x0 >= (float)x - 1.0f && x0 <= (float)x + 2.0f) && (x1 >= (float)x - 1.0f && x1 <= (float)x + 2.0f) && y0 != y1 && (e->direction == 1.0f || e->direction == -1.0f) && e->sy <= e->ey && y0 >= e->sy - 1.0f && y1 <= e->ey + 1.0f && (y1 - y0) != 0.0f`

**Postcondition:** `ensures (y0 == y1 || y0 > e->ey || y1 < e->sy) ==> scanline[x] is unchanged; ensures (x0 >= (float)(x+1) && x1 >= (float)(x+1)) ==> scanline[x] is unchanged; ensures !((y0 == y1 || y0 > e->ey || y1 < e->sy) || (x0 >= (float)(x+1) && x1 >= (float)(x+1))) ==> scanline[x] is updated by adding e->direction multiplied by the trapezoid/triangle area contribution of the clipped edge segment within pixel x; no elements of scanline other than scanline[x] are modified; no out-of-bounds memory access occurs; the function does not write to any memory outside scanline[x] and the fields of e`

## Counterexample

**Violated property:** `stbtt__handle_clipped_edge.pointer_dereference.23`

**Key variable assignments:**
```
_scanline_val = 1.084202e-19
scanline = _scanline_val!0@1
x = 1073741807
_e_val = <symbolic struct/array — see classification.json>
e = _e_val!0@1
x0 = 1.816387
y0 = +inf
x1 = -5.028774e+36
y1 = 1.474560e+5
```

## Root cause

CBMC reports a `stbtt__handle_clipped_edge.pointer_dereference.23` failure — a memory-safety violation in `stbtt__handle_clipped_edge`.

**Realism checker's key concern:** The specific counterexample witness (x ≈ 1 billion, ey = +inf) is a CBMC artifact from analyzing the function as an unconstrained entry point. In real execution, x is bounded by the rasterization width passed from stbtt__fill_active_edges_new. Whether that bounding is strict enough to prevent all out-of-bounds x values from malformed fonts is the real question — not addressed by this witness.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`stbtt__handle_clipped_edge` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific counterexample witness (x ≈ 1 billion, ey = +inf) is a CBMC artifact from analyzing the function as an unconstrained entry point. In real execution, x is bounded by the rasterization width passed from stbtt__fill_active_edges_new. Whether that bounding is strict enough to prevent all out-of-bounds x values from malformed fonts is the real question — not addressed by this witness.

Q1 (Can the violation TYPE occur?): The violation is a potential out-of-bounds access to `scanline[x]`. In `stbtt__fill_active_edges_new`, the `x` parameter is derived from floating-point active edge positions cast to `int` — e.g., `(int) x0` where `x0` comes from rasterized font geometry. If an attacker controls the font file, they could craft edge coordinates that produce `x` values outside the scanline buffer's allocated width. The stb_truetype library is widely used to parse potentially untrusted font data. The cast `(int) x0` has no bounds guard inside this function, and the scanline buffer is sized to a specific rasterization width. If upstream bounds checking in `stbtt__fill_active_edges_new` is incomplete or bypassable via malformed font metrics, `x` could exceed valid array bounds. So the violation TYPE (out-of-bounds array access via large x) is plausible in a security context. Q2 (Is this specific witness realistic?): The specific counterexample value `x = 1073741807` (~1 billion) is a CBMC symbolic extreme — no real font rasterization would produce pixel positions of that magnitude. The witness also has `e->ey = +inf` and `y0 = +inf`, which are also CBMC symbolic extremes. These specific values are not achievable in practice. The key concern is that CBMC analyzed the function in isolation (it's marked as a system entry point with no callers), so it injected arbitrary unconstrained values. In real execution, the caller `stbtt__fill_active_edges_new` provides x values bounded by the scanline width, and those bounds should prevent this extreme. However, if the font data corrupts or bypasses those constraints, a smaller-but-still-oob x value is conceivable.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
