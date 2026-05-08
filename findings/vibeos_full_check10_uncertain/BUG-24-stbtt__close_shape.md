# BUG-24 — `stbtt__close_shape` (ttf)

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

**Precondition:** `requires valid_range(vertices, 0, num_vertices + 2) && num_vertices >= 0 && (was_off == 0 || was_off == 1) && (start_off == 0 || start_off == 1)`

**Postcondition:** `ensures \result >= num_vertices && \result <= num_vertices + 2 && valid_range(vertices, 0, \result)`

## Counterexample

**Violated property:** `stbtt__close_shape.pointer_arithmetic.5`

**Key variable assignments:**
```
_vertices_val = <symbolic struct/array — see classification.json>
vertices = _vertices_val!0@1
num_vertices = 1811939328
was_off = 1
start_off = 1
sx = 0
sy = 0
scx = 0
scy = -2147483648
cx = 0
cy = -2147483648
result = 0
return_value_stbtt__close_shape = 0
tmp_post_num_vertices = 1811939327
```

## Root cause

CBMC reports a `stbtt__close_shape.pointer_arithmetic.5` failure — a arithmetic / overflow violation in `stbtt__close_shape`.

**Realism checker's key concern:** The specific witness requires num_vertices=1.8 billion against a 1-element buffer — a CBMC symbolic artifact. In real code, the two-pass design of stbtt__GetGlyphShapeTT should bound num_vertices, but a crafted font could potentially create a discrepancy between the count and fill passes, making the violation type (OOB write) plausible but not confirmed by this witness.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`stbtt__close_shape` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific witness requires num_vertices=1.8 billion against a 1-element buffer — a CBMC symbolic artifact. In real code, the two-pass design of stbtt__GetGlyphShapeTT should bound num_vertices, but a crafted font could potentially create a discrepancy between the count and fill passes, making the violation type (OOB write) plausible but not confirmed by this witness.

Q1 (Can the violation TYPE occur?): The violation is a pointer arithmetic / out-of-bounds access: `vertices[num_vertices]` is computed when `num_vertices` could be larger than the allocated buffer. In the real program, `stbtt__GetGlyphShapeTT` uses a two-pass approach — first counting required vertices, then allocating, then filling. If a malformed/attacker-controlled TrueType font creates an inconsistency between the counting pass and the filling pass (e.g., through crafted contour data), `num_vertices` during the fill pass could exceed the allocated buffer size, causing an out-of-bounds write in `stbtt_setvertex`. This is a realistic class of vulnerability for font parsers handling untrusted input. Historically, stb_truetype has had such issues. So Q1=YES, the violation type is plausible.

Q2 (Are the specific witness values realistic?): The counterexample uses `num_vertices = 1811939328` (~1.8 billion), which is a clear CBMC symbolic artifact — CBMC treats `num_vertices` as an unconstrained `int`. In real execution, `num_vertices` is bounded by the number of contour points in the font, which for any reasonably-sized font file would be orders of magnitude smaller. Additionally, `vertices` points to a single-element buffer in the counterexample, which is clearly a harness artifact. So Q2=NO, the specific witness is unrealistic.

Conclusion: The violation TYPE (out-of-bounds write via unchecked `num_vertices`) is a real concern for font parsers with malformed input, but the specific CBMC witness requires impossible conditions in practice. The actual exploitability depends entirely on whether the two-pass allocation in `stbtt__GetGlyphShapeTT` correctly bounds `num_vertices` relative to the allocated array size.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
