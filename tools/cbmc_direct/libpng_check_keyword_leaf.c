/* Leaf-component entry harness: png_check_keyword(png_ptr, key, new_key) — a
 * self-contained keyword sanitizer. It scans a NUL-terminated C string `key`,
 * copying allowed bytes into the caller-supplied `new_key` buffer (which the
 * libpng contract requires to be 80 bytes) and returns the sanitized length.
 *
 * png_ptr is touched ONLY for png_debug (a no-op without PNG_DEBUG) and for
 * warning emission on truncation/bad-character; those warning functions are
 * stubbed (see libpng_check_keyword_stubs.c) so no initialized png_struct is
 * needed. The buffer-safety logic (bounded writes into new_key) is fully
 * concrete and independent of png_ptr. We feed a nondet, NUL-terminated input
 * and a 80-byte output buffer matching the documented contract. */
#include <stdlib.h>
#include "pngpriv.h"
#ifndef MAXLEN
#define MAXLEN 8
#endif
void cbmc_entry(void) {
    unsigned char data[MAXLEN + 1];
    __CPROVER_size_t size;
    __CPROVER_assume(size <= MAXLEN);
    data[size] = 0;               /* NUL-terminate the key */
    png_byte new_key[80];         /* documented contract: 80 bytes */
    (void)png_check_keyword((png_struct *)0, (const char *)data, new_key);
}
