# BUG-22 — `stbtt_GetNumberOfFonts_internal` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
stbtt_GetNumberOfFonts -> stbtt_GetNumberOfFonts_internal
```

## Spec (LLM-generated)

**Precondition:** `requires valid(font_collection) && valid_range(font_collection, 0, 12) && font_collection is a non-null pointer to a buffer of at least 12 bytes of font data (TrueType/OpenType or TTC font collection)`

**Postcondition:** `ensures \result >= 0 && (\result == 1 if font_collection points to a single valid TrueType/OpenType font, \result == the number of fonts encoded at font_collection+8 as a signed 32-bit big-endian integer if font_collection is a valid TTC collection with version 1.0 or 2.0, \result == 0 otherwise) && no out-of-bounds memory accesses occur during execution && \result does not cause integer overflow when used as a font count`

## Counterexample

**Violated property:** `stbtt_GetNumberOfFonts_internal.pointer_arithmetic.17`

**Key variable assignments:**
```
_font_collection_val = 116
font_collection = _font_collection_val!0@1
result = 0
return_value_stbtt_GetNumberOfFonts_internal = 0
return_value_stbtt__isfont_stub = 0
font = _font_collection_val!0@1
goto_symex$$return_value$$stbtt__isfont_stub = 0
```

## Root cause

CBMC reports a `stbtt_GetNumberOfFonts_internal.pointer_arithmetic.17` failure — a arithmetic / overflow violation in `stbtt_GetNumberOfFonts_internal`.

**Realism checker's key concern:** No concern about false-positiveness — the real concern is that stbtt_GetNumberOfFonts_internal lacks both a length parameter and any bounds-check before accessing up to 12 bytes of the supplied pointer, making it trivially triggerable with a short or NULL-padded attacker-supplied buffer.

**Validator reasoning:** Counterexample state is reachable from caller(s): ['stbtt_GetNumberOfFonts']. Call chain: ['stbtt_GetNumberOfFonts', 'stbtt_GetNumberOfFonts_internal']. Full chain traced to system entry.

## How to trigger

Reach `stbtt_GetNumberOfFonts_internal` via the call chain `stbtt_GetNumberOfFonts → stbtt_GetNumberOfFonts_internal` and supply inputs that match the counterexample variable assignments above.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** No concern about false-positiveness — the real concern is that stbtt_GetNumberOfFonts_internal lacks both a length parameter and any bounds-check before accessing up to 12 bytes of the supplied pointer, making it trivially triggerable with a short or NULL-padded attacker-supplied buffer.

Q1 — Can the violation TYPE occur? Yes. The function accepts a raw `unsigned char*` with no accompanying length parameter, yet accesses bytes at offsets 0–3, 4–7, and 8–11. If the supplied buffer is shorter than 12 bytes, any of those accesses are out-of-bounds reads. There is zero bounds-checking anywhere in the function. This is a classic unbounded read vulnerability in a parser that processes external (potentially attacker-controlled) font data. The call chain `stbtt_GetNumberOfFonts → stbtt_GetNumberOfFonts_internal` shows the public API accepts a raw `const char* data` with no length, so a malformed or truncated font blob trivially reaches this code. Q2 — Are the specific witness values achievable? The counterexample has `_font_collection_val = 116` (ASCII 't'), a single-byte object, with subsequent accesses to bytes 1, 2, 3, etc. being out-of-bounds. This is fully achievable: passing a font_collection buffer containing only a few bytes (e.g., a truncated/malformed font file) is exactly the kind of input an attacker would craft. The dynamic harness reporting `not_triggered` does not disprove the bug — out-of-bounds reads in adjacent mapped memory frequently do not produce a crash signal but still constitute exploitable undefined behavior (information disclosure, heap layout probing). In a security threat model the absence of a crash is irrelevant; the read is still undefined behavior and potentially exploitable.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
