# BUG-02 — `stbtt__buf_get` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `vendor/stb_truetype.h` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
stbtt_GetGlyphBitmap -> stbtt_GetGlyphBitmapSubpixel -> stbtt_GetGlyphShape -> stbtt__GetGlyphShapeT2 -> stbtt__run_charstring -> stbtt__buf_get
```

## Spec (LLM-generated)

**Precondition:** `requires valid(b) && b->cursor >= 0 && b->size >= 0 && n >= 1 && n <= 4 && b->cursor + n <= b->size && valid_range(b->data, b->cursor, b->cursor + n)`

**Postcondition:** `ensures b->cursor == \old(b->cursor) + n && \result == the big-endian unsigned integer formed by reading n bytes from b->data starting at \old(b->cursor) && \result >= 0`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
b.cursor = 8388607 (0x7FFFFF)
b.size   = 1073741824 (0x40000000)
b.data   = unknown (pointer to backing allocation)
n        = 1
```

## Root cause

`stbtt__buf_get` reads `n` bytes from the CFF font buffer by calling `stbtt__buf_get8` in a loop. The bounds check in `stbtt__buf_get8` compares `cursor >= size`, but `size` is derived directly from the font file and may not reflect the true size of the backing memory allocation. A crafted CFF font can set the `size` field to a large value (here 1 GiB) while the actual allocated buffer is much smaller, allowing `cursor` to pass the check but still access memory well beyond the allocation boundary.

## How to trigger

Load a CFF/OpenType font with a maliciously inflated buffer size field embedded in the CFF index data. Call `stbtt_GetGlyphBitmap` (or any glyph rendering function) on a glyph whose charstring traversal reaches `stbtt__run_charstring`. The `stbtt__buf` structs are built from untrusted font-file offsets, so the crafted size value propagates through to `stbtt__buf_get8`, which will access memory beyond the real allocation.

## Realism assessment

**Verdict:** REALISTIC

Step 1 — Understand the violation: The function `stbtt__buf_get` reads `n` bytes from a buffer `b` by calling `stbtt__buf_get8` in a loop. The `((void)0)` is a disabled assertion (originally STBTT_assert(n >= 1 && n <= 4)). The SIGSEGV arises from `stbtt__buf_get8` accessing `b->data[b->cursor]` when `data` points to invalid memory.

Step 2 — Examine the counterexample: cursor=8388607, size=1073741824, data=unknown. The `cursor < size` condition holds, so `stbtt__buf_get8` will attempt to dereference `b->data[8388607]`. If the actual backing allocation for `data` is smaller than `size` claims (which can happen with malformed font data), this is a real out-of-bounds read.

Step 3 — Assess the call chain: `stbtt_GetGlyphBitmap → ... → stbtt__run_charstring → stbtt__buf_get`. This is a CFF font parser. The `stbtt__buf` structs are constructed from values embedded in the font file itself (offsets, sizes come from the font binary). A maliciously crafted CFF font can set `size` to a large value while the underlying allocation backing `data` is much smaller.

Step 4 — The bounds check in `stbtt__buf_get8` compares `cursor >= size`, but `size` is derived from the font file and may not reflect the true allocation size. If `size` is inflated in the font data, the bounds check passes but memory beyond the allocation is accessed.

Step 5 — Dynamic confirmation: The harness confirmed SIGSEGV.

Step 6 — Call sites like `stbtt__cff_index_get` and `stbtt__cff_get_index` pass `offsize` (derived from untrusted font bytes) as `n`, and buffers derived from untrusted font offsets as `b`. This is a parser handling external/untrusted data, making the scenario realistically exploitable with a crafted font file.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
