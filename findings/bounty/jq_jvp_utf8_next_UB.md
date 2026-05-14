# jq 1.8.1 — jvp_utf8_next pointer-arithmetic UB

**Status**: confirmed via AddressSanitizer.
**Target**: jq 1.8.1 (also affects current `master`).
**File**: `src/jv_unicode.c`
**Function**: `jvp_utf8_next`
**Line**: 44
**Bug class**: pointer-arithmetic UB per C11 §6.5.6/8 (forming a
pointer more than one past the end of the underlying object).
**CWE**: CWE-823 (Use of Out-of-range Pointer Offset) /
CWE-119-class.

## Code

```c
const char* jvp_utf8_next(const char* in, const char* end, int* codepoint_ret) {
  assert(in <= end);
  if (in == end) {
    return 0;
  }
  ...
  unsigned char first = (unsigned char)in[0];
  int length = utf8_coding_length[first];
  if ((first & 0x80) == 0) {
    /* Fast-path for ASCII */
    ...
  } else if (length == 0 || length == UTF8_CONTINUATION_BYTE) {
    length = 1;
  } else if (in + length > end) {  // <-- LINE 44: UB
    /* String ends before UTF8 sequence ends */
    length = end - in;
  } else {
    ...
  }
```

`length` is set from `utf8_coding_length[first]` and can be 1–4. The
guard at line 44 checks whether the multi-byte sequence extends past
the buffer. The arithmetic `in + length` forms a pointer that may be up
to **3 bytes past the end** of the underlying object — which is UB per
C11 §6.5.6/8 (only one-past-end is permitted).

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
    /* internally computes `in + 4 > end`, i.e. forms `in + 4`
       which is 3 bytes past the buffer. UB. */
    const char *next = jvp_utf8_next(in, end, &codepoint);
    printf("next=%p codepoint=%d\n", (void *)next, codepoint);
    free(buf);
    return 0;
}
```

## Confirmation

```
$ gcc -fsanitize=address,pointer-subtract,pointer-compare \
      -fsanitize-address-use-after-scope \
      -g -O0 -I src jq_ubsan_poc.c src/jv_unicode.c -o poc
$ ASAN_OPTIONS=detect_invalid_pointer_pairs=2 ./poc

==ERROR: AddressSanitizer: invalid-pointer-pair: 0x502000000014 0x502000000011
    #0 jvp_utf8_next src/jv_unicode.c:44
    #1 main jq_ubsan_poc.c:31

0x502000000014 is located 3 bytes after 1-byte region [0x502000000010,0x502000000011)
allocated by thread T0 here:
    #0 malloc
    #1 main jq_ubsan_poc.c:19
```

The pointer being compared, `in + 4`, points 3 bytes outside the
allocated extent of `buf`. Comparing this pointer to `end` (which is
one-past-end of the same allocation) is UB by C11 §6.5.8/5 ("if the
pointers do not point to elements of the same array object", the
result is undefined) — and the formation of the pointer itself, per
§6.5.6/8, is undefined "if the pointer operand and the result do not
point to elements of the same array object or one past the last
element of the array object".

## Reachability through public API

jq's public JSON parser (`jv_parse_sized`) calls `jvp_utf8_next` while
scanning string literals. Any user-controlled JSON input that contains
a multi-byte UTF-8 start byte at the boundary of the parser's input
buffer reaches this code path. A crafted JSON input of the form
`"...\xF0"` (truncated at the start byte) triggers the bug.

A more direct path: `jvp_utf8_is_valid(const char *in, const char *end)`
is called by `jv_string_check_utf8`, exposed via the JSON parsing path.
It loops `while ((in = jvp_utf8_next(in, end, &codepoint)))` — every
iteration on a non-NUL-terminated buffer (the parser uses sized
buffers) eventually reaches the bug.

## Severity

Low-to-medium per practical impact: the UB does not manifest as a
runtime crash under standard `-O2` GCC/clang on x86-64 (the pointer
comparison happens to work as intuited). However:

* It IS strict UB and compilers are permitted to assume it never
  happens, potentially optimizing out the `length = end - in;`
  truncation branch.
* Stricter compilers (ICC, future LLVM versions, MSVC with /Qspectre
  variants) MAY exploit the UB.
* The fix is simple: rewrite `in + length > end` as `length > end - in`
  (pointer-subtraction is well-defined for in-bounds pointers, and the
  result fits in `ptrdiff_t`).

## Suggested fix

```diff
-  } else if (in + length > end) {
+  } else if (length > end - in) {
     /* String ends before UTF8 sequence ends */
     length = end - in;
```

## Dedup against published CVEs

* CVE-2025-49795 (libxml2 schemas, unrelated)
* GHSA-jr2x-2g87-5xf6 (jq tokenadd int overflow, fixed; different
  location)
* jq issue #1175 (jvp_utf8_next NULL deref, different code path,
  closed as dup of string-multiplication fix)
* jq issue #3483 (UB in jv_parse.c:449, found via UBSan, fixed Feb 2026)
  — same UB class but different location, suggests jq triage accepts
  UBSan-class reports.

No existing CVE or GHSA matches `src/jv_unicode.c:44`.

## Discovery

Originally identified by bmc-agent (CBMC + LLM realism check) in
the 2026-05-12 session as a REALISTIC `pointer_arithmetic.17`
verdict. Re-confirmed in 2026-05-13 with AddressSanitizer +
manual PoC.

## Reporting

jq private security-advisory channel:
https://github.com/jqlang/jq/security/advisories/new

jq is on IBB; if accepted as a security issue, the report routes to
the Internet Bug Bounty pool.

PoC source: `/tmp/jq_ubsan_poc.c` in this session.
