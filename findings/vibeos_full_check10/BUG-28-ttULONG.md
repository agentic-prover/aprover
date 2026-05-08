# BUG-28 — `ttULONG` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | inconclusive |
| **Module** | `kernel/ttf.c` |
| **Bug type** | memory_safety |
| **Violated property** | `ttULONG.pointer_dereference.11` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_InitFont → stbtt_InitFont_internal → ttULONG

## Spec (LLM-generated)

**Precondition:** `requires valid_range(p, 0, 4) && p != null`

**Postcondition:** `ensures \result == (((stbtt_uint32)p[0]) << 24) | (((stbtt_uint32)p[1]) << 16) | (((stbtt_uint32)p[2]) << 8) | ((stbtt_uint32)p[3]) && \result >= 0 && \result <= 0xFFFFFFFF`

## Counterexample

**Violated property:** `ttULONG.pointer_dereference.11`

**Key variable assignments:**
```
_p_val = 0
p = _p_val!0@1
result = 0u
return_value_ttULONG = 0u
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['stbtt_InitFont_internal', 'stbtt_GetGlyphSVG', 'stbtt_FindGlyphIndex', 'stbtt__GetGlyfOffset', 'stbtt__GetGlyphKernInfoAdvance', 'stbtt__get_svg', 'stbtt_GetFontOffsetForIndex_internal', 'stbtt__find_table']. Call chain: ['stbtt_PackFontRange', 'stbtt_PackFontRanges', 'stbtt_InitFont', 'stbtt_InitFont_internal', 'ttULONG']. Full chain traced to system entry.

## Dynamic confirmation

Dynamic harness outcome: `inconclusive`. Dynamic harness compilation failed even without global state injection for 'stbtt_PackFontRange'. Error: /tmp/tmp_9zkbteh.c: In function ‘stbtt_InitFont_internal’:
/tmp/tmp_9zkbteh.c:1197:16: error: incompatible types when assigning to type ‘stbtt__buf’ from type ‘int’
 1197 |    info->cff = stbtt__new_buf(((void *)0), 0);
      |                ^~~~~~~~~~~~~~
/tmp/tmp_9zkbteh.c:1216:25: error: incompa

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** None that makes this unrealistic — all callers pass externally-controlled font data and at least some call sites lack prior bounds validation of the computed pointer before passing it to ttULONG.

Q1 (Can the violation TYPE occur?): YES. The function `ttULONG` dereferences `p` unconditionally with no NULL or bounds check. It is called as `ttULONG(data+encoding_record+4)` in `stbtt_InitFont_internal`, where both `data` and `encoding_record` are derived from external/untrusted font file content. Two realistic attack scenarios exist: (1) A caller passes NULL fontdata — looking at `stbtt_PackFontRanges`, `fontdata` is an external input that could be NULL, and while `stbtt_InitFont_internal` performs some table existence checks, the path to a `ttULONG` call on a NULL-derived pointer may survive those checks; (2) An attacker crafts a malicious TTF font file where `encoding_record` is set to a large value so that `data + encoding_record + 4` points beyond the font buffer's valid range, causing an out-of-bounds read — a well-known vulnerability class in font parsing code. The entire call chain originates from font data ingestion (`stbtt_PackFontRange`), which is a classic untrusted-input entry point. Q2 (Are specific witness values achievable?): The witness shows `p = 0` (NULL), which could arise if `fontdata` is NULL or if offset arithmetic wraps around. While the exact aliasing construct CBMC uses may be a symbolic artifact, the NULL case is reachable if the caller provides NULL font data without prior validation. More importantly, even if the NULL scenario is unlikely, the out-of-bounds scenario with a crafted font file is independently and highly realistic. stb_truetype has historically had CVEs for exactly this class of vulnerability in its font table offset parsing.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
