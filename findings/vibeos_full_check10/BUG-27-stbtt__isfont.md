# BUG-27 — `stbtt__isfont` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | inconclusive |
| **Module** | `kernel/ttf.c` |
| **Bug type** | memory_safety |
| **Violated property** | `stbtt__isfont.pointer_dereference.11` |
| **Realism** | realistic (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

stbtt_FindMatchingFont → stbtt_FindMatchingFont_internal → stbtt__matches → stbtt__isfont

## Spec (LLM-generated)

**Precondition:** `valid_range(font, 0, 4) && !null(font)`

**Postcondition:** `\result == 0 || \result == 1 && (\result != 0 iff (font[0..3] matches one of the recognized font magic byte sequences: {0x31,0x00,0x00,0x00}, {'t','y','p','1'}, {'O','T','T','O'}, {0x00,0x01,0x00,0x00}, or {'t','r','u','e'})) && (\result != 0 implies font points to a valid single font, not a font collection) && (\result == 0 implies font does not begin with any recognized single-font magic bytes)`

## Counterexample

**Violated property:** `stbtt__isfont.pointer_dereference.11`

**Key variable assignments:**
```
_font_val = 49
font = _font_val!0@1
result = 0
return_value_stbtt__isfont = 0
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['stbtt__matches', 'stbtt_GetNumberOfFonts_internal', 'stbtt_GetFontOffsetForIndex_internal']. Call chain: ['stbtt_FindMatchingFont', 'stbtt_FindMatchingFont_internal', 'stbtt__matches', 'stbtt__isfont']. Full chain traced to system entry.

## Dynamic confirmation

Dynamic harness outcome: `inconclusive`. Dynamic harness compilation failed even without global state injection for 'stbtt_FindMatchingFont'. Error: /tmp/tmpztsthm1t.c:1079:13: error: redefinition of ‘ttLONG’
 1079 | stbtt_int32 ttLONG(stbtt_uint8* p)
      |             ^~~~~~
/tmp/tmpztsthm1t.c:700:20: note: previous definition of ‘ttLONG’ with type ‘stbtt_int32(stbtt_uint8 *)’ {aka ‘int(unsigned char *)’}
  700 | static stbtt_int32 ttLONG(stb

## Realism assessment

**Verdict:** REALISTIC (medium confidence)

**Key concern:** The specific CBMC witness (1-byte allocation) is a symbolic artifact; in practice the exploitable scenario is a truncated/crafted font file where fc+offset leaves fewer than 4 bytes remaining, causing an out-of-bounds read of 1–3 bytes past the buffer end.

Q1 (Can the violation TYPE occur?): Yes. The function `stbtt__isfont` dereferences `font[0]` through `font[3]` — four bytes — without any null check or bounds check. The function signature carries no buffer length, so there is no way to verify that at least 4 bytes are accessible. In the call chain from `stbtt__matches`, `fc+offset` is passed where `offset` comes from `stbtt_GetFontOffsetForIndex`, which derives offsets from untrusted font-file data. An attacker supplying a crafted or truncated font file could arrange for `offset` to place the pointer within 1–3 bytes of the end of the buffer, causing `font[1]`, `font[2]`, or `font[3]` to read beyond the allocation — a classic out-of-bounds read in a font parser. Q2 (Are the specific witness values achievable?): The CBMC counterexample models `font` as a pointer to a single-byte object (`_font_val = 49`), which is a CBMC symbolic-execution artifact. In real execution the pointer would be into a larger font-collection buffer. However, the underlying violation class — accessing 4 bytes when fewer than 4 remain in the buffer — is entirely achievable with a truncated or maliciously crafted font file. The call chain passes through `stbtt_FindMatchingFont`, a public entry point that accepts raw font data, making attacker control of the buffer size plausible. The absence of any length/bounds parameter in `stbtt__isfont` means no caller can safely prevent this without external guards, and call-site analysis does not show any such guards for the `stbtt__matches` path.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
