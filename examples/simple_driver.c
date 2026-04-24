/*
 * simple_driver.c — Ring-buffer character device
 *
 * Call hierarchy (this is what makes GRACE interesting):
 *
 *   dev_open  → rb_init
 *   dev_write → rb_is_full, rb_write
 *   dev_read  → rb_is_empty, rb_read
 *   dev_close → (none)
 *
 * So GRACE must generate specs top-down:
 *   Layer 1: dev_open, dev_write, dev_read, dev_close  (entry points)
 *   Layer 2: rb_init, rb_is_full, rb_write, rb_is_empty, rb_read  (internals)
 *
 * Intentional bug:
 *   rb_write() computes free space as (capacity - count + 1) instead of
 *   (capacity - count), an off-by-one that allows writing one extra byte
 *   when the buffer is exactly full, corrupting rb->count > rb->capacity.
 *
 * Spurious counterexample opportunity:
 *   rb_is_full() has postcondition "returns 1 iff rb->count == rb->capacity".
 *   With a weak precondition (count unconstrained), CBMC can find inputs
 *   where count > capacity — but dev_write() always calls rb_init() first
 *   via dev_open(), so count > capacity is unreachable from any real caller.
 *   GRACE should detect this as spurious and refine the precondition.
 */

#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* Data structures                                                      */
/* ------------------------------------------------------------------ */

typedef struct {
    uint8_t *buf;      /* backing storage                        */
    size_t   capacity; /* total capacity in bytes                */
    size_t   head;     /* write index                            */
    size_t   tail;     /* read index                             */
    size_t   count;    /* bytes currently stored                 */
} ring_buffer_t;

typedef struct {
    ring_buffer_t rb;          /* embedded ring buffer            */
    uint8_t       backing[64]; /* fixed backing store             */
    int           is_open;
} char_dev_t;

/* ------------------------------------------------------------------ */
/* Internal ring-buffer helpers (Layer 2 — called by dev_* functions) */
/* ------------------------------------------------------------------ */

/*
 * rb_init — initialise ring buffer with given capacity.
 * Called by: dev_open
 */
static void rb_init(ring_buffer_t *rb, uint8_t *buf, size_t capacity)
{
    rb->buf      = buf;
    rb->capacity = capacity;
    rb->head     = 0;
    rb->tail     = 0;
    rb->count    = 0;
}

/*
 * rb_is_full — return 1 if no space remains.
 * Called by: dev_write
 */
static int rb_is_full(const ring_buffer_t *rb)
{
    return rb->count == rb->capacity;
}

/*
 * rb_is_empty — return 1 if no data available.
 * Called by: dev_read
 */
static int rb_is_empty(const ring_buffer_t *rb)
{
    return rb->count == 0;
}

/*
 * rb_write — append up to `len` bytes; return bytes written.
 * Called by: dev_write
 *
 * BUG: free = (capacity - count) + 1  should be  (capacity - count).
 *      This lets one extra byte be written when the buffer is full.
 */
static size_t rb_write(ring_buffer_t *rb, const uint8_t *data, size_t len)
{
    size_t free     = (rb->capacity - rb->count) + 1; /* <-- off-by-one bug */
    size_t to_write = (len > free) ? free : len;

    for (size_t i = 0; i < to_write; i++) {
        rb->buf[rb->head] = data[i];
        rb->head = (rb->head + 1) % rb->capacity;
        rb->count++;
    }
    return to_write;
}

/*
 * rb_read — consume up to `len` bytes into `buf`; return bytes read.
 * Called by: dev_read
 */
static size_t rb_read(ring_buffer_t *rb, uint8_t *buf, size_t len)
{
    size_t avail   = rb->count;
    size_t to_read = (len > avail) ? avail : len;

    for (size_t i = 0; i < to_read; i++) {
        buf[i]   = rb->buf[rb->tail];
        rb->tail = (rb->tail + 1) % rb->capacity;
        rb->count--;
    }
    return to_read;
}

/* ------------------------------------------------------------------ */
/* Public device API (Layer 1 — entry points, no callers in this file) */
/* ------------------------------------------------------------------ */

/*
 * dev_open — initialise device and open it.
 * Precondition:  dev != NULL
 * Postcondition: dev->is_open == 1, ring buffer initialised and empty
 */
int dev_open(char_dev_t *dev)
{
    if (dev->is_open)
        return -1; /* already open */
    rb_init(&dev->rb, dev->backing, sizeof(dev->backing));
    dev->is_open = 1;
    return 0;
}

/*
 * dev_write — write data into the device buffer.
 * Precondition:  dev != NULL, dev->is_open == 1, data != NULL, len > 0
 * Postcondition: returns bytes written (<= len); 0 if full
 */
int dev_write(char_dev_t *dev, const uint8_t *data, size_t len)
{
    if (!dev->is_open)
        return -1;
    if (rb_is_full(&dev->rb))
        return 0;
    return (int)rb_write(&dev->rb, data, len);
}

/*
 * dev_read — read data from the device buffer.
 * Precondition:  dev != NULL, dev->is_open == 1, buf != NULL, len > 0
 * Postcondition: returns bytes read (<= len); 0 if empty
 */
int dev_read(char_dev_t *dev, uint8_t *buf, size_t len)
{
    if (!dev->is_open)
        return -1;
    if (rb_is_empty(&dev->rb))
        return 0;
    return (int)rb_read(&dev->rb, buf, len);
}

/*
 * dev_close — close the device.
 * Precondition:  dev != NULL, dev->is_open == 1
 * Postcondition: dev->is_open == 0
 */
int dev_close(char_dev_t *dev)
{
    if (!dev->is_open)
        return -1;
    dev->is_open = 0;
    return 0;
}
