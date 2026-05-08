# BUG-18 — `vfs_close_handle` (vfs)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/vfs.c` |
| **Bug type** | semantic |
| **Violated property** | `vfs_close_handle.precondition_instance.1` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kapi_close → vfs_close_handle

## Spec (LLM-generated)

**Precondition:** `null(node) || (valid(node) && (null(node->data) || valid(node->data)) && owns(node) && (null(node->data) || owns(node->data)))`

**Postcondition:** `the memory pointed to by node (and node->data if non-null) has been freed; callers must not access node or node->data after this call`

## Counterexample

**Violated property:** `vfs_close_handle.precondition_instance.1`

**Key variable assignments:**
```
_node_val = {'members': [{'name': 'name', 'value': {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '000...
node = _node_val!0@1
```

## Root cause / validation reasoning

Cross-file caller 'kapi_close' can reach the CEx state. Call chain: ['kapi_close', 'vfs_close_handle']. Full chain traced to system entry.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The harness tests a simulated buggy version that differs from the actual function (which has a null check for data). The realistic concern is whether node->data or node itself could point to non-heap memory (e.g., stored_path assigned to data in vfs_lookup could be a static/stack string), making free() calls undefined behaviour — but this cannot be confirmed without seeing kapi_close's full body and how vfs_node_t instances are constructed.

Q1 — Can the violation TYPE occur? Yes. The actual function calls `free(node->data)` and `free(node)`. Two real vulnerability classes exist here: (a) if `node->data` was assigned a non-heap pointer (the global context shows `data = stored_path` in `vfs_lookup` — if `stored_path` points to static or stack memory, `free(node->data)` is undefined behaviour/crash); (b) if `node` itself is not heap-allocated (e.g., stack-allocated struct passed in), `free(node)` is UB. Both scenarios are reachable via the `kapi_close → vfs_close_handle` chain since kapi_close's body is unavailable and inputs may be attacker-controlled. The call-site analysis does not rule these out. Q2 — Are the specific witness values realistic? The CBMC counterexample marks `node->data` as 'unknown' (unconstrained symbolic value), which is a CBMC modeling artifact rather than a specific concrete value observed in real execution. The harness also simulates a *different* bug (NULL deref without the null guard) that does not match the actual function body, which already guards against NULL data before freeing. So the specific witness is partly artificial — the actual function body's null check on `data` prevents the NULL-deref scenario in the harness. However, the underlying concern (non-heap `data` pointer, or `node` not being malloc-owned) remains a realistic issue given the global assignment `data = stored_path` in `vfs_lookup`. The verdict is UNCERTAIN because the violation class is real but the CBMC witness is likely an artifact of the symbolic harness, not the actual function code.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
