/* Leaf-component entry harness: cmark_utf8proc_check(dest, line, size) — a
 * self-contained (buf,len) routine (UTF-8 validation/normalization). Fully
 * concrete, no stubs. The tractable end of the spectrum. */
#include <stdlib.h>
#include "cmark.h"
#include "buffer.h"
#include "utf8.h"
#ifndef MAXLEN
#define MAXLEN 8
#endif
void cbmc_entry(void) {
    unsigned char data[MAXLEN];
    __CPROVER_size_t size;
    __CPROVER_assume(size <= MAXLEN);
    cmark_mem *mem = cmark_get_default_mem_allocator();
    cmark_strbuf buf;
    cmark_strbuf_init(mem, &buf, 0);
    cmark_utf8proc_check(&buf, (const uint8_t *)data, (bufsize_t)size);
    cmark_strbuf_free(&buf);
}
