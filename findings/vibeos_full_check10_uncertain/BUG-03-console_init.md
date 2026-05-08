# BUG-03 — `console_init` (console)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/console.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

```
kernel_main -> console_init
```

## Spec (LLM-generated)

**Precondition:** `requires (fb_base == ((void *)0)) || (fb_width > 0 && fb_height > 0 && fb_width % 8 == 0 && fb_height % 16 == 0)`

**Postcondition:** `ensures (fb_base == ((void *)0)) || (console_initialized == 1 && cursor_row == 0 && cursor_col == 0 && scroll_offset == 0 && num_cols == fb_width / 8 && num_rows == fb_height / 16)`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
console_initialized = 0
cursor_col = 0
cursor_row = 0
fb_base = ((uint32_t *)NULL)
fb_height = 16u
fb_width = 8u
hw_scroll_available = 0
num_cols = 0
num_rows = 0
scroll_offset = 0u
virtual_height = 0u
```

## Root cause

CBMC reports a `main.assertion.1` failure — a semantic / contract violation in `console_init`.

**Realism checker's key concern:** The specific CBMC witness has `fb_base=NULL` triggering early return, which is a symbolic initial-state artifact. However, the real concern is that no lower-bound check exists on `fb_height` or `fb_width` before division; a hardware-supplied height < 16 with valid `fb_base` yields `num_rows=0`, causing unsigned underflow (`num_rows-1`) in `newline` and potential out-of-bounds framebuffer writes.

**Validator reasoning:** Cross-file caller 'kernel_main' can reach the CEx state. Call chain: ['kernel_main', 'console_init']. Full chain traced to system entry.

## How to trigger

Reach `console_init` via the call chain `kernel_main → console_init` and supply inputs that match the counterexample variable assignments above.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific CBMC witness has `fb_base=NULL` triggering early return, which is a symbolic initial-state artifact. However, the real concern is that no lower-bound check exists on `fb_height` or `fb_width` before division; a hardware-supplied height < 16 with valid `fb_base` yields `num_rows=0`, causing unsigned underflow (`num_rows-1`) in `newline` and potential out-of-bounds framebuffer writes.

Q1 (Can the violation TYPE occur?): The function computes `num_rows = fb_height / 16`. If `fb_height` is between 0 and 15 and `fb_base != NULL`, then `num_rows` is set to 0. The global variable context reveals that `newline` uses `cursor_row = num_rows - 1`, and if `num_rows` is an unsigned type equal to 0, this silently wraps to a very large value (e.g., 0xFFFFFFFF), producing an out-of-bounds cursor that would be used in framebuffer write operations — a realistic memory safety concern in an embedded/kernel driver context. Similarly, `num_cols = 0` from `fb_width < 8` could cause issues. So the underlying vulnerability class (integer underflow / unchecked division yielding zero) IS real. Q2 (Is this specific witness realistic?): The counterexample has `fb_base = NULL`, causing an early return before `num_cols`/`num_rows` are updated. So in this particular witness, `num_rows` stays at 0 from some prior state rather than being set to 0 by the division. The specific path (`fb_base = NULL` → early return → assertion fails on pre-existing state) may be a CBMC artifact reflecting symbolic initial state rather than a real execution path. In real execution, either `console_init` was never called (so `console_initialized=0` is expected) or the prior state would be meaningful. The assertion being checked (`main.assertion.1`) is not visible in the function body, suggesting it's a harness-level property about postconditions. The specific witness is likely an artifact, but the underlying vulnerability — `num_rows=0` from small `fb_height` when `fb_base != NULL`, leading to underflow in callers — is a real concern in this driver.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
