> **RESOLVED 2026-05-27: this CEx points at a real heap-buffer-overflow bug.**
> Manual triage (the exact `archive_acl_text_len` byte-for-byte audit recommended
> below in "Replication path") found the under-budget at the trailing `:id` write
> for NFSv4 USER/GROUP entries with `name == NULL`. ASAN-confirmed reproducer +
> patch in `findings/libarchive_archive_acl_to_text_heap_overflow_nfsv4_2026-05-27.md`.
> The CBMC CEx documented here was the *pointer* to the bug; the bug itself lives
> one frame up the caller chain in `archive_acl_text_len` vs `append_entry`.

# libarchive `archive_acl.c::append_id` — pointer-dereference CEx (CBMC, UNRESOLVED)

**Source**: libarchive `archive_acl.c`, function `append_id`
**Property**: `append_id.pointer_dereference.11`
**bmc-agent verdict**: `outcome=unresolved` (see "Verdict & analysis" below)
**Sweep**: postfix8, 2026-05-27
**Commit baseline**: bmc-agent `a12ab7e`
**Reproducer status**: CBMC counterexample is **deterministically reproducible** from the harness below; the LLM-generated public-API system-entry reproducer returned `// UNREPRODUCIBLE` (see "Why this is UNRESOLVED, not REAL_BUG").

---

## TL;DR

CBMC produces a `pointer_dereference` counterexample on `append_id` when given a nondet symbolic-offset cursor pointer. The same CEx surfaced as UNRESOLVED rather than REAL_BUG because:

1. The function is a `static` internal helper not exposed in the libarchive public API.
2. The chain `append_entry → append_id` does NOT reach a system-entry point through the source.
3. The LLM declined to synthesize a public-API reproducer (returned the `UNREPRODUCIBLE` marker).

bmc-agent's classifier therefore correctly avoids a v23-class false positive (`gpt2_zero_grad` / `fill_in_parameter_sizes` pattern). This file documents the CEx for human triage — whether real callers in libarchive maintain the implicit invariant that prevents the CEx state.

---

## Affected function

`archive_acl.c:1019-1027`:

```c
static void
append_id(char **p, int id)
{
    if (id < 0)
        id = 0;
    if (id > 9)
        append_id(p, id / 10);
    *(*p)++ = "0123456789"[id % 10];
}
```

Internal helper used by `append_entry` (`archive_acl.c:1091`) to write a decimal user/group ID into a caller-provided buffer cursor. The caller is responsible for pre-sizing the buffer (via `archive_acl_text_len()` called by `archive_acl_to_text_l/w`).

---

## Reproducing the CBMC counterexample

### Prerequisites

- CBMC 5.95.1 (`cbmc-5.95.1`)
- libarchive 3.7.x source at `/tmp/libarchive_bench/libarchive/libarchive/`
- libarchive build dir at `/tmp/libarchive_bench/libarchive/build/` (so `config.h` resolves)

### Harness (`harness.c`)

```c
/* Auto-generated CBMC harness (real-libc mode) for: append_id */
/* Source: <preprocessed archive_acl.c — see "Preprocessed source" below> */
/* Harness entry: main */

#include "<path to preprocessed archive_acl.c>"

int main(void) {
    /* Step 1: nondeterministic inputs */
    /* in-out cursor for 'p': backing buffer + advanceable cursor */
    char _p_backing[5];
    unsigned int _p_nul_at;
    __CPROVER_assume(_p_nul_at <= (unsigned int)4);
    _p_backing[_p_nul_at] = '\0';
    char *_p_cursor = _p_backing;
    char** p = &_p_cursor;
    int id;
    /* Step 2: precondition assumptions */
    __CPROVER_assume(p != NULL);
    __CPROVER_assume(*p != NULL);
    /* Step 3: call function under test */
    append_id(p, id);
    return 0;
}
```

### Preprocessed source

`append_id` references symbols from the libarchive build context (`HAVE_CONFIG_H`, archive type definitions). Either:

