# BUG-10 — `stbtt_FreeShape` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/ttf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid(info) && (null(v) || owns(v))`

**Postcondition:** `true`

## Counterexample

**Violated property:** `stbtt_FreeShape.precondition_instance.2`

**Key variable assignments:**
```
_info_val = <symbolic struct/array — see classification.json>
info = _info_val!0@1
_v_val = <symbolic struct/array — see classification.json>
v = _v_val!0@1
```

## Root cause

CBMC reports a `stbtt_FreeShape.precondition_instance.2` failure — a semantic / contract violation in `stbtt_FreeShape`.

**Validator reasoning:** 'stbtt_FreeShape' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`stbtt_FreeShape` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

A standalone GCC-compiled reproducer was generated and executed; it crashed with `SIGSEGV`. The reproducer source is preserved in the run's `classification.json` under `dynamic_result.harness_source`.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

Q1 (can the violation TYPE occur?): Yes. The function `stbtt_FreeShape` dereferences `info->userdata` unconditionally before calling `free(v)`. If `info` is NULL (or otherwise invalid), this dereference causes undefined behaviour (null pointer dereference / SIGSEGV). This is a public API function with no call sites found in the codebase, meaning external callers supply `info` with no enforced preconditions. An attacker or buggy caller can trivially pass NULL for `info`. Q2 (are the witness values achievable?): Yes. The dynamic harness confirmed a SIGSEGV signal, demonstrating the crash occurs in practice with the specified inputs. The counterexample shows `info` pointing to a struct whose `userdata` field is NULL, but the SIGSEGV in execution most likely comes from `info` itself being NULL (the harness is cut off but the signal confirms it). Even if `info` is not NULL but `info->userdata` access triggers another memory issue, the underlying violation—accessing a member through an unchecked pointer in a public API entry point—is completely realistic. The global variable context confirms `userdata` can be `((void *)0)` in real usage (stbtt_BakeFontBitmap_internal), and since the function accesses `info->userdata` only to silence a compiler warning via `(void)`, the safety issue is the unchecked `info` pointer itself. No caller analysis guards against NULL.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
