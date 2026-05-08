# BUG-07 — `keyboard_getc` (keyboard)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/keyboard.c` |
| **Bug type** | semantic |
| **Violated property** | `main.assertion.1` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kernel_main → shell_run → keyboard_getc

## Spec (LLM-generated)

**Precondition:** `requires true`

**Postcondition:** `ensures \result == -1 || (\result >= 0 && \result <= 255)`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
irq_count = 0
kbd_base = ((volatile uint32_t *)NULL)
key_buf_read = 0
key_buf_write = 0
key_buffer = {'elements': [{'index': 0, 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'index': 1, 'value': {'binary': '00000000000...
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
result = -2147483648
return_value_keyboard_getc = -2147483648
return_value_hal_keyboard_getc = -2147483648
goto_symex$$return_value$$keyboard_getc = -2147483648
```

## Root cause / validation reasoning

Cross-file caller 'shell_run' can reach the CEx state. Call chain: ['kernel_main', 'shell_run', 'keyboard_getc']. Full chain traced to system entry.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific INT_MIN return from hal_keyboard_getc() is a CBMC symbolic artifact. However, whether hal_keyboard_getc() dereferences kbd_base (NULL) internally is unknown without its body — if it does, the bug is real; if not, this is a false positive about return-value range.

Q1 (Can the violation TYPE occur?): Yes. The global context confirms that `kbd_base` is set by `find_virtio_input()` in `keyboard_init`. If no virtio input device is found, or if `keyboard_init` is not called before `keyboard_getc`, `kbd_base` remains NULL. When `kbd_base=NULL` and the buffer is empty (`key_buf_read == key_buf_write`), the code falls through to `hal_keyboard_getc()`. If `hal_keyboard_getc()` internally accesses hardware registers through `kbd_base` (or a related global pointer), this creates a real NULL dereference. The call chain kernel_main → shell_run → keyboard_getc is a normal boot path where initialization ordering bugs are common. Q2 (Is this specific witness realistic?): Partially. The `kbd_base=NULL` scenario is entirely realistic and achievable in real execution. However, the return value of `-2147483648` (INT_MIN) from `hal_keyboard_getc()` is a CBMC artifact representing an unconstrained symbolic integer — in real execution, `hal_keyboard_getc()` would return a constrained value. The specific assertion violated (`main.assertion.1`) is not visible, but the witness suggests it may be checking a valid return range or memory safety inside `hal_keyboard_getc()`. The key concern is whether `hal_keyboard_getc()` itself dereferences `kbd_base` — if it does, the bug is a real NULL dereference on the uninitialized `kbd_base=NULL` path. If `hal_keyboard_getc()` is an independent HAL path, the INT_MIN return is purely a CBMC artifact.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
