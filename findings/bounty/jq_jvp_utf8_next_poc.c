/* PoC for jq jvp_utf8_next pointer-arithmetic UB
 *
 * Trigger: pass a buffer containing a 4-byte UTF-8 start byte at the
 * END of the buffer. jvp_utf8_next computes `in + length > end` where
 * length=4 but only 1 byte remains, forming a pointer 3 bytes past
 * the end of the underlying object. Per C11 §6.5.6/8, forming such a
 * pointer is undefined behavior (only one-past-end is allowed).
 *
 * Compile with: gcc -fsanitize=undefined -o jq_ubsan_poc jq_ubsan_poc.c \
 *                jv_unicode.o (or include the source)
 */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "jv_unicode.h"

int main(void) {
    /* Tightly-sized buffer: exactly 1 byte, no slack, no NUL. */
    char *buf = malloc(1);
    if (!buf) return 1;
    buf[0] = (char)0xF0;   /* 4-byte UTF-8 start byte (U+10000 range) */

    const char *in = buf;
    const char *end = buf + 1;       /* one past the end — the only valid past-end pointer */
    int codepoint = -1;

    /* This call internally computes `in + 4 > end` — `in + 4` is
     * three bytes past the buffer's allocated extent. Forming such a
     * pointer for ANY purpose is UB.
     */
    const char *next = jvp_utf8_next(in, end, &codepoint);
    printf("next=%p codepoint=%d\n", (void *)next, codepoint);
    free(buf);
    return 0;
}
