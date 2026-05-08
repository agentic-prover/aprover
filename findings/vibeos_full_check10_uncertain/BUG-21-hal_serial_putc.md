# BUG-21 — `hal_serial_putc` (serial)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/serial.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires true`

**Postcondition:** `ensures the character c has been transmitted to the serial hardware FIFO at base address 0x09000000; the function does not return until the transmit FIFO has space and c has been written to the data register`

## Counterexample

**Violated property:** `hal_serial_putc.unwind.0`

**Key variable assignments:**
```
c = 0
```

## Root cause

CBMC reports a `hal_serial_putc.unwind.0` failure — a semantic / contract violation in `hal_serial_putc`.

**Realism checker's key concern:** The loop termination depends on hardware register state (not attacker-controlled input), so while a genuine infinite-loop is possible under hardware malfunction, it is not directly exploitable as a security vulnerability. The function also has no call sites (dead code), further limiting real exposure.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`hal_serial_putc` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The loop termination depends on hardware register state (not attacker-controlled input), so while a genuine infinite-loop is possible under hardware malfunction, it is not directly exploitable as a security vulnerability. The function also has no call sites (dead code), further limiting real exposure.

Q1 (Can the violation TYPE occur?): The violation is a loop-unwinding bound (*.unwind.*) on a hardware busy-wait polling loop. The loop reads a memory-mapped UART status register at 0x09000018 and spins while bit 5 (TX full/busy) is set. In real embedded systems, this loop can genuinely fail to terminate if: (a) the UART peripheral is not properly initialized, (b) the UART clock is not running, (c) there is no hardware at that address and reads return a stuck value, or (d) the TX FIFO is permanently full due to a hardware fault. So the violation TYPE (non-termination/infinite loop) is possible in real execution — not just a CBMC artifact. Q2 (Are the specific witness values realistic?): The counterexample assigns c=0, which is a perfectly valid character. The loop termination does not depend on c at all — it depends entirely on the hardware state of the volatile register. The CBMC witness simply exposed CBMC's inability to prove termination under its unwind bound, but the underlying scenario of a stuck TX busy bit is real. KEY CONCERN: From a security standpoint, the attacker cannot directly control the hardware register value, so this is not a traditional exploitable vulnerability (e.g., buffer overflow, null dereference). The risk is a denial-of-service via infinite loop if the hardware is in a bad state when the function is called. Additionally, the function is marked as dead code with no call sites, reducing real-world exposure. The verdict is UNCERTAIN because the infinite-loop risk is real under hardware-failure conditions but is not directly attacker-controllable via input.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
