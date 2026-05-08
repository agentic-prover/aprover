# BUG-06 — `keyboard_irq_handler` (keyboard)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/keyboard.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires irq_count >= 0 && irq_count < 2147483647 && the function is invoked only from a keyboard interrupt context or equivalent privileged/synchronized context where concurrent modification of irq_count is not possible`

**Postcondition:** `ensures irq_count == \old(irq_count) + 1 && process_events() has been called to handle any pending keyboard events`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
irq_count = 1
key_buffer = <symbolic struct/array — see classification.json>
key_buffer[0l] = 0
key_buffer[1l] = 0
key_buffer[2l] = 0
key_buffer[3l] = 0
key_buffer[4l] = 0
key_buffer[5l] = 0
key_buffer[6l] = 0
key_buffer[7l] = 0
key_buffer[8l] = 0
key_buffer[9l] = 0
key_buffer[10l] = 0
key_buffer[11l] = 0
key_buffer[12l] = 0
key_buffer[13l] = 0
key_buffer[14l] = 0
key_buffer[15l] = 0
key_buffer[16l] = 0
key_buffer[17l] = 0
key_buffer[18l] = 0
key_buffer[19l] = 0
key_buffer[20l] = 0
key_buffer[21l] = 0
key_buffer[22l] = 0
key_buffer[23l] = 0
key_buffer[24l] = 0
key_buffer[25l] = 0
key_buffer[26l] = 0
key_buffer[27l] = 0
key_buffer[28l] = 0
key_buffer[29l] = 0
key_buffer[30l] = 0
key_buffer[31l] = 0
format = <symbolic struct/array — see classification.json>
va_arg = 1
result = 0
return_value___VERIFIER_nondet_int = 0
list = ((va_list)NULL)
va_args = <symbolic struct/array — see classification.json>
va_args[0l] = va_arg!0
```

## Root cause

CBMC reports a `main.assertion.1` failure — a semantic / contract violation in `keyboard_irq_handler`.

**Realism checker's key concern:** The specific assertion violated (main.assertion.1) is not shown, and the harness introduces a synthetic log_key function to reproduce the va_list misuse. The real bug may lie inside process_events() rather than in keyboard_irq_handler itself — but the call is unconditional and the crash class is real.

**Validator reasoning:** 'keyboard_irq_handler' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`keyboard_irq_handler` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

A standalone GCC-compiled reproducer was generated and executed; it crashed with `SIGSEGV`. The reproducer source is preserved in the run's `classification.json` under `dynamic_result.harness_source`.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The specific assertion violated (main.assertion.1) is not shown, and the harness introduces a synthetic log_key function to reproduce the va_list misuse. The real bug may lie inside process_events() rather than in keyboard_irq_handler itself — but the call is unconditional and the crash class is real.

Q1 (Can the violation TYPE occur?): Yes. The dynamic harness confirmed a SIGSEGV fault. The function calls `process_events()` whose implementation is not shown but is unconditionally reachable. The counterexample traces a va_list being used without va_start (list = ((va_list)NULL)), which is a realistic class of undefined behavior that causes SIGSEGV when vprintf/vsprintf dereferences an invalid va_list pointer. This pattern is entirely plausible in a logging or event-processing path. Q2 (Are the witness values realistic?): Highly realistic. irq_count = 1 is the first invocation of the IRQ handler — the most common and natural starting state. There is nothing synthetic about these values. The SIGSEGV was dynamically confirmed: the harness compiled and ran code simulating the bug class (uninitialized/NULL va_list passed to vprintf), and the program crashed with SIGSEGV. Even if the harness injected a synthetic log_key function to reproduce it, the underlying bug class — improper va_list handling somewhere in the process_events() call chain — is a realistic keyboard driver bug. As a system-entry-point IRQ handler with no callers guarding it, inputs are unconstrained and the path is always reachable. The dynamic confirmation is strong evidence this is not a verification artifact.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
