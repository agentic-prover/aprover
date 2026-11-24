/*
 * VibeOS Memory Management
 */

#ifndef MEMORY_H
#define MEMORY_H

#include <stdint.h>
#include <stddef.h>

// Detected RAM (set by memory_init from DTB)
extern uint64_t ram_base;
extern uint64_t ram_size;

// Heap bounds (set at runtime)
extern uint64_t heap_start;
extern uint64_t heap_end;

// Initialize memory management (parses DTB to detect RAM)
void memory_init(void);

// Simple heap allocator
void *malloc(size_t size);
void free(void *ptr);
void *calloc(size_t nmemb, size_t size);
void *realloc(void *ptr, size_t size);

// Memory stats
size_t memory_used(void);
size_t memory_free(void);

// Heap bounds (for debugging)
uint64_t memory_heap_start(void);
uint64_t memory_heap_end(void);

// Stack info (returns current SP)
uint64_t memory_get_sp(void);

// Count allocations (for debugging)
int memory_alloc_count(void);

#endif
