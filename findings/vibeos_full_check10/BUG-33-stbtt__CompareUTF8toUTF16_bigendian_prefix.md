# BUG-33 — `stbtt__CompareUTF8toUTF16_bigendian_prefix` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(s1, 0, len1) && valid_range(s2, 0, len2) && len1 >= 0 && len2 >= 0 && len2 % 2 == 0`

**Postcondition:** `ensures \result >= -1 && (\result == -1 || (\result >= 0 && \result <= len1)) && (\result >= 0 implies \result represents the number of UTF-8 bytes in s1[0..\result) that exactly match the UTF-16 big-endian encoding in s2[0..len2), and all memory accesses to s1[0..\result) and s2[0..len2) are within bounds)`

## Counterexample

**Violated property:** `stbtt__CompareUTF8toUTF16_bigendian_prefix.unwind.0`

**Key variable assignments:**
```
_s1_val = 0
s1 = _s1_val!0@1
len1 = 33554435
_s2_val = 0
s2 = <symbolic struct/array — see classification.json>
len2 = 1073741818
result = 0
return_value_stbtt__CompareUTF8toUTF16_bigendian_prefix = 0
i = 4
ch = 0
tmp_post_i = 3
```

## Root cause

CBMC reports a `stbtt__CompareUTF8toUTF16_bigendian_prefix.unwind.0` failure — a semantic / contract violation in `stbtt__CompareUTF8toUTF16_bigendian_prefix`.

**Realism checker's key concern:** No validation that len2 is even: an odd attacker-controlled len2 causes signed-integer underflow and an infinite loop; additionally, extremely large even values cause a denial-of-service, and odd values cause an out-of-bounds read on s2.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`stbtt__CompareUTF8toUTF16_bigendian_prefix` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** No validation that len2 is even: an odd attacker-controlled len2 causes signed-integer underflow and an infinite loop; additionally, extremely large even values cause a denial-of-service, and odd values cause an out-of-bounds read on s2.

Q1 (Can the violation TYPE occur?): YES. The loop decrements `len2` by exactly 2 on every normal iteration (`len2 -= 2`), and by 4 for surrogate pairs. If `len2` starts as an odd value, parity is preserved and the value passes through 1, then underflows to -1 (signed int), then -3, -5… The condition `while (len2)` is true for all non-zero values, including every negative odd integer, producing an infinite loop. Even with an even `len2`, extremely large values (no upper-bound validation) produce DoS-level iteration counts. Additionally, when len2 is odd, the first iteration reads `s2[0]` and `s2[1]` when only 1 byte may be valid — an out-of-bounds read.

Q2 (Is this specific witness realistic?): The witness has `len2 = 1073741818` (even, ~10^9). In isolation this would terminate but require ~500M loop iterations — a denial-of-service. The CBMC unwind bound simply fires before the symbolic loop completes, but the underlying concern (unbounded/near-unbounded loop, and odd-len2 infinite loop) is genuine.

Call-site analysis confirms the function is used in font-name parsing (`stbtt__matchpair`), where `slen` and `nlen` come from parsed font file tables. An attacker-supplied font can supply an odd UTF-16 byte count, triggering the infinite loop / out-of-bounds read. No caller validates that len2 is even before the call.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
