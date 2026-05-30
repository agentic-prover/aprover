/* Leaf-component entry harness: expat utf8_toUtf8(enc, &from, fromLim, &to, toLim)
 * from xmltok.c. A self-contained (ptr,end)-range routine that copies a UTF-8
 * byte range into an output buffer while avoiding splitting multi-byte
 * characters (calls _INTERNAL_trim_to_complete_utf8_characters + memcpy).
 *
 * The routine ignores its ENCODING* argument (UNUSED_P(enc)), so passing NULL is
 * sound. We include xmltok.c so the *exact* library source (static function) is
 * verified — no copy/divergence.
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

  /* Bounded output buffer. UTF-8->UTF-8 is a (clamped) copy, so MAXLEN is
   * enough to exercise both the output-exhausted and copy paths; pick a small
   * independent bound so the toLim<fromLim branch is reachable too. */
  char out[MAXLEN];
  __CPROVER_size_t outsize;
  __CPROVER_assume(outsize <= MAXLEN);

  const char *from = (const char *)data;
  const char *fromLim = (const char *)data + size;
  char *to = out;
  const char *toLim = out + outsize;

  utf8_toUtf8(NULL, &from, fromLim, &to, toLim);
}
