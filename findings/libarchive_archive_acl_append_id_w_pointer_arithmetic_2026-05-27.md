# libarchive `archive_acl.c::append_id_w` — pointer-arithmetic CEx (CBMC, REAL_BUG candidate)

**Source**: libarchive `archive_acl.c`, function `append_id_w`
**Property**: `append_id_w.pointer_arithmetic.5`
**bmc-agent verdict**: `outcome=real_bug` (sys_entry_reached=True; dyn-val=not_triggered)
**Sweep**: postfix8, 2026-05-27
**Commit baseline**: bmc-agent `a12ab7e`

---

## TL;DR

CBMC reports `pointer_arithmetic` (C11 §6.5.6/8 undefined behavior) on the post-increment of the cursor inside `append_id_w`. The bug-agent promoted this to `outcome=real_bug` because:

1. **Static reachability** — caller chain `archive_acl_to_text_w → append_entry_w → append_id_w → append_id_w` traces to a libarchive PUBLIC API entry point.
2. **Dyn-val ran but is uninformative** — `dynamic_result.outcome = not_triggered`. The LLM synthesized a public-API reproducer using `archive_entry_acl_to_text_w`, it compiled and ran to completion with no fault. **Silent-UB (pointer-arithmetic UB) does NOT trap at runtime without UBSan/ASan**, so a clean dyn-val run does NOT disprove the bug.
3. **Realism LLM** judged the CEx realistic (1 of the 6 realism verdicts in this archive_acl.c pass).

This is the exact case the postfix8 classifier-downgrade fix (`00f0f08`) was DESIGNED to leave alone: silent-UB classes are explicitly excluded from the dyn-val-NOT_TRIGGERED downgrade to avoid erasing the May-7-VibeOS `malloc.overflow.1`-class of real bugs.

---

## Affected function

`archive_acl.c::append_id_w` (the wide-string analogue of `append_id`):

```c
static void
append_id_w(wchar_t **wp, int id)
{
    if (id < 0)
        id = 0;
    if (id > 9)
        append_id_w(wp, id / 10);
    *(*wp)++ = L"0123456789"[id % 10];   /* pointer_arithmetic.5 here */
}
```

The chain to public API:

```
archive_entry_acl_to_text_w   (public — declared in <archive_entry.h>)
  → archive_acl_to_text_w     (this file)
    → archive_acl_text_len    (size precomputation)
    → malloc(size * sizeof(wchar_t))
    → append_entry_w(buf, ..., id)
      → append_id_w(buf, id)
        → append_id_w(buf, id/10)        /* recurse on the high digits */
```

---

## Counterexample witness

| Variable | Value | Note |
|---|---|---|
| `id` | `1` | No recursion path taken (id<10) |
| `_wp_backing` | 5-element `wchar_t` array | Harness backing |
| `_wp_backing[0..4]` | `49, 50, 52, 55, 50` (= `'1','2','4','7','2'`) | Pre-existing contents (irrelevant to fault) |
| `_wp_cursor` | symbolic | The fault depends on this offset |
| `wp` | `_wp_cursor!0@1` | CBMC chose wp to alias the symbolic cursor |

CBMC walks 247 trace steps and reports `[append_id_w.pointer_arithmetic.5] FAILURE` on the post-increment of `*wp`.

---

## Why this is `REAL_BUG`, not `UNRESOLVED`

The exact reasoning chain bmc-agent persisted:

> Counterexample state is reachable from caller(s): `['append_id_w', 'append_entry_w']`. Call chain: `['archive_acl_to_text_w', 'append_entry_w', 'append_id_w', 'append_id_w']`. Full chain traced to system entry.

Combined with:
- **Realism verdict**: realistic (LLM judged the CEx plausible given the call chain)
- **Dyn-val outcome**: `not_triggered` — but `pointer_arithmetic` is on the silent-UB list (`_SILENT_UB_PROPERTY_SUFFIXES` in `pipeline.py:64-71`), so the auto-downgrade does NOT fire. Per the comment at that location, "Misclassifying these as artifacts erases real bugs — see the May-7 VibeOS malloc.overflow.1 regression that exposed this."

---

## Public-API reproducer (compiled clean by dyn-val)

The LLM produced this and CBMC's dyn-val harness compiled + ran it successfully (no fault):

