/* PoC for jq jvp_utf8_backtrack pointer-arithmetic UB
 *
 * src/jv_unicode.c:21 — the backwards-scan loop does `start--` past
 * `min` when the buffer ends with continuation bytes, forming a
 * one-before-begin pointer (C11 §6.5.6/8 UB). The next loop-condition
 * check `start >= min` is then a comparison of an out-of-range
 * pointer (C11 §6.5.8/5 UB).
 *
 * Trigger: pass a buffer where every byte from start down to min
 * (inclusive) is a UTF-8 continuation byte (0x80-0xBF).
 *
 * Reachable from jq's public file-input path via
 *   jv_file.c:53  jvp_utf8_backtrack(buf + n - 1, buf, &len)
 * with n ≤ 4 and the read bytes all in 0x80-0xBF.
 *
 * Compile (from jq source root):
 *   gcc -fsanitize=address,pointer-subtract,pointer-compare \
 *       -fsanitize-address-use-after-scope \
 *       -g -O0 -I src jq_backtrack_poc.c src/jv_unicode.c -o poc
 *   ASAN_OPTIONS=detect_invalid_pointer_pairs=2 ./poc
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "jv_unicode.h"

int main(void) {
    /* 2-byte buffer of pure continuation bytes — the smallest input
     * that walks past `min` while keeping the entry assertion
     * (min <= start) satisfied with min != start. */
    char *buf = malloc(2);
    if (!buf) return 1;
    buf[0] = (char)0x80;
    buf[1] = (char)0x80;

    const char *start = buf + 1;   /* last byte of buffer */
    const char *min   = buf;       /* first byte of buffer */
    int missing = 0;

    const char *r = jvp_utf8_backtrack(start, min, &missing);
    printf("r=%p missing=%d\n", (const void *)r, missing);
    free(buf);
    return 0;
}
