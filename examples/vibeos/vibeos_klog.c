/*
 * VibeOS Kernel Log — self-contained for AMC verification.
 *
 * Source: https://github.com/kaansenol5/VibeOS/blob/main/kernel/klog.c
 * Changes: inlined memset; removed #include "klog.h"/"string.h".
 *
 * Key properties CBMC checks:
 *  - klog_head wraps correctly: always in [0, KLOG_BUFFER_SIZE)
 *  - klog_read: offset + size never overflows; copy never OOB
 *  - klog_size: returns value <= KLOG_BUFFER_SIZE
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define KLOG_BUFFER_SIZE (64 * 1024)

static char klog_buffer[KLOG_BUFFER_SIZE];
static size_t klog_head = 0;
static size_t klog_total = 0;
static int klog_initialized = 0;

void klog_init(void) {
    memset(klog_buffer, 0, sizeof(klog_buffer));
    klog_head = 0;
    klog_total = 0;
    klog_initialized = 1;
}

void klog_putc(char c) {
    if (!klog_initialized) return;

    klog_buffer[klog_head] = c;
    klog_head = (klog_head + 1) % KLOG_BUFFER_SIZE;
    klog_total++;
}

size_t klog_size(void) {
    if (klog_total > KLOG_BUFFER_SIZE) {
        return KLOG_BUFFER_SIZE;
    }
    return klog_total;
}

size_t klog_read(char *buf, size_t offset, size_t size) {
    if (!klog_initialized || !buf || size == 0) return 0;

    size_t log_size = klog_size();
    if (offset >= log_size) return 0;

    size_t available = log_size - offset;
    if (size > available) size = available;

    size_t start;
    if (klog_total > KLOG_BUFFER_SIZE) {
        start = (klog_head + offset) % KLOG_BUFFER_SIZE;
    } else {
        start = offset;
    }

    for (size_t i = 0; i < size; i++) {
        buf[i] = klog_buffer[(start + i) % KLOG_BUFFER_SIZE];
    }

    return size;
}
