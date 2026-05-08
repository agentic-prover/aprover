# BUG-13 — `stbtt__buf_get8` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | not_triggered |
| **Module** | `kernel/ttf.c` |
| **Bug type** | memory_safety |
| **Violated property** | `stbtt__buf_get8.pointer_dereference.25` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_InitFont → stbtt_InitFont_internal → stbtt__buf_get8

## Spec (LLM-generated)

**Precondition:** `requires valid(b) && b->data != null && b->cursor >= 0 && b->cursor <= b->size && b->size >= 0 && valid_range(b->data, 0, b->size)`

**Postcondition:** `ensures (\old(b->cursor) < b->size ==> (\result == b->data[\old(b->cursor)] && \result >= 0 && \result <= 255 && b->cursor == \old(b->cursor) + 1)) && (\old(b->cursor) >= b->size ==> (\result == 0 && b->cursor == \old(b->cursor)))`

## Counterexample

**Violated property:** `stbtt__buf_get8.pointer_dereference.25`

**Key variable assignments:**
```
_b_val = {'members': [{'name': 'data', 'value': {'name': 'unknown'}}, {'name': 'cursor', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width'...
b = _b_val!0@1
result = 0
return_value_stbtt__buf_get8 = 0
tmp_post = 0
_b_val.cursor = 1
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['stbtt_InitFont_internal', 'stbtt__cff_skip_operand', 'stbtt__dict_get', 'stbtt__cff_int', 'stbtt__buf_get', 'stbtt__run_charstring', 'stbtt__cff_index_get', 'stbtt__cid_get_glyph_subrs', 'stbtt__cff_get_index']. Call chain: ['stbtt_PackFontRange', 'stbtt_PackFontRanges', 'stbtt_InitFont', 'stbtt_InitFont_internal', 'stbtt__buf_get8']. Full chain traced to system entry.

## Dynamic confirmation

Dynamic harness outcome: `not_triggered`. Dynamic harness ran to completion without triggering a fault.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific counterexample with data='unknown' (symbolic/NULL) is a CBMC artifact — real callers always provide a non-null data pointer. However, the underlying bug class (reading past the actual font data buffer because `size` is set from untrusted font content without bounds-checking the underlying allocation) is realistic and security-relevant for a font parser.

Q1 — Can the violation TYPE occur in real execution? Yes. The function `stbtt__buf_get8` dereferences `b->data[b->cursor]` only after confirming `cursor < size`. The real concern is whether `size` can be set to a value larger than the actual backing allocation of `data`. In a font parser (stbtt), both `data` and `size` in `stbtt__buf` are derived from the binary font blob. An attacker supplying a crafted font can set table offsets and sizes to values that cause `size` to be much larger than the real underlying byte array, turning any access where `cursor < size` but `cursor >= actual_data_length` into an out-of-bounds read — a realistic information-disclosure vulnerability. Additionally, if `stbtt__new_buf(NULL, 0)` paths are later mutated (e.g., via `stbtt__buf_range` setting a non-zero size on a null-data buffer), a null-pointer dereference could occur. Q2 — Are the specific counterexample values achievable? The counterexample has `data = unknown` (likely a CBMC symbolic non-deterministic value) with `size = 1073741825`. A genuinely NULL `data` with a non-zero `size` is unlikely in normal stb_truetype paths because `stbtt__new_buf` and `stbtt__buf_range` either set both or leave size=0. However, the scenario of a legitimately non-null `data` backed by N bytes but with `size > N` (attacker-controlled from font content) is the real threat and is achievable. The dynamic harness did not confirm a crash only because it accessed index 0 of a 1-byte stack array, which is in bounds; with cursor advancing across many calls, the out-of-bounds read would occur. The specific CBMC witness (data=unknown) is a symbolic artifact, but the vulnerability class (OOB read via attacker-influenced `size`) is real.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
