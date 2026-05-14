# jq 1.8.1 — jvp_utf8_backtrack pointer-arithmetic UB

**Status**: confirmed via AddressSanitizer.
**Target**: jq 1.8.1 (also affects current `master`).
**File**: `src/jv_unicode.c`
**Function**: `jvp_utf8_backtrack`
**Line**: 18 (loop header)
**Bug class**: pointer-arithmetic UB per C11 §6.5.6/8 (forming a
pointer one-before-begin of an object) plus §6.5.8/5 (relational
comparison of an out-of-range pointer).
**CWE**: CWE-823 (Use of Out-of-range Pointer Offset).

## Relation to GHSA-ggc9-rpv2-xgpm

Sibling of the previously-reported `jvp_utf8_next` UB
(`jv_unicode.c:44`, also in this file). Both arise from the same
pattern: pointer arithmetic that forms a pointer outside the bounds
of the input buffer. This finding is the *backwards-scan* variant.

## Code

```c
const char* jvp_utf8_backtrack(const char* start, const char* min, int *missing_bytes) {
  assert(min <= start);
  if (min == start) {
    return min;
  }
  int length = 0;
  int seen = 1;
  while (start >= min && (length = utf8_coding_length[(unsigned char)*start]) == UTF8_CONTINUATION_BYTE) {
    start--;       // <-- when start == min, this forms min-1 (UB)
    seen++;
  }
  // next iteration's `start >= min` then compares min-1 to min (also UB)
  ...
}
```

When every byte from `start` down to `min` (inclusive) is a UTF-8
continuation byte (`0x80`–`0xBF`), the loop reaches `start == min`,
reads `*min` (still in-bounds), evaluates `(*min & 0xC0) == 0x80` as
true, then executes `start--`. That decrement forms the pointer
`min - 1`, which is undefined behavior:

* §6.5.6/8: "If both the pointer operand and the result point to
  elements of the same array object, or one past the last element of
  the array object, the evaluation shall not produce an overflow;
  otherwise, the behavior is undefined." `min - 1` points neither
  inside the array nor to one-past-end.
* §6.5.8/5: the next loop check `start >= min` then performs a
  relational comparison between two pointers that do not point into
  the same array object — undefined behavior.

## Reproducer

```c
#include <stdio.h>
#include <stdlib.h>
#include "jv_unicode.h"

int main(void) {
    char *buf = malloc(2);
    if (!buf) return 1;
    buf[0] = (char)0x80;
    buf[1] = (char)0x80;
    const char *start = buf + 1;
    const char *min   = buf;
    int missing = 0;
    const char *r = jvp_utf8_backtrack(start, min, &missing);
    printf("r=%p missing=%d\n", (const void *)r, missing);
    free(buf);
    return 0;
}
```

## Confirmation

```
$ gcc -fsanitize=address,pointer-subtract,pointer-compare \
      -fsanitize-address-use-after-scope \
      -g -O0 -I src poc.c src/jv_unicode.c -o poc
$ ASAN_OPTIONS=detect_invalid_pointer_pairs=2 ./poc

==ERROR: AddressSanitizer: invalid-pointer-pair: 0x50200000000f 0x502000000010
    #0 jvp_utf8_backtrack src/jv_unicode.c:18
    #1 main poc.c:40

0x50200000000f is located 1 bytes before 2-byte region [0x502000000010,0x502000000012)
allocated by thread T0 here:
    #0 malloc
    #1 main poc.c:31
```

The compared pair `(min-1, min)` violates the in-bounds requirement
for `<`, `>=` per C11 §6.5.8/5. Equivalently, the formation of
`min - 1` by the prior `start--` violates §6.5.6/8.

## Reachability via public API

Three direct call sites in jq pass a buffer-end pointer and buffer
start to this function — all reachable from public input:

* **`src/jv_file.c:53`** — file-input path:
  ```c
  if (jvp_utf8_backtrack(buf + (n - 1), buf, &len) && len > 0 && ...)
  ```
  Triggered by any input file whose **first `n ≤ 4` bytes** (where
  `n` is the count of bytes read into the 4096-byte buffer) are all
  UTF-8 continuation bytes. Easy to trigger:
  ```
  printf '\x80\x80' > /tmp/x
  jq -R . /tmp/x        # raw mode
  jq . /tmp/x           # json mode (lexer rejects, but jvp_utf8_backtrack still runs)
  ```

* **`src/jv_print.c:417`** —
  ```c
  const char *s = jvp_utf8_backtrack(outbuf + bufsize - 4, outbuf, NULL);
  ```
  Internal output buffer; same UB shape if the 4-byte tail is all
  continuation bytes.

* **`src/builtin.c:1321`** — string trim builtin:
  ```c
  const char *ns = jvp_utf8_backtrack(trim_end - 1, trim_start, NULL);
  ```
  Reachable from `.lstrip` / `.rstrip` / `.gsub` style filters on
  user-controlled strings.

## Severity

Low-to-medium per practical impact — same considerations as the
sibling `jvp_utf8_next` finding:

* Strict UB. Under standard `-O2` on x86-64 the pointer comparison
  happens to behave as the programmer intended, so no observed crash
  in stock builds today. ASan / UBSan immediately abort on triggering
  workloads.
* Compilers may legitimately exploit the UB (assume `start--` only
  occurs when `start > min`, then elide the subsequent guard).
* Triggers UBSan/ASan crashes during fuzzing — affects downstream
  consumers running jq under sanitizers (Linux distro reproducible
  builds, OSS-Fuzz, hardened embedders).

## Suggested fix

Restructure the loop to never form `start = min - 1`:

```diff
-  int length = 0;
-  int seen = 1;
-  while (start >= min && (length = utf8_coding_length[(unsigned char)*start]) == UTF8_CONTINUATION_BYTE) {
-    start--;
-    seen++;
-  }
+  int length = 0;
+  int seen = 1;
+  for (;;) {
+    length = utf8_coding_length[(unsigned char)*start];
+    if (length != UTF8_CONTINUATION_BYTE) break;
+    if (start == min) {
+      /* All bytes [min .. orig_start] are continuation bytes — no
+         leading byte found within the window. */
+      return NULL;
+    }
+    start--;
+    seen++;
+  }
```

This reads `*start`, decides whether to step back, and only decrements
when the next position is still in-bounds. No UB is formed.

## Discovery

Identified by bmc-agent's lookalike sweep (CBMC `pointer_arithmetic.9`
property fired; dynamic-validation reproducer aborted with SIGABRT;
hand-written PoC reproduced under AddressSanitizer with
`detect_invalid_pointer_pairs=2`).

## Reporting

Adding to the existing private advisory **GHSA-ggc9-rpv2-xgpm** as a
related finding (same file, same UB class, same fix-class).

PoC source: `findings/bounty/jq_jvp_utf8_backtrack_poc.c`.
