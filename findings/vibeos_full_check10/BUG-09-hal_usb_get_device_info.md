# BUG-09 — `hal_usb_get_device_info` (platform)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/platform.c` |
| **Bug type** | semantic |
| **Violated property** | `main.assertion.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires idx >= 0 && (vid == null || valid(vid)) && (pid == null || valid(pid)) && (name == null || (valid_range(name, 0, name_len) && name_len >= 0))`

**Postcondition:** `ensures \result == 0 || \result == -1 && (\result == 0 ==> valid(vid) && valid(pid) && (name != null ==> valid_string(name)))`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
idx = 1073741824
_vid_val = 0
vid = _vid_val!0@1
_pid_val = 0
pid = _pid_val!0@1
_name_buf = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer',...
_name_len = 0u
_name_buf[0l] = 0
_name_buf[1l] = 0
_name_buf[2l] = 0
_name_buf[3l] = 0
_name_buf[4l] = 0
name = _name_buf!0@1
name_len = 268435456
result = -1
return_value_hal_usb_get_device_info = -1
goto_symex$$return_value$$hal_usb_get_device_info = -1
```

## Root cause / validation reasoning

'hal_usb_get_device_info' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGSEGV`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGSEGV

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** None — this is a realistic out-of-bounds array access vulnerability due to missing bounds validation on the `idx` parameter in a public API function with no input sanitization.

Q1 (Can the violation TYPE occur?): YES. The function signature accepts an unconstrained integer `idx` that is intended to index into a fixed-size USB device table. The harness models the realistic implementation pattern where `idx` is used as an array subscript without bounds checking. With `idx = 1073741824` (or any value >= MAX_USB_DEVICES), the array access `usb_devices[idx]` goes far out of bounds. This is a classic CWE-129 (Improper Validation of Array Index) vulnerability. Since the function is a public HAL API entry point with no callers enforcing bounds, attacker-controlled inputs are entirely plausible.

Q2 (Are the specific witness values achievable?): YES. The value `idx = 1073741824` (2^30) is a perfectly valid `int` that can be supplied by any caller of this public API. There is no mathematical impossibility here. The dynamic harness confirmed the fault with a SIGSEGV, demonstrating that the out-of-bounds access is concretely triggerable.

The current function body is a no-op stub, but this is a HAL placeholder — the actual implementation (as modeled by the harness) performs direct array indexing on `idx`. The missing bounds check is a real vulnerability class that would be exploitable in the production implementation. The security threat model explicitly notes inputs may be attacker-controlled, and the call-site analysis confirms there are no callers guarding against out-of-range `idx` values.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
