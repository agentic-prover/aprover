# BUG-19 — `fat_name_to_str` (fat32)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/fat32.c` |
| **Bug type** | semantic |
| **Violated property** | `fat_name_to_str.unwind.0` |
| **Realism** | uncertain (— confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(fat_name, 0, 11) && valid_range(out, 0, 256) && fat_name[0] != 0x00 && fat_name[0] != 0xE5`

**Postcondition:** `ensures valid_string(out) && the length of out is less than 13 (at most 8 base chars + 1 dot + 3 extension chars + null terminator) && out contains a null-terminated lowercase string representation of the 8.3 FAT filename stored in the first 11 bytes of fat_name, with uppercase ASCII letters converted to lowercase`

## Counterexample

**Violated property:** `fat_name_to_str.unwind.0`

**Key variable assignments:**
```
_fat_name_buf = {'elements': [{'index': 0, 'value': {'binary': '01000010', 'data': "'B'", 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '01000001', 'data': "'A'", 'name': 'integ...
_fat_name_len = 4u
_fat_name_buf[4l] = 0
_fat_name_buf[0l] = 'B'
_fat_name_buf[1l] = 'A'
_fat_name_buf[2l] = 'A'
_fat_name_buf[3l] = 0
fat_name = _fat_name_buf!0@1
_out_buf = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer',...
_out_len = 4u
_out_buf[4l] = 0
_out_buf[0l] = 'B'
_out_buf[1l] = 'A'
_out_buf[2l] = 'A'
_out_buf[3l] = 0
out = _out_buf!0@1
i = 4
j = 4
tmp_post_j = 3
```

## Root cause / validation reasoning

Counterexample is spurious — no caller can produce the state {'__CPROVER_dead_object': 'NULL', '__CPROVER_deallocated': 'NULL', '__CPROVER_max_malloc_size': '36028797018963968ul', '__CPROVER_memory_leak': 'NULL', '__CPROVER_rounding_mode': '0', '_fat_name_buf': '{\'elements\': [{\'index\': 0, \'value\': {\'binary\': \'01000010\', \'data\': "\'B\'", \'name\': \'integer\', \'type\': \'char\', \'width\': 8}}, {\'index\': 1, \'value\': {\'binary\': \'01000001\', \'data\': "\'A\'", \'name\': \'integer\', \'type\': \'char\', \'width\': 8}}, {\'index\': 2, \'value\': {\'binary\': \'01000001\', \'data\': "\'A\'", \'name\': \'integer\', \'type\': \'char\', \'width\': 8}}, {\'index\': 3, \'value\': {\'binary\': \'00000000\', \'data\': \'0\', \'name\': \'integer\', \'type\': \'char\', \'width\': 8}}, {\'index\': 4, \'value\': {\'binary\': \'00000000\', \'data\': \'0\', \'name\': \'integer\', \'type\': \'char\', \'width\': 8}}], \'name\': \'array\'}', '_fat_name_len': '4u', '_fat_name_buf[4l]': '0', '_fat_name_buf[0l]': "'B'", '_fat_name_buf[1l]': "'A'", '_fat_name_buf[2l]': "'A'", '_fat_name_buf[3l]': '0', 'fat_name': '_fat_name_buf!0@1', '_out_buf': "{'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 2, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 3, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 4, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}], 'name': 'array'}", '_out_len': '4u', '_out_buf[4l]': '0', '_out_buf[0l]': "'B'", '_out_buf[1l]': "'A'", '_out_buf[2l]': "'A'", '_out_buf[3l]': '0', 'out': '_out_buf!0@1', 'i': '4', 'j': '4', 'tmp_post_j': '3'}. Precondition refined over 0 iteration(s).

## Realism assessment

**Verdict:** UNCERTAIN (— confidence)

Could not parse LLM response: ## Analysis

### Q1: Can this violation TYPE occur in real execution?

The violated property is `fat_name_to_str.unwind.0` — a **loop unwinding bound** hit by CBMC during symbolic execution. I need to

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
