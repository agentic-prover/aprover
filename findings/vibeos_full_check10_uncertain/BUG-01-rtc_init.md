# BUG-01 — `rtc_init` (rtc)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/rtc.c` |
| **Bug type** | memory_safety |
| **Violated property** | `rtc_init.pointer_dereference.1` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires the RTC hardware registers at base address 0x09010000 are memory-mapped and accessible; requires this function is called before any other RTC operations; requires the system has sufficient privileges to access hardware registers at 0x09010000`

**Postcondition:** `ensures the RTC hardware has been initialized (control register at offset 0x00C set to 1); ensures the RTC is ready for subsequent timestamp reads; ensures an initialization message with the current timestamp has been printed to stdout`

## Counterexample

**Violated property:** `rtc_init.pointer_dereference.1`

**Key variable assignments:**
```
(none reported)
```

## Root cause / validation reasoning

'rtc_init' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGSEGV`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGSEGV

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The hardcoded MMIO addresses are architecturally valid only on the target embedded SoC. Running the function on x86 Linux (as the dynamic harness did) causes SIGSEGV due to unmapped memory—this is an environment mismatch, not an attacker-exploitable bug. Since the function takes no parameters and uses no external data, there is no attack surface.

Q1 (Can the violation TYPE occur?): The function dereferences two hardcoded MMIO addresses (0x0901000C and 0x09010000). These are compile-time constants—no runtime input influences them. The violation type (invalid pointer dereference) CAN occur when the code runs outside its intended embedded hardware environment, where those physical addresses are not memory-mapped. The dynamic harness confirmed this with SIGSEGV on an x86 Linux host. However, on the target embedded platform, these addresses correspond to actual hardware registers and the dereferences are architecturally valid. Q2 (Is the specific witness realistic?): The CBMC counterexample reflects the tool's abstract memory model, not actual inputs—there are no inputs to this function at all. The dynamic SIGSEGV is real but is an environment mismatch: the code is designed for a specific SoC with MMIO mapped at 0x09010000, but was executed on x86 Linux where that region is unmapped. From a security perspective, there are zero attacker-controlled inputs—the addresses, the cast, the offsets are all constants baked at compile time. An attacker cannot influence the execution path or the addresses accessed. The 'vulnerability' is purely an environment portability issue, not a security-exploitable memory safety bug. The finding is real in the wrong environment but irrelevant under the embedded deployment model it was designed for.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
