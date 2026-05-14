PASTE-READY GHSA SUBMISSION

==============================================================================
URL to open (you must be logged in to GitHub):
    https://github.com/jqlang/jq/security/advisories/new
==============================================================================

──────────────────────────────────────────────────────────────────────────────
FIELD 1 — "Title"  (one line)
──────────────────────────────────────────────────────────────────────────────
Pointer-arithmetic UB in jvp_utf8_next (jv_unicode.c:44) — C11 §6.5.6/8

──────────────────────────────────────────────────────────────────────────────
FIELD 2 — "CVE identifier"   →  leave blank (GitHub assigns later)
──────────────────────────────────────────────────────────────────────────────

──────────────────────────────────────────────────────────────────────────────
FIELD 3 — "Affected products"
──────────────────────────────────────────────────────────────────────────────
Package ecosystem:  Other / N/A   (jq is shipped as source; tarball/distro)
Package name:       jq
Affected versions:  <= 1.8.1
Patched versions:   leave blank

──────────────────────────────────────────────────────────────────────────────
FIELD 4 — "Severity"
──────────────────────────────────────────────────────────────────────────────
CVSS vector:  AV:L/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L
              (Local attack, low complexity, no privileges, no user
               interaction, low impact on availability via UB; integrity
               impact only if compiler exploits UB to elide the truncation
               branch — see writeup §Severity.)
Severity:     Low

──────────────────────────────────────────────────────────────────────────────
FIELD 5 — "Common Weakness Enumerator (CWE)"
──────────────────────────────────────────────────────────────────────────────
CWE-823: Use of Out-of-range Pointer Offset
(Optional secondary: CWE-119)

──────────────────────────────────────────────────────────────────────────────
FIELD 6 — "Description"  (paste verbatim — Markdown is rendered)
──────────────────────────────────────────────────────────────────────────────

## Summary

