# Heap buffer overflow in `archive_acl_to_text_l` / `archive_acl_to_text_w` for nameless NFSv4 USER/GROUP entries

## Summary

`archive_acl_to_text_l` and `archive_acl_to_text_w` write past the end of a
heap-allocated buffer when serializing an NFSv4 ACL containing a `USER` or
`GROUP` entry whose `name` field is `NULL`, when the caller does **not** set
the `ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID` style flag.

The overflow is `1 + idlen` bytes (one byte for `':'` plus 1–10 bytes of
decimal digits, i.e. up to **11 bytes**) past a `malloc`'d region whose size
was computed by `archive_acl_text_len`.

The bug is caught at runtime by libarchive's own post-write guard
(`__archive_errx(1, "Buffer overrun")` at `archive_acl.c:1008` in 3.8.7),
which calls `abort()`. Before that guard fires, the overflowing bytes have
already been written to the heap, so any caller that has installed an
alternate `__archive_errx` handler — or any out-of-process attacker who can
control the timing — has a short window where the corruption is observable.

## Affected versions

Reproduced and confirmed against:

- libarchive **3.7.2** (Ubuntu 24.04 system package)
- libarchive **3.8.7** (latest stable, released 2026-04-13), built from source

Inspection shows the buggy size calculation in `archive_acl_text_len` is
present and identical in every released libarchive version from **3.3.0
through 3.8.7** inclusive. The function was introduced in commit
`379867ecb` ("Break up, simplify and improve OS-independent ACL code",
2016-12-27, released in 3.3.0) and has not been substantively changed since.

Unreleased `master` (post-3.8.7) is **not affected** because of an unrelated
refactor that replaces the `archive_acl_text_len` + `malloc(length)` pattern
with growable `archive_string` / `archive_wstring` buffers. The double-id
output is still produced on master (see "Note on master" below) but no
longer causes memory corruption.

## How the bug was found

This bug surfaced in the CBMC bounded model checker as a
`pointer_dereference` counterexample on the internal static helper
`append_id`. The original CBMC report was classified UNRESOLVED because the
LLM-based reproducer agent could not synthesize a public-API call sequence
to drive the witness state, and the analysis pipeline (correctly) refused
to mark it REAL_BUG without one.

The manual triage steps the report itself recommends — auditing
`archive_acl_text_len` byte-for-byte against `append_entry`'s write paths —
identify the under-budgeting at the trailing-`:id` write site (line 1119 in
3.8.7) for the NFSv4 USER/GROUP case.

## Root cause

`append_entry` calls `append_id` in two places: an inline id-as-name
fallback when no name is supplied (line 1071), and a trailing `:id` write
when `id != -1` at the end of the function (line 1120):

```c
/* archive_acl.c, line 1118 (3.8.7) */
if (id != -1) {
    *(*p)++ = ':';
    append_id(p, id);
}
```

For NFSv4 USER/GROUP entries, `id` is **never reset to `-1`** after the
inline append_id call, because line 1072 conditions the reset on POSIX.1e:

```c
append_id(p, id);
if ((type & ARCHIVE_ENTRY_ACL_TYPE_NFS4) == 0)
    id = -1;
```

So NFSv4 USER/GROUP entries always emit `:id` twice in the output — once
after `user:` / `group:` and once at the very end.

`archive_acl_text_len`, however, only budgets the trailing `:id` when the
caller has explicitly opted into it via the `EXTRA_ID` style flag:

```c
/* archive_acl.c, line 639 (3.8.7) */
if ((ap->tag == ARCHIVE_ENTRY_ACL_USER ||
    ap->tag == ARCHIVE_ENTRY_ACL_GROUP) &&
    (flags & ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID) != 0) {
    length += 1; /* colon */
    /* ID digit count */
    idlen = 1;
    ...
    length += idlen;
}
```

NFSv4 mode triggers the second write but does not trigger this size budget,
so the writer overruns the buffer by `1 + idlen` bytes.

## Why the existing test suite doesn't catch this

Every NFSv4 USER/GROUP entry in `libarchive/test/test_acl_nfs4.c` provides a
non-NULL `name` (`"user77"`, `"user108"`, etc.). The `name == NULL` branch
of `append_entry` is not exercised under NFSv4 by any test, so the
double-emission of the id — and the resulting overflow — never appears in
CI output.

## Reproducer

```c
/*
 * Build: cc -Wall -fsanitize=address reproducer.c -larchive -o repro
 *   Run: ./repro
 */
#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>

int main(void) {
    struct archive_entry *entry = archive_entry_new();
    archive_entry_acl_clear(entry);

    archive_entry_acl_add_entry(
        entry,
        ARCHIVE_ENTRY_ACL_TYPE_ALLOW,
        ARCHIVE_ENTRY_ACL_READ_DATA | ARCHIVE_ENTRY_ACL_WRITE_DATA,
        ARCHIVE_ENTRY_ACL_USER,
        2147483647,   /* INT_MAX -> 10 digits */
        NULL);        /* no name */

    const char *txt = archive_entry_acl_to_text(
        entry, NULL, ARCHIVE_ENTRY_ACL_TYPE_NFS4);
    printf("to_text: %s\n", txt ? txt : "(null)");
    archive_entry_free(entry);
    return 0;
}
```

