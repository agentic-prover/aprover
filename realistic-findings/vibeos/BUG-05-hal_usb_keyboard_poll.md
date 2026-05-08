# BUG-05 — `hal_usb_keyboard_poll` (usb_hid)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/usb_hid.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no callers)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(report, 0, report_len) && report_len > 0`

**Postcondition:** `ensures (\result == -1) || (\result == 0) || (\result >= 1 && \result <= 8 && \result <= report_len && valid_range(report, 0, \result)); ensures \result == -1 ==> (USB subsystem not initialized, no device connected, or no keyboard address assigned); ensures \result == 0 ==> no keyboard report was available in the ring buffer; ensures \result > 0 ==> report[0..\result) has been filled with keyboard HID report data`

## Counterexample

**Violated property:** `hal_usb_keyboard_poll.precondition_instance.3`

**Key variable assignments:**
```
usb_state.initialized    = 131072  (non-zero, passes init check)
usb_state.device_connected = 8192  (non-zero, device present)
usb_state.keyboard_addr  = 65536   (non-zero, passes keyboard check)
report                   = NULL (0)
report_len               = 22
kbd_ring_pop return value = 32 (ring has data)
len                      = 8
```

## Root cause

`hal_usb_keyboard_poll` checks for USB initialization and keyboard address before polling the ring buffer, but never validates that the `report` output pointer is non-NULL. When the ring buffer has data (`kbd_ring_pop` returns > 0), the function calls `memcpy(report, ring_report, len)` with a NULL destination. The dynamic harness confirmed SIGSEGV. The function is a public HAL entry point with no visible callers, so the NULL guard cannot be assumed from call sites.

## How to trigger

In a VibeOS environment with a USB keyboard connected (`usb_state.keyboard_addr` set, `usb_state.initialized` and `usb_state.device_connected` non-zero), call `hal_usb_keyboard_poll(NULL, 22)`. The USB state guards pass, the ring buffer is popped, and `memcpy` is called with a NULL destination pointer, triggering SIGSEGV.

## Realism assessment

**Verdict:** REALISTIC

1. **Function role**: `hal_usb_keyboard_poll` is a HAL public API entry point with no call sites found in the codebase — inputs are entirely unconstrained. Any caller (OS, application, firmware) can pass arbitrary parameters.

2. **The crash path**: The counterexample shows `report == NULL` while `kbd_ring_pop` returns a non-zero value (32 in the stub), causing the code to enter the branch and execute `memcpy(report, ring_report, len)` with a NULL destination. There is no NULL check on `report` anywhere in the function body before this `memcpy`.

3. **Dynamic confirmation**: The dynamic harness independently triggered SIGSEGV (signal=11), confirming the crash is a real memory fault, not a theoretical artifact. This rules out a false positive.

4. **Realistic scenario**: A caller could reasonably pass `report = NULL` with a non-zero `report_len` to probe whether keyboard data is available (polling pattern), expecting the function to guard against NULL. This is especially plausible in embedded/HAL contexts where API contracts are not always enforced by callers. The state values `usb_state.initialized = 131072`, `usb_state.device_connected = 8192`, and `usb_state.keyboard_addr = 65536` are all plausible (non-zero, flag-style values) that would pass the early guards, leading execution to the vulnerable memcpy.

5. **No mitigating guards**: The function has three early-return checks for USB state, but none check whether `report` itself is valid before using it in `memcpy`.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
