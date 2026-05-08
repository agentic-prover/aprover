# BUG-11 — `stbtt_PackBegin` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid(spc) && pw > 0 && ph > 0 && padding >= 0 && padding < pw && (null(pixels) || valid_range(pixels, 0, pw * ph)) && (stride_in_bytes == 0 || stride_in_bytes >= pw)`

**Postcondition:** `(esult == 1 && valid(spc) && spc->width == pw && spc->height == ph && spc->pixels == pixels && spc->padding == padding && spc->stride_in_bytes == (stride_in_bytes != 0 ? stride_in_bytes : pw) && spc->h_oversample == 1 && spc->v_oversample == 1 && spc->skip_missing == 0 && valid(spc->pack_info) && valid(spc->nodes) && (pixels != null(pixels) -> all bytes in pixels[0..pw*ph) are zeroed)) || (esult == 0 && spc is unmodified and all allocated memory is freed)`

## Counterexample

**Violated property:** `main.overflow.1`

**Key variable assignments:**
```
_spc_val = <symbolic struct/array — see classification.json>
spc = _spc_val!0@1
_pixels_val = 0
pixels = _pixels_val!0@1
pw = 9
ph = 954437177
stride_in_bytes = 0
padding = 0
alloc_context = NULL
```

## Root cause

CBMC reports a `main.overflow.1` failure — a arithmetic / overflow violation in `stbtt_PackBegin`.

**Validator reasoning:** 'stbtt_PackBegin' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`stbtt_PackBegin` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (— confidence)

Could not parse LLM response: ## Analysis

### Q1: Can this type of violation (integer overflow) occur in the real program?

Yes. The most significant arithmetic overflow risk in `stbtt_PackBegin` is the expression `pw * ph` in:

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
