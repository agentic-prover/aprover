# BUG-15 — `ttUSHORT` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/ttf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
stbtt_GetKerningTableLength -> ttUSHORT
```

## Spec (LLM-generated)

**Precondition:** `requires valid_range(p, 0, 2) && p != null`

**Postcondition:** `ensures \result == (stbtt_uint16)(p[0] * 256 + p[1]) && \result >= 0 && \result <= 65535 && (p[0] is not modified) && (p[1] is not modified)`

## Counterexample

**Violated property:** `ttUSHORT.pointer_dereference.11`

**Key variable assignments:**
```
_p_val = 1
p = _p_val!0@1
result = 0
return_value_ttUSHORT = 0
```

## Root cause

CBMC reports a `ttUSHORT.pointer_dereference.11` failure — a memory-safety violation in `ttUSHORT`.

**Realism checker's key concern:** The specific counterexample witness value (p=1) is a CBMC artifact, but the dynamic harness confirmed SIGSEGV in real execution, meaning the underlying out-of-bounds pointer dereference is real and reachable through malformed font input.

**Validator reasoning:** Counterexample state is reachable from caller(s): ['stbtt_GetKerningTableLength', 'stbtt_GetFontNameString', 'stbtt_InitFont_internal', 'stbtt__GetGlyphShapeTT', 'stbtt_FindSVGDoc', 'stbtt__GetCoverageIndex', 'stbtt_FindGlyphIndex', 'stbtt__GetGlyphGPOSInfoAdvance', 'stbtt__matchpair', 'stbtt__matches', 'stbtt_GetKerningTable', 'stbtt__GetGlyfOffset', 'stbtt_ScaleForMappingEmToPixels', 'stbtt__GetGlyphKernInfoAdvance', 'stbtt__find_table', 'stbtt__GetGlyphClass', 'stbtt_GetGlyphHMetrics']. Call chain: ['stbtt_GetKerningTableLength', 'ttUSHORT']. Full chain traced to system entry.

## How to trigger

Reach `ttUSHORT` via the call chain `stbtt_GetKerningTableLength → ttUSHORT` and supply inputs that match the counterexample variable assignments above.

A standalone GCC-compiled reproducer was generated and executed; it crashed with `SIGSEGV`. The reproducer source is preserved in the run's `classification.json` under `dynamic_result.harness_source`.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The specific counterexample witness value (p=1) is a CBMC artifact, but the dynamic harness confirmed SIGSEGV in real execution, meaning the underlying out-of-bounds pointer dereference is real and reachable through malformed font input.

Q1 (Can the violation TYPE occur?): Yes. The function `ttUSHORT` reads `p[0]` and `p[1]` with no null-check or bounds validation. In the caller `stbtt_GetKerningTableLength`, `data = info->data + info->kern` is computed, and then `ttUSHORT` is called with `data+2`, `data+8`, and `data+10`. If `info->data` is NULL, or if `info->kern` is a value derived from a malformed/malicious font file that places the kern table near the end of the data buffer, the pointer arithmetic could produce an invalid pointer. Since stb_truetype is a font parser accepting external (potentially attacker-controlled) binary data, this class of vulnerability is entirely plausible. Q2 (Are the specific witness values realistic?): The counterexample's `p = 1` looks like a CBMC symbolic artifact, but crucially, the dynamic harness confirmed a SIGSEGV signal — meaning the actual fault was triggered under real execution conditions. This confirms the bug is not merely a symbolic analysis artifact. The call chain goes through a font file parser that reads attacker-controlled bytes (`info->kern` offset is derived from font file data), and the guard `if (!info->kern) return 0` only protects against a zero offset — it doesn't validate that `info->data + info->kern + 11` is still within the data buffer bounds. A crafted font with a kern offset near the end of the data triggers an out-of-bounds read.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
