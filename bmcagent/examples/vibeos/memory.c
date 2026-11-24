/*
 * VibeOS Memory Management
 *
 * Simple first-fit heap allocator. Not the fastest, but easy to understand.
 * Each allocation has a header with size and free flag.
 *
 * RAM is detected at runtime by parsing the Device Tree Blob (DTB).
 */

#include "memory.h"
#include "dtb.h"
#include "printf.h"

// Detected RAM info (populated by memory_init)
uint64_t ram_base;
uint64_t ram_size;

// Heap bounds
uint64_t heap_start;
uint64_t heap_end;

// Block header - sits before each allocation
typedef struct block_header {
    size_t size;                    // Size of data area (not including header)
    uint8_t is_free;                // 1 if block is free, 0 if allocated
    struct block_header *next;      // Next block in list
} block_header_t;

#define HEADER_SIZE sizeof(block_header_t)
#define ALIGN_UP(x, align) (((x) + ((align) - 1)) & ~((align) - 1))

static block_header_t *free_list = NULL;

// O(1) counters - updated on malloc/free instead of scanning
static size_t stat_used = 0;      // Total bytes in use (including headers)
static size_t stat_free = 0;      // Total bytes free
static int stat_alloc_count = 0;  // Number of active allocations

// Defined in linker script - end of BSS in RAM
// Declared as char[] so the symbol name gives the address directly
extern char _bss_end[];

// Stack location (must match boot.S!)
#ifdef TARGET_PI
#define KERNEL_STACK_TOP 0x1B000000   // Pi: Stack in cacheable region (must match boot-pi.S!)
#define DTB_ADDR         0x00000000   // Pi: DTB at start of RAM
#else
#define KERNEL_STACK_TOP 0x5F000000   // QEMU: stack in high RAM
#define DTB_ADDR         0x40000000   // QEMU: DTB at RAM start
#endif

// Leave some room below stack for safety (1MB)
#define STACK_BUFFER (1 * 1024 * 1024)

void memory_init(void) {
    // Note: Don't use printf here - console isn't initialized yet!

    // Parse DTB to get RAM info
    struct dtb_memory_info mem_info;
    if (dtb_parse((void *)DTB_ADDR, &mem_info) != 0) {
        // Fallback to safe defaults if DTB parsing fails
#ifdef TARGET_PI
        ram_base = 0x00000000;
        ram_size = 512 * 1024 * 1024;  // 512MB (Pi Zero 2W)
#else
        ram_base = 0x40000000;
        ram_size = 256 * 1024 * 1024;  // 256MB (QEMU default)
#endif
    } else {
        ram_base = mem_info.base;
        ram_size = mem_info.size;
    }

    // Heap starts after BSS, aligned to 16 bytes
    // Add 64KB buffer after BSS for safety
    heap_start = ALIGN_UP((uint64_t)_bss_end + 0x10000, 16);

    // Heap ends well before the stack - leave room for programs!
    // Programs load after heap_end, so we need to reserve space.
    // Reserve at least 64MB for program area (between heap and stack)
    uint64_t ram_end = ram_base + ram_size;
    uint64_t program_reserve = 64 * 1024 * 1024;  // 64MB for programs
    uint64_t heap_max = KERNEL_STACK_TOP - STACK_BUFFER - program_reserve;

    // But also can't exceed actual RAM
    if (heap_max > ram_end - program_reserve) {
        heap_max = ram_end - STACK_BUFFER - program_reserve;
    }

    // Sanity: heap must be at least 64MB
    if (heap_max < heap_start + 64 * 1024 * 1024) {
        heap_max = heap_start + 64 * 1024 * 1024;
    }

    heap_end = heap_max;

    printf("[MEM] heap: 0x%lx - 0x%lx, stack at 0x%lx\n",
           heap_start, heap_end, (uint64_t)KERNEL_STACK_TOP);

    // Initialize with one giant free block
    free_list = (block_header_t *)heap_start;
    free_list->size = heap_end - heap_start - HEADER_SIZE;
    free_list->is_free = 1;
    free_list->next = NULL;

    // Initialize O(1) counters
    stat_used = 0;
    stat_free = free_list->size;
    stat_alloc_count = 0;
}

void *malloc(size_t size) {
    if (size == 0) return NULL;

    // Align size to 16 bytes
    size = ALIGN_UP(size, 16);

    block_header_t *current = free_list;

    // First-fit: find first block that's big enough
    while (current != NULL) {
        if (current->is_free && current->size >= size) {
            // Found a suitable block
            size_t old_size = current->size;

            // Split if there's enough room for another block
            if (current->size >= size + HEADER_SIZE + 16) {
                // Create new block after this allocation
                block_header_t *new_block = (block_header_t *)((uint8_t *)current + HEADER_SIZE + size);
                new_block->size = current->size - size - HEADER_SIZE;
                new_block->is_free = 1;
                new_block->next = current->next;

                current->size = size;
                current->next = new_block;

                // Update counters: we used size+header, free lost size+header
                stat_used += size + HEADER_SIZE;
                stat_free -= size + HEADER_SIZE;
            } else {
                // No split: entire block becomes used
                stat_used += old_size + HEADER_SIZE;
                stat_free -= old_size;
            }

            current->is_free = 0;
            stat_alloc_count++;
            return (void *)((uint8_t *)current + HEADER_SIZE);
        }
        current = current->next;
    }

    // No suitable block found
    return NULL;
}

void free(void *ptr) {
    if (ptr == NULL) return;

    // Get header
    block_header_t *block = (block_header_t *)((uint8_t *)ptr - HEADER_SIZE);

    // Update counters before marking free
    stat_used -= block->size + HEADER_SIZE;
    stat_free += block->size;
    stat_alloc_count--;

    block->is_free = 1;

    // Coalesce adjacent free blocks
    block_header_t *current = free_list;
    while (current != NULL) {
        if (current->is_free && current->next != NULL && current->next->is_free) {
            // Merge with next block - reclaim header space
            stat_free += HEADER_SIZE;
            current->size += HEADER_SIZE + current->next->size;
            current->next = current->next->next;
            // Don't advance - check if we can merge again
        } else {
            current = current->next;
        }
    }
}

void *calloc(size_t nmemb, size_t size) {
    size_t total = nmemb * size;
    void *ptr = malloc(total);
    if (ptr != NULL) {
        // Zero the memory
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

    // If current block is big enough, just return it
    if (block->size >= size) {
        return ptr;
    }

    // Otherwise allocate new block and copy
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

size_t memory_used(void) {
    return stat_used;  // O(1) - no scanning!
}

size_t memory_free(void) {
    return stat_free;  // O(1) - no scanning!
}

uint64_t memory_heap_start(void) {
    return heap_start;
}

uint64_t memory_heap_end(void) {
    return heap_end;
}

uint64_t memory_get_sp(void) {
    uint64_t sp;
    __asm__ volatile("mov %0, sp" : "=r"(sp));
    return sp;
}

int memory_alloc_count(void) {
    return stat_alloc_count;  // O(1) - no scanning!
}
