# BUG-19 — `malloc` (memory)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Dynamic outcome** | not_triggered |
| **Module** | `kernel/memory.c` |
| **Bug type** | arithmetic |
| **Violated property** | `malloc.overflow.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

calloc → malloc

## Spec (LLM-generated)

**Precondition:** `size >= 0`

**Postcondition:** `(esult == NULL) || (valid_range(esult, 0, size) && owns(esult) && esult points to a writable memory region of at least size bytes aligned to 16 bytes)`

## Counterexample

**Violated property:** `malloc.overflow.1`

**Key variable assignments:**
```
free_list = ((block_header_t *)NULL)
heap_end = 0ul
heap_start = 0ul
ram_base = 0ul
ram_size = 0ul
stat_alloc_count = 0
stat_free = 0ul
stat_used = 0ul
size = 18446744073709551608ul
result = NULL
return_value_malloc = NULL
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['calloc']. Call chain: ['calloc', 'malloc']. Full chain traced to system entry.

## Dynamic confirmation

Dynamic harness outcome: `not_triggered`. Dynamic harness ran to completion without triggering a fault.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** None — the calloc multiplication overflow feeding into malloc's alignment overflow is a real, attacker-exploitable vulnerability class when the heap has any free blocks. The specific CBMC witness values (SIZE_MAX*8 overflow) are directly constructible by an attacker.

Q1 (can this violation TYPE occur?): Yes. The malloc function performs an alignment rounding: `size = (size + 15) & ~15`. When called with `size = 18446744073709551608` (2^64 - 8), the addition `size + 15` wraps around to `7` under unsigned arithmetic, then `7 & ~15 = 0`. This means the alignment step silently transforms a near-maximal allocation request into size=0, but there is no post-alignment zero check, so malloc proceeds with size=0. Any free block would satisfy `current->size >= 0`, potentially returning a block that was not properly accounted for. The calloc caller is the source: `size_t total = nmemb * size` has no overflow guard, so attacker-controlled `nmemb` and `size` can produce an overflowed `total` passed straight to malloc. This is a classic integer-overflow-to-small-allocation vulnerability class. Q2 (are the specific witness values achievable?): Yes. The specific counterexample `size = SIZE_MAX * 8 mod 2^64 = 18446744073709551608` is exactly achievable by calling `calloc(SIZE_MAX, 8)`. While the CBMC witness happens to have `free_list = NULL` (causing immediate NULL return and no memory corruption in this path), the same overflow path with a non-empty free list would allocate a real block with a zeroed/wrong size. Furthermore, even in the NULL free_list case, calloc's loop `for (i=0; i < total; i++)` with the original overflowed `total` value would cause a massive out-of-bounds write if malloc ever returned non-NULL. The dynamic harness did not trigger a signal because calloc returned NULL (empty heap in harness), but the vulnerability class is real under realistic embedded/custom heap conditions.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
