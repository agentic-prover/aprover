# BUG-20 — `wsod_draw_text` (irq)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/irq.c` |
| **Bug type** | semantic |
| **Violated property** | `wsod_draw_text.unwind.0` |
| **Realism** | uncertain (— confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_string(s) && x >= 0 && y >= 0 && (fb_base != NULL) && fb_width > 0 && fb_height > 0 && x < fb_width && y < fb_height && (x + 8 * strlen(s)) does not overflow int`

**Postcondition:** `ensures each character in s is rendered to the framebuffer starting at pixel coordinates (x, y), with successive characters drawn 8 pixels to the right; the framebuffer memory is modified accordingly; no memory outside the framebuffer bounds is written; no return value is produced`

## Counterexample

**Violated property:** `wsod_draw_text.unwind.0`

**Key variable assignments:**
```
fb_base = {'name': 'pointer', 'type': 'uint32_t *'}
fb_height = 4286578689u
fb_width = 2147483623u
x = -2147483648
y = 268435456
_s_buf = {'elements': [{'index': 0, 'value': {'binary': '00000100', 'data': '4', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '00000001', 'data': '1', 'name': 'integer',...
_s_len = 4u
_s_buf[4l] = 0
_s_buf[0l] = 4
_s_buf[1l] = 1
_s_buf[2l] = 2
_s_buf[3l] = 1
s = {'name': 'unknown'}
```

## Root cause / validation reasoning

Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

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
