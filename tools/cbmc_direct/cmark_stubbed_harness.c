/* CEGAR-style entry harness: drive cmark_parse_document (real block-structure
 * parser, concrete) but ABSTRACT the heavy inline/entity machinery as
 * UNDER-approximating stubs (no-op / return-0). Under-approx stubs add NO
 * false positives (they just explore fewer behaviours); refinement = un-stub
 * a callee to gain coverage. Any CEx here is concrete input bytes, replayable
 * through the REAL un-stubbed program under ASan = sound refinement oracle. */
#include <stdlib.h>
#include "cmark.h"
#include "parser.h"
#include "node.h"
#include "references.h"
#include "inlines.h"
#include "houdini.h"
#include "buffer.h"

/* --- abstraction stubs (cut the blowup: inlines.c + entities.inc table) --- */
void cmark_parse_inlines(cmark_mem *mem, cmark_node *parent,
                         cmark_reference_map *refmap, int options) { (void)mem;(void)parent;(void)refmap;(void)options; }
bufsize_t cmark_parse_reference_inline(cmark_mem *mem, cmark_chunk *input,
                                       cmark_reference_map *refmap) { (void)mem;(void)input;(void)refmap; return 0; }
void houdini_unescape_html_f(cmark_strbuf *ob, const uint8_t *src, bufsize_t size) { (void)ob;(void)src;(void)size; }

#ifndef MAXLEN
#define MAXLEN 8
#endif
void cbmc_entry(void) {
    unsigned char data[MAXLEN];
    __CPROVER_size_t size;
    __CPROVER_assume(size <= MAXLEN);
    cmark_node *doc = cmark_parse_document((const char *)data, size, 0);
    if (doc) cmark_node_free(doc);
}
