/* ASan replay driver mirroring expat_utf8_toUtf8_leaf.c (utf8_toUtf8).
 * A crash here on a concretized CBMC counterexample = confirmed real bug + PoC. */
#include <stdio.h>
#include <stdlib.h>
#include "xmltok.c"

int main(int argc, char **argv) {
  if (argc < 2) { fprintf(stderr, "usage: %s input\n", argv[0]); return 2; }
  FILE *f = fopen(argv[1], "rb"); if (!f) { perror("fopen"); return 2; }
  fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
  if (n < 0) return 2;
  unsigned char *buf = (unsigned char *)malloc(n ? n : 1);
  size_t got = fread(buf, 1, n, f); fclose(f);

  /* Output buffer same size as input is the worst case for a copy. */
  char *out = (char *)malloc(got ? got : 1);

  const char *from = (const char *)buf;
  const char *fromLim = (const char *)buf + got;
  char *to = out;
  const char *toLim = out + got;

  utf8_toUtf8(NULL, &from, fromLim, &to, toLim);

  free(out);
  free(buf);
  return 0;
}
