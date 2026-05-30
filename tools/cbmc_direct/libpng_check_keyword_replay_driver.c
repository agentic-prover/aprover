/* ASan replay driver mirroring libpng_check_keyword_leaf.c
 * (png_check_keyword). A crash here on a concretized CBMC counterexample =
 * confirmed real bug + PoC. Reads a file as the keyword bytes (NUL-terminated),
 * runs the same sanitizer with the documented 80-byte output buffer. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "pngpriv.h"
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s input\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb"); if (!f) { perror("fopen"); return 2; }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    if (n < 0) return 2;
    char *key = (char *)malloc((size_t)n + 1);
    size_t got = fread(key, 1, (size_t)n, f); fclose(f);
    key[got] = 0;                 /* NUL-terminate the key */
    png_byte new_key[80];         /* documented contract: 80 bytes */
    (void)png_check_keyword((png_struct *)0, key, new_key);
    free(key);
    return 0;
}
