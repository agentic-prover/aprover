# BUG-29 — `hal_usb_keyboard_poll` (usb_hid)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | not_triggered |
| **Module** | `kernel/usb_hid.c` |
| **Bug type** | semantic |
| **Violated property** | `hal_usb_keyboard_poll.precondition_instance.3` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid_range(report, 0, report_len) && report_len > 0`

**Postcondition:** `(esult == -1 || esult == 0 || (esult > 0 && esult <= 8 && esult <= report_len)) && (esult > 0 ==> valid_range(report, 0, esult) && 'report[0..esult) contains a valid HID keyboard report copied from the internal ring buffer')`

## Counterexample

**Violated property:** `hal_usb_keyboard_poll.precondition_instance.3`

**Key variable assignments:**
```
usb_state.initialized = 1
usb_state.num_channels = 0
usb_state.device_connected = 134217728
usb_state.device_speed = 0
usb_state.next_address = 0
usb_state.data_toggle = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
usb_state.data_toggle[0l] = 0
usb_state.data_toggle[1l] = 0
usb_state.data_toggle[2l] = 0
usb_state.data_toggle[3l] = 0
usb_state.data_toggle[4l] = 0
usb_state.data_toggle[5l] = 0
usb_state.data_toggle[6l] = 0
usb_state.data_toggle[7l] = 0
usb_state.data_toggle[8l] = 0
usb_state.data_toggle[9l] = 0
usb_state.data_toggle[10l] = 0
usb_state.data_toggle[11l] = 0
usb_state.data_toggle[12l] = 0
usb_state.data_toggle[13l] = 0
usb_state.data_toggle[14l] = 0
usb_state.data_toggle[15l] = 0
usb_state.devices = {'elements': [{'index': 0, 'value': {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'n...
usb_state.devices[0l] = {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'speed', 'value': {'binary': '...
usb_state.devices[0l].address = 0
usb_state.devices[0l].speed = 0
usb_state.devices[0l].max_packet_size = 0
usb_state.devices[0l].is_hub = 0
usb_state.devices[0l].hub_ports = 0
usb_state.devices[0l].parent_hub = 0
usb_state.devices[0l].parent_port = 0
usb_state.devices[1l] = {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'speed', 'value': {'binary': '...
usb_state.devices[1l].address = 0
usb_state.devices[1l].speed = 0
usb_state.devices[1l].max_packet_size = 0
usb_state.devices[1l].is_hub = 0
usb_state.devices[1l].hub_ports = 0
usb_state.devices[1l].parent_hub = 0
usb_state.devices[1l].parent_port = 0
usb_state.devices[2l] = {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'speed', 'value': {'binary': '...
usb_state.devices[2l].address = 0
usb_state.devices[2l].speed = 0
usb_state.devices[2l].max_packet_size = 0
usb_state.devices[2l].is_hub = 0
usb_state.devices[2l].hub_ports = 0
usb_state.devices[2l].parent_hub = 0
usb_state.devices[2l].parent_port = 0
usb_state.devices[3l] = {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'speed', 'value': {'binary': '...
usb_state.devices[3l].address = 0
usb_state.devices[3l].speed = 0
usb_state.devices[3l].max_packet_size = 0
usb_state.devices[3l].is_hub = 0
usb_state.devices[3l].hub_ports = 0
usb_state.devices[3l].parent_hub = 0
usb_state.devices[3l].parent_port = 0
usb_state.devices[4l] = {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'speed', 'value': {'binary': '...
usb_state.devices[4l].address = 0
usb_state.devices[4l].speed = 0
usb_state.devices[4l].max_packet_size = 0
usb_state.devices[4l].is_hub = 0
usb_state.devices[4l].hub_ports = 0
usb_state.devices[4l].parent_hub = 0
usb_state.devices[4l].parent_port = 0
usb_state.devices[5l] = {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'speed', 'value': {'binary': '...
usb_state.devices[5l].address = 0
usb_state.devices[5l].speed = 0
usb_state.devices[5l].max_packet_size = 0
usb_state.devices[5l].is_hub = 0
usb_state.devices[5l].hub_ports = 0
usb_state.devices[5l].parent_hub = 0
usb_state.devices[5l].parent_port = 0
usb_state.devices[6l] = {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'speed', 'value': {'binary': '...
usb_state.devices[6l].address = 0
usb_state.devices[6l].speed = 0
usb_state.devices[6l].max_packet_size = 0
usb_state.devices[6l].is_hub = 0
usb_state.devices[6l].hub_ports = 0
usb_state.devices[6l].parent_hub = 0
usb_state.devices[6l].parent_port = 0
usb_state.devices[7l] = {'members': [{'name': 'address', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'speed', 'value': {'binary': '...
usb_state.devices[7l].address = 0
usb_state.devices[7l].speed = 0
usb_state.devices[7l].max_packet_size = 0
usb_state.devices[7l].is_hub = 0
usb_state.devices[7l].hub_ports = 0
usb_state.devices[7l].parent_hub = 0
usb_state.devices[7l].parent_port = 0
usb_state.num_devices = 0
usb_state.keyboard_addr = 64
usb_state.keyboard_ep = 0
usb_state.keyboard_mps = 0
usb_state.keyboard_interval = 0
usb_state.mouse_addr = 0
usb_state.mouse_ep = 0
usb_state.mouse_mps = 0
usb_state.mouse_interval = 0
usb_state.enum_parent_hub = 0
usb_state.enum_parent_port = 0
usb_state.enum_speed = 0
_report_val = 0
report = ring_report!0@1
report_len = 7
result = 1073741824
return_value_hal_usb_keyboard_poll = 0
ring_report = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
return_value_kbd_ring_pop_stub = 1073741824
goto_symex$$return_value$$kbd_ring_pop_stub = 1073741824
len = 7
```

## Root cause / validation reasoning

'hal_usb_keyboard_poll' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

Dynamic harness outcome: `not_triggered`. Dynamic harness ran to completion without triggering a fault.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The specific CBMC witness uses impossible pointer aliasing and an extreme stub return value, but the real bug class — unchecked `report` pointer (null dereference) and unchecked negative `report_len` (sign-to-size_t confusion in memcpy causing buffer overflow) — is genuine and exploitable from any external caller of this public API.

Q1 — Can the violation TYPE occur in practice?

Yes. The function `hal_usb_keyboard_poll` is a public API entry point with no callers in the codebase, meaning its parameters are entirely unconstrained. The function performs `memcpy(report, ring_report, len)` where:
1. `report` is never null-checked before use — if a caller passes NULL, this is an immediate null dereference.
2. `len = (report_len < 8) ? report_len : 8` — if `report_len` is negative, `len` inherits the negative value, and `memcpy` receives a negative `int` implicitly converted to a huge `size_t`, triggering a massive out-of-bounds write (buffer overflow).
3. There is no check that `report` actually points to a buffer of at least `len` bytes — even with a valid non-NULL pointer, a too-small buffer causes an overflow.

These are all real vulnerability classes (null dereference, integer-sign-to-size confusion causing buffer overflow) reachable with inputs from any caller.

Q2 — Are the specific counterexample values realistic?

The specific witness has `report = ring_report!0@1` which is a CBMC aliasing artifact (self-aliasing the local stack array), and `return_value_kbd_ring_pop_stub = 1073741824` which is an extreme stub value. These exact values are not realistically achievable. However, the underlying bug class — passing a NULL or too-small `report`, or a negative `report_len` — is fully reachable via any external caller. The dynamic harness did not trigger a fault, but that is because the harness likely used a valid buffer with a safe `report_len`.

The precondition violation (precondition_instance.3) is almost certainly `valid_range(report, 0, report_len)` — which fails if `report` is NULL, `report_len` is negative, or the buffer is too small. All three scenarios are realistic from an attacker-controlled input.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
