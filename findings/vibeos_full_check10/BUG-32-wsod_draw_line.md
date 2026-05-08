# BUG-32 — `wsod_draw_line` (irq)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/irq.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `fb_base != NULL && fb_width > 0 && fb_height > 0 && y >= 0 && y < fb_height && fb_width > 80 (to ensure the loop range [40, fb_width-40) is non-empty and no integer underflow occurs in fb_width - 40)`

**Postcondition:** `a horizontal line of black pixels (color 0x00000000) is drawn across the framebuffer at row y, from x=40 to x=fb_width-41 inclusive; the framebuffer memory is updated only within the valid pixel range for row y; no other caller-visible state is modified; memory safety is preserved (no out-of-bounds writes occur)`

## Counterexample

**Violated property:** `wsod_draw_line.unwind.0`

**Key variable assignments:**
```
fb_base = <symbolic struct/array — see classification.json>
fb_height = 1u
fb_width = 1u
y = 0
x = 44u
```

## Root cause

CBMC reports a `wsod_draw_line.unwind.0` failure — a semantic / contract violation in `wsod_draw_line`.

**Realism checker's key concern:** Integer underflow in `fb_width - 40` when fb_width < 40 (unsigned wrap-around) causes the loop bound to become ~2^32, resulting in a near-infinite loop. This is especially dangerous in exception/fault handlers where fb_width may not be initialized.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`wsod_draw_line` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** Integer underflow in `fb_width - 40` when fb_width < 40 (unsigned wrap-around) causes the loop bound to become ~2^32, resulting in a near-infinite loop. This is especially dangerous in exception/fault handlers where fb_width may not be initialized.

Q1 (Can the violation TYPE occur?): Yes. The loop condition is `x < fb_width - 40` where `fb_width` is a uint32_t global. If `fb_width < 40`, the subtraction `fb_width - 40` wraps around (unsigned integer underflow), producing a value close to 2^32 (e.g., fb_width=1 → 4294967257). The loop would then run ~4 billion iterations instead of terminating quickly. This is a real integer underflow / near-infinite loop vulnerability. CBMC's loop-unwind violation is detecting this exactly: the loop does not terminate within any realistic bound when fb_width is small. Q2 (Are the specific witness values achievable?): Yes. fb_width is a global representing the framebuffer width, set from hardware or driver initialization. The function is called from exception handlers (handle_sync_exception, handle_serror) — precisely the code paths that execute when the system is in a degraded or partially-initialized state. It is entirely plausible that fb_width has not been properly initialized (0 or very small) when a fault occurs early in system startup, or that fb_width is attacker-influenced via a memory-mapped interface or corrupted hardware register. The call context (WSOD screen drawing in exception handlers) makes this scenario realistic rather than merely theoretical.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