```c
#include <archive.h>
#include <archive_entry.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    struct archive *a = archive_write_new();
    struct archive_entry *entry = archive_entry_new();

    archive_entry_acl_clear(entry);

    /* Add an ACL entry with id=1 (from counterexample) */
    archive_entry_acl_add_entry(entry,
                                ARCHIVE_ENTRY_ACL_TYPE_ALLOW,
                                ARCHIVE_ENTRY_ACL_READ | ARCHIVE_ENTRY_ACL_WRITE,
                                ARCHIVE_ENTRY_ACL_USER);

    wchar_t *text = NULL;
    ssize_t text_len = 0;

    /* archive_entry_acl_to_text_w internally calls archive_acl_to_text_w → append_entry_w → append_id_w */
    text = archive_entry_acl_to_text_w(entry, &text_len, ARCHIVE_ENTRY_ACL_TYPE_NFS4);

    if (text != NULL) {
        free(text);
    }
    archive_entry_free(entry);
    archive_write_free(a);
    return 0;
}
```

Build: `cc -o repro repro.c -larchive && ./repro`.

Concrete runtime exit: clean (no SIGFAULT). **This does NOT prove the bug isn't real** — pointer-arithmetic UB is C11 §6.5.6/8 *undefined behavior that doesn't trap*. Compile + run under UBSan to see if it surfaces:

```bash
gcc -fsanitize=undefined -fsanitize=pointer-overflow \
    -o repro_ubsan repro.c -larchive
./repro_ubsan
```

---

## Reproducing the CBMC counterexample directly

### Harness (`harness.c`)

```c
/* Auto-generated CBMC harness (real-libc mode) for: append_id_w */
#include "<path to preprocessed archive_acl.c>"

int main(void) {
    /* in-out cursor for 'wp' */
    wchar_t _wp_backing[5];
    unsigned int _wp_nul_at;
    __CPROVER_assume(_wp_nul_at <= (unsigned int)4);
    _wp_backing[_wp_nul_at] = L'\0';
    wchar_t *_wp_cursor = _wp_backing;
    wchar_t** wp = &_wp_cursor;
    int id;
    __CPROVER_assume(wp != NULL);
    __CPROVER_assume(*wp != NULL);
    append_id_w(wp, id);
    return 0;
}
```

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

Expected: `[append_id_w.pointer_arithmetic.5] FAILURE` (plus the related pointer-arith assertions in the same function).

---

## Honest read on REAL_BUG vs FP

The same shape was diagnosed earlier on `append_id.pointer_arithmetic.5` (the narrow-string analogue) as a probable harness FP — see https://github.com/agentic-prover/aprover/blob/main/findings/libarchive_archive_acl_append_id_pointer_dereference_2026-05-27.md and aprover issue #27 for the reasoning. Key counter-argument: the harness's `__CPROVER_assume(*wp != NULL)` doesn't constrain the cursor's OFFSET within the backing buffer, so CBMC may choose a wp value that's past-end before `append_id_w` is even entered. A real caller (`append_entry_w` after `archive_acl_text_len`-sized malloc) does NOT produce that state.

However, for THIS specific CEx (`append_id_w.pointer_arithmetic.5`):
- `sys_entry_reached=True` — caller chain confirms reachability theoretically
- Dyn-val ran clean — at runtime, no fault
- The post-increment from offset N to offset N+1 IS pointer-arithmetic-defined for any N ≤ array-len (one-past-end is allowed)
- The fault would only manifest if `wp` is ALREADY past the array end when `append_id_w` is entered
- That would imply `archive_acl_text_len` under-counts the wide-char bytes needed

**Triage steps to confirm or refute REAL_BUG**:

1. Read `archive_acl_text_len` (also in `archive_acl.c`). Verify it includes the per-entry contribution for `append_id_w`'s decimal-id write (i.e., counts `ndigits(id)` wchars when the tag is USER or GROUP).
2. Run the public-API reproducer above with `id` set to `INT_MAX` (10 digits) under UBSan; see if `pointer-overflow` fires at runtime.
3. If UBSan trips on a legitimate public-API call, file upstream at `libarchive/libarchive`.
4. If UBSan stays clean for all reachable `id` values, this is a harness FP — extend bmc-agent's cursor-pointer setup to constrain the offset.

---

## Related findings on this sweep

* `append_id.pointer_arithmetic.5` (narrow-string analogue): same shape, also classified real_bug — see https://github.com/agentic-prover/aprover/blob/main/findings/libarchive_archive_acl_append_id_pointer_dereference_2026-05-27.md
* `append_id_w.pointer_dereference.11` (this sweep): UNRESOLVED — same function, different property
* aprover issue #27: postfix8 summary across all archive_acl.c findings

---

## Artifacts

* Original classification: `/tmp/libarchive_postfix8/libarchive_postfix8/archive_acl/append_id_w/classifications/append_id_w.pointer_arithmetic.5.json`
* Original harness: `/tmp/libarchive_postfix8/libarchive_postfix8/archive_acl/append_id_w/harness.c`
* Sweep log: `/tmp/libarchive_postfix8/sweep.log`
