# BUG-04 — `stbtt_PackEnd` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGABRT |
| **Module** | `vendor/stb_truetype.h` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no callers)

## Spec (LLM-generated)

**Precondition:** `valid(spc) && valid(spc->nodes) && valid(spc->pack_info) && spc was previously initialized by a successful call to stbtt_PackBegin`

**Postcondition:** `the memory resources associated with spc (specifically spc->nodes and spc->pack_info) have been freed and spc is no longer valid for use in packing operations`

## Counterexample

**Violated property:** `stbtt_PackEnd.precondition_instance.10`

**Key variable assignments:**
```
spc.pack_info       = <previously freed pointer>
spc.nodes           = <previously freed pointer>
spc.pixels          = NULL
spc.width           = 0
spc.height          = 0
spc.h_oversample    = 0
spc.v_oversample    = 0
__CPROVER_deallocated = non-null (memory already freed)
```

## Root cause

`stbtt_PackEnd` frees `spc->pack_info` and `spc->nodes` but does not null out these pointers after freeing them. If the function is called twice on the same `stbtt_pack_context` (a double-close pattern), the second call passes the already-freed pointers directly to `free()`, causing a double-free detected as SIGABRT. The function has no callers visible in the codebase, making it a pure public API where callers are entirely unconstrained.

## How to trigger

Initialize a pack context with `stbtt_PackBegin`, then call `stbtt_PackEnd` twice on the same context. The second call frees the dangling `spc->pack_info` and `spc->nodes` pointers a second time. This is a common coding error when pack contexts are re-used or when error-handling code calls cleanup on a partially-initialized structure.

## Realism assessment

**Verdict:** REALISTIC

The counterexample shows `__CPROVER_deallocated` is set to a non-null pointer when either `spc->nodes` or `spc->pack_info` is freed, indicating a double-free scenario. The dynamic harness confirmed with SIGABRT — the heap allocator detected a double-free.

The realistic scenario: `stbtt_PackEnd` is a public API cleanup function. If a caller invokes it twice on the same `stbtt_pack_context`, both `spc->nodes` and `spc->pack_info` will be freed a second time. The function does not null out these pointers after freeing them, so the second call blindly passes already-freed pointers to `free()`. This is a classic double-free vulnerability pattern in cleanup functions.

The call-site analysis shows NO internal call sites, confirming this is an external API entry point. External callers have no enforced usage contract preventing double-calls. Defensive cleanup functions typically set pointers to NULL after freeing to guard against this. This function does not.

The global context shows `nodes` and `pack_info` are only ever assigned in `stbtt_PackBegin`, meaning after `stbtt_PackEnd` runs, the context retains stale (now-freed) pointer values. A second `stbtt_PackEnd` call on that same context will pass those stale pointers to `free()`, triggering the SIGABRT.

This is a real, exploitable bug class (double-free via re-use of an ended pack context), not a verification artifact — it requires no impossible inputs, only a double-call pattern common in real application code.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