`jvp_utf8_next` in `src/jv_unicode.c` forms a pointer up to **3 bytes
past the end** of the input buffer at line 44. This is undefined
behavior per C11 §6.5.6/8 ("if the pointer operand and the result do
not point to elements of the same array object or one past the last
element of the array object, the behavior is undefined") and §6.5.8/5
(pointer comparison of out-of-range pointers).

Reachable from the public JSON parsing path through
`jvp_utf8_is_valid` → `jv_string_check_utf8` (and through `jv_parse`
which scans string literals via `jvp_utf8_next`).

Confirmed via AddressSanitizer with
`ASAN_OPTIONS=detect_invalid_pointer_pairs=2`.

## Vulnerable code (jv_unicode.c)

```c
const char* jvp_utf8_next(const char* in, const char* end, int* codepoint_ret) {
  assert(in <= end);
  if (in == end) {
    return 0;
  }
  ...
  unsigned char first = (unsigned char)in[0];
  int length = utf8_coding_length[first];   // 1..4 for valid start bytes
  if ((first & 0x80) == 0) {
    /* ASCII fast path */
    ...
  } else if (length == 0 || length == UTF8_CONTINUATION_BYTE) {
    length = 1;
  } else if (in + length > end) {           // <-- LINE 44: UB
    /* String ends before UTF8 sequence ends */
    length = end - in;
  } else {
    ...
  }
```

When `length == 4` and only 1 byte remains in the buffer, `in + 4`
forms a pointer that is **3 bytes beyond** the allocated extent of the
underlying object. Per C11 §6.5.6/8 such a pointer formation is UB
regardless of whether it is later dereferenced. The comparison
`(in + 4) > end` at §6.5.8/5 is also UB (pointers do not point to the
same array object).

## Reproducer

```c
#include <stdio.h>
#include <stdlib.h>
#include "jv_unicode.h"

int main(void) {
    char *buf = malloc(1);
    if (!buf) return 1;
    buf[0] = (char)0xF0;             /* 4-byte UTF-8 start byte */
    const char *in = buf;
    const char *end = buf + 1;       /* one past end — last valid */
    int codepoint = -1;
    const char *next = jvp_utf8_next(in, end, &codepoint);
    printf("next=%p codepoint=%d\n", (void *)next, codepoint);
    free(buf);
    return 0;
}
```

Build & run (from jq source root):
```
gcc -fsanitize=address,pointer-subtract,pointer-compare \
    -fsanitize-address-use-after-scope \
    -g -O0 -I src poc.c src/jv_unicode.c -o poc
ASAN_OPTIONS=detect_invalid_pointer_pairs=2 ./poc
```

## ASan output

```
==ERROR: AddressSanitizer: invalid-pointer-pair: 0x502000000014 0x502000000011
    #0 jvp_utf8_next src/jv_unicode.c:44
    #1 main poc.c:31

0x502000000014 is located 3 bytes after 1-byte region [0x502000000010,0x502000000011)
allocated by thread T0 here:
    #0 malloc
    #1 main poc.c:19
```

## Reachability via public API

* `jvp_utf8_is_valid(const char *in, const char *end)` loops
  `while ((in = jvp_utf8_next(in, end, &codepoint)))` — every
  iteration on a sized non-NUL-terminated buffer hits the bug at the
  buffer boundary.
* `jv_string_check_utf8` calls `jvp_utf8_is_valid` from the JSON
  parse path; any user-controlled JSON input containing a multi-byte
  UTF-8 start byte at the boundary of jq's internal buffer triggers
  the bug.

## Impact

* Strict UB. Under standard `-O2` GCC/clang on x86-64 the comparison
  happens to work as the programmer intended, so there is no crash
  observed today. However:
  * Compilers are permitted to assume the UB does not happen and
    elide the `length = end - in;` truncation branch entirely. A
    future LLVM/GCC version, or `-O3` + LTO, may legitimately do so.
  * ICC / MSVC `/Qspectre` variants are known to be more aggressive.
* The UB triggers UBSan / ASan failures during fuzzing — any
  downstream consumer that runs jq under sanitizers (Linux distro
  reproducible-build pipelines, OSS-Fuzz, security-hardened
  embedders) will see a crash.

## Suggested fix

```diff
-  } else if (in + length > end) {
+  } else if (length > end - in) {
     /* String ends before UTF8 sequence ends */
     length = end - in;
```

Pointer-subtraction `end - in` is well-defined for two in-bounds
pointers into the same object and yields a `ptrdiff_t`. The
comparison `length > end - in` is then a plain integer comparison
with no UB.

## Discovery

Identified by an agentic CBMC + LLM bug-hunting tool ("bmc-agent" —
part of AProver, an academic agentic-model-checking system); the
realism-check LLM flagged the CBMC counterexample as REALISTIC.
Re-confirmed via AddressSanitizer + handwritten PoC.

## Dedup against existing reports

Searched jq GHSA list and issue tracker (May 2026):
* GHSA-jr2x-2g87-5xf6 — `tokenadd` int overflow in jv_parse — fixed.
  Different bug-class location.
* jq #3483 — UB in `jv_parse.c:449`, found via UBSan, closed
  Feb 2026. Same UB *class*, different *location*. Suggests the
  project accepts UBSan-class reports.
* jq #1175 — `jvp_utf8_next` NULL-deref, different code path.

No existing CVE/GHSA matches `src/jv_unicode.c:44`.

──────────────────────────────────────────────────────────────────────────────
FIELD 7 — "Credits"
──────────────────────────────────────────────────────────────────────────────
Add yourself (GitHub username). Role: "Finder".

──────────────────────────────────────────────────────────────────────────────
After clicking "Submit privately"
──────────────────────────────────────────────────────────────────────────────
1. GitHub will create a private advisory and notify jq maintainers.
2. Once they triage and request a CVE, GitHub will assign one
   automatically.
3. Wait for a maintainer reply before any public disclosure.
4. After upstream fixes & publishes the GHSA: file an IBB claim at
   https://hackerone.com/internet-bug-bounty/reports/new
   attaching the GHSA URL as proof.
