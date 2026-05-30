/* Leaf-component entry harness: HUF_readStats(huffWeight, hwSize, rankStats,
 * &nbSymbols, &tableLog, src, srcSize) — a self-contained (buf,len) routine
 * that parses a Huffman-table weight header from zstd's entropy decoder. It
 * reads from src and writes into caller-provided output buffers (an internal
 * workspace is stack-allocated by HUF_readStats itself); no big context struct.
 *
 * Note: in the FSE-compressed-header path HUF_readStats calls
 * FSE_decompress_wksp_bmi2. Under the LIGHT CBMC config (fse_decompress.c is
 * NOT linked) that becomes a nondet stub — fine, it cannot corrupt our buffers
 * since its outputs are bounds-checked afterwards. The replay build links the
 * real fse_decompress.c. */
#include <stddef.h>
#include "huf.h"
#ifndef MAXLEN
#define MAXLEN 8
#endif
void cbmc_entry(void) {
    unsigned char data[MAXLEN];
    __CPROVER_size_t size;
    __CPROVER_assume(size <= MAXLEN);

    /* Output buffers, sized as in real callers. */
    unsigned char huffWeight[HUF_SYMBOLVALUE_MAX + 1];
    unsigned int  rankStats[HUF_TABLELOG_MAX + 1];
    unsigned int  nbSymbols = 0;
    unsigned int  tableLog  = 0;

    HUF_readStats(huffWeight, HUF_SYMBOLVALUE_MAX + 1, rankStats,
                  &nbSymbols, &tableLog, (const void *)data, (size_t)size);
}
