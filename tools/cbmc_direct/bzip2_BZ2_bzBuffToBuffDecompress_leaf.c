/* Leaf-component entry harness: BZ2_bzBuffToBuffDecompress(dest, destLen,
 * source, sourceLen, small, verbosity) — the one-shot bzip2 decompression API.
 * It takes a (source, sourceLen) compressed input buffer and writes into a
 * bounded (dest, destLen) output buffer. A real (buf,len) attack surface.
 * Fully concrete (uses bzlib's default malloc/free allocator), no stubs. */
#include <stdlib.h>
#include "bzlib.h"
#ifndef MAXLEN
#define MAXLEN 6
#endif
void cbmc_entry(void) {
    unsigned char data[MAXLEN];
    __CPROVER_size_t size;
    __CPROVER_assume(size <= MAXLEN);
    char dest[64];
    unsigned int destLen = 64;
    BZ2_bzBuffToBuffDecompress(dest, &destLen,
                               (char *)data, (unsigned int)size,
                               0 /*small*/, 0 /*verbosity*/);
}
