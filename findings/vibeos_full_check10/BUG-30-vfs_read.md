# BUG-30 — `vfs_read` (vfs)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/vfs.c` |
| **Bug type** | semantic |
| **Violated property** | `vfs_read.precondition_instance.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kapi_read → vfs_read

## Spec (LLM-generated)

**Precondition:** `requires (null(file) || valid(file)) && (null(buf) || valid_range(buf, 0, size)) && (file != null && valid(file) -> file->type == 1) && (file != null && valid(file) -> valid(file->data)) && size >= 0 && offset >= 0`

**Postcondition:** `ensures \result == -1 || \result >= 0 && \result <= size && (\result == -1 -> (null(file) || file->type != 1 || null(buf))) && (\result >= 0 -> valid_range(buf, 0, \result))`

## Counterexample

**Violated property:** `vfs_read.precondition_instance.1`

**Key variable assignments:**
```
use_fat32 = 0
_file_val = {'members': [{'name': 'name', 'value': {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '000...
file = _file_val!0@1
_buf_buf = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer',...
_buf_len = 0
_buf_buf[0l] = 0
_buf_buf[1l] = 0
_buf_buf[2l] = 0
_buf_buf[3l] = 0
_buf_buf[4l] = 0
buf = _buf_buf!0@1
size = 10416249831166781030ul
offset = 1248089072155099136ul
result = 0
return_value_vfs_read = 0
to_read = 10416249831166781030ul
```

## Root cause / validation reasoning

Cross-file caller 'kapi_read' can reach the CEx state. Call chain: ['kapi_read', 'vfs_read']. Full chain traced to system entry.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** The specific CBMC witness uses astronomically large symbolic values for size and offset; realistic exploitation would use moderate values targeting a file with data==NULL or a buf smaller than size, but the vulnerability class (null-dereference / buffer-overflow via memcpy with unchecked file->data and unchecked buf capacity) is unambiguously reachable through the kapi_read entry point.

Q1 — Can the violation TYPE occur? Yes, on multiple sub-paths in the non-fat32 branch:

(a) NULL/invalid file->data dereference: The global context shows that create_mem_file initialises data = NULL. If a file node is created but never written (so data remains NULL) while size is somehow nonzero (e.g., set by a separate code path or a race), the check `offset >= file->size` passes, to_read is computed, and then `memcpy(buf, file->data + offset, to_read)` dereferences NULL + offset — a reliable crash/exploit primitive. The counterexample marks data as 'unknown' (CBMC symbolic), which represents exactly this scenario.

(b) Destination buffer overflow: The function trusts the caller-supplied `size` parameter to bound `to_read`, but it never verifies that `buf` actually has `size` bytes of capacity. Since kapi_read feeds external/user input through to vfs_read, an attacker can supply a `size` far larger than the actual backing buffer. The counterexample's `_buf_len = 0` (5-byte backing buffer) vs `to_read = 10^19` illustrates this gap.

(c) Integer overflow in `file->size - offset`: Although the guard `offset >= file->size` prevents underflow in to_read itself, if offset + size overflows size_t the subsequent check `to_read > size` can be bypassed, leaving to_read = size (attacker-controlled) while the real available data is much less.

Q2 — Are the specific witness values achievable? The exact numerical values (10^19 for size/offset) are CBMC symbolic extremes and unlikely in practice. However, the bug class — passing a size larger than the buf backing store, or reaching memcpy with file->data == NULL — is absolutely achievable in real execution given that (i) kapi_read is a kernel API entry point taking unchecked user-supplied size/offset, (ii) create_mem_file sets data = NULL by default, and (iii) no null-check on file->data exists before the memcpy. The call chain from a kernel API with attacker-controlled inputs directly to unchecked memcpy is a textbook exploitation scenario.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
