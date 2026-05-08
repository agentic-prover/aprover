# BUG-23 — `stbtt_GetScaledFontVMetrics` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid_range(fontdata, 0, 1) && index >= 0 && size != 0.0f && valid(ascent) && valid(descent) && valid(lineGap) && fontdata points to a valid TrueType/OpenType font collection with at least (index+1) fonts`

**Postcondition:** `valid(ascent) && valid(descent) && valid(lineGap) && *ascent >= 0.0f && *descent <= 0.0f && *lineGap >= 0.0f && the values *ascent, *descent, and *lineGap are scaled font vertical metrics in pixels corresponding to the given size (positive size means pixel height, negative size means EM-to-pixel mapping)`

## Counterexample

**Violated property:** `stbtt_ScaleForMappingEmToPixels_stub.overflow.1`

**Key variable assignments:**
```
_fontdata_val = 0
fontdata = _fontdata_val!0@1
index = 1073741824
size = +NaN
_ascent_val = 0
ascent = _ascent_val!0@1
_descent_val = 0
descent = _descent_val!0@1
_lineGap_val = 0
lineGap = _lineGap_val!0@1
i_ascent = 83902820
i_descent = 0
i_lineGap = 502069912
scale = 0
info = info!0@1
return_value_stbtt_GetFontOffsetForIndex_stub = 1073741824
data = _fontdata_val!0@1
result = 0
goto_symex$$return_value$$stbtt_GetFontOffsetForIndex_stub = 1073741824
offset = 1073741824
tmp_if_expr = 0
return_value_stbtt_ScaleForMappingEmToPixels_stub = 0
pixels = -NaN
```

## Root cause

CBMC reports a `stbtt_ScaleForMappingEmToPixels_stub.overflow.1` failure — a arithmetic / overflow violation in `stbtt_GetScaledFontVMetrics`.

**Realism checker's key concern:** No input validation on `size` (NaN triggers wrong code path) and no bounds checking on `fontdata`/`index` in a public API that processes potentially untrusted font data.

**Validator reasoning:** 'stbtt_GetScaledFontVMetrics' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`stbtt_GetScaledFontVMetrics` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** No input validation on `size` (NaN triggers wrong code path) and no bounds checking on `fontdata`/`index` in a public API that processes potentially untrusted font data.

Q1 (Can the violation TYPE occur?): Yes. `stbtt_GetScaledFontVMetrics` is a public API with no input validation. The ternary expression `size > 0 ? ... : stbtt_ScaleForMappingEmToPixels(&info, -size)` is the critical path. IEEE 754 NaN comparisons always return false, so `size = NaN` satisfies `!(size > 0)` and routes to `stbtt_ScaleForMappingEmToPixels(&info, -NaN)`. Passing NaN to a function expecting a positive pixel size causes arithmetic overflow/undefined behavior in the downstream computation (division by NaN or NaN propagation into integer truncation). Additionally, with no bounds checking on `fontdata` or `index`, a large index value causes `stbtt_GetFontOffsetForIndex` to compute an unchecked offset, which could result in out-of-bounds memory reads during `stbtt_InitFont`. Both vulnerabilities represent real bug classes. Q2 (Are the specific witness values achievable?): NaN float values are fully valid IEEE 754 bit patterns (0x7FC00000) that can arrive from: attacker-controlled network/file data memcpy'd into a float, prior arithmetic producing NaN, or direct API misuse. The `index = 1073741824` is a normal int value. The small 1-byte `fontdata` with a large index is a classic pattern for triggering font-parser out-of-bounds reads — a well-known vulnerability class in font libraries. As a public API callable from external code with no precondition guards on `size`, `index`, or buffer length, this is a realistic attack surface.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
