/*
 * memory_allocator.c — Simple bump allocator
 *
 * A small self-contained example for GRACE evaluation (Phase 4).
 *
 * Intentional bug:
 *   alloc_free() does not check whether `ptr` is NULL before dereferencing
 *   it to set the freed flag.  Passing NULL causes a null-pointer dereference.
 */

#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* Data structures                                                      */
/* ------------------------------------------------------------------ */

#define ALLOC_ALIGN 8   /* alignment in bytes */

/* Header prepended to each allocation */
typedef struct alloc_header {
    size_t size;         /* allocation size (excluding header) */
    int    freed;        /* 1 if this block has been freed     */
} alloc_header_t;

typedef struct {
    uint8_t *pool;       /* backing memory pool                */
    size_t   pool_size;  /* total pool size in bytes           */
    size_t   used;       /* bytes already allocated (incl. headers) */
    int      initialized;
} bump_allocator_t;

/* Error codes */
#define ALLOC_OK         0
#define ALLOC_ERR_NULL  -1
#define ALLOC_ERR_OOM   -2
#define ALLOC_ERR_INIT  -3

/* ------------------------------------------------------------------ */
/* Lifecycle                                                           */
/* ------------------------------------------------------------------ */

/*
 * alloc_init — initialise the bump allocator with a backing pool.
 *
 * Precondition:  alloc != NULL, pool != NULL, size >= sizeof(alloc_header_t)
 * Postcondition: alloc->initialized == 1, alloc->used == 0
 */
int alloc_init(bump_allocator_t *alloc, uint8_t *pool, size_t size)
{
    if (alloc == NULL || pool == NULL)
        return ALLOC_ERR_NULL;
    if (size < sizeof(alloc_header_t))
        return ALLOC_ERR_OOM;

    alloc->pool        = pool;
    alloc->pool_size   = size;
    alloc->used        = 0;
    alloc->initialized = 1;
    return ALLOC_OK;
}

/*
 * alloc_reset — reset the allocator, freeing all allocations.
 *
 * Precondition:  alloc != NULL, alloc->initialized == 1
 * Postcondition: alloc->used == 0
 */
int alloc_reset(bump_allocator_t *alloc)
{
    if (alloc == NULL)
        return ALLOC_ERR_NULL;
    if (!alloc->initialized)
        return ALLOC_ERR_INIT;

    alloc->used = 0;
    return ALLOC_OK;
}

/*
 * alloc_available — return the number of bytes still available.
 *
 * Precondition:  alloc != NULL, alloc->initialized == 1
 * Postcondition: return value == alloc->pool_size - alloc->used
 */
size_t alloc_available(const bump_allocator_t *alloc)
{
    if (alloc == NULL || !alloc->initialized)
        return 0;
    if (alloc->used >= alloc->pool_size)
        return 0;
    return alloc->pool_size - alloc->used;
}

/* ------------------------------------------------------------------ */
/* Allocation / deallocation                                           */
/* ------------------------------------------------------------------ */

/*
 * alloc_malloc — allocate `size` bytes, returning a pointer.
 *
 * Precondition:  alloc != NULL, alloc->initialized == 1, size > 0,
 *                alloc_available(alloc) >= sizeof(alloc_header_t) + size
 * Postcondition: returned pointer is non-NULL and within the pool,
 *                alloc->used increases by (sizeof(alloc_header_t) + aligned_size)
 */
void *alloc_malloc(bump_allocator_t *alloc, size_t size)
{
    if (alloc == NULL || !alloc->initialized)
        return NULL;
    if (size == 0)
        return NULL;

    /* Align size to ALLOC_ALIGN */
    size_t aligned = (size + ALLOC_ALIGN - 1) & ~(size_t)(ALLOC_ALIGN - 1);
    size_t total   = sizeof(alloc_header_t) + aligned;

    if (alloc->used + total > alloc->pool_size)
        return NULL;   /* out of memory */

    alloc_header_t *hdr = (alloc_header_t *)(alloc->pool + alloc->used);
    hdr->size  = aligned;
    hdr->freed = 0;
    alloc->used += total;

    return (void *)(hdr + 1);
}

/*
 * alloc_free — mark an allocated block as freed.
 *
 * Note: bump allocators do not reclaim memory; this just records the block
 * as freed for accounting purposes.
 *
 * Precondition:  ptr != NULL (i.e. a valid pointer returned by alloc_malloc)
 * Postcondition: the header preceding ptr has freed == 1
 *
 * BUG: missing NULL check before dereferencing ptr.
 *      If ptr == NULL, subtracting sizeof(alloc_header_t) gives an
 *      invalid address and the subsequent write is a null-pointer dereference.
 */
int alloc_free(void *ptr)
{
    /* BUG: no NULL check — null-pointer dereference when ptr == NULL */
    alloc_header_t *hdr = (alloc_header_t *)ptr - 1;   /* <-- intentional bug */
    hdr->freed = 1;
    return ALLOC_OK;
}
