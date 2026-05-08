# BUG-26 — `ttf_init` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires the VFS subsystem is initialized and accessible, and the memory allocator (malloc) is operational`

**Postcondition:** `ensures \result == 0 if the TTF subsystem was successfully initialized or was already initialized (ttf_ready was set before the call), and \result == -1 if initialization failed due to a missing font file, invalid font file size, memory allocation failure, read failure, or font parsing failure; ensures that when \result == 0, the global TTF state is ready for use (ttf_ready == 1, font_data is valid, font_info is initialized, size_caches are populated, and temp_bitmap is allocated)`

## Counterexample

**Violated property:** `ttf_init.overflow.1`

**Key variable assignments:**
```
font_data = ((uint8_t *)NULL)
font_data_size = 0
font_info.userdata = NULL
font_info.data = ((uint8_t *)NULL)
font_info.fontstart = 0
font_info.numGlyphs = 0
font_info.loca = 0
font_info.head = 0
font_info.glyf = 0
font_info.hhea = 0
font_info.hmtx = 0
font_info.kern = 0
font_info.gpos = 0
font_info.svg = 0
font_info.index_map = 0
font_info.indexToLocFormat = 0
font_info.cff.data = ((uint8_t *)NULL)
font_info.cff.cursor = 0
font_info.cff.size = 0
font_info.charstrings.data = ((uint8_t *)NULL)
font_info.charstrings.cursor = 0
font_info.charstrings.size = 0
font_info.gsubrs.data = ((uint8_t *)NULL)
font_info.gsubrs.cursor = 0
font_info.gsubrs.size = 0
font_info.subrs.data = ((uint8_t *)NULL)
font_info.subrs.cursor = 0
font_info.subrs.size = 0
font_info.fontdicts.data = ((uint8_t *)NULL)
font_info.fontdicts.cursor = 0
font_info.fontdicts.size = 0
font_info.fdselect.data = ((uint8_t *)NULL)
font_info.fdselect.cursor = 0
font_info.fdselect.size = 0
size_cache_sizes = <symbolic struct/array — see classification.json>
size_cache_sizes[0l] = 12
size_cache_sizes[1l] = 14
size_cache_sizes[2l] = 16
size_cache_sizes[3l] = 18
size_cache_sizes[4l] = 20
size_cache_sizes[5l] = 24
size_cache_sizes[6l] = 28
size_cache_sizes[7l] = 32
size_caches = <symbolic struct/array — see classification.json>
size_caches[0l] = <symbolic struct/array — see classification.json>
size_caches[0l].size = 0
size_caches[0l].scale = 0
size_caches[0l].entries = <symbolic struct/array — see classification.json>
size_caches[0l].count = 0
size_caches[0l].$pad4 = 0
size_caches[1l] = <symbolic struct/array — see classification.json>
size_caches[1l].size = 0
size_caches[1l].scale = 0
size_caches[1l].entries = <symbolic struct/array — see classification.json>
size_caches[1l].count = 0
size_caches[1l].$pad4 = 0
size_caches[2l] = <symbolic struct/array — see classification.json>
size_caches[2l].size = 0
size_caches[2l].scale = 0
size_caches[2l].entries = <symbolic struct/array — see classification.json>
size_caches[2l].count = 0
size_caches[2l].$pad4 = 0
size_caches[3l] = <symbolic struct/array — see classification.json>
size_caches[3l].size = 0
size_caches[3l].scale = 0
size_caches[3l].entries = <symbolic struct/array — see classification.json>
size_caches[3l].count = 0
size_caches[3l].$pad4 = 0
size_caches[4l] = <symbolic struct/array — see classification.json>
size_caches[4l].size = 0
size_caches[4l].scale = 0
size_caches[4l].entries = <symbolic struct/array — see classification.json>
size_caches[4l].count = 0
size_caches[4l].$pad4 = 0
size_caches[5l] = <symbolic struct/array — see classification.json>
size_caches[5l].size = 0
size_caches[5l].scale = 0
size_caches[5l].entries = <symbolic struct/array — see classification.json>
size_caches[5l].count = 0
size_caches[5l].$pad4 = 0
size_caches[6l] = <symbolic struct/array — see classification.json>
size_caches[6l].size = 0
size_caches[6l].scale = 0
size_caches[6l].entries = <symbolic struct/array — see classification.json>
size_caches[6l].count = 0
size_caches[6l].$pad4 = 0
size_caches[7l] = <symbolic struct/array — see classification.json>
size_caches[7l].size = 0
size_caches[7l].scale = 0
size_caches[7l].entries = <symbolic struct/array — see classification.json>
size_caches[7l].count = 0
size_caches[7l].$pad4 = 0
temp_bitmap = ((uint8_t *)NULL)
temp_bitmap_size = 0
ttf_ready = 0
result = 0
return_value_ttf_init = 0
font_file = <symbolic struct/array — see classification.json>
return_value_vfs_lookup = <symbolic struct/array — see classification.json>
```

## Root cause

CBMC reports a `ttf_init.overflow.1` failure — a arithmetic / overflow violation in `ttf_init`.

**Validator reasoning:** 'ttf_init' has cross-file callers but no reachability was confirmed via CBMC — reporting as confirmed_bmc.

## How to trigger

`ttf_init` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (— confidence)

LLM call failed: LLM request failed after 3 attempts: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'prompt is too long: 1760435 tokens > 1000000 maximum'}, 'request_id': 'req_011CapEc5UKbpZK82pyqvMt1'}

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
