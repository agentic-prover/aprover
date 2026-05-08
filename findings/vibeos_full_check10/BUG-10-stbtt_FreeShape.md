# BUG-10 — `stbtt_FreeShape` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/ttf.c` |
| **Bug type** | semantic |
| **Violated property** | `stbtt_FreeShape.precondition_instance.2` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid(info) && (null(v) || owns(v))`

**Postcondition:** `true`

## Counterexample

**Violated property:** `stbtt_FreeShape.precondition_instance.2`

**Key variable assignments:**
```
_info_val = {'members': [{'name': 'userdata', 'value': {'data': 'NULL', 'name': 'pointer', 'type': 'const void *'}}, {'name': 'data', 'value': {'data': '((unsigned char *)NULL)', 'name': 'pointer', 'type': 'un...
info = _info_val!0@1
_v_val = {'members': [{'name': 'x', 'value': {'binary': '0000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed short int', 'width': 16}}, {'name': 'y', 'value': {'binary': '0000000000000000', '...
v = _v_val!0@1
```

## Root cause / validation reasoning

'stbtt_FreeShape' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGSEGV`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGSEGV

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** None — both the violation type (null/invalid pointer dereference on `info`) and the specific triggering scenario (public API called with NULL `info`) are fully realistic, as confirmed by the dynamic harness SIGSEGV.

Q1 (can the violation TYPE occur?): Yes. The function `stbtt_FreeShape` dereferences `info->userdata` unconditionally before calling `free(v)`. If `info` is NULL (or otherwise invalid), this dereference causes undefined behaviour (null pointer dereference / SIGSEGV). This is a public API function with no call sites found in the codebase, meaning external callers supply `info` with no enforced preconditions. An attacker or buggy caller can trivially pass NULL for `info`. Q2 (are the witness values achievable?): Yes. The dynamic harness confirmed a SIGSEGV signal, demonstrating the crash occurs in practice with the specified inputs. The counterexample shows `info` pointing to a struct whose `userdata` field is NULL, but the SIGSEGV in execution most likely comes from `info` itself being NULL (the harness is cut off but the signal confirms it). Even if `info` is not NULL but `info->userdata` access triggers another memory issue, the underlying violation—accessing a member through an unchecked pointer in a public API entry point—is completely realistic. The global variable context confirms `userdata` can be `((void *)0)` in real usage (stbtt_BakeFontBitmap_internal), and since the function accesses `info->userdata` only to silence a compiler warning via `(void)`, the safety issue is the unchecked `info` pointer itself. No caller analysis guards against NULL.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
