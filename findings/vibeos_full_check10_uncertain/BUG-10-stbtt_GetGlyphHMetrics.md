# BUG-10 — `stbtt_GetGlyphHMetrics` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

```
stbtt_PackFontRange -> stbtt_PackFontRanges -> stbtt_PackFontRangesRenderIntoRects -> stbtt_GetGlyphHMetrics
```

## Spec (LLM-generated)

**Precondition:** `requires valid(info) && valid(info->data) && info->hhea >= 0 && info->hmtx >= 0 && glyph_index >= 0 && (null(advanceWidth) || valid(advanceWidth)) && (null(leftSideBearing) || valid(leftSideBearing)) && the font data buffer at info->data is large enough such that info->data + info->hhea + 35 is in bounds (to read numOfLongHorMetrics) && if glyph_index < numOfLongHorMetrics: info->data + info->hmtx + 4*glyph_index + 3 is in bounds; if glyph_index >= numOfLongHorMetrics: info->data + info->hmtx + 4*(numOfLongHorMetrics-1) + 1 is in bounds for advanceWidth and info->data + info->hmtx + 4*numOfLongHorMetrics + 2*(glyph_index - numOfLongHorMetrics) + 1 is in bounds for leftSideBearing && numOfLongHorMetrics > 0 (to avoid underflow in 4*(numOfLongHorMetrics-1)) && 4*glyph_index does not overflow int && 4*numOfLongHorMetrics + 2*(glyph_index - numOfLongHorMetrics) does not overflow int`

**Postcondition:** `ensures (null(advanceWidth) || (*advanceWidth is set to the horizontal advance width for glyph_index in font design units, as a signed 16-bit value read from the hmtx table)) && (null(leftSideBearing) || (*leftSideBearing is set to the left side bearing for glyph_index in font design units, as a signed 16-bit value read from the hmtx table)) && no out-of-bounds memory access occurs && if advanceWidth is non-null then *advanceWidth >= 0 (advance width is non-negative per OpenType spec) && the function writes only to *advanceWidth and *leftSideBearing and does not modify *info or any other memory`

## Counterexample

**Violated property:** `stbtt_GetGlyphHMetrics.overflow.7`

**Key variable assignments:**
```
_info_val = <symbolic struct/array — see classification.json>
info = _info_val!0@1
glyph_index = 1610629127
_advanceWidth_val = 0
advanceWidth = _advanceWidth_val!0@1
_leftSideBearing_val = 0
leftSideBearing = _leftSideBearing_val!0@1
numOfLongHorMetrics = 32768
return_value_ttUSHORT_stub = 32768
p = <symbolic struct/array — see classification.json>
result = 0
goto_symex$$return_value$$ttUSHORT_stub = 32768
return_value_ttSHORT_stub$1 = 0
goto_symex$$return_value$$ttSHORT_stub = 0
return_value_ttSHORT_stub$2 = 0
```

## Root cause

CBMC reports a `stbtt_GetGlyphHMetrics.overflow.7` failure — a arithmetic / overflow violation in `stbtt_GetGlyphHMetrics`.

**Validator reasoning:** Counterexample state is reachable from caller(s): ['stbtt_PackFontRangesRenderIntoRects', 'stbtt_GetCodepointHMetrics', 'stbtt_BakeFontBitmap_internal']. Call chain: ['stbtt_PackFontRange', 'stbtt_PackFontRanges', 'stbtt_PackFontRangesRenderIntoRects', 'stbtt_GetGlyphHMetrics']. Full chain traced to system entry.

## How to trigger

Reach `stbtt_GetGlyphHMetrics` via the call chain `stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_PackFontRangesRenderIntoRects → stbtt_GetGlyphHMetrics` and supply inputs that match the counterexample variable assignments above.

## Realism assessment

**Verdict:** UNCERTAIN (— confidence)

Could not parse LLM response: ## Analysis

### Q1: Can this violation TYPE occur in real execution?

**Yes.** The function reads `numOfLongHorMetrics` from attacker-controlled font data (`info->data + info->hhea + 34`), then perfo

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
