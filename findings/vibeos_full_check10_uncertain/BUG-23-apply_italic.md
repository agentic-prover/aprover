# BUG-23 — `apply_italic` (ttf)

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

**Precondition:** `requires valid_range(bitmap, 0, stride * h) && stride > 0 && h > 0 && content_w > 0 && content_w <= stride && valid(new_w) && stride * h <= temp_bitmap_size && !null(bitmap) && !null(new_w) && stride * h >= 0 && (stride * h) does not overflow int arithmetic`

**Postcondition:** `ensures *new_w >= 0 && *new_w <= stride && the contents of bitmap[0..stride*h) are modified in-place to apply italic shearing (each row's pixels shifted right by (h-1-y)*0.2f) && memory safety is preserved: no out-of-bounds reads from bitmap[0..stride*h) and no out-of-bounds writes to bitmap[0..stride*h)`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
temp_bitmap = ((uint8_t *)NULL)
temp_bitmap_size = 0
_bitmap_val = 0
bitmap = _bitmap_val!0@1
stride = 1540
content_w = 1
h = 1073741824
_new_w_val = -2147483648
new_w = _new_w_val!0@1
```

## Root cause

CBMC reports a `main.assertion.1` failure — a semantic / contract violation in `apply_italic`.

**Realism checker's key concern:** The specific CBMC witness requires temp_bitmap=NULL (causing early return, so the function body never executes the overflow) — this exact path is a harness artifact. However, the same overflow class IS exploitable when temp_bitmap is non-NULL with an attacker-controlled large h from a malformed font file.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`apply_italic` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific CBMC witness requires temp_bitmap=NULL (causing early return, so the function body never executes the overflow) — this exact path is a harness artifact. However, the same overflow class IS exploitable when temp_bitmap is non-NULL with an attacker-controlled large h from a malformed font file.

Q1 (Can the violation TYPE occur in real execution?): YES. The core vulnerability class here is integer overflow in `stride * h` used in size comparisons and memset/memcpy calls. With `stride` and `h` both being `int`, `stride * h` can overflow for large glyph heights derived from untrusted font files. For example, with stride=1540 and h=2^30, `stride * h = 1540 * 1073741824 = 385 * 2^32 ≡ 0 (mod 2^32)`, causing the guard check `if (stride * h > temp_bitmap_size)` to incorrectly pass. Subsequently, `memset(temp_bitmap, 0, 0)` writes nothing, but the loop `for (int y = 0; y < h; y++)` iterates with the real (huge) h value, and `temp_bitmap[y * stride + dst_x]` with large `y` causes `y * stride` to overflow and access arbitrary memory — a real out-of-bounds write. Additionally, `y * stride + dst_x` in the inner loop uses int arithmetic that can overflow for large y values. This is exploitable via attacker-controlled font files that set extreme glyph heights. Q2 (Are the specific witness values realistic?): NO. The counterexample sets `temp_bitmap = NULL`, causing immediate early return at the first guard. This specific path (with NULL temp_bitmap bypassing the CBMC assertion about `*new_w`) is a CBMC harness artifact. However, the integer overflow issue in `stride * h` and the loop index arithmetic is real and reachable on paths where `temp_bitmap` is non-NULL but `h` is large enough to trigger overflow.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
