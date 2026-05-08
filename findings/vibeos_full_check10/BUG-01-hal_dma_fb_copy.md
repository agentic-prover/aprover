# BUG-01 — `hal_dma_fb_copy` (dma)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGABRT |
| **Module** | `kernel/platform.c` |
| **Bug type** | arithmetic |
| **Violated property** | `main.pointer_arithmetic.2` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(dst, 0, width * height) && valid_range(src, 0, width * height) && width > 0 && height > 0 && width * height <= 0xFFFFFFFF && (dst + width * height <= src || src + width * height <= dst)`

**Postcondition:** `ensures \result == 0 && forall i, 0 <= i < width * height ==> dst[i] == src[i]`

## Counterexample

**Violated property:** `main.pointer_arithmetic.2`

**Key variable assignments:**
```
_dst_val = 0u
dst = _dst_val!0@1
_src_val = 0u
src = _src_val!0@1
width = 2415919136u
height = 16u
```

## Root cause / validation reasoning

'hal_dma_fb_copy' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGABRT`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGABRT

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** No concern — this is a genuine integer overflow in untrusted size arithmetic in a public DMA API with no input validation, confirmed by dynamic execution.

Q1 — Can the violation TYPE occur in real execution? YES. The function computes `width * height * sizeof(uint32_t)` where `width` and `height` are unvalidated uint32_t inputs. When width=2415919136 (0x90000000) and height=16, the product 0x90000000 * 16 = 0x900000000, which truncates to 0x00000000 in 32-bit arithmetic. The resulting memcpy size becomes 0, meaning a copy that was supposed to transfer a full framebuffer silently copies nothing. With other large-but-non-wrapping values, the computed size could be far smaller than the actual source/destination buffers require, leading to partial copies or out-of-bounds reads. Additionally, if `dst` or `src` are NULL (as the counterexample shows _dst_val=0), passing them to memcpy with a non-zero size (other input combinations) is undefined behaviour. Q2 — Are these witness values realistic? YES. The function is declared as a system entry point (no callers in codebase), meaning it accepts external/untrusted framebuffer parameters. Width and height values for display configurations routinely come from hardware registers, network protocols, or user-space requests, all of which could be attacker-controlled. The values in the counterexample (width≈2.4×10⁹, height=16) are plausible as malformed DMA parameters. The dynamic harness independently confirmed the fault (SIGABRT), and the combination of unchecked arithmetic on size parameters in a DMA driver API directly maps to a known exploitable vulnerability class (integer overflow leading to heap underwrite or silent data loss).

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
