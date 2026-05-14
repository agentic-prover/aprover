# Title

xmlBufGrowInternal: NULL pointer arithmetic at buf.c:476 when called on a detached XML_BUFFER_ALLOC_IO buffer

# Body

## Summary

`xmlBufGrowInternal` performs a pointer subtraction `buf->content - buf->contentIO` at `buf.c:476` while the guard at line 475 only checks `buf->contentIO != NULL`. If `buf->content` is `NULL` and `buf->contentIO` is non-NULL, the subtraction is undefined behaviour (C11 §6.5.6) and crashes with SIGSEGV in practice (reproduced under ASan).

`xmlBufDetach` (`buf.c:716-733`) is the canonical way to leave a buffer in exactly that state: it clears `buf->content` to `NULL` but does not touch `buf->contentIO`. So any post-detach call into the grow path triggers UB.

This is not reachable through libxml2's own parser surface — every in-tree caller of `xmlBufDetach` (6 sites in `tree.c`) follows the pattern `create → fill → detach → free`, with detach being the last functional call before the buffer is freed. **This report is therefore not a security advisory**; it is a latent-UB / API-hygiene report. External library users who call `xmlBufGrow` (or any grow-path API) on a buffer they have previously detached will hit it.

## Affected code

```c
// buf.c:475-486 — xmlBufGrowInternal
if ((buf->alloc == XML_BUFFER_ALLOC_IO) && (buf->contentIO != NULL)) {
    size_t start_buf = buf->content - buf->contentIO;   // line 476: NULL - non-NULL

    newbuf = (xmlChar *) xmlRealloc(buf->contentIO, start_buf + size);
    if (newbuf == NULL) {
        xmlBufMemoryError(buf, "growing buffer");
        return(0);
    }
    buf->contentIO = newbuf;
    buf->content = newbuf + start_buf;
}
```

and the producer of the inconsistent state:

```c
// buf.c:716-733 (abbreviated) — xmlBufDetach
xmlBufDetach(xmlBufPtr buf) {
    ...
    ret = buf->content;
    buf->content = NULL;       // cleared
    // buf->contentIO not touched — retains prior value
    buf->size = 0;
    buf->use = 0;
    return ret;
}
```

After `xmlBufDetach` on a buffer originally created with `alloc == XML_BUFFER_ALLOC_IO` and a real `contentIO` allocation, the buffer is left in `(content=NULL, contentIO=non-NULL, alloc=XML_BUFFER_ALLOC_IO)`. The next `xmlBufGrow(buf, len)` hits the guarded branch at line 475 and computes `NULL - contentIO` at line 476.

## Reproducer

Compile with ASan + `detect_invalid_pointer_pairs=2`. A minimal external-user program that calls `xmlBufGrow` after `xmlBufDetach` on an `XML_BUFFER_ALLOC_IO` buffer crashes at the pointer subtract instruction. (Happy to attach the standalone reproducer if useful — omitting it from this issue to keep the report focused.)

## Suggested fix

Either:

1. Make `xmlBufDetach` also clear `contentIO` to keep the `content==NULL ⇔ contentIO==NULL` invariant:

   ```diff
        ret = buf->content;
        buf->content = NULL;
   +    buf->contentIO = NULL;
        buf->size = 0;
        buf->use = 0;
   ```

   This is the closest match for the existing intent of `xmlBufDetach` — the buffer is effectively empty after detach, so all storage pointers should be NULL.

2. Tighten the guard at `buf.c:475` to also require `buf->content != NULL`:

   ```diff
   -    if ((buf->alloc == XML_BUFFER_ALLOC_IO) && (buf->contentIO != NULL)) {
   +    if ((buf->alloc == XML_BUFFER_ALLOC_IO) && (buf->contentIO != NULL) && (buf->content != NULL)) {
   ```

   This is defensive in depth but doesn't address the deeper invariant violation produced by `xmlBufDetach`.

I'd lean toward (1), since the post-detach state is the actual bug. Happy to send a small MR with either patch if there's a preference.

## Discovery context

Found by an experimental BMC-based agent (bounded model checker + dynamic-validation harness) during a sweep of `buf.c`. CBMC produced the pointer-arithmetic counterexample under harness-allowed parameter states; an automatically-generated dynamic reproducer compiled and ran the witness and caught SIGSEGV at the pointer subtraction. Reachability was then audited by hand: all 6 in-tree callers of `xmlBufDetach` go directly to `xmlBufFree`, so there is no parser-surface path. Reporting it nonetheless as defensive UB hardening with a one-line fix.
