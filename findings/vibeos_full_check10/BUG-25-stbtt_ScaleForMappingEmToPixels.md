# BUG-25 — `stbtt_ScaleForMappingEmToPixels` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
stbtt_PackFontRange -> stbtt_PackFontRanges -> stbtt_PackFontRangesRenderIntoRects -> stbtt_ScaleForMappingEmToPixels
```

## Spec (LLM-generated)

**Precondition:** `requires valid(info) && valid_range(info->data, info->head + 18, info->head + 20) && pixels != 0 && (pixels < 0 ? -pixels > 0 : pixels > 0) && ttUSHORT(info->data + info->head + 18) != 0`

**Postcondition:** `ensures \result > 0 && \result is a finite positive float representing the scale factor to map EM units to pixels, computed as pixels / unitsPerEm where unitsPerEm = ttUSHORT(info->data + info->head + 18) > 0`

## Counterexample

**Violated property:** `stbtt_ScaleForMappingEmToPixels.pointer_arithmetic.1`

**Key variable assignments:**
```
_info_val = <symbolic struct/array — see classification.json>
info = _info_val!0@1
pixels = -2.802597e-45
result = 0
return_value_stbtt_ScaleForMappingEmToPixels = 0
unitsPerEm = 0
return_value_ttUSHORT_stub = 0
```

## Root cause

CBMC reports a `stbtt_ScaleForMappingEmToPixels.pointer_arithmetic.1` failure — a arithmetic / overflow violation in `stbtt_ScaleForMappingEmToPixels`.

**Realism checker's key concern:** The attacker can craft a font file where `stbtt__find_table` returns a `head` table offset such that `info->data + info->head + 18` points outside the allocated font buffer, causing an exploitable out-of-bounds read — classic in font parsing security vulnerabilities.

**Validator reasoning:** Counterexample state is reachable from caller(s): ['stbtt_PackFontRangesRenderIntoRects', 'stbtt_PackFontRangesGatherRects']. Call chain: ['stbtt_PackFontRange', 'stbtt_PackFontRanges', 'stbtt_PackFontRangesRenderIntoRects', 'stbtt_ScaleForMappingEmToPixels']. Full chain traced to system entry.

## How to trigger

Reach `stbtt_ScaleForMappingEmToPixels` via the call chain `stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_PackFontRangesRenderIntoRects → stbtt_ScaleForMappingEmToPixels` and supply inputs that match the counterexample variable assignments above.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The attacker can craft a font file where `stbtt__find_table` returns a `head` table offset such that `info->data + info->head + 18` points outside the allocated font buffer, causing an exploitable out-of-bounds read — classic in font parsing security vulnerabilities.

Q1 — Can the violation TYPE occur? Yes. The expression `info->data + info->head + 18` performs pointer arithmetic using `info->head`, which is an offset into the font binary parsed from `stbtt_InitFont`. Both `info->data` and `info->head` are derived from externally supplied `fontdata`. In a security context where fontdata comes from an attacker (a malformed TTF/OTF file), two scenarios can trigger an out-of-bounds pointer arithmetic violation: (a) if `stbtt__find_table` returns an offset for `head` such that `info->head + 18` exceeds the allocated font buffer size, `ttUSHORT` would read out-of-bounds; (b) if `fontdata` is NULL or too small, `info->data` could be NULL/invalid. There is no bounds check before this pointer arithmetic in the function. The font-parsing code path (stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_PackFontRangesRenderIntoRects → stbtt_ScaleForMappingEmToPixels) accepts arbitrary user-supplied font binary data without validating that `head + 18` is within the buffer bounds. Q2 — Is this specific witness realistic? The counterexample has `info->data` as 'unknown' (a CBMC symbolic artifact), but the `head = 22` value is plausible from real font parsing. Even if this exact witness path is a CBMC artifact, the underlying vulnerability class — an out-of-bounds read from `data + head + 18` using an attacker-controlled head offset in a font parser — is a well-known, exploitable vulnerability class in font rasterization libraries (e.g., CVE-class bugs in FreeType, stb_truetype). The call chain processes untrusted external data and performs no bounds validation before the pointer arithmetic.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
