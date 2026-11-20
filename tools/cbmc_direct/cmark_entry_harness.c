/* Direct CBMC entry-point harness for cmark — parse-only (the recursive
 * HTML renderer is dropped to keep BMC tractable). Drives the real attack
 * surface cmark_parse_document(data,size). Any CEx is concrete input bytes. */
#include <stdlib.h>
#include "cmark.h"
#ifndef MAXLEN
#define MAXLEN 4
#endif
void cbmc_entry(void) {
    unsigned char data[MAXLEN];
    __CPROVER_size_t size;
    __CPROVER_assume(size <= MAXLEN);
    cmark_node *doc = cmark_parse_document((const char *)data, size, 0);
    if (doc) cmark_node_free(doc);
}