### Without sanitizer (libarchive 3.8.7)

```
$ ./repro
Fatal Internal Error in libarchive: Buffer overrun
Aborted (core dumped)
```

### With AddressSanitizer (libarchive 3.8.7)

```
==16387==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x504000000040
READ of size 56 at 0x504000000040 thread T0
    #0 strlen
    #1 archive_acl_to_text_l libarchive/archive_acl.c:1008
    #2 main reproducer.c:38

0x504000000040 is located 0 bytes after 48-byte region [0x504000000010,0x504000000040)
allocated by thread T0 here:
    #0 malloc
    #1 archive_acl_to_text_l libarchive/archive_acl.c:946
```

The allocation at line 946 is the `malloc(length)` sized by
`archive_acl_text_len`. The strlen read at line 1008 is the post-write
`len = strlen(s)` whose result feeds the `Buffer overrun` guard at line
1009; ASAN catches that read first because the writer has already produced
a non-NUL-terminated string that extends past the redzone.

The patched output (see below) is
`user:2147483647:rw------------:-------:allow:2147483647` — 59 bytes —
which is exactly 11 bytes (`":" + "2147483647"`) more than the 48 bytes
the pre-patch `archive_acl_text_len` budgeted.

## Patch

```diff
--- a/libarchive/archive_acl.c
+++ b/libarchive/archive_acl.c
@@ -636,9 +636,18 @@
         } else
             length += 3; /* rwx */
 
+        /*
+         * append_entry() writes a trailing ":<id>" for USER/GROUP
+         * entries when either the EXTRA_ID style flag is set, or
+         * the ACL is NFSv4 (in which case `id` is not reset to -1
+         * after the inline id-as-name fallback). Budget for both
+         * cases here; otherwise NFSv4 entries with no name overrun
+         * the allocated buffer by `1 + idlen` bytes (colon + digits).
+         */
         if ((ap->tag == ARCHIVE_ENTRY_ACL_USER ||
             ap->tag == ARCHIVE_ENTRY_ACL_GROUP) &&
-            (flags & ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID) != 0) {
+            (((flags & ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID) != 0)
+            || want_type == ARCHIVE_ENTRY_ACL_TYPE_NFS4)) {
             length += 1; /* colon */
             /* ID digit count */
             idlen = 1;
```

## Verification

With the patch applied to libarchive 3.8.7:

- The reproducer above runs to completion under AddressSanitizer with no
  heap-buffer-overflow report. Output:
  `user:2147483647:rw------------:-------:allow:2147483647`
- The full `libarchive_test` suite still passes (`PASS: 1, FAIL: 0`).

## Note on master

Unreleased master (commit at time of writing has refactored
`archive_acl_to_text_*` to use growable `archive_string` buffers, removing
`archive_acl_text_len` entirely) is **not vulnerable to the overflow**, but
still emits the id twice in the NFSv4-no-name case because `append_entry`'s
duplicate-write logic is unchanged. That is a semantic / format-correctness
question rather than a memory-safety one and is out of scope for this fix.

If maintainers consider the double-emission itself incorrect, the fix in
master is a one-liner in `append_entry`: drop the
`(type & ARCHIVE_ENTRY_ACL_TYPE_NFS4) == 0` condition on the `id = -1`
reset at line 932 of master's `archive_acl.c`. That change would also
require updating `archive_acl_text_len` correspondingly in the 3.7/3.8
branches if it were backported — the patch above only fixes the
size-calculation discrepancy, which is the minimal change needed to close
the memory-safety bug. Maintainers may prefer to ship the size-fix now and
address the semantic question separately.

## Severity / exploitability

The overflow is a heap write of bounded size (≤ 11 bytes per nameless
NFSv4 USER/GROUP entry in the serialized ACL; multiple entries compound).
The overflowing bytes are colon and ASCII digits — narrow attacker control
of content. The number of overflowing bytes is attacker-influenced via the
numeric id.

Public-API reachability is via any caller that:

1. Constructs or imports an `archive_entry` containing an NFSv4 ACL with a
   USER or GROUP entry whose name is not set (e.g. NFSv4 ACLs imported from
   filesystems where the entry's uid/gid did not resolve to a username), and
2. Calls `archive_entry_acl_to_text()` or `archive_entry_acl_to_text_w()`
   without the `ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID` style flag.

This pattern can arise during archive creation (`bsdtar`-style tools
serializing NFSv4 ACLs they read from disk) when an ACL contains a
numeric-only USER or GROUP that has no matching passwd/group entry.
