# BUG-16 — `hal_usb_mouse_poll` (usb_hid)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/usb_hid.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(report, 0, report_len) && report_len > 0`

**Postcondition:** `ensures (esult == -1 || esult == 0 || (esult > 0 && esult <= 8 && esult <= report_len)) && (esult > 0 ==> valid_range(report, 0, esult) && "report[0..esult) contains mouse report data copied from internal ring buffer")`

## Counterexample

**Violated property:** `hal_usb_mouse_poll.precondition_instance.3`

**Key variable assignments:**
```
usb_state.initialized = 1
usb_state.num_channels = 0
usb_state.device_connected = 134217728
usb_state.device_speed = 0
usb_state.next_address = 0
usb_state.data_toggle = <symbolic struct/array — see classification.json>
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
usb_state.devices = <symbolic struct/array — see classification.json>
usb_state.devices[0l] = <symbolic struct/array — see classification.json>
usb_state.devices[0l].address = 0
usb_state.devices[0l].speed = 0
usb_state.devices[0l].max_packet_size = 0
usb_state.devices[0l].is_hub = 0
usb_state.devices[0l].hub_ports = 0
usb_state.devices[0l].parent_hub = 0
usb_state.devices[0l].parent_port = 0
usb_state.devices[1l] = <symbolic struct/array — see classification.json>
usb_state.devices[1l].address = 0
usb_state.devices[1l].speed = 0
usb_state.devices[1l].max_packet_size = 0
usb_state.devices[1l].is_hub = 0
usb_state.devices[1l].hub_ports = 0
usb_state.devices[1l].parent_hub = 0
usb_state.devices[1l].parent_port = 0
usb_state.devices[2l] = <symbolic struct/array — see classification.json>
usb_state.devices[2l].address = 0
usb_state.devices[2l].speed = 0
usb_state.devices[2l].max_packet_size = 0
usb_state.devices[2l].is_hub = 0
usb_state.devices[2l].hub_ports = 0
usb_state.devices[2l].parent_hub = 0
usb_state.devices[2l].parent_port = 0
usb_state.devices[3l] = <symbolic struct/array — see classification.json>
usb_state.devices[3l].address = 0
usb_state.devices[3l].speed = 0
usb_state.devices[3l].max_packet_size = 0
usb_state.devices[3l].is_hub = 0
usb_state.devices[3l].hub_ports = 0
usb_state.devices[3l].parent_hub = 0
usb_state.devices[3l].parent_port = 0
usb_state.devices[4l] = <symbolic struct/array — see classification.json>
usb_state.devices[4l].address = 0
usb_state.devices[4l].speed = 0
usb_state.devices[4l].max_packet_size = 0
usb_state.devices[4l].is_hub = 0
usb_state.devices[4l].hub_ports = 0
usb_state.devices[4l].parent_hub = 0
usb_state.devices[4l].parent_port = 0
usb_state.devices[5l] = <symbolic struct/array — see classification.json>
usb_state.devices[5l].address = 0
usb_state.devices[5l].speed = 0
usb_state.devices[5l].max_packet_size = 0
usb_state.devices[5l].is_hub = 0
usb_state.devices[5l].hub_ports = 0
usb_state.devices[5l].parent_hub = 0
usb_state.devices[5l].parent_port = 0
usb_state.devices[6l] = <symbolic struct/array — see classification.json>
usb_state.devices[6l].address = 0
usb_state.devices[6l].speed = 0
usb_state.devices[6l].max_packet_size = 0
usb_state.devices[6l].is_hub = 0
usb_state.devices[6l].hub_ports = 0
usb_state.devices[6l].parent_hub = 0
usb_state.devices[6l].parent_port = 0
usb_state.devices[7l] = <symbolic struct/array — see classification.json>
usb_state.devices[7l].address = 0
usb_state.devices[7l].speed = 0
usb_state.devices[7l].max_packet_size = 0
usb_state.devices[7l].is_hub = 0
usb_state.devices[7l].hub_ports = 0
usb_state.devices[7l].parent_hub = 0
usb_state.devices[7l].parent_port = 0
usb_state.num_devices = 0
usb_state.keyboard_addr = 0
usb_state.keyboard_ep = 0
usb_state.keyboard_mps = 0
usb_state.keyboard_interval = 0
usb_state.mouse_addr = 33554432
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
return_value_hal_usb_mouse_poll = 0
ring_report = <symbolic struct/array — see classification.json>
return_value_mouse_ring_pop_stub = 1073741824
goto_symex$$return_value$$mouse_ring_pop_stub = 1073741824
len = 7
```

## Root cause

CBMC reports a `hal_usb_mouse_poll.precondition_instance.3` failure — a semantic / contract violation in `hal_usb_mouse_poll`.

**Realism checker's key concern:** The `report` pointer parameter is never validated before being passed to `memcpy`. A NULL or invalid pointer passed by any caller (including an adversary) will cause a SIGSEGV when `mouse_ring_pop` returns non-zero. The dynamic harness confirmed this crash.

**Validator reasoning:** 'hal_usb_mouse_poll' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`hal_usb_mouse_poll` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

A standalone GCC-compiled reproducer was generated and executed; it crashed with `SIGSEGV`. The reproducer source is preserved in the run's `classification.json` under `dynamic_result.harness_source`.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The `report` pointer parameter is never validated before being passed to `memcpy`. A NULL or invalid pointer passed by any caller (including an adversary) will cause a SIGSEGV when `mouse_ring_pop` returns non-zero. The dynamic harness confirmed this crash.

Q1 (Can the violation TYPE occur?): Yes. The function `hal_usb_mouse_poll` receives a `uint8_t* report` pointer from the caller and uses it directly in `memcpy(report, ring_report, len)` without any NULL or validity check. If `report` is NULL (or invalid) and the code path reaches the `memcpy` call (i.e., `mouse_ring_pop` returns a truthy value), this will cause a NULL/invalid pointer dereference — a classic memory safety violation. The function is a public API entry point with no callers in the codebase, meaning inputs are entirely unconstrained and attacker-controllable. Q2 (Is this specific witness realistic?): Yes. The counterexample shows `_report_val = 0` consistent with a NULL report pointer. More importantly, the dynamic harness independently confirmed a SIGSEGV, meaning the actual compiled code crashed with the given inputs. The `report` parameter has no guard check in the function body, `usb_state.mouse_addr = 33554432` (non-zero, passing the address check), `usb_state.initialized = 1` and `usb_state.device_connected = 134217728` (non-zero, truthy), and `mouse_ring_pop` returning non-zero causes execution to reach `memcpy`. All preconditions to trigger the crash with a NULL `report` are realistically satisfiable. The absence of any NULL check on `report` before the `memcpy` is the confirmed vulnerability.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
