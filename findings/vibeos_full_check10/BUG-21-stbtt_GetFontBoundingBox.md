# BUG-21 — `stbtt_GetFontBoundingBox` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/ttf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid(info) && valid(info->data) && valid(x0) && valid(y0) && valid(x1) && valid(y1) && info->head >= 0 && valid_range(info->data, info->head + 36, info->head + 44)`

**Postcondition:** `valid(x0) && valid(y0) && valid(x1) && valid(y1) && (*x0 <= *x1) && "*x0, *y0, *x1, *y1 contain the font-wide bounding box in font design units as stored in the 'head' table of the font"`

## Counterexample

**Violated property:** `stbtt_GetFontBoundingBox.pointer_arithmetic.1`

**Key variable assignments:**
```
_info_val = <symbolic struct/array — see classification.json>
info = _info_val!0@1
_x0_val = 0
x0 = _x0_val!0@1
_y0_val = 0
y0 = _y0_val!0@1
_x1_val = 0
x1 = _x1_val!0@1
_y1_val = 0
y1 = _y1_val!0@1
return_value_ttSHORT_stub = 0
```

## Root cause

CBMC reports a `stbtt_GetFontBoundingBox.pointer_arithmetic.1` failure — a arithmetic / overflow violation in `stbtt_GetFontBoundingBox`.

**Realism checker's key concern:** No buffer length tracking exists in stbtt_fontinfo; there is no check that info->data[info->head + 42] is within bounds before dereferencing, making out-of-bounds reads from crafted font files straightforwardly exploitable.

**Validator reasoning:** 'stbtt_GetFontBoundingBox' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`stbtt_GetFontBoundingBox` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** No buffer length tracking exists in stbtt_fontinfo; there is no check that info->data[info->head + 42] is within bounds before dereferencing, making out-of-bounds reads from crafted font files straightforwardly exploitable.

Q1 (Can the violation TYPE occur?): Yes. The function reads from `info->data + info->head + 36` through `+ 42`, which assumes the font data buffer extends at least `head + 44` bytes. This is a font parsing library (stb_truetype) that processes external, potentially attacker-controlled font files. There is no bounds check before any of the four pointer arithmetic operations. A maliciously crafted font file could: (a) position the 'head' table near the end of the file so that offsets +36 through +42 read past the buffer end, (b) provide a corrupt `head` offset value that places it far into or beyond the data buffer, or (c) be initialized with info->data as NULL if stbtt_InitFont failed and the caller didn't verify the return value. The global context confirms `data = info->data + info->kern` and similar unchecked patterns elsewhere, and `head = stbtt__find_table(...)` which can return 0 for a missing table. Q2 (Is the specific witness realistic?): The witness uses head=5 and unknown data. While head=5 is an unusual value (the head table wouldn't normally be at byte offset 5 in a real font), the underlying scenario — data buffer too small for the required offsets — is absolutely achievable with a crafted font. The dynamic harness failed to compile due to a macro redefinition conflict, not due to any logical impossibility. Since this is a public API entry point processing external font data with no input validation, the violation class (out-of-bounds pointer arithmetic/read) is realistic and exploitable.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
