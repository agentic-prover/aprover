/* ASan replay driver mirroring zstd_HUF_readStats_leaf.c (HUF_readStats).
 * A crash here on a concretized CBMC counterexample = confirmed real bug + PoC. */
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include "huf.h"
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s input\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb"); if (!f) { perror("fopen"); return 2; }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    if (n < 0) return 2;
    unsigned char *buf = (unsigned char *)malloc(n ? n : 1);
    size_t got = fread(buf, 1, n, f); fclose(f);

    unsigned char huffWeight[HUF_SYMBOLVALUE_MAX + 1];
    unsigned int  rankStats[HUF_TABLELOG_MAX + 1];
    unsigned int  nbSymbols = 0;
    unsigned int  tableLog  = 0;
    HUF_readStats(huffWeight, HUF_SYMBOLVALUE_MAX + 1, rankStats,
                  &nbSymbols, &tableLog, (const void *)buf, got);
    free(buf);
    return 0;
}
