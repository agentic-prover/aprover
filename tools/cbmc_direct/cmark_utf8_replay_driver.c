/* ASan replay driver mirroring cmark_utf8_leaf.c (cmark_utf8proc_check).
 * A crash here on a concretized CBMC counterexample = confirmed real bug + PoC. */
#include <stdio.h>
#include <stdlib.h>
#include "cmark.h"
#include "buffer.h"
#include "utf8.h"
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s input\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb"); if (!f) { perror("fopen"); return 2; }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    if (n < 0) return 2;
    unsigned char *buf = (unsigned char *)malloc(n ? n : 1);
    size_t got = fread(buf, 1, n, f); fclose(f);
    cmark_mem *mem = cmark_get_default_mem_allocator();
    cmark_strbuf b; cmark_strbuf_init(mem, &b, 0);
    cmark_utf8proc_check(&b, (const uint8_t *)buf, (bufsize_t)got);
    cmark_strbuf_free(&b);
    free(buf);
    return 0;
}
