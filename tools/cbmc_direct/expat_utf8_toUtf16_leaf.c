/* Leaf-component entry harness: expat utf8_toUtf16(enc, &from, fromLim, &to, toLim)
 * from xmltok.c. A self-contained (ptr,end)-range routine that decodes a UTF-8
 * byte range into UTF-16 code units, dispatching on the encoding's per-byte
 * type table (BT_LEAD2/3/4) and emitting surrogate pairs for 4-byte sequences.
 *
 * Unlike *_toUtf8, this routine *reads* the ENCODING byte-type table via
 * SB_BYTE_TYPE(enc, from). We use the library's own fully-initialized built-in
 * `utf8_encoding` (a static const struct normal_encoding compiled from the real
 * asciitab.h/utf8tab.h), which is exactly what the parser uses at runtime, so
 * the precondition is real and sound.
 *
 * We include xmltok.c so the *exact* library source is verified. No stubs. */
#include <stdlib.h>
#include "xmltok.c"

#ifndef MAXLEN
#define MAXLEN 8
#endif

void cbmc_entry(void) {
  unsigned char data[MAXLEN];
  __CPROVER_size_t size;
  __CPROVER_assume(size <= MAXLEN);

  /* Output is UTF-16 code units; at most one (or a surrogate pair) per input
   * char. Independent nondet bound (clamped to MAXLEN units) so the
   * output-exhausted branches are reachable. */
  unsigned short out[MAXLEN];
  __CPROVER_size_t outsize;
  __CPROVER_assume(outsize <= MAXLEN);

  const char *from = (const char *)data;
  const char *fromLim = (const char *)data + size;
  unsigned short *to = out;
  const unsigned short *toLim = out + outsize;

  utf8_toUtf16(&utf8_encoding.enc, &from, fromLim, &to, toLim);
}
