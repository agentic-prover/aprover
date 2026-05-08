# BUG-17 — `vfs_append` (vfs)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | not_triggered |
| **Module** | `kernel/vfs.c` |
| **Bug type** | memory_safety |
| **Violated property** | `vfs_append.pointer_dereference.99` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires (null(file) || valid(file)) && (size == 0 || valid_range(buf, 0, size)) && (size <= 2147483647) && (!null(file) && file->type == 1 implies (valid(file->data) || null(file->data)))`

**Postcondition:** `ensures (esult == (int)size) || (esult == -1) && ((esult == -1) implies (null(file) || file->type != 1 || allocation failure or I/O error occurred)) && ((esult == (int)size) implies the data in buf[0..size) has been appended to the file's existing content)`

## Counterexample

**Violated property:** `vfs_append.pointer_dereference.99`

**Key variable assignments:**
```
use_fat32 = 0
_file_val = {'members': [{'name': 'name', 'value': {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '000...
file = _file_val!0@1
_buf_buf = {'elements': [{'index': 0, 'value': {'binary': '00000001', 'data': '1', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer',...
_buf_len = 1
_buf_buf[1l] = 0
_buf_buf[0l] = 1
_buf_buf[2l] = ' '
_buf_buf[3l] = 0
_buf_buf[4l] = 8
buf = _buf_buf!0@1
size = 0ul
result = 0
return_value_vfs_append = 0
new_size = 16120114370612188845ul
new_cap = 16120114370612188909ul
new_data = dynamic_object
return_value_malloc$0 = dynamic_object
malloc_size = 16120114370612188909ul
malloc_res = dynamic_object
malloc_value = dynamic_object
dynamic_object_size = 16120114370612188909ul
dynamic_object = {'name': 'unknown'}
record_malloc = False
return_value___VERIFIER_nondet___CPROVER_bool$1 = False
record_may_leak = False
return_value___VERIFIER_nondet___CPROVER_bool$2 = False
goto_symex$$return_value$$malloc = dynamic_object
dst = dynamic_object
src = {'name': 'unknown'}
n = 16120114370612188845ul
src_n$array_size = 16120114370612188845ul
src_n = {'name': 'unknown'}
byte_extract_little_endian(dynamic_object, 0l, char [src_n$array_size]) = {'name': 'unknown'}
ptr = {'name': 'unknown'}
return_value___VERIFIER_nondet___CPROVER_bool = True
```

## Root cause / validation reasoning

'vfs_append' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

Dynamic harness outcome: `not_triggered`. Dynamic harness ran to completion without triggering a fault.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific witness requires a vfs_node_t with an astronomically large `file->size` and invalid `file->data` simultaneously — this combination is a CBMC symbolic extreme, not a realistic initialized struct state. However, the integer overflow path in `file->size + size` and the lack of bounds checking on `file->data` relative to `file->size` represent a real (if harder to trigger) vulnerability class for attacker-controlled inputs.

Q1 (Can the violation TYPE occur?): Yes. In the non-FAT32 path, when `new_size > file->capacity`, the code allocates `new_cap = new_size + 64` bytes, then calls `memcpy(new_data, file->data, file->size)` if `file->data` is non-null. Two real vulnerability classes exist here: (a) if `file->size` is large enough that `file->size + size` integer-overflows (size_t wraparound), `new_size` becomes small, malloc succeeds, and the subsequent memcpy reads far beyond the actual allocated `file->data` buffer; (b) if `file->size` is inconsistent with the actual allocation backing `file->data`, the memcpy over-reads. Since this is a public API with no callers guarding input validity, an attacker who can construct a malicious `vfs_node_t` (or cause memory corruption elsewhere) could trigger this. Q2 (Is this specific witness realistic?): The specific counterexample has `file->size = 16120114370612188845ul` (effectively a garbage/uninitialized value) with `file->data` unknown, and `size = 0`. This is a CBMC symbolic artifact — in real execution, `vfs_node_t` structs are initialized with `size=0, data=NULL` (per `create_mem_file`), so a legitimate node would not have a huge `size` with invalid `data`. However, the global context confirms `size` and `data` are set in separate operations, leaving a window for inconsistency. The integer overflow path (`file->size + size` wrapping) is the more realistic attack vector but requires `size` (a function argument, not `file->size`) to be near `SIZE_MAX`. The dynamic harness did not trigger, consistent with the witness being a CBMC artifact rather than a directly reproducible case.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
