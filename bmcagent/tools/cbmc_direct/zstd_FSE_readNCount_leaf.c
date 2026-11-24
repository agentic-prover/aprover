/* Leaf-component entry harness: FSE_readNCount(normalizedCounter, &maxSV,
 * &tableLog, headerBuffer, hbSize) — a self-contained (buf,len) routine that
 * parses an FSE normalized-count header from zstd's entropy decoder. It only
 * reads from headerBuffer and writes into caller-provided output buffers, with
 * no big context struct. The tractable end of the spectrum. */
#include <stddef.h>
#define FSE_STATIC_LINKING_ONLY  /* for FSE_MAX_SYMBOL_VALUE */
#include "fse.h"
#ifndef MAXLEN
#define MAXLEN 8
#endif
void cbmc_entry(void) {
    unsigned char data[MAXLEN];
    __CPROVER_size_t size;
    __CPROVER_assume(size <= MAXLEN);

    /* Output buffers, sized for the maximum symbol count (FSE_MAX_SYMBOL_VALUE). */
    short normalizedCounter[FSE_MAX_SYMBOL_VALUE + 1];
    unsigned maxSymbolValue = FSE_MAX_SYMBOL_VALUE;
    unsigned tableLog = 0;

    FSE_readNCount(normalizedCounter, &maxSymbolValue, &tableLog,
                   (const void *)data, (size_t)size);
}
