# BUG-26 — `stbtt_ScaleForPixelHeight` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | inconclusive |
| **Module** | `kernel/ttf.c` |
| **Bug type** | arithmetic |
| **Violated property** | `stbtt_ScaleForPixelHeight.pointer_arithmetic.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_PackFontRangesRenderIntoRects → stbtt_ScaleForPixelHeight

## Spec (LLM-generated)

**Precondition:** `requires valid(info) && valid_range(info->data, info->hhea + 4, info->hhea + 8) && height > 0.0f && (ttSHORT(info->data + info->hhea + 4) - ttSHORT(info->data + info->hhea + 6)) != 0 && info->data != NULL && info->hhea >= 0`

**Postcondition:** `ensures \result > 0.0f && \result is a finite positive float representing the scale factor such that multiplying unscaled font metrics (in font units) by \result yields dimensions in pixels corresponding to the given pixel height; specifically \result == height / (ascent - descent) where ascent and descent are read from the hhea table of the font`

## Counterexample

**Violated property:** `stbtt_ScaleForPixelHeight.pointer_arithmetic.1`

**Key variable assignments:**
```
_info_val = {'members': [{'name': 'userdata', 'value': {'data': 'NULL', 'name': 'pointer', 'type': 'const void *'}}, {'name': 'data', 'value': {'name': 'unknown'}}, {'name': 'fontstart', 'value': {'binary': '0...
info = _info_val!0@1
height = 2.756410e-40
result = 0
return_value_stbtt_ScaleForPixelHeight = 0
fheight = 0
return_value_ttSHORT_stub = 0
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['stbtt_PackFontRangesRenderIntoRects', 'stbtt_PackFontRangesGatherRects', 'stbtt_BakeFontBitmap_internal']. Call chain: ['stbtt_PackFontRange', 'stbtt_PackFontRanges', 'stbtt_PackFontRangesRenderIntoRects', 'stbtt_ScaleForPixelHeight']. Full chain traced to system entry.

## Dynamic confirmation

Dynamic harness outcome: `inconclusive`. Dynamic harness compilation failed even without global state injection for 'stbtt_PackFontRange'. Error: /tmp/tmp29cq1351.c: In function ‘stbtt_InitFont_internal’:
/tmp/tmp29cq1351.c:1197:16: error: incompatible types when assigning to type ‘stbtt__buf’ from type ‘int’
 1197 |    info->cff = stbtt__new_buf(((void *)0), 0);
      |                ^~~~~~~~~~~~~~
/tmp/tmp29cq1351.c:1216:25: error: incompa

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** A maliciously crafted font file with a large 'hhea' table offset could cause `info->data + info->hhea + 6` to read beyond the allocated font data buffer, yielding an exploitable out-of-bounds read via pointer arithmetic on attacker-controlled offset data.

Q1 (Can the violation TYPE occur?): Yes. The function performs pointer arithmetic `info->data + info->hhea + 4` and `info->data + info->hhea + 6` without any bounds check against the size of the font data buffer. `info->hhea` is populated by `stbtt_InitFont_internal` from the font file's table directory via `stbtt__find_table`. A maliciously crafted TTF/OTF font file could set the 'hhea' table offset to a value that, when added to the data pointer (+4 or +6), extends past the end of the allocated font buffer, producing an out-of-bounds read. This is a well-known vulnerability class in font parsing code (stb_truetype has historical CVEs of this exact type).

Q2 (Are the specific witness values achievable?): The counterexample shows `info->hhea = 524311` (a large offset) with `info->data` as symbolic/unknown. In real execution, the font file is attacker-controlled — the call chain originates from `stbtt_PackFontRange`, which accepts an arbitrary `fontdata` pointer. If the font buffer is smaller than `info->hhea + 6` bytes, the arithmetic overruns it. There is no bounds check between `stbtt_InitFont` storing the hhea offset and this function using it. The specific witness value (524311) is plausible for a crafted font file; the exact pointer value is symbolic but the scenario is realistically reachable.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
