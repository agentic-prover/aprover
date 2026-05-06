# BUG-07 — `vfs_open_handle` (vfs)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/vfs.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
kapi_open -> vfs_open_handle
```

## Spec (LLM-generated)

**Precondition:** `valid_string(path) && the path string length is at most 255 characters if the node's data field is non-null (to safely fit in the 256-byte copy buffer)`

**Postcondition:** `(\result == null(\result)) || (valid(\result) && owns(\result) && the returned node is an independent heap-allocated copy of the VFS node found at path, and if the original node had a non-null data field then \result->data is also a separately heap-allocated copy that callers must free independently)`

## Counterexample

**Violated property:** `vfs_open_handle.precondition_instance.10`

**Key variable assignments:**
```
path              = "" (empty string, _path_buf all zeros)
_path_len         = 4
node (malloc'd)   = valid heap object, all fields zero
node.data         = non-NULL pointer (dynamic_object$0)
malloc_size       = 256
```

## Root cause

`vfs_open_handle` allocates a fixed 256-byte `path_copy` buffer and calls `strcpy(path_copy, (char*)temp->data)` to copy the node's data content. The `data` field of a VFS node can hold arbitrary content written by `vfs_write` or `vfs_append` — neither of which enforces a 255-byte maximum. If a node's data content exceeds 255 bytes, the `strcpy` overflows the heap-allocated 256-byte buffer, corrupting adjacent heap memory. No length check is performed before the copy.

## How to trigger

1. Use `vfs_write` to write more than 255 bytes into a VFS file node, setting its `data` field to a long allocation.
2. Call `kapi_open` on the path to that node.
3. `vfs_open_handle` will call `vfs_lookup` (returning the node), then perform `strcpy(path_copy, (char*)temp->data)` into a 256-byte buffer, overflowing it.

## Realism assessment

**Verdict:** REALISTIC

The violated property is a precondition for `strcpy(path_copy, (char*)temp->data)`, where `path_copy` is a fixed 256-byte buffer. The key concern is a buffer overflow: if `temp->data` points to a string longer than 255 bytes, the `strcpy` will overflow the 256-byte allocation.

1. **Call chain reachability**: The function is reachable via `kapi_open → vfs_open_handle`, which is a kernel API entry point that accepts user-supplied paths — inputs are not constrained.

2. **Data field can hold arbitrary content**: The global variable context shows `data` is assigned in `vfs_write` (arbitrary user-written file content) and `vfs_append` (appended user content), not just paths. A VFS node's `data` field can therefore hold arbitrarily long data supplied by users.

3. **Counterexample scenario**: `vfs_lookup` returns a valid non-NULL node whose `data` field is non-NULL. The code then does `strcpy(path_copy, (char*)temp->data)` into a 256-byte buffer. If the node's `data` content exceeds 255 bytes — entirely possible since `vfs_write`/`vfs_append` can store arbitrary-length data — this is a heap buffer overflow.

4. **No caller constraint prevents this**: The call-site analysis shows `kapi_open`'s body is unavailable, and the function is described as an external API. There is no evidence that callers ensure `temp->data` is always < 256 bytes.

5. **Not a false positive**: This is not triggered by NULL pointers, extreme integer values, or artificially zero-initialized globals. The counterexample shows a plausible execution where a valid VFS node with a non-NULL `data` field is looked up, and the unconditional `strcpy` into a 256-byte buffer overflows.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
