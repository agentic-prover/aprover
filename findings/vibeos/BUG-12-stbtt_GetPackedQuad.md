# BUG-12 — `stbtt_GetPackedQuad` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `vendor/stb_truetype.h` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no callers)

## Spec (LLM-generated)

**Precondition:** `valid_range(chardata, 0, char_index + 1) && char_index >= 0 && pw > 0 && ph > 0 && valid(xpos) && valid(ypos) && valid(q)`

**Postcondition:** `valid(q) && valid(xpos) && q->s0 >= 0.0 && q->s1 >= 0.0 && q->t0 >= 0.0 && q->t1 >= 0.0 && q->s0 <= 1.0 && q->s1 <= 1.0 && q->t0 <= 1.0 && q->t1 <= 1.0 && *xpos == \old(*xpos) + chardata[char_index].xadvance && q->x0 <= q->x1 && q->y0 <= q->y1`

## Counterexample

**Violated property:** `main.assertion.12`

**Key variable assignments:**
```
char_index    = 1
pw            = 2
ph            = 1
xpos          = -2614272.0
ypos          = -3.737230e+35
align_to_integer = 0
chardata      = 1-element array (only index 0 is valid)
```

## Root cause

`stbtt_GetPackedQuad` accesses `chardata[char_index]` with `char_index = 1` while the `chardata` array contains only a single element (index 0). The function performs no bounds checking on `char_index` against the actual array size, which is by design in the lightweight stb_truetype library — it trusts the caller. However, this creates an out-of-bounds access if the caller miscalculates the character index relative to the packed font range. The counterexample uses artificially constrained inputs (a 1-element array with a 2x1 texture atlas), which would not arise in normal library usage.

## How to trigger

In a real scenario, this would require a coding error in the caller: computing `char_index` using an incorrect `first_char` offset, allocating a smaller-than-required `chardata` array, or using a mismatched character range. For example, if `first_char = 'A'` but `char_index = codepoint - 'B'` for a character not in the packed range, the index could exceed the array bounds.

Note: The dynamic harness did NOT trigger a fault with the given counterexample inputs, suggesting the concrete scenario may require specific runtime conditions that are not readily reproduced.

## Realism assessment

**Verdict:** UNCERTAIN

The counterexample sets char_index = 1 with a chardata array that has only a single element (index 0), causing an out-of-bounds pointer dereference at `b = chardata + 1`. The suspicious output values (t0 = 388, t1 = 64552, s1 = 928) are consistent with reading garbage/uninitialized memory from beyond the end of the allocated chardata array.

For realism assessment:
1. The function has no bounds checking on `char_index` against the actual array size — this is by design in stb_truetype, which is a lightweight header library that trusts the caller.
2. In real usage, `char_index` is typically computed as `(codepoint - first_char)` and the chardata array is allocated to cover the full packed glyph range. These should be coordinated by the caller.
3. However, if caller code has a bug (e.g., wrong first_char offset, wrong array allocation size, or mismatched character range), `char_index` could exceed the array bounds — this is a realistic misuse scenario.
4. The dynamic harness did NOT trigger the fault, suggesting the specific counterexample setup (1-element array with index 1) may be an artifact of unconstrained symbolic inputs.
5. pw=2, ph=1 are unrealistically small texture atlas dimensions (real font atlases are typically 256x256 or larger), and the single-element chardata array is artificially constrained.
6. The violated property is labeled 'main.assertion.12' without clarity on what assertion is being checked, making it harder to assess the precise nature of the violation.

The scenario could occur with a buggy caller but the counterexample inputs are artificially constrained in a way that wouldn't arise in normal library usage.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
