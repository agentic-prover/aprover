/* ASan replay driver mirroring zstd_FSE_readNCount_leaf.c (FSE_readNCount).
 * A crash here on a concretized CBMC counterexample = confirmed real bug + PoC. */
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#define FSE_STATIC_LINKING_ONLY  /* for FSE_MAX_SYMBOL_VALUE */
#include "fse.h"
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s input\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb"); if (!f) { perror("fopen"); return 2; }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    if (n < 0) return 2;
    unsigned char *buf = (unsigned char *)malloc(n ? n : 1);
    size_t got = fread(buf, 1, n, f); fclose(f);

    short normalizedCounter[FSE_MAX_SYMBOL_VALUE + 1];
    unsigned maxSymbolValue = FSE_MAX_SYMBOL_VALUE;
    unsigned tableLog = 0;
    FSE_readNCount(normalizedCounter, &maxSymbolValue, &tableLog,
                   (const void *)buf, got);
    free(buf);
    return 0;
}
