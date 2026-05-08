# BUG-04 — `newline` (console)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/console.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

```
kernel_main -> console_puts -> console_putc -> newline
```

## Spec (LLM-generated)

**Precondition:** `console is initialized (num_rows >= 1 && num_cols >= 1 && cursor_row >= 0 && cursor_row < num_rows && cursor_col >= 0 && cursor_col < num_cols) && the framebuffer and scroll buffer are valid and accessible && cursor is hidden (cursor_visible == false)`

**Postcondition:** `cursor_col == 0 && cursor_row >= 0 && cursor_row < num_rows && (if the old cursor_row was num_rows - 1 then scroll_up() was called and cursor_row == num_rows - 1, else cursor_row == old cursor_row + 1) && the framebuffer/display state is consistent with the new cursor position && no out-of-bounds memory accesses occurred during scroll_up() if invoked`

## Counterexample

**Violated property:** `scroll_up_stub.overflow.1`

**Key variable assignments:**
```
cursor_col = 0
cursor_row = 1
fb_height = 2147516416u
fb_width = 3221233721u
num_rows = 0
scroll_offset = 0u
virtual_height = 0u
```

## Root cause

CBMC reports a `scroll_up_stub.overflow.1` failure — a arithmetic / overflow violation in `newline`.

**Realism checker's key concern:** The CBMC witness is inconsistent (fb_height=2147516416 but num_rows=0 cannot co-exist given num_rows=fb_height/16), making this a CBMC artifact for the specific witness. However, num_rows=0 is reachable when fb_height<16, and the underflow is a real vulnerability class on that path.

**Validator reasoning:** Counterexample state is reachable from caller(s): ['console_putc']. Call chain: ['kernel_main', 'console_puts', 'console_putc', 'newline']. Full chain traced to system entry.

## How to trigger

Reach `newline` via the call chain `kernel_main → console_puts → console_putc → newline` and supply inputs that match the counterexample variable assignments above.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The CBMC witness is inconsistent (fb_height=2147516416 but num_rows=0 cannot co-exist given num_rows=fb_height/16), making this a CBMC artifact for the specific witness. However, num_rows=0 is reachable when fb_height<16, and the underflow is a real vulnerability class on that path.

Q1 — Can the violation TYPE occur? Yes. The core bug is in `cursor_row = num_rows - 1` when `num_rows == 0`. Since `num_rows` and `cursor_row` are unsigned (uint32_t), this produces a wrap-around to 4294967295 (0xFFFFFFFF), causing all subsequent framebuffer accesses using `cursor_row` as an index to be wildly out of bounds. Additionally, `scroll_up` itself may perform `% virtual_height` where `virtual_height` could also be 0, causing integer division by zero (UB / trap). The global context shows `num_rows = fb_height / 16` — any system where `fb_height < 16` (e.g., malformed UEFI GOP framebuffer descriptor, emulator, or attacker-controlled boot parameters) would set `num_rows = 0`. There is no guard in `newline()` against `num_rows == 0` before the subtraction. Q2 — Are the specific witness values achievable? The CBMC witness is internally inconsistent: it shows `fb_height = 2147516416` yet `num_rows = 0`. In real execution with `num_rows = fb_height / 16`, these two values cannot co-exist. CBMC is treating the globals as independent symbolic variables, making the exact witness an artifact. However, `num_rows = 0` IS achievable in practice when `fb_height < 16`, which can occur with certain hardware or malformed firmware data. The violation type (unsigned underflow → corrupted cursor_row → out-of-bounds framebuffer access) is a real exploitable path, though the specific counterexample witness is inconsistent with the initialisation formula.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
