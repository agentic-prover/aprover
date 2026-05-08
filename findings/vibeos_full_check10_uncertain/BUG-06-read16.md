# BUG-06 — `read16` (fat32)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/fat32.c` |
| **Bug type** | memory_safety |
| **Violated property** | `read16.pointer_dereference.11` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kapi_write → vfs_write → fat32_write_file → find_entry_in_dir → read16

## Spec (LLM-generated)

**Precondition:** `requires valid_range(p, 0, 2)`

**Postcondition:** `ensures \result == (uint16_t)(p[0] | (p[1] << 8)) && \result >= 0 && \result <= 65535`

## Counterexample

**Violated property:** `read16.pointer_dereference.11`

**Key variable assignments:**
```
_p_val = 32
p = _p_val!0@1
result = 0
return_value_read16 = 0
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['find_entry_in_dir']. Call chain: ['kapi_write', 'vfs_write', 'fat32_write_file', 'find_entry_in_dir', 'read16']. Full chain traced to system entry.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific counterexample pointer value (_p_val!0@1) is a CBMC symbolic artifact and not literally achievable. However, if cluster_buf is not null-checked after allocation, or if cluster_buf_size is smaller than 32*(i+1)+27 bytes (e.g., due to attacker-controlled FAT metadata), the same dereference violation type is achievable.

Q1 (Can the violation TYPE occur?): YES. The function read16 dereferences p[0] and p[1] without any null or bounds check. It is called as read16(e + 20) and read16(e + 26) where e = cluster_buf + (i * 32). If cluster_buf is a dynamically allocated global pointer (e.g., via malloc) that was not initialized or whose allocation failed and was not checked, e and thus p could be NULL or point to invalid memory. Additionally, if cluster_buf_size is smaller than expected (e.g., attacker-controlled via FAT filesystem metadata), then e + 20 or e + 26 could be out-of-bounds relative to the actual allocation. The call chain runs through kapi_write, a kernel API entry point accepting attacker-supplied data, making this a realistic threat surface. Q2 (Are the specific witness values achievable?): The CBMC counterexample shows p = _p_val!0@1 with _p_val = 32, which is a CBMC symbolic aliasing artifact rather than a literal address-32 pointer. However, the underlying concern — that cluster_buf might be NULL or that e + 20/e + 26 might exceed the actual buffer allocation — is not contradicted by call-site analysis. The cluster_buf_size global is not validated against a minimum safe value, and entries_per_cluster = cluster_buf_size / 32 does not prevent e+26+1 from exceeding the buffer if entries are near the end. The specific CBMC witness is a symbolic artifact, but the violation class (invalid/out-of-bounds dereference through an unchecked pointer derived from a global buffer) is real and reachable through the documented code path.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
