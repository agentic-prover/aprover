# BUG-12 — `stbtt_PackFontRangesPackRects` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | inconclusive |
| **Module** | `kernel/ttf.c` |
| **Bug type** | memory_safety |
| **Violated property** | `stbrp_pack_rects_stub.pointer_dereference.1` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

stbtt_PackFontRange → stbtt_PackFontRanges → stbtt_PackFontRangesPackRects

## Spec (LLM-generated)

**Precondition:** `requires valid(spc) && valid(spc->pack_info) && valid_range(rects, 0, num_rects) && num_rects >= 0 && num_rects is the total number of characters across all font ranges as gathered by stbtt_PackFontRangesGatherRects, and no integer overflow occurs in num_rects arithmetic`

**Postcondition:** `ensures each rect in rects[0..num_rects) has been assigned a packed position (was_packed field and x,y coordinates updated in-place), the rects array is modified in-place with packing results, memory safety is preserved (no out-of-bounds writes), and the results are valid for use by stbtt_PackFontRangesRenderIntoRects`

## Counterexample

**Violated property:** `stbrp_pack_rects_stub.pointer_dereference.1`

**Key variable assignments:**
```
_spc_val = {'members': [{'name': 'user_allocator_context', 'value': {'data': 'NULL', 'name': 'pointer', 'type': 'const void *'}}, {'name': 'pack_info', 'value': {'name': 'unknown'}}, {'name': 'width', 'value'...
spc = _spc_val!0@1
_rects_val = {'members': [{'name': 'x', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'y', 'value': {'binary': '0000000000...
rects = _rects_val!0@1
num_rects = 33554432
con = {'name': 'unknown'}
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['stbtt_PackFontRanges']. Call chain: ['stbtt_PackFontRange', 'stbtt_PackFontRanges', 'stbtt_PackFontRangesPackRects']. Full chain traced to system entry. Callee feasibility confirmed.

## Dynamic confirmation

Dynamic harness outcome: `inconclusive`. Dynamic harness compilation failed even without global state injection for 'stbtt_PackFontRange'. Error: /tmp/tmpm9j3zefl.c: In function ‘stbtt_InitFont_internal’:
/tmp/tmpm9j3zefl.c:1197:16: error: incompatible types when assigning to type ‘stbtt__buf’ from type ‘int’
 1197 |    info->cff = stbtt__new_buf(((void *)0), 0);
      |                ^~~~~~~~~~~~~~
/tmp/tmpm9j3zefl.c:1216:25: error: incompa

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** CBMC's witness treats pack_info as fully unconstrained, which is a verification artifact. In real execution, pack_info is NULL only if stbtt_PackBegin was not called or returned failure (memory allocation failed) without the caller checking the return value. This is a real but relatively low-probability scenario that is more of a robustness bug than a direct attacker-exploitable path, unless the stbtt_pack_context struct is externally serialized/deserialized from untrusted input.

Q1 (Can the violation TYPE occur?): The violation is a potential null/invalid pointer dereference inside stbrp_pack_rects when spc->pack_info is cast to stbrp_context*. According to the global context, pack_info is assigned in stbtt_PackBegin. If a caller invokes stbtt_PackFontRange (or stbtt_PackFontRanges) without a prior successful stbtt_PackBegin call, or if stbtt_PackBegin returns 0 (allocation failure) and the return value is not checked, pack_info could be NULL or uninitialized. The stbtt_PackFontRanges caller does not check spc->pack_info validity before calling stbtt_PackFontRangesPackRects, so the path to a null dereference in stbrp_pack_rects is real. Q2 (Are the specific witness values realistic?): The counterexample shows pack_info as 'unknown' (fully symbolic/unconstrained), which is a CBMC artifact—CBMC assumes no initialization guarantees on the struct. In real execution, pack_info would be NULL only if PackBegin failed without the caller noticing, or if spc itself is attacker-supplied with NULL fields. The specific witness (pack_info=symbolic unknown) is not directly achievable, but the underlying scenario (NULL pack_info after a failed or missing PackBegin) is plausible in a security context where callers may not check return values or where the pack_context struct is partially externally controlled. The call chain from stbtt_PackFontRange does not guard against pack_info being NULL before reaching this function.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
