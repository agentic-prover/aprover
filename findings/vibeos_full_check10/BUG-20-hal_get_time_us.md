# BUG-20 — `hal_get_time_us` (platform)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/platform.c` |
| **Bug type** | arithmetic |
| **Violated property** | `hal_get_time_us.overflow.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kernel_main → hal_usb_init → usb_core_reset → usleep → hal_get_time_us

## Spec (LLM-generated)

**Precondition:** `requires true`

**Postcondition:** `ensures \result >= 0 && the hardware counter registers cntpct_el0 and cntfrq_el0 are accessible (i.e., the caller is running at a privilege level that permits reading these ARM system registers) && freq != 0 (cntfrq_el0 is non-zero, as required by the ARM architecture)`

## Counterexample

**Violated property:** `hal_get_time_us.overflow.1`

**Key variable assignments:**
```
result = 0u
return_value_hal_get_time_us = 0u
cnt = 288230376151711744ul
freq = 0ul
```

## Root cause / validation reasoning

Cross-file caller 'usleep' can reach the CEx state. Call chain: ['kernel_main', 'hal_usb_init', 'usb_core_reset', 'usleep', 'hal_get_time_us']. Full chain traced to system entry.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** No guard against freq==0 before the division; on uninitialized or emulated ARM hardware cntfrq_el0 returns 0, triggering division by zero in a security-critical early-boot code path.

Q1 — Can a division-by-zero occur? Yes. The function reads `cntfrq_el0` (the ARM generic timer frequency register) directly via inline assembly and uses it as the divisor with no guard. If `freq` is 0, execution triggers division by zero and undefined behaviour. On real ARM bare-metal systems this register is written by firmware/bootloader at EL3 or EL2 before the OS starts. If the bootloader doesn't initialise it, or if the code runs under an emulator/QEMU that doesn't implement the register (which often returns 0 for unimplemented system registers), `freq` will be 0. Given this is kernel-level HAL code (`kernel_main` entry point), running without a fully-featured bootloader or under emulation is a common deployment scenario. Q2 — Are the specific witness values achievable? `cnt = 2^58` is a plausible 64-bit hardware counter reading. `freq = 0` is achievable if the timer register was never written or if the architecture returns RAZ (read-as-zero) for unimplemented registers — exactly what QEMU does for unrecognised system registers. The dynamic harness confirms the fault path: when `mock_freq = 0`, the division-by-zero is triggered. No call-site analysis shows all callers guard against a zero frequency. The violation type (division by zero on a hardware-derived divisor) is entirely realistic and exploitable in the sense that it causes a crash/denial-of-service during USB initialisation on boot.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
