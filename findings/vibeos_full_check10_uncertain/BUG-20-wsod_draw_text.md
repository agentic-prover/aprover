# BUG-20 — `wsod_draw_text` (irq)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/irq.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_string(s) && x >= 0 && y >= 0 && (fb_base != NULL) && fb_width > 0 && fb_height > 0 && x < fb_width && y < fb_height && (x + 8 * strlen(s)) does not overflow int`

**Postcondition:** `ensures each character in s is rendered to the framebuffer starting at pixel coordinates (x, y), with successive characters drawn 8 pixels to the right; the framebuffer memory is modified accordingly; no memory outside the framebuffer bounds is written; no return value is produced`

## Counterexample

**Violated property:** `wsod_draw_text.unwind.0`

**Key variable assignments:**
```
fb_base = <symbolic struct/array — see classification.json>
fb_height = 4286578689u
fb_width = 2147483623u
x = -2147483648
y = 268435456
_s_buf = <symbolic struct/array — see classification.json>
_s_len = 4u
_s_buf[4l] = 0
_s_buf[0l] = 4
_s_buf[1l] = 1
_s_buf[2l] = 2
_s_buf[3l] = 1
s = <symbolic struct/array — see classification.json>
```

## Root cause

CBMC reports a `wsod_draw_text.unwind.0` failure — a semantic / contract violation in `wsod_draw_text`.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`wsod_draw_text` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (— confidence)

Could not parse LLM response: ```json
{
  "verdict": "UNREALISTIC",
  "reasoning": "Q1 — Can the loop non-termination violation type occur? The `while (*s)` loop terminates whenever it reaches a null byte. For this to be a real in

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
