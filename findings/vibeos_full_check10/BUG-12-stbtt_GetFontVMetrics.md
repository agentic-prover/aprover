# BUG-12 — `stbtt_GetFontVMetrics` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/ttf.c` |
| **Bug type** | arithmetic |
| **Violated property** | `stbtt_GetFontVMetrics.pointer_arithmetic.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

stbtt_GetScaledFontVMetrics → stbtt_GetFontVMetrics

## Spec (LLM-generated)

**Precondition:** `requires valid(info) && valid(info->data) && info->hhea >= 0 && (info->hhea + 10) <= info->data_length && (ascent == null(ascent) || valid(ascent)) && (descent == null(descent) || valid(descent)) && (lineGap == null(lineGap) || valid(lineGap)) && info has been successfully initialized by stbtt_InitFont (info->data is a valid font buffer of at least info->data_length bytes, and info->hhea is a valid offset into that buffer such that bytes at offsets info->hhea+4, info->hhea+5, info->hhea+6, info->hhea+7, info->hhea+8, info->hhea+9 are all within bounds)`

**Postcondition:** `ensures (ascent != null(ascent) => valid(ascent) && *ascent == ttSHORT(info->data + info->hhea + 4)) && (descent != null(descent) => valid(descent) && *descent == ttSHORT(info->data + info->hhea + 6)) && (lineGap != null(lineGap) => valid(lineGap) && *lineGap == ttSHORT(info->data + info->hhea + 8)) && the values written are raw unscaled signed 16-bit integer vertical metrics from the font's hhea table (ascent, descent, lineGap in font design units), no out-of-bounds memory access occurs, and no undefined behaviour is introduced`

## Counterexample

**Violated property:** `stbtt_GetFontVMetrics.pointer_arithmetic.1`

**Key variable assignments:**
```
_info_val = {'members': [{'name': 'userdata', 'value': {'data': 'NULL', 'name': 'pointer', 'type': 'const void *'}}, {'name': 'data', 'value': {'name': 'unknown'}}, {'name': 'fontstart', 'value': {'binary': '0...
info = _info_val!0@1
_ascent_val = 0
ascent = _ascent_val!0@1
_descent_val = 0
descent = _descent_val!0@1
_lineGap_val = 0
lineGap = _lineGap_val!0@1
return_value_ttSHORT_stub = 0
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['stbtt_GetScaledFontVMetrics']. Call chain: ['stbtt_GetScaledFontVMetrics', 'stbtt_GetFontVMetrics']. Full chain traced to system entry.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGSEGV`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGSEGV

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** Attacker-controlled font data can set an arbitrarily large hhea table offset, causing stbtt_GetFontVMetrics to perform an out-of-bounds read via unchecked pointer arithmetic on info->data + info->hhea + {4,6,8}. This is confirmed by dynamic execution (SIGSEGV).

Q1 (Can the violation TYPE occur?): Yes. The function performs unchecked pointer arithmetic: `info->data + info->hhea + 4/6/8`. The `hhea` field is populated by `stbtt__find_table()` in `stbtt_InitFont_internal`, which reads table offsets directly from the raw font binary. A maliciously crafted font file can supply an arbitrarily large `hhea` offset (e.g., 268435448 as in the counterexample), causing `info->data + info->hhea + 8` to point far beyond the allocated font buffer. There is no bounds check anywhere in `stbtt_GetFontVMetrics` or its callers to validate that `info->hhea` is within the actual data buffer size. This is a classic out-of-bounds read in a font parser consuming untrusted input. Q2 (Is this specific witness realistic?): Yes. The `hhea` offset of 268435448 (0x0FFFFFF8) is exactly the kind of value a crafted font file could embed in its table directory. The call chain reaches this function from `stbtt_GetScaledFontVMetrics` with `fontdata` being an external font blob. No caller validates that `info->hhea` plus the access offset is within the bounds of `info->data`. The dynamic harness independently confirmed a SIGSEGV crash, directly validating this is a real memory-safety fault and not a CBMC artifact.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
