/*
 * VibeOS memory allocator — self-contained version for AMC verification.
 *
 * Source: https://github.com/kaansenol5/VibeOS/blob/main/kernel/memory.c
 * Changes: stubbed dtb_parse/printf, replaced inline asm in memory_get_sp,
 * added stub _bss_end symbol so CBMC can link without the linker script.
 */

#include <stdint.h>
#include <stddef.h>

/* ---- stubs for kernel-only dependencies ---------------------------------- */

struct dtb_memory_info { uint64_t base; uint64_t size; };
static int dtb_parse(void *addr, struct dtb_memory_info *out) {
    (void)addr;
    out->base = 0x40000000;
    out->size = 256 * 1024 * 1024;
    return 0;
}
static void printf(const char *fmt, ...) { (void)fmt; }

/* linker-script symbol — place heap right after this stub */
static char _bss_end_storage[1];
char *_bss_end = _bss_end_storage;

/* ---- original memory.c (unmodified logic) -------------------------------- */

uint64_t ram_base;
uint64_t ram_size;
uint64_t heap_start;
uint64_t heap_end;

typedef struct block_header {
    size_t size;
    uint8_t is_free;
    struct block_header *next;
} block_header_t;

#define HEADER_SIZE sizeof(block_header_t)
#define ALIGN_UP(x, align) (((x) + ((align) - 1)) & ~((align) - 1))

static block_header_t *free_list = NULL;

static size_t stat_used = 0;
static size_t stat_free = 0;
static int stat_alloc_count = 0;

#define KERNEL_STACK_TOP 0x5F000000
#define DTB_ADDR         0x40000000
#define STACK_BUFFER (1 * 1024 * 1024)

/* heap backing store for CBMC — 1 MB static buffer */
#define HEAP_SIZE (1 * 1024 * 1024)
static uint8_t heap_buf[HEAP_SIZE];

void memory_init(void) {
    struct dtb_memory_info mem_info;
    if (dtb_parse((void *)DTB_ADDR, &mem_info) != 0) {
        ram_base = 0x40000000;
        ram_size = 256 * 1024 * 1024;
    } else {
        ram_base = mem_info.base;
        ram_size = mem_info.size;
    }

    /* For CBMC: use the static heap_buf instead of raw address arithmetic */
    heap_start = (uint64_t)(uintptr_t)heap_buf;
    heap_end   = heap_start + HEAP_SIZE;

    free_list = (block_header_t *)(uintptr_t)heap_start;
    free_list->size   = HEAP_SIZE - HEADER_SIZE;
    free_list->is_free = 1;
    free_list->next   = NULL;

    stat_used = 0;
    stat_free = free_list->size;
    stat_alloc_count = 0;
}

void *malloc(size_t size) {
    if (size == 0) return NULL;

    size = ALIGN_UP(size, 16);

    block_header_t *current = free_list;

    while (current != NULL) {
        if (current->is_free && current->size >= size) {
            size_t old_size = current->size;

            if (current->size >= size + HEADER_SIZE + 16) {
                block_header_t *new_block = (block_header_t *)((uint8_t *)current + HEADER_SIZE + size);
                new_block->size   = current->size - size - HEADER_SIZE;
                new_block->is_free = 1;
                new_block->next   = current->next;

                current->size = size;
                current->next = new_block;

                stat_used += size + HEADER_SIZE;
                stat_free -= size + HEADER_SIZE;
            } else {
                stat_used += old_size + HEADER_SIZE;
                stat_free -= old_size;
            }

            current->is_free = 0;
            stat_alloc_count++;
            return (void *)((uint8_t *)current + HEADER_SIZE);
        }
        current = current->next;
    }

    return NULL;
}

void free(void *ptr) {
    if (ptr == NULL) return;

    block_header_t *block = (block_header_t *)((uint8_t *)ptr - HEADER_SIZE);

    stat_used -= block->size + HEADER_SIZE;
    stat_free += block->size;
    stat_alloc_count--;

    block->is_free = 1;

    block_header_t *current = free_list;
    while (current != NULL) {
        if (current->is_free && current->next != NULL && current->next->is_free) {
            stat_free += HEADER_SIZE;
            current->size += HEADER_SIZE + current->next->size;
            current->next = current->next->next;
        } else {
            current = current->next;
        }
    }
}

void *calloc(size_t nmemb, size_t size) {
    size_t total = nmemb * size;
    void *ptr = malloc(total);
    if (ptr != NULL) {
        uint8_t *p = (uint8_t *)ptr;
        for (size_t i = 0; i < total; i++) {
            p[i] = 0;
        }
    }
    return ptr;
}

void *realloc(void *ptr, size_t size) {
    if (ptr == NULL) return malloc(size);
    if (size == 0) {
        free(ptr);
        return NULL;
    }

    block_header_t *block = (block_header_t *)((uint8_t *)ptr - HEADER_SIZE);

    if (block->size >= size) {
        return ptr;
    }

    void *new_ptr = malloc(size);
    if (new_ptr != NULL) {
        uint8_t *src = (uint8_t *)ptr;
        uint8_t *dst = (uint8_t *)new_ptr;
        for (size_t i = 0; i < block->size; i++) {
            dst[i] = src[i];
        }
        free(ptr);
    }
    return new_ptr;
}

size_t memory_used(void)          { return stat_used; }
size_t memory_free(void)          { return stat_free; }
uint64_t memory_heap_start(void)  { return heap_start; }
uint64_t memory_heap_end(void)    { return heap_end; }
int memory_alloc_count(void)      { return stat_alloc_count; }

uint64_t memory_get_sp(void) {
    /* inline asm removed for CBMC — not relevant to allocator verification */
    return 0;
}
