# BUG-03 — `stbtt__h_prefilter` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGABRT |
| **Module** | `vendor/stb_truetype.h` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
stbtt_PackFontRange -> stbtt_PackFontRanges -> stbtt_PackFontRangesRenderIntoRects -> stbtt__h_prefilter
```

## Spec (LLM-generated)

**Precondition:** `requires (valid_range(pixels, 0, h * stride_in_bytes) && w >= 1 && h >= 1 && stride_in_bytes >= w && kernel_width > 1 && kernel_width <= w)`

**Postcondition:** `ensures the pixel buffer at pixels has been horizontally filtered in-place using a box filter of width kernel_width across all h rows each of width w with row stride stride_in_bytes; each pixel in the range valid_range(pixels, 0, h * stride_in_bytes) has been overwritten with the box-filtered value and no out-of-bounds memory access occurred; the memory region valid_range(pixels, 0, h * stride_in_bytes) remains valid after the call`

## Counterexample

**Violated property:** `stbtt__h_prefilter.precondition_instance.2`

**Key variable assignments:**
```
pixels        = valid pointer (value byte = 254)
w             = 44
h             = 1
stride_in_bytes = 1073741825 (0x40000001)
kernel_width  = 43
buffer[0..7]  = 0 (8-byte stack array)
safe_w        = 1
```

## Root cause

`stbtt__h_prefilter` uses an 8-byte stack buffer `buffer[8]` as a ring buffer for the horizontal box filter. The function calls `memset(buffer, 0, kernel_width)` to zero the ring buffer, but `kernel_width` is a user-controlled parameter (derived from `h_oversample` in the pack context) and is not validated against the fixed 8-byte capacity. When `kernel_width > 8`, the `memset` overflows the stack buffer into adjacent stack memory. The counterexample uses `kernel_width = 43`, overflowing by 35 bytes, which the dynamic harness confirmed triggers SIGABRT from heap/stack corruption detection.

## How to trigger

Call `stbtt_PackBegin` to initialize a pack context, then set `spc.h_oversample = 9` (or any value > 8) before calling `stbtt_PackFontRange`. The oversample value propagates to `kernel_width` in `stbtt__h_prefilter` and overflows the 8-byte `buffer` on the first row. A single-byte overflow (kernel_width = 9) is sufficient; the counterexample uses 43.

## Realism assessment

**Verdict:** REALISTIC

The core vulnerability is `memset(buffer, 0, kernel_width)` where `buffer` is only 8 bytes but `kernel_width` is an attacker/user-controlled value (43 in the counterexample). This is a clear stack buffer overflow.

1. **The overflow trigger**: Any `kernel_width > 8` causes `memset(buffer, 0, kernel_width)` to write beyond the 8-byte stack buffer. The counterexample uses 43, but even `kernel_width = 9` would overflow by 1 byte.

2. **Call-site reachability**: The function is called from `stbtt_PackFontRangesRenderIntoRects` with `kernel_width` derived from `spc->h_oversample`, which is set from `ranges[i].h_oversample`. This value is provided by the API caller — it's user-controlled. While typical usage is 1–4x oversampling, there is no bounds check validating `kernel_width <= 8` before the memset.

3. **Dynamic confirmation**: The harness confirmed the fault (SIGABRT) with concrete inputs, not just abstract counterexample values. The crash is deterministic.

4. **Secondary issue**: Even if kernel_width ≤ 8, the `buffer[(i+kernel_width) & (8-1)]` ring-buffer indexing only works correctly when kernel_width is a power of 2 dividing 8, so even non-overflow cases with e.g. kernel_width=5 or 6 have logic bugs — but the memset overflow is the critical safety issue.

5. **Not a false positive**: The counterexample does not rely on NULL pointers, zero-initialized globals, or impossible allocator behavior. The `kernel_width` value is entirely user-controlled through a public API.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
