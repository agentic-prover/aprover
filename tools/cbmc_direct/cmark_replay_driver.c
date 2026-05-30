/* ASan replay driver — mirrors cmark_entry_harness.c exactly. Reads a
 * concrete input file (the concretized CBMC counterexample) and runs the
 * same path. A crash/ASan/UBSan report here = confirmed real bug + PoC. */
#include <stdio.h>
#include <stdlib.h>
#include "cmark.h"
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s input\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror("fopen"); return 2; }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    if (n < 0) return 2;
    unsigned char *buf = (unsigned char *)malloc(n ? n : 1);
    size_t got = fread(buf, 1, n, f); fclose(f);
    cmark_node *doc = cmark_parse_document((const char *)buf, got, 0);
    if (doc) {
        char *html = cmark_render_html(doc, 0);
        if (html) free(html);
        cmark_node_free(doc);
    }
    free(buf);
    return 0;
}
