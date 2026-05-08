# BUG-17 — `read32` (fat32)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/fat32.c` |
| **Bug type** | arithmetic |
| **Violated property** | `read32.overflow.3` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kapi_write → vfs_write → fat32_write_file → find_entry_in_dir → read32

## Spec (LLM-generated)

**Precondition:** `requires valid_range(p, 0, 4) && p != null`

**Postcondition:** `ensures \result == (uint32_t)(p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24))`

## Counterexample

**Violated property:** `read32.overflow.3`

**Key variable assignments:**
```
_p_val = 128
p = _p_val!0@1
result = 0u
return_value_read32 = 0u
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['find_entry_in_dir', 'fat32_list_dir']. Call chain: ['kapi_write', 'vfs_write', 'fat32_write_file', 'find_entry_in_dir', 'read32']. Full chain traced to system entry.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** None — both the violation type (signed integer overflow via uint8_t promotion before shift) and the specific triggering value (byte value 128+ at position 3) are realistic and easily produced by normal FAT32 filesystem data.

Q1 — Can the violation TYPE occur? Yes. The violation is signed integer overflow. In `p[3] << 24`, `p[3]` is `uint8_t` which undergoes integer promotion to `int` (signed 32-bit). If `p[3]` is any value in 128–255 (bit 7 set), shifting left by 24 bits pushes a 1 into the sign bit of a 32-bit signed int (e.g., 128 << 24 = 0x80000000, which is INT_MIN). This is undefined behavior under C's signed integer overflow rules, regardless of the fact that the return type is `uint32_t`. The correct fix is to cast before shifting: `(uint32_t)p[3] << 24`.

Q2 — Are the witness values achievable? Absolutely. The call site is `read32(e + 28)` where `e` is a pointer into `cluster_buf`, a raw FAT32 directory entry read from disk. Bytes 28–31 of a directory entry encode the file size field. Any file larger than 2 GB would have the high byte of its size field set to >= 128, directly triggering this overflow. Since the call chain goes through `kapi_write → vfs_write → fat32_write_file → find_entry_in_dir`, this processes attacker-controlled filesystem data. The counterexample value of `_p_val = 128` is completely realistic — it corresponds to the high byte of any FAT32 file whose size has bit 31 set (i.e., a 2 GB+ file). This is a classic C integer promotion bug and is definitively triggerable in production.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
