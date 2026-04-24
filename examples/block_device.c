/*
 * block_device.c — Simple block device with seek/read/write
 *
 * A small self-contained example for GRACE evaluation (Phase 4).
 *
 * Intentional bug:
 *   blk_seek() computes the new position as:
 *       dev->position = dev->position + offset
 *   When `dev->position` is close to SIZE_MAX and `offset` is large,
 *   this addition can overflow a size_t, wrapping around to a small value
 *   and bypassing the bounds check.
 */

#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* Data structures                                                      */
/* ------------------------------------------------------------------ */

#define BLK_BLOCK_SIZE 512

typedef struct {
    uint8_t *data;       /* backing storage                           */
    size_t   capacity;   /* total device size in bytes                */
    size_t   position;   /* current seek position                     */
    int      is_open;    /* 1 if open, 0 if closed                   */
    uint32_t error_flags;/* accumulated error flags                   */
} blk_dev_t;

/* Error codes */
#define BLK_OK           0
#define BLK_ERR_CLOSED  -1
#define BLK_ERR_BOUNDS  -2
#define BLK_ERR_NULL    -3

/* ------------------------------------------------------------------ */
/* Lifecycle                                                           */
/* ------------------------------------------------------------------ */

/*
 * blk_init — initialise a block device with a backing buffer.
 *
 * Precondition:  dev != NULL, data != NULL, size > 0
 * Postcondition: dev->is_open == 1, dev->position == 0
 */
int blk_init(blk_dev_t *dev, uint8_t *data, size_t size)
{
    if (dev == NULL || data == NULL)
        return BLK_ERR_NULL;

    dev->data        = data;
    dev->capacity    = size;
    dev->position    = 0;
    dev->is_open     = 1;
    dev->error_flags = 0;
    return BLK_OK;
}

/*
 * blk_close — close the block device.
 *
 * Precondition:  dev != NULL, dev->is_open == 1
 * Postcondition: dev->is_open == 0
 */
int blk_close(blk_dev_t *dev)
{
    if (dev == NULL)
        return BLK_ERR_NULL;
    if (!dev->is_open)
        return BLK_ERR_CLOSED;
    dev->is_open = 0;
    return BLK_OK;
}

/* ------------------------------------------------------------------ */
/* I/O operations                                                      */
/* ------------------------------------------------------------------ */

/*
 * blk_read — read `len` bytes from the device at the current position.
 *
 * Precondition:  dev != NULL, buf != NULL, dev->is_open == 1,
 *                dev->position + len <= dev->capacity
 * Postcondition: buf[0..len-1] filled with device data,
 *                dev->position advanced by len
 */
int blk_read(blk_dev_t *dev, uint8_t *buf, size_t len)
{
    if (dev == NULL || buf == NULL)
        return BLK_ERR_NULL;
    if (!dev->is_open)
        return BLK_ERR_CLOSED;
    if (dev->position + len > dev->capacity)
        return BLK_ERR_BOUNDS;

    memcpy(buf, dev->data + dev->position, len);
    dev->position += len;
    return BLK_OK;
}

/*
 * blk_write — write `len` bytes to the device at the current position.
 *
 * Precondition:  dev != NULL, data != NULL, dev->is_open == 1,
 *                dev->position + len <= dev->capacity
 * Postcondition: device data updated, dev->position advanced by len
 */
int blk_write(blk_dev_t *dev, const uint8_t *data, size_t len)
{
    if (dev == NULL || data == NULL)
        return BLK_ERR_NULL;
    if (!dev->is_open)
        return BLK_ERR_CLOSED;
    if (dev->position + len > dev->capacity)
        return BLK_ERR_BOUNDS;

    memcpy(dev->data + dev->position, data, len);
    dev->position += len;
    return BLK_OK;
}

/*
 * blk_seek — seek to a position relative to the current position.
 *
 * Precondition:  dev != NULL, dev->is_open == 1,
 *                0 <= dev->position + offset <= dev->capacity
 * Postcondition: dev->position == old(dev->position) + offset
 *
 * BUG: the addition `dev->position + offset` can overflow size_t when
 *      both values are large. The overflow wraps around to a small value,
 *      bypassing the `new_pos > dev->capacity` check and resulting in
 *      an out-of-bounds position that corrupts subsequent reads/writes.
 */
int blk_seek(blk_dev_t *dev, size_t offset)
{
    if (dev == NULL)
        return BLK_ERR_NULL;
    if (!dev->is_open)
        return BLK_ERR_CLOSED;

    /* BUG: integer overflow when dev->position + offset wraps around */
    size_t new_pos = dev->position + offset;   /* <-- intentional overflow bug */
    if (new_pos > dev->capacity)
        return BLK_ERR_BOUNDS;

    dev->position = new_pos;
    return BLK_OK;
}
