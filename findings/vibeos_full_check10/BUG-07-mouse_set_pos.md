# BUG-07 — `mouse_set_pos` (mouse)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGABRT |
| **Module** | `kernel/mouse.c` |
| **Bug type** | arithmetic |
| **Violated property** | `mouse_set_pos.overflow.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires fb_width > 0 && fb_height > 0 && x >= 0 && x <= (int)fb_width && y >= 0 && y <= (int)fb_height`

**Postcondition:** `ensures mouse_x >= 0 && mouse_x <= 32767 && mouse_y >= 0 && mouse_y <= 32767`

## Counterexample

**Violated property:** `mouse_set_pos.overflow.1`

**Key variable assignments:**
```
fb_height = 4294967295u
fb_width = 4294967295u
mouse_x = 0
mouse_y = 0
x = 65536
y = 65536
```

## Root cause / validation reasoning

'mouse_set_pos' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGABRT`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGABRT

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The fb_width=UINT_MAX in the counterexample is unrealistic, but this is irrelevant because the signed integer overflow occurs in `x * 32768` before any use of fb_width. Any x >= 65536 triggers the overflow regardless of fb_width.

Q1 — Can the violation TYPE (signed integer overflow) occur? YES. The expression `x * 32768` uses signed int arithmetic. Since 32768 = 2^15, any `x >= 65536` causes `x * 32768 >= 2^31`, which overflows INT_MAX (2,147,483,647). Similarly, `x <= -65537` would underflow. This overflow is a straightforward arithmetic property of the computation — it does not depend on `fb_width` or `fb_height` at all. The function has no bounds check on `x` or `y` before the multiplication. As a public API entry point with no callers in the codebase, inputs are completely unconstrained. Any caller passing a pixel coordinate from a large display (e.g., x=65536 is plausible for a 4K/8K display or virtual coordinate space) or an attacker-controlled value triggers UB.

Q2 — Are the specific witness values achievable? The value `fb_width = UINT_MAX` is unrealistic for a real framebuffer. However, this is irrelevant: the overflow occurs in `x * 32768` BEFORE the division by fb_width. The overflow only requires `x = 65536`, which is entirely plausible (e.g., a 65536-pixel-wide virtual display, or attacker input). The dynamic harness confirmed a fault (SIGABRT). Even if the specific fb_width witness is a CBMC artifact, the underlying overflow class is triggerable by ordinary inputs (x >= 65536).

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