* Use `cbmc --real-libc` mode (lets CBMC's preprocessor expand from the original source), OR
* Pre-expand once:

```bash
gcc -E \
    -I/tmp/libarchive_bench/libarchive/build \
    -I/tmp/libarchive_bench/libarchive/libarchive \
    -DHAVE_CONFIG_H \
    /tmp/libarchive_bench/libarchive/libarchive/archive_acl.c \
    > /tmp/archive_acl_preprocessed.c
```

Then `#include "/tmp/archive_acl_preprocessed.c"` in the harness.

### CBMC invocation

```bash
cbmc harness.c \
    --json-ui \
    --unwind 12 \
    --unwinding-assertions \
    --signed-overflow-check \
    --pointer-overflow-check \
    --pointer-check \
    --bounds-check \
    -I/tmp/libarchive_bench/libarchive/build \
    -I/tmp/libarchive_bench/libarchive/libarchive \
    -DHAVE_CONFIG_H
```

Expected result: CBMC verifies the `pointer_dereference` properties on `append_id` and reports `[append_id.pointer_dereference.11] FAILURE`.

---

## Counterexample witness (function-relevant)

CBMC's chosen values that drive the failing assertion:

| Variable | Value | Notes |
|---|---|---|
| `id` | `1` | No recursion path (id<10) — the failure is on the single non-recursive write |
| `_p_nul_at` | `0` | Harness places `'\0'` at `_p_backing[0]` |
| `_p_backing[0..4]` | `'1','0','4','6','6'` | Pre-existing buffer contents (then `_p_backing[_p_nul_at]='\0'` overwrites byte 0) |
| `_p_cursor` | symbolic offset into `_p_backing` | Source of the bug witness — CBMC chose a value not constrained by the harness's `*p != NULL` assume |
| `p` | `&_p_cursor` | Stack address of the cursor variable |

CBMC trace length: 247 steps. The fault point is the write `*(*p)++ = "0123456789"[id % 10]` at `archive_acl.c:1026` — specifically the `(*p)++` post-increment whose post-state is a pointer beyond the `_p_backing[5]` allocation.

---

## Verdict & analysis (why UNRESOLVED, not REAL_BUG)

bmc-agent's classifier produced this reasoning verbatim:

> Implicit-precondition CEx on `append_id` (property `append_id.pointer_dereference.11`): the function lacks an explicit NULL/validity check on a pointer parameter, the immediate caller `['append_entry', 'append_id']` just forwards the parameter without constructing it, and the upward chain `['append_entry', 'append_id']` did not reach system entry. Real callers along a complete chain to main typically maintain the implicit invariant (e.g. main constructs the struct via a build/init routine). Without a system-entry reproducer we can't confirm this is reachable in practice — classifying UNRESOLVED rather than REAL_BUG to avoid the v23-class false positive (`gpt2_zero_grad` / `fill_in_parameter_sizes` pattern).

### Why the dynamic-validation harness gave up

The DynamicReproAgent LLM call returned this marker for the system-entry reproducer:

> `// UNREPRODUCIBLE: append_id and append_entry are internal static functions not exposed in public API; the specific internal buffer state (_p_backing with 5 bytes, _p_cursor pointer offset) cannot be controlled through archive_entry_acl_* public functions`

This is the LLM's honest answer: it could not construct a public-API call sequence (e.g., `archive_entry_acl_add_entry_w(...)` → `archive_entry_acl_to_text_w(...)`) that exercises the witness state. **That does NOT mean no such sequence exists** — only that the LLM couldn't find one.

### Public-API context in libarchive

`append_id` is called from `append_entry` (line 1091):
```c
} else if (tag == ARCHIVE_ENTRY_ACL_USER
    || tag == ARCHIVE_ENTRY_ACL_GROUP) {
    append_id(p, id);   // <-- here
    ...
}
```

`append_entry` is called from `archive_acl_to_text_l/w`. The chain to the public API is:

```
archive_entry_acl_to_text_w   (public)
  → archive_acl_to_text_w     (in this file)
    → archive_acl_text_len    (size precomputation)
    → malloc(size)            (allocate exactly that many bytes)
    → append_entry(buf, ...)  (writes into the sized buffer)
      → append_id(buf, id)    (writes decimal id at current cursor)
```

The implicit invariant: `archive_acl_text_len` precomputes the EXACT byte count needed (including all decimal-id writes), and the public function `malloc`s exactly that size. **If `archive_acl_text_len` ever undercounts the bytes a subsequent `append_id` will write**, the bug is real and the CEx is reproducible.

---

## Replication path for confirming / refuting REAL_BUG

For a human triaging this CEx, the path to a definitive yes/no:

1. **Read `archive_acl_text_len` in `archive_acl.c`**. Verify it accounts for every decimal-id write the `append_entry → append_id` chain can perform for every ACL entry shape (USER, GROUP, USER_OBJ, GROUP_OBJ, MASK, OTHER, EVERYONE, ALLOW/DENY).
2. **Build a libarchive client that crafts an entry with maximal-length decimal ids** (`INT_MAX` user or group id ≈ 10 digits) and calls `archive_entry_acl_to_text_w(entry, ...)`. Compare bytes-written vs `archive_acl_text_len` prediction.
3. If mismatch found, the CEx is REAL_BUG and worth a libarchive issue. If `archive_acl_text_len` is correct, the CEx is an implicit-invariant FP (the same v23 pattern).

The author has NOT performed step 1-3; bmc-agent's UNRESOLVED is the truthful "needs human" verdict.

---

## Related findings on the same sweep

* `append_id.pointer_arithmetic.5` — same harness, classified `real_bug` (probable harness FP — pointer-cursor over-permissiveness); see `findings/auto_archive_acl_clear_double_free.md` for similar patterns
* `append_entry.pointer_arithmetic.23`, `append_entry.pointer_dereference.125` — caller-contract slip class (analogous to postfix5's `archive_match_owner_excluded`)
* `next_field_w.pointer_arithmetic.{11,29,35}` — inter-object pointer compare via independent harness backing arrays

See GitHub issue agentic-prover/aprover#27 for the broader postfix8 summary.

---

## Artifacts

* Original classification JSON: `/tmp/libarchive_postfix8/libarchive_postfix8/archive_acl/append_id/classifications/append_id.pointer_dereference.11.json` (403 lines)
* Original harness: `/tmp/libarchive_postfix8/libarchive_postfix8/archive_acl/append_id/harness.c`
* Sweep log: `/tmp/libarchive_postfix8/sweep.log` (run started 2026-05-27 10:53 UTC)
