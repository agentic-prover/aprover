# BUG-24 — `stbtt_PackEnd` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | inconclusive |
| **Module** | `kernel/ttf.c` |
| **Bug type** | memory_safety |
| **Violated property** | `stbtt_PackEnd.pointer_dereference.39` |
| **Realism** | realistic (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid(spc) && valid(spc->nodes) && valid(spc->pack_info) && spc was previously initialized by a successful call to stbtt_PackBegin`

**Postcondition:** `the memory resources associated with spc (nodes and pack_info) have been freed and spc is no longer valid for use in packing operations`

## Counterexample

**Violated property:** `stbtt_PackEnd.pointer_dereference.39`

**Key variable assignments:**
```
_spc_val = {'members': [{'name': 'user_allocator_context', 'value': {'data': 'NULL', 'name': 'pointer', 'type': 'const void *'}}, {'name': 'pack_info', 'value': {'name': 'pointer', 'type': 'const void *'}}, {...
spc = _spc_val!0@1
ptr = {'name': 'unknown'}
return_value___VERIFIER_nondet___CPROVER_bool = True
```

## Root cause / validation reasoning

'stbtt_PackEnd' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

Dynamic harness outcome: `inconclusive`. Harness generation failed.

## Realism assessment

**Verdict:** REALISTIC (medium confidence)

**Key concern:** No null check on `spc` before dereference; no guard against calling PackEnd without a successful PackBegin; double-free possible if called twice on same context — all realistic for a public API endpoint.

Q1 (Can the violation TYPE occur?): YES. The function `stbtt_PackEnd` is a public API with no callers in the codebase and no precondition guards. It unconditionally dereferences `spc` to access `spc->nodes` and `spc->pack_info`. Any external caller can pass a NULL `spc`, triggering a null pointer dereference. Additionally, the global context shows `nodes` is only initialized in `stbtt_PackBegin` — if `stbtt_PackEnd` is called without a prior successful `stbtt_PackBegin`, or called twice (double-free), the `nodes` and `pack_info` fields may be uninitialized, NULL, or already freed, leading to undefined behavior in `free()` (which is implementation-defined but can crash or be exploited). Q2 (Are the specific witness values achievable?): Partially. The counterexample shows `__CPROVER_deallocated = {'name': 'unknown'}` and `nodes` with 'unknown' value, which may represent either a previously-freed pointer or an uninitialized field — both are realistic in practice when `stbtt_PackEnd` is called without a matching `stbtt_PackBegin`, or called twice. The specific CBMC artifact is that `spc` itself appears valid in the witness while `nodes` is problematic, but that scenario is entirely achievable in real code. From a security standpoint, an attacker who controls the `spc` struct (e.g., via a crafted font file workflow that triggers a mismatched Begin/End sequence) could cause arbitrary memory to be freed, enabling heap exploitation.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
