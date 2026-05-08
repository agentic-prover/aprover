# BUG-08 — `vfs_close_handle` (vfs)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/vfs.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
kapi_close -> vfs_close_handle
```

## Spec (LLM-generated)

**Precondition:** `requires null(node) || (valid(node) && (null(node->data) || valid(node->data)))`

**Postcondition:** `ensures true; // node and node->data are freed; callers must not access node or node->data after this call`

## Counterexample

**Violated property:** `vfs_close_handle.precondition_instance.10`

**Key variable assignments:**
```
node              = valid pointer (_node_val, all name bytes zero)
node.data         = unknown (dangling/previously freed pointer)
node.size         = 0
node.capacity     = 0
node.child_count  = 0
node.parent       = NULL
__CPROVER_deallocated = non-null (already freed memory)
```

## Root cause

`vfs_close_handle` frees `node->data` if non-NULL, then frees `node` itself, but does not set `node->data = NULL` after freeing. If the function is called twice on the same node (double-close via `kapi_close`), or if two VFS node handles share the same `data` pointer through a shallow copy, the second call to `free(node->data)` passes an already-freed pointer to the allocator, causing undefined behavior. The function is reached via the kernel close API and is exposed to double-close patterns inherent in handle table management.

## How to trigger

1. Open a file via `kapi_open`, obtaining a VFS node handle.
2. Close it twice via `kapi_close` — a double-close pattern that can occur when error-handling code or reference counting logic is incorrect.
3. On the second close, `vfs_close_handle` checks `if (node->data)` and finds a non-NULL dangling pointer (because it was not nulled after the first free), then calls `free(node->data)` on the already-freed memory.

## Realism assessment

**Verdict:** REALISTIC

The counterexample models a scenario where `node` is a valid (non-NULL) heap-allocated `vfs_node_t`, but `node->data` points to already-deallocated memory (`__CPROVER_deallocated` set). This represents a double-free or use-after-free scenario.

Several realistic paths lead here:

1. **Double-close**: `kapi_close` is a kernel API entry point. If the same handle is closed twice (a common bug in OS/kernel code when handle table management is flawed), the second call to `vfs_close_handle` would pass a node whose `data` was already freed by the first call. The null-check `if (node->data)` does NOT protect against this because after `free(node->data)`, the pointer is not zeroed out.

2. **Shared data pointers**: The global context shows `data` is assigned from `path_copy` (in `vfs_open_handle`), `new_data` (in `vfs_append`), and `new_data` (in `vfs_write`). If two VFS nodes ever share the same `data` pointer through a shallow copy or aliasing bug, closing one node would cause `node->data` in the other to become a dangling pointer, leading to a double-free when the second node is closed.

3. **External data management**: Since `data` is managed by multiple functions, it's plausible that calling code frees the data buffer independently and then calls `vfs_close_handle`, which would double-free it.

The call chain `kapi_close → vfs_close_handle` places this at a system call boundary, making it reachable from untrusted user space. The absence of call-site constraints and the fact that kernel file-descriptor close operations are a well-known vulnerability class (double-close) makes this exploitable in practice. The function does not set `node->data = NULL` after freeing, which is a clear code defect.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
