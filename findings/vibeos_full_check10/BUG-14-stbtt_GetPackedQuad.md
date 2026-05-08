# BUG-14 — `stbtt_GetPackedQuad` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/ttf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(chardata, 0, char_index + 1) && char_index >= 0 && pw > 0 && ph > 0 && valid(xpos) && valid(ypos) && valid(q)`

**Postcondition:** `ensures valid(q) && valid(xpos) && q->s0 >= 0.0f && q->s1 <= 1.0f && q->t0 >= 0.0f && q->t1 <= 1.0f && *xpos == \old(*xpos) + chardata[char_index].xadvance && q->x0 <= q->x1 && q->y0 <= q->y1`

## Counterexample

**Violated property:** `stbtt_GetPackedQuad.pointer_dereference.11`

**Key variable assignments:**
```
_chardata_val = <symbolic struct/array — see classification.json>
chardata = _chardata_val!0@1
pw = 8388608
ph = 16388033
char_index = 1192
_xpos_val = -0.021272
xpos = _xpos_val!0@1
_ypos_val = 2
ypos = _ypos_val!0@1
_q_val = <symbolic struct/array — see classification.json>
q = _q_val!0@1
align_to_integer = 4194304
ipw = 1.192093e-7
iph = 6.102014e-8
b = <symbolic struct/array — see classification.json>
x = 0
return_value_floor = 0
```

## Root cause

CBMC reports a `stbtt_GetPackedQuad.pointer_dereference.11` failure — a memory-safety violation in `stbtt_GetPackedQuad`.

**Realism checker's key concern:** Unchecked `char_index` used directly in pointer arithmetic: `b = chardata + char_index` with no validation that `char_index < array_length`, confirmed by dynamic SIGSEGV.

**Validator reasoning:** 'stbtt_GetPackedQuad' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`stbtt_GetPackedQuad` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

A standalone GCC-compiled reproducer was generated and executed; it crashed with `SIGSEGV`. The reproducer source is preserved in the run's `classification.json` under `dynamic_result.harness_source`.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** Unchecked `char_index` used directly in pointer arithmetic: `b = chardata + char_index` with no validation that `char_index < array_length`, confirmed by dynamic SIGSEGV.

Q1 (Can the violation TYPE occur?): Yes. The function computes `b = chardata + char_index` with no bounds checking whatsoever on `char_index`. If `char_index` is larger than the allocated size of the `chardata` array, the pointer `b` will be out-of-bounds, and all subsequent dereferences (`b->xoff`, `b->xoff2`, `b->yoff`, etc.) will be invalid memory reads. This is a classic unchecked array index bug on a public API that accepts external data.

Q2 (Are the specific witness values achievable?): The specific counterexample has `chardata` as a 1-element array with `char_index = 1192`, which is obviously out of bounds. While this particular allocation size may be a CBMC artifact, the underlying scenario is entirely realistic: a caller could pass a `chardata` array allocated for, say, ASCII characters (96 entries) but then call this function with a `char_index` corresponding to a Unicode codepoint or an attacker-controlled value. There is zero validation in the function to detect this. Furthermore, the dynamic harness CONFIRMED the fault with SIGSEGV, which provides strong evidence this is a real, triggerable bug.

From a security perspective: this is a public API function (no callers constrain it), `char_index` can be attacker-controlled (e.g., from font/text rendering paths accepting user input), and the out-of-bounds read could be escalated to an info-leak or used as part of a larger exploit chain.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
