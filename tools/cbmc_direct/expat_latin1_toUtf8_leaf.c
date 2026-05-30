/* Leaf-component entry harness: expat latin1_toUtf8(enc, &from, fromLim, &to, toLim)
 * from xmltok.c. A self-contained (ptr,end)-range routine that converts a
 * Latin-1 byte range to UTF-8, emitting 1 or 2 output bytes per input byte and
 * stopping on output exhaustion.
 *
 * The routine ignores its ENCODING* argument (UNUSED_P(enc)), so passing NULL is
 * sound. We include xmltok.c so the *exact* library source is verified.
 *
 * Fully concrete, no stubs. */
#include <stdlib.h>
#include "xmltok.c"

#ifndef MAXLEN
#define MAXLEN 8
#endif

void cbmc_entry(void) {
  unsigned char data[MAXLEN];
  __CPROVER_size_t size;
  __CPROVER_assume(size <= MAXLEN);

  /* Worst case is 2 output bytes per input byte; use an independent nondet
   * output bound (clamped to 2*MAXLEN) so both the completed and
   * output-exhausted branches are reachable. */
  char out[2 * MAXLEN];
  __CPROVER_size_t outsize;
  __CPROVER_assume(outsize <= 2 * MAXLEN);

  const char *from = (const char *)data;
  const char *fromLim = (const char *)data + size;
  char *to = out;
  const char *toLim = out + outsize;

  latin1_toUtf8(NULL, &from, fromLim, &to, toLim);
}
