# BUG-34 — `stbtt_setvertex` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Dynamic outcome** | not_triggered |
| **Module** | `kernel/ttf.c` |
| **Bug type** | arithmetic |
| **Violated property** | `stbtt_setvertex.overflow.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

stbtt__csctx_v → stbtt_setvertex

## Spec (LLM-generated)

**Precondition:** `requires valid(v) && (type == STBTT_vmove || type == STBTT_vline || type == STBTT_vcurve)`

**Postcondition:** `ensures v->type == type && v->x == (stbtt_int16)x && v->y == (stbtt_int16)y && v->cx == (stbtt_int16)cx && v->cy == (stbtt_int16)cy`

## Counterexample

**Violated property:** `stbtt_setvertex.overflow.1`

**Key variable assignments:**
```
_v_val = {'members': [{'name': 'x', 'value': {'binary': '0000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed short int', 'width': 16}}, {'name': 'y', 'value': {'binary': '0000000000000000', '...
v = _v_val!0@1
type = 1
x = 32768
y = -1738539008
cx = 32896
cy = 34816
_v_val.type = 1
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['stbtt__csctx_v', 'stbtt__GetGlyphShapeTT']. Call chain: ['stbtt__csctx_v', 'stbtt_setvertex'].

## Dynamic confirmation

Dynamic harness outcome: `not_triggered`. Dynamic harness ran to completion without triggering a fault.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The violation is a silent truncation rather than a crash, explaining the dynamic harness not triggering a signal. However, the underlying bug class — unguarded narrowing cast of font-file-derived int32 to int16 — is real and exploitable: crafted fonts can silently corrupt vertex geometry, with potential downstream memory-safety implications in rasterization code.

Q1 — Can the violation TYPE occur? Yes. The function casts stbtt_int32 parameters to stbtt_int16 fields without any range check. If any of x, y, cx, or cy exceeds INT16_MAX (32767) or falls below INT16_MIN (-32768), the cast produces implementation-defined behavior under the C standard (value truncation on virtually all platforms). Since stb_truetype is a font file parser, these coordinate values derive directly from external (attacker-controllable) font data. The call chain goes through stbtt__csctx_v, which processes CFF glyph outlines read from a font file. A maliciously crafted font file can supply coordinate values exceeding 16-bit range, triggering this violation in every real deployment that parses untrusted fonts.

Q2 — Are the specific witness values achievable? Yes. x=32768 is exactly INT16_MAX+1, a plausible coordinate in a crafted font. The caller stbtt__csctx_v passes x/y/cx/cy as stbtt_int32 without any bounds clamping. Similarly, stbtt__close_shape computes (cx+scx)>>1 which could yield large values if cx and scx are both large positives from font data. The dynamic harness did not crash because on x86/x64 the cast silently truncates — this is the expected behavior: no signal is raised, but data is silently corrupted. The CBMC property (overflow in signed narrowing cast) is technically valid and represents real data-integrity risk. In a security context, corrupted vertex coordinates can propagate to downstream rasterization arithmetic, potentially causing out-of-bounds memory accesses in callers that use the resulting vertex array.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
