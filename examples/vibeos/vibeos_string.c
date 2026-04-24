/*
 * VibeOS String Functions — self-contained for AMC verification.
 *
 * Source: https://github.com/kaansenol5/VibeOS/blob/main/kernel/string.c
 * Changes: removed #include "string.h"/"memory.h"; added typedefs; stubbed malloc.
 */

#include <stdint.h>
#include <stddef.h>

/* stub for kernel malloc used by strdup */
static void *malloc(size_t size) {
    (void)size;
    return (void *)0;  /* CBMC: non-null needed — harness will __CPROVER_assume */
}

void *memcpy(void *dest, const void *src, size_t n) {
    uint8_t *d = (uint8_t *)dest;
    const uint8_t *s = (const uint8_t *)src;

    if (((uintptr_t)d & 7) == 0 && ((uintptr_t)s & 7) == 0) {
        uint64_t *d64 = (uint64_t *)d;
        const uint64_t *s64 = (const uint64_t *)s;
        size_t n64 = n / 8;
        for (size_t i = 0; i < n64; i++) {
            d64[i] = s64[i];
        }
        d = (uint8_t *)(d64 + n64);
        s = (const uint8_t *)(s64 + n64);
        n = n & 7;
    }

    for (size_t i = 0; i < n; i++) {
        d[i] = s[i];
    }
    return dest;
}

void *memset(void *s, int c, size_t n) {
    uint8_t *p = (uint8_t *)s;
    uint8_t byte = (uint8_t)c;

    if (((uintptr_t)p & 7) == 0 && n >= 8) {
        uint64_t pattern = byte;
        pattern |= pattern << 8;
        pattern |= pattern << 16;
        pattern |= pattern << 32;

        uint64_t *p64 = (uint64_t *)p;
        size_t n64 = n / 8;
        for (size_t i = 0; i < n64; i++) {
            p64[i] = pattern;
        }
        p = (uint8_t *)(p64 + n64);
        n = n & 7;
    }

    for (size_t i = 0; i < n; i++) {
        p[i] = byte;
    }
    return s;
}

void *memset32(void *s, uint32_t val, size_t count) {
    uint32_t *p = (uint32_t *)s;

    if (((uintptr_t)p & 7) == 0 && count >= 2) {
        uint64_t pattern = ((uint64_t)val << 32) | val;
        uint64_t *p64 = (uint64_t *)p;
        size_t n64 = count / 2;
        for (size_t i = 0; i < n64; i++) {
            p64[i] = pattern;
        }
        if (count & 1) {
            p[count - 1] = val;
        }
    } else {
        for (size_t i = 0; i < count; i++) {
            p[i] = val;
        }
    }
    return s;
}

void *memmove(void *dest, const void *src, size_t n) {
    uint8_t *d = (uint8_t *)dest;
    const uint8_t *s = (const uint8_t *)src;

    if (d < s || d >= s + n) {
        return memcpy(dest, src, n);
    } else if (d > s) {
        if (((uintptr_t)(d + n) & 7) == 0 && ((uintptr_t)(s + n) & 7) == 0) {
            size_t n64 = n / 8;
            size_t rem = n & 7;

            for (size_t i = 0; i < rem; i++) {
                d[n - 1 - i] = s[n - 1 - i];
            }

            uint64_t *d64 = (uint64_t *)d;
            const uint64_t *s64 = (const uint64_t *)s;
            for (size_t i = n64; i > 0; i--) {
                d64[i - 1] = s64[i - 1];
            }
        } else {
            for (size_t i = n; i > 0; i--) {
                d[i - 1] = s[i - 1];
            }
        }
    }
    return dest;
}

int memcmp(const void *s1, const void *s2, size_t n) {
    const uint8_t *p1 = (const uint8_t *)s1;
    const uint8_t *p2 = (const uint8_t *)s2;
    for (size_t i = 0; i < n; i++) {
        if (p1[i] != p2[i]) {
            return p1[i] - p2[i];
        }
    }
    return 0;
}

size_t strlen(const char *s) {
    size_t len = 0;
    while (s[len]) {
        len++;
    }
    return len;
}

char *strcpy(char *dest, const char *src) {
    char *d = dest;
    while ((*d++ = *src++));
    return dest;
}

char *strncpy(char *dest, const char *src, size_t n) {
    size_t i;
    for (i = 0; i < n && src[i]; i++) {
        dest[i] = src[i];
    }
    for (; i < n; i++) {
        dest[i] = '\0';
    }
    return dest;
}

int strcmp(const char *s1, const char *s2) {
    while (*s1 && (*s1 == *s2)) {
        s1++;
        s2++;
    }
    return *(unsigned char *)s1 - *(unsigned char *)s2;
}

int strncmp(const char *s1, const char *s2, size_t n) {
    for (size_t i = 0; i < n; i++) {
        if (s1[i] != s2[i]) {
            return (unsigned char)s1[i] - (unsigned char)s2[i];
        }
        if (s1[i] == '\0') {
            return 0;
        }
    }
    return 0;
}

char *strcat(char *dest, const char *src) {
    char *d = dest;
    while (*d) d++;
    while ((*d++ = *src++));
    return dest;
}

char *strchr(const char *s, int c) {
    while (*s) {
        if (*s == (char)c) {
            return (char *)s;
        }
        s++;
    }
    return (c == '\0') ? (char *)s : NULL;
}

char *strrchr(const char *s, int c) {
    const char *last = NULL;
    while (*s) {
        if (*s == (char)c) {
            last = s;
        }
        s++;
    }
    return (c == '\0') ? (char *)s : (char *)last;
}

char *strdup(const char *s) {
    size_t len = strlen(s) + 1;
    char *newstr = malloc(len);
    if (newstr) {
        memcpy(newstr, s, len);
    }
    return newstr;
}

static char tolower(char c) {
    if (c >= 'A' && c <= 'Z') {
        return c + ('a' - 'A');
    }
    return c;
}

int strcasecmp(const char *s1, const char *s2) {
    while (*s1 && *s2) {
        char c1 = tolower(*s1);
        char c2 = tolower(*s2);
        if (c1 != c2) {
            return c1 - c2;
        }
        s1++;
        s2++;
    }
    return tolower(*s1) - tolower(*s2);
}

static int is_delim(char c, const char *delim) {
    while (*delim) {
        if (c == *delim) return 1;
        delim++;
    }
    return 0;
}

char *strtok_r(char *str, const char *delim, char **saveptr) {
    char *start;
    char *end;

    if (str) {
        start = str;
    } else {
        start = *saveptr;
    }

    if (!start) return NULL;

    while (*start && is_delim(*start, delim)) {
        start++;
    }

    if (*start == '\0') {
        *saveptr = NULL;
        return NULL;
    }

    end = start;
    while (*end && !is_delim(*end, delim)) {
        end++;
    }

    if (*end) {
        *end = '\0';
        *saveptr = end + 1;
    } else {
        *saveptr = NULL;
    }

    return start;
}

char *strstr(const char *haystack, const char *needle) {
    if (!*needle) return (char *)haystack;

    for (; *haystack; haystack++) {
        const char *h = haystack;
        const char *n = needle;
        while (*h && *n && *h == *n) {
            h++;
            n++;
        }
        if (!*n) return (char *)haystack;
    }
    return NULL;
}

void *memchr(const void *s, int c, size_t n) {
    const unsigned char *p = (const unsigned char *)s;
    unsigned char uc = (unsigned char)c;
    for (size_t i = 0; i < n; i++) {
        if (p[i] == uc) return (void *)(p + i);
    }
    return NULL;
}
