/* ASan replay driver mirroring expat_utf8_toUtf16_leaf.c (utf8_toUtf16).
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

  /* One UTF-16 unit per input byte is a safe upper bound. */
  unsigned short *out = (unsigned short *)malloc((got ? got : 1) * sizeof(unsigned short));

  const char *from = (const char *)buf;
  const char *fromLim = (const char *)buf + got;
  unsigned short *to = out;
  const unsigned short *toLim = out + got;

  utf8_toUtf16(&utf8_encoding.enc, &from, fromLim, &to, toLim);

  free(out);
  free(buf);
  return 0;
}
