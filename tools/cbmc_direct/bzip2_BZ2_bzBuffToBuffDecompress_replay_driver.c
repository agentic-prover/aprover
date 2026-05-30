/* ASan replay driver mirroring bzip2_BZ2_bzBuffToBuffDecompress_leaf.c.
 * A crash here on a concretized CBMC counterexample = confirmed real bug + PoC.
 * Reads the input file as the compressed `source` buffer and decompresses it
 * into a bounded `dest` buffer. */
#include <stdio.h>
#include <stdlib.h>
#include "bzlib.h"
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s input\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb"); if (!f) { perror("fopen"); return 2; }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    if (n < 0) return 2;
    unsigned char *buf = (unsigned char *)malloc(n ? n : 1);
    size_t got = fread(buf, 1, n, f); fclose(f);
    char dest[64];
    unsigned int destLen = 64;
    BZ2_bzBuffToBuffDecompress(dest, &destLen,
                               (char *)buf, (unsigned int)got,
                               0 /*small*/, 0 /*verbosity*/);
    free(buf);
    return 0;
}
