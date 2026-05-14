# libxml2 — xmlBufGrowInternal NULL-deref pointer arithmetic

**Status**: candidate. Dynamic-harness SIGSEGV confirmed. Realism
audit: REALISTIC, high confidence. Reachability via standard public
API requires user to call `xmlBufGrow` after `xmlBufDetach` — needs
verification before submission.

**File**: `buf.c`
**Function**: `xmlBufGrowInternal`
**Line**: 476 (pointer subtraction)
**Bug class**: NULL pointer arithmetic + undefined behavior;
exploitable as SIGSEGV.
**CWE**: CWE-476 (NULL Pointer Dereference) / CWE-466 (Return of
Pointer Value Outside of Expected Range).

## Code

```c
// buf.c:475-486 — xmlBufGrowInternal
if ((buf->alloc == XML_BUFFER_ALLOC_IO) && (buf->contentIO != NULL)) {
    size_t start_buf = buf->content - buf->contentIO;   // ← line 476: NULL - non-NULL

    newbuf = (xmlChar *) xmlRealloc(buf->contentIO, start_buf + size);
    if (newbuf == NULL) {
        xmlBufMemoryError(buf, "growing buffer");
        return(0);
    }
    buf->contentIO = newbuf;
    buf->content = newbuf + start_buf;
}
```

The guard at line 475 checks `buf->contentIO != NULL` but does NOT
check `buf->content != NULL`. The subtraction at line 476 is
undefined when `buf->content` is `NULL` — and the result `start_buf`
is then passed to `xmlRealloc` and used as a pointer offset.

## How `buf->content == NULL && buf->contentIO != NULL` can arise

`xmlBufDetach` (`buf.c:716–733`) explicitly leaves the buffer in this
state:

```c
xmlBufDetach(xmlBufPtr buf) {
    ...
    if (buf->buffer != NULL)  return(NULL);   // legacy xmlBuffer back-pointer
    if (buf->error)           return(NULL);

    ret = buf->content;
    buf->content = NULL;          // ← cleared
    // buf->contentIO NOT touched ← stays as whatever it was
    buf->size = 0;
    buf->use = 0;
    return ret;
}
```

If the buf was created with `alloc == XML_BUFFER_ALLOC_IO` and
`contentIO` set to a real allocation, then after `xmlBufDetach`:
* `buf->alloc` = XML_BUFFER_ALLOC_IO  (unchanged)
* `buf->contentIO` = non-NULL          (unchanged)
* `buf->content` = NULL                (cleared)

A subsequent `xmlBufGrow(buf, len)` then hits the guarded branch at
line 475 and performs `NULL - contentIO` at line 476 — UB and a
practical SIGSEGV on x86-64.

## Reproduction (dynamic harness)

Auto-generated reproducer compiled with GCC and ran on host; caught
SIGSEGV at the pointer-subtract instruction. Reproduction confirmed
by `bmc-agent`'s dynamic-validation tier.

## Reachability — needs verification

For this to be bounty-tier, we need a public-API path:
1. Create an IO-mode xmlBuf (via `xmlBufCreate` + `xmlBufSetAllocationScheme(XML_BUFFER_ALLOC_IO)`, or via the internal helpers).
2. Call `xmlBufDetach`.
3. Call `xmlBufGrow` (or anything that internally calls `xmlBufGrowInternal`).

Step 3 is the question: do any in-tree callers of `xmlBufGrow` /
`xmlBufAdd` operate on a buf that has been previously `xmlBufDetach`-ed?
If yes, this is exploitable. If `xmlBufDetach` is always the last call
on the buf before `xmlBufFree`, then external library users are the
only way to reach the bug (CWE-617-style API contract violation).

## Suggested fix

Either:
1. Make `xmlBufDetach` also clear `contentIO` to keep the
   `content==NULL ⇔ contentIO==NULL` invariant; or
2. Tighten the guard at line 475 to also require
   `buf->content != NULL`.

Both are one-line fixes.

## Discovery

Found by bmc-agent during a sweep of libxml2/buf.c. CBMC produced a
pointer-arithmetic counterexample; dynamic harness compiled and ran
the witness, caught SIGSEGV. Realism audit accepted the finding as
REALISTIC with high confidence based on `xmlBufDetach`'s clearing
behavior.

## Reachability finding (post-analysis)

All 6 in-tree callers of `xmlBufDetach` (in `tree.c` lines 1403, 1472,
1603, 1673, 5590, 5617, 5642) use the pattern:
```
buf = xmlBufCreate(...);
... fill buf ...
result = xmlBufDetach(buf);
xmlBufFree(buf);   // detach is last functional call
```

The grow-after-detach pattern that triggers the bug is **not present
in-tree**. External libxml2 API users could theoretically construct
it, but that places this finding in the same "API contract violation"
class as jq's `jv_number_negate` reachable-assertion — informational
rather than bounty-tier.

**Recommendation**: file as a non-security bug report with libxml2
upstream (one-line fix: clear `contentIO` in `xmlBufDetach`) but do
NOT submit as IBB-track security advisory.
