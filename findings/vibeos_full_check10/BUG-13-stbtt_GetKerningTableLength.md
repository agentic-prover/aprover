# BUG-13 — `stbtt_GetKerningTableLength` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/ttf.c` |
| **Bug type** | arithmetic |
| **Violated property** | `stbtt_GetKerningTableLength.pointer_arithmetic.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid(info) && valid(info->data) && (info->kern == 0 || valid_range(info->data, info->kern, info->kern + 12))`

**Postcondition:** `\result >= 0 && (info->kern == 0 ==> \result == 0)`

## Counterexample

**Violated property:** `stbtt_GetKerningTableLength.pointer_arithmetic.1`

**Key variable assignments:**
```
_info_val = {'members': [{'name': 'userdata', 'value': {'data': 'NULL', 'name': 'pointer', 'type': 'const void *'}}, {'name': 'data', 'value': {'name': 'unknown'}}, {'name': 'fontstart', 'value': {'binary': '0...
info = _info_val!0@1
result = 0
return_value_stbtt_GetKerningTableLength = 0
data = ((unsigned char *)NULL)
```

## Root cause / validation reasoning

'stbtt_GetKerningTableLength' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGSEGV`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGSEGV

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The specific CBMC witness with NULL data pointer is likely a symbolic artifact, but the underlying bug class — out-of-bounds pointer arithmetic from an attacker-controlled kern table offset in a malicious font file — is real and confirmed by the dynamic harness (SIGSEGV).

Q1 (Can the violation TYPE occur?): Yes. The function computes `info->data + info->kern` where `kern` is an offset read from a font file via `stbtt__find_table`. A malicious font file can specify an arbitrarily large kern table offset, causing the pointer arithmetic to go out of bounds of `info->data`'s allocation. The only guard is `if (!info->kern) return 0;` which only checks for zero — a large out-of-bounds offset passes this check and proceeds to dereference `data+2`, `data+8`, and `data+10`. For a security threat model with attacker-controlled font files, this is a realistic attack surface.

Q2 (Are the witness values achievable?): The specific witness has `info->data` as 'unknown' and `info->kern = 2147483367` (near INT_MAX). The NULL `data` result may be a CBMC symbolic artifact (NULL + large_offset wrapping). However, in real execution, `info->data` would be a valid pointer to a font buffer, and a crafted font file could supply `kern = 2147483367` from the font table directory. Adding this to any realistic pointer would go far out of bounds. The dynamic harness independently confirmed a SIGSEGV, meaning the fault is concretely reproducible. The global context confirms `kern` is set directly from font file data (`kern = stbtt__find_table(data, ...)`) without any bounds validation against the actual buffer size. This is a classic out-of-bounds read via attacker-controlled offset in a binary parser.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
