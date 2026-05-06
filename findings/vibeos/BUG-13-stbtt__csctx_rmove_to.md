# BUG-13 — `stbtt__csctx_rmove_to` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `vendor/stb_truetype.h` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no callers)

## Spec (LLM-generated)

**Precondition:** `requires valid(ctx) && (dx and dy are finite float values)`

**Postcondition:** `ensures ctx->x == \old(ctx->x) + dx && ctx->y == \old(ctx->y) + dy && ctx->first_x == ctx->x && ctx->first_y == ctx->y && a vmove vertex has been recorded at position ((int)ctx->x, (int)ctx->y) and any previously open shape has been closed`

## Counterexample

**Violated property:** `main.assertion.2`

**Key variable assignments:**
```
ctx.x        = -6146.866211
ctx.first_x  = -6146.866211
ctx.y        = +NaN  (0x7f800001 bit pattern)
ctx.first_y  = +NaN
dx           = 6146.866211
dy           = -1.401298e-45  (subnormal float)
tmp_assign   = -6146.866211
tmp_assign$0 = +NaN  (NaN propagated through addition)
```

## Root cause

`stbtt__csctx_rmove_to` accumulates relative position deltas and then casts the result to `int` via `(int)ctx->y`. When `ctx->y` holds a NaN value (which can propagate through float arithmetic on charstring stack entries from CFF font data), the cast `(int)ctx->y` is undefined behavior in C (C11 §6.3.1.4). On x86 with SSE, `cvttss2si` produces `INT_MIN` (0x80000000) for NaN inputs — but this is implementation-defined. NaN can enter `ctx->y` through a sequence of accumulated relative `dy` values from a malformed CFF charstring: if the charstring encodes extreme delta values such that `Inf + (-Inf)` produces NaN, all subsequent y-coordinate operations propagate the NaN.

**Important note:** This bug is only reachable when a CFF/OpenType font is loaded. The default VibeOS font `Roboto-Regular.ttf` is TrueType format and does NOT exercise the CFF charstring interpreter path (`stbtt__run_charstring`). This bug requires explicitly loading a CFF-format font.

## How to trigger

1. Load a CFF/OpenType font (`.otf` extension, or a `.ttf` file containing CFF outlines) into VibeOS — this requires a code path change since the default Roboto font is TrueType.
2. Provide a font with a malformed CFF charstring that encodes a sequence of relative `rmoveto` deltas designed to produce Inf or NaN in the accumulated y coordinate (e.g., very large positive delta followed by very large negative delta).
3. Call any glyph rendering function that traverses the charstring, reaching `stbtt__csctx_rmove_to` with `ctx->y == NaN`.
4. The `(int)ctx->y` cast produces implementation-defined results, potentially corrupting the glyph vertex coordinates.

## Realism assessment

**Verdict:** REALISTIC*

\* Only reachable when a CFF/OpenType font is loaded (default VibeOS font is TrueType).

The violation occurs at the cast `(int)ctx->y` when `ctx->y` is NaN. This traces to:

1. **Source of NaN**: The counterexample shows `ctx->y = +NaN` entering the function. In stb_truetype, `ctx->y` accumulates values from charstring stack entries (`s[sp-1]`) via repeated `+dy` operations across many `stbtt__csctx_rmove_to` calls.

2. **Attack surface**: The call sites are `stbtt__csctx_rmove_to(c, s[sp-2], s[sp-1])` etc., where `s[]` contains values parsed from CFF charstrings in a font file. Font files are external, potentially untrusted input. Malformed fonts could encode extreme delta values. Through large accumulated sums, `ctx->y` could reach ±Inf, and subsequent arithmetic (e.g., Inf + (-Inf)) could produce NaN. Alternatively, some CFF implementations that decode real numbers could produce NaN directly.

3. **The UB**: Casting a NaN float to `int` via `(int)ctx->y` is genuine C undefined behavior (C11 §6.3.1.4). On x86 with SSE, `cvttss2si` produces 0x80000000 (INT_MIN) for NaN, but this is implementation-defined and not guaranteed.

4. **Not a verification artifact**: The scenario requires `ctx->y` to be NaN before the call, which is achievable through prior accumulated float arithmetic on maliciously crafted charstring deltas. This is a realistic attack vector since stb_truetype is commonly used to parse untrusted fonts.

5. **Call-site consistency**: No call site validates the float values or guards against NaN propagation before passing to this function.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
