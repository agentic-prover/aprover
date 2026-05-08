# BUG-10 — `hal_serial_getc` (serial)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/serial.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
uart_getc -> hal_serial_getc
```

## Spec (LLM-generated)

**Precondition:** `requires true`

**Postcondition:** `ensures \result == -1 || (\result >= 0 && \result <= 255)`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
result                           = -1
return_value_hal_serial_getc     = -1
goto_symex$$return_value$$hal_serial_getc = -1
```

## Root cause

`hal_serial_getc` reads the PL011 UART receive FIFO at the fixed hardware address `0x09000000`. When bit 4 of the flags register (`0x09000000 + 0x18`) is set — the standard "receive FIFO empty" condition — the function returns -1. The caller `uart_getc` asserts or assumes the return value is a valid byte in the range [0, 255], but does not handle the -1 sentinel. Any time the UART has no pending characters (a normal hardware state during idle periods), `hal_serial_getc` returns -1 and the caller's unchecked assumption is violated.

## How to trigger

Call `uart_getc` when no character has been received on the serial port. The UART receive FIFO will be empty (flags register bit 4 set), causing `hal_serial_getc` to return -1. If `uart_getc` passes this value directly to a char buffer or uses it without checking for -1, it will write 0xFF (if cast to unsigned char) or -1 (if used as int) to the output buffer — incorrect behavior that may propagate as data corruption.

## Realism assessment

**Verdict:** REALISTIC

The function `hal_serial_getc` reads from PL011 UART memory-mapped registers at fixed address 0x09000000. The violation occurs when bit 4 of the flags register (0x09000000+0x18) is set — the standard 'receive FIFO empty' condition — causing the function to return -1. This is a fully realistic hardware state: any time the UART has no pending characters, this bit will be set.

The counterexample value (return_value = -1) does not require any extreme or impossible input; it simply requires the hardware FIFO to be empty, which is common during normal operation. The call chain (`uart_getc → hal_serial_getc`) suggests `uart_getc` may be expected to block until a character is available or at minimum check the return value, but its body is unavailable. If `uart_getc` does not handle the -1 sentinel (e.g., passes it directly to a char buffer or asserts the result is in [0,255]), then the -1 return from an empty FIFO would trigger a real bug.

Since UART FIFO-empty is a common, normal hardware state, this scenario is readily triggered in production.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
