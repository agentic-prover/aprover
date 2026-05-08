# BUG-15 — `stbtt__buf_range` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | inconclusive |
| **Module** | `kernel/ttf.c` |
| **Bug type** | semantic |
| **Violated property** | `main.assertion.3` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_InitFont → stbtt_InitFont_internal → stbtt__buf_range

## Spec (LLM-generated)

**Precondition:** `requires valid(b) && valid_range(b->data, 0, b->size) && b->size >= 0 && b->cursor >= 0 && b->cursor <= b->size && o >= 0 && s >= 0 && o <= b->size && s <= b->size - o && (o + s) >= 0 && (o + s) <= b->size`

**Postcondition:** `ensures \result.cursor == 0 && (o < 0 || s < 0 || o > b->size || s > b->size - o ? (\result.data == ((void*)0) && \result.size == 0) : (\result.data == b->data + o && \result.size == s)) && (s == 0 || valid_range(\result.data, 0, \result.size))`

## Counterexample

**Violated property:** `main.assertion.3`

**Key variable assignments:**
```
_b_val = {'members': [{'name': 'data', 'value': {'name': 'unknown'}}, {'name': 'cursor', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width'...
b = _b_val!0@1
o = 260046783
s = 0
result = {'members': [{'name': 'data', 'value': {'name': 'unknown'}}, {'name': 'cursor', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width'...
return_value_stbtt__buf_range = {'members': [{'name': 'data', 'value': {'data': '((unsigned char *)NULL)', 'name': 'pointer', 'type': 'unsigned char *'}}, {'name': 'cursor', 'value': {'binary': '00000000000000000000000000000000',...
r = {'members': [{'name': 'data', 'value': {'data': '((unsigned char *)NULL)', 'name': 'pointer', 'type': 'unsigned char *'}}, {'name': 'cursor', 'value': {'binary': '00000000000000000000000000000000',...
return_value_stbtt__new_buf_stub = {'members': [{'name': 'data', 'value': {'data': '((unsigned char *)NULL)', 'name': 'pointer', 'type': 'unsigned char *'}}, {'name': 'cursor', 'value': {'binary': '00000000000000000000000000000000',...
p = NULL
size = 0ul
goto_symex$$return_value$$stbtt__new_buf_stub.data = {'name': 'unknown'}
goto_symex$$return_value$$stbtt__new_buf_stub.cursor = 0
goto_symex$$return_value$$stbtt__new_buf_stub.size = 0
return_value_stbtt__new_buf_stub.data = {'name': 'unknown'}
return_value_stbtt__new_buf_stub.cursor = 0
return_value_stbtt__new_buf_stub.size = 0
r.data = ((unsigned char *)NULL)
r.cursor = 0
r.size = 0
goto_symex$$return_value$$stbtt__buf_range.data = ((unsigned char *)NULL)
goto_symex$$return_value$$stbtt__buf_range.cursor = 0
goto_symex$$return_value$$stbtt__buf_range.size = 0
return_value_stbtt__buf_range.data = ((unsigned char *)NULL)
return_value_stbtt__buf_range.cursor = 0
return_value_stbtt__buf_range.size = 0
result.data = ((unsigned char *)NULL)
result.cursor = 0
result.size = 0
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['stbtt_InitFont_internal', 'stbtt__dict_get', 'stbtt__cff_index_get', 'stbtt__get_subrs', 'stbtt__cff_get_index']. Call chain: ['stbtt_PackFontRange', 'stbtt_PackFontRanges', 'stbtt_InitFont', 'stbtt_InitFont_internal', 'stbtt__buf_range']. Full chain traced to system entry.

## Dynamic confirmation

Dynamic harness outcome: `inconclusive`. Dynamic harness compilation failed even without global state injection for 'stbtt_PackFontRange'. Error: /tmp/tmpvz6prz2m.c: In function ‘stbtt_InitFont_internal’:
/tmp/tmpvz6prz2m.c:1197:16: error: incompatible types when assigning to type ‘stbtt__buf’ from type ‘int’
 1197 |    info->cff = stbtt__new_buf(((void *)0), 0);
      |                ^~~~~~~~~~~~~~
/tmp/tmpvz6prz2m.c:1216:25: error: incompa

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific witness values (data=NULL with size=402652670) are a CBMC artifact — real stbtt__buf construction prevents this combination. However, the broader vulnerability class of out-of-bounds pointer arithmetic from attacker-controlled font offsets/sizes remains a genuine concern, though the existing bounds checks in this function provide substantial protection.

Q1 (Can the violation TYPE occur?): The function `stbtt__buf_range` is called during font parsing with attacker-controlled data. The call chain goes through `stbtt_PackFontRange` → `stbtt_InitFont_internal`, where offsets and sizes are derived from untrusted font file bytes. The violation appears to be about pointer arithmetic (`r.data = b->data + o`) where `b->data` could be NULL or the computed pointer could be out of bounds for the underlying allocation. The violation type — performing pointer arithmetic on a potentially invalid base pointer when parsing untrusted input — is a realistic concern in font parsers.

Q2 (Are the specific witness values achievable?): The counterexample shows `b->data = unknown` (symbolically potentially NULL) with `b->size = 402652670` (a large non-zero value). In real code, `stbtt__buf` is constructed either via `stbtt__new_buf(NULL, 0)` (giving data=NULL with size=0, which the bounds checks would catch) or from actual font bytes (giving non-NULL data). The scenario where `b->data` is NULL while `b->size` is ~400MB is a CBMC symbolic artifact — in practice, a non-zero size buffer should always have a non-null data pointer. However, the underlying concern is real: `b->data` is set from raw font data offsets (`info->data + offset`) without guaranteeing the resulting pointer is valid for the full claimed size. An attacker crafting a font with large offsets but a small actual allocation could cause `b->data + o` to read out of the allocated region.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
