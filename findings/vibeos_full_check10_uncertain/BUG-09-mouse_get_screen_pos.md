# BUG-09 — `mouse_get_screen_pos` (mouse)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/mouse.c` |
| **Bug type** | semantic |
| **Violated property** | `main.assertion.1` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

cursor_update → mouse_get_screen_pos

## Spec (LLM-generated)

**Precondition:** `requires (null(x) || valid(x)) && (null(y) || valid(y))`

**Postcondition:** `ensures (null(x) || (*x >= 0 && *x <= (int)fb_width)) && (null(y) || (*y >= 0 && *y <= (int)fb_height)) && (null(x) || valid(x)) && (null(y) || valid(y))`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
fb_height = 0u
fb_width = 0u
mouse_base = ((uint32_t *)NULL)
mouse_x = 0
mouse_y = 0
_x_val = 0
x = _x_val!0@1
_y_val = 0
y = _y_val!0@1
```

## Root cause / validation reasoning

Cross-file caller 'cursor_update' can reach the CEx state. Call chain: ['cursor_update', 'mouse_get_screen_pos']. Full chain traced to system entry.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific CBMC assertion (main.assertion.1) is not shown directly in the source, and the witness values result in mouse_base=NULL taking the hal_mouse_get_state path where x/y are valid — not null. The real concern (inconsistent null-guard across code branches + potential integer overflow) exists but the exact violated property is ambiguous, making it uncertain whether CBMC triggered on a real bug or a stub-modeling artifact.

Q1 — Can the violation TYPE occur in the real program?

The function has two distinct code paths: (1) when mouse_base is NULL, it calls hal_mouse_get_state(x, y, NULL) without any null-check on x or y first; (2) when mouse_base is non-NULL, it explicitly guards with 'if (x)' and 'if (y)' before dereferencing. This is an inconsistency: callers can legitimately pass NULL for x or y (the non-base path handles it correctly), but on the mouse_base==NULL path, those nulls are forwarded unchecked to hal_mouse_get_state, which may dereference them. Additionally, the multiplication 'mouse_x * (int)fb_width' can overflow if mouse_x is large and fb_width is large — both are derived from external hardware state and could be attacker-influenced in a virtio/hardware-facing driver context.

Q2 — Are the specific counterexample witness values achievable?

The counterexample sets mouse_base=NULL (achievable if find_virtio_tablet() returns NULL, i.e., no tablet device is found), fb_width=0 and fb_height=0 (achievable if framebuffer is not yet initialized), and x/y pointing to valid stack integers (not null). These values are plausible in early-boot or device-absent scenarios. However, the specific CBMC witness does NOT demonstrate a null-pointer dereference for x/y (both point to valid memory), so the exact path CBMC flagged may be an artifact of how it models the hal_mouse_get_state stub. The actual property 'main.assertion.1' is unspecified — it could be an overflow check on the multiplication or the inconsistent null-passing to the stub.

Conclusion: The vulnerability class (inconsistent null-checking of x/y across branches, potential integer overflow in mouse_x * fb_width) is real and reachable. The specific CBMC witness is a plausible but not conclusive match — the stub modeling may cause a false trigger on this exact path.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
