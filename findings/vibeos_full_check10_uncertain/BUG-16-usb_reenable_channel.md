# BUG-16 — `usb_reenable_channel` (usb_transfer)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/usb_transfer.c` |
| **Bug type** | arithmetic |
| **Violated property** | `usb_reenable_channel.overflow.3` |
| **Realism** | uncertain (— confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kernel_main → hal_usb_init → usb_enumerate_device → usb_enumerate_device_at → usb_get_device_descriptor → usb_control_transfer → usb_wait_for_dma_complete → usb_reenable_channel

## Spec (LLM-generated)

**Precondition:** `requires ch >= 0 && ch < 16 && the memory-mapped USB host channel registers at address (0x3F980000 + 0x500 + ch * 0x20) are valid and accessible (HCCHAR register), and the USB host configuration register at (0x3F980000 + 0x408) is valid and accessible (HCFG register for odd-frame detection); no integer overflow occurs in the address computation since ch is bounded to [0,15] and ch*0x20 <= 0x1E0`

**Postcondition:** `ensures the USB host channel ch's HCCHAR register has been written with: the enable bit (bit 31) set, the disable bit (bit 30) cleared, and the odd-frame bit (bit 29) set to match the current frame parity from HCFG (bit 0 of register at 0x3F980000+0x408); a data synchronization barrier (dsb) has been issued after the register write; the channel is re-enabled and ready to perform another transfer attempt; memory safety is preserved (no out-of-bounds access, no undefined behaviour)`

## Counterexample

**Violated property:** `usb_reenable_channel.overflow.3`

**Key variable assignments:**
```
ch = 1
hcchar = 536870912u
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['usb_wait_for_dma_complete']. Call chain: ['kernel_main', 'hal_usb_init', 'usb_enumerate_device', 'usb_enumerate_device_at', 'usb_get_device_descriptor', 'usb_control_transfer', 'usb_wait_for_dma_complete', 'usb_reenable_channel']. Full chain traced to system entry.

## Realism assessment

**Verdict:** UNCERTAIN (— confidence)

Could not parse LLM response: ## Analysis

### Q1: Can the violation TYPE occur in the real program?

The function `usb_reenable_channel` contains several potential overflow sites:

1. **`(ch) * 0x20` — address arithmetic with `ch

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
