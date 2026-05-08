# BUG-18 — `write32` (fat32)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/fat32.c` |
| **Bug type** | memory_safety |
| **Violated property** | `write32.pointer_dereference.11` |
| **Realism** | realistic (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kapi_write → vfs_write → fat32_write_file → create_dir_entry → write32

## Spec (LLM-generated)

**Precondition:** `requires valid_range(p, 0, 4) && !null(p)`

**Postcondition:** `ensures p[0] == (val & 0xFF) && p[1] == ((val >> 8) & 0xFF) && p[2] == ((val >> 16) & 0xFF) && p[3] == ((val >> 24) & 0xFF) && no memory outside p[0..3] is modified`

## Counterexample

**Violated property:** `write32.pointer_dereference.11`

**Key variable assignments:**
```
_p_val = 0
p = _p_val!0@1
val = 0u
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['create_dir_entry', 'update_dir_entry']. Call chain: ['kapi_write', 'vfs_write', 'fat32_write_file', 'create_dir_entry', 'write32']. Full chain traced to system entry.

## Realism assessment

**Verdict:** REALISTIC (medium confidence)

**Key concern:** In create_dir_entry, the pointer `e` pointing to a directory entry buffer is likely obtained from a cluster/sector read function. If that function can return NULL and the result is used without a NULL check before calling write32(e + 28, ...), the dereference of an invalid pointer is a real memory-safety vulnerability reachable from attacker-controlled file path inputs.

Q1 (Can the violation TYPE occur?): The function write32 dereferences pointer `p` at offsets 0–3 with no NULL or validity check. The two call sites are `write32(e + 28, 0)` in `create_dir_entry` and `write32(e + 28, size)` in `update_dir_entry`. In both cases, `e` is a pointer to a directory entry within a cluster buffer obtained from FAT32 disk I/O. If `e` is NULL (e.g., a failed sector read returns NULL and the return value is not checked before computing `e + 28`), `p` will be a near-NULL invalid pointer (address 28 on most platforms), and all four byte writes will be invalid memory accesses. Looking at the truncated body of `create_dir_entry`, there is a loop over LFN entries followed by additional logic, and the code shows patterns where buffer pointers are derived from `find_free_dir_entries` results and cluster reads — these are common places where NULL checks are omitted. The call chain goes through `kapi_write`, which is a kernel API entry point handling attacker-controlled file paths, making the full chain reachable from external input.

Q2 (Is this witness realistic?): The counterexample sets `_p_val = 0`, representing the scenario where `e` in `create_dir_entry` is NULL, causing `e + 28` to be an invalid pointer passed as `p`. This is plausible: if the cluster buffer read for the directory entry fails or returns NULL and the return is not validated before adding the 28-byte offset, the real program would execute the same invalid dereference. The specific values (val=0) are innocuous but the pointer invalidity is the real concern and is achievable.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
