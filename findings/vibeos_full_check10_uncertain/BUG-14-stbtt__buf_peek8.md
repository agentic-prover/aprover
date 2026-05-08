# BUG-14 — `stbtt__buf_peek8` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

```
stbtt_PackFontRange -> stbtt_PackFontRanges -> stbtt_InitFont -> stbtt_InitFont_internal -> stbtt__dict_get_ints -> stbtt__dict_get -> stbtt__cff_skip_operand -> stbtt__buf_peek8
```

## Spec (LLM-generated)

**Precondition:** `requires valid(b) && b->cursor >= 0 && b->cursor <= b->size && valid_range(b->data, 0, b->size)`

**Postcondition:** `ensures (b->cursor < b->size ==> \result == b->data[b->cursor]) && (b->cursor >= b->size ==> \result == 0) && b->cursor == \old(b->cursor) && b->size == \old(b->size) && b->data == \old(b->data)`

## Counterexample

**Violated property:** `stbtt__buf_peek8.pointer_dereference.25`

**Key variable assignments:**
```
_b_val = <symbolic struct/array — see classification.json>
b = _b_val!0@1
result = 0
return_value_stbtt__buf_peek8 = 0
```

## Root cause

CBMC reports a `stbtt__buf_peek8.pointer_dereference.25` failure — a memory-safety violation in `stbtt__buf_peek8`.

**Realism checker's key concern:** The specific CBMC witness has `data` as a symbolic/unknown pointer (a CBMC artifact), making the exact counterexample unrealistic. However, the vulnerability class (out-of-bounds read from `b->data[b->cursor]` when `size` from a malformed CFF table exceeds the true buffer allocation) is realistic for attacker-supplied font files.

**Validator reasoning:** Counterexample state is reachable from caller(s): ['stbtt__cff_skip_operand', 'stbtt__dict_get']. Call chain: ['stbtt_PackFontRange', 'stbtt_PackFontRanges', 'stbtt_InitFont', 'stbtt_InitFont_internal', 'stbtt__dict_get_ints', 'stbtt__dict_get', 'stbtt__cff_skip_operand', 'stbtt__buf_peek8']. Full chain traced to system entry.

## How to trigger

Reach `stbtt__buf_peek8` via the call chain `stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_InitFont → stbtt_InitFont_internal → stbtt__dict_get_ints → stbtt__dict_get → stbtt__cff_skip_operand → stbtt__buf_peek8` and supply inputs that match the counterexample variable assignments above.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific CBMC witness has `data` as a symbolic/unknown pointer (a CBMC artifact), making the exact counterexample unrealistic. However, the vulnerability class (out-of-bounds read from `b->data[b->cursor]` when `size` from a malformed CFF table exceeds the true buffer allocation) is realistic for attacker-supplied font files.

Q1 (Can the violation TYPE occur?): YES. The function `stbtt__buf_peek8` only guards against `cursor >= size`, but never validates that `data` is non-NULL or that `size` accurately reflects the true allocation size. This is a font file parser in stb_truetype, and `stbtt__buf` structs are derived from CFF table data in untrusted font files. A malformed font could embed a CFF dictionary sub-buffer with a claimed `size` exceeding the actual allocated region. When `cursor < size` passes but `cursor` is beyond the true allocation, `data[cursor]` causes an out-of-bounds read — a classic parsing vulnerability in attacker-controlled binary data.

Q2 (Are the specific witness values realistic?): NO. The counterexample shows `data` as 'unknown' (a CBMC symbolic artifact representing an uninitialized pointer), and `size = 1073741825` (a suspiciously large symbolic value). In real execution, `data` is always initialized by callers (e.g., from `stbtt_InitFont_internal` via `stbtt__new_buf`), so the exact CBMC witness path is an artifact. However, the underlying bug class — out-of-bounds access when a `stbtt__buf`'s `size` field exceeds the actual backing allocation due to malformed font data — is entirely realistic. The call chain goes through font-parsing entry points (`stbtt_PackFontRange`) that process externally-supplied font binary data, making attacker control over `size` values plausible via crafted CFF tables.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
