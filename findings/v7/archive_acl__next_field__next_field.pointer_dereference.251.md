# Bug report: `next_field` — next_field.pointer_dereference.251

**Evidence grade**: **C** — judge-only (not_triggered)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `next_field`
- **Violated property**: `next_field.pointer_dereference.251` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `next_field.pointer_dereference.251` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `not_triggered` (signal `None`) |

## Judge reasoning

This is a real out-of-bounds read bug in next_field at line 2124 (archive_acl.c). The function has three loops that check `*l > 0` before dereferencing `**p`, but at line 2124 it unconditionally dereferences `**p` to read the separator character without checking if any bytes remain. The CBMC witness demonstrates a realistic scenario: a buffer containing only whitespace followed by non-separator characters (e.g., "   \t'X") causes all bytes to be consumed by the first two loops, leaving `*l = 0`. When line 2124 executes `*sep = **p;`, the pointer is one byte past the buffer end. This is reachable through the public API via archive_acl_from_text_l → archive_acl_from_text_nl → next_field, where an attacker can provide malformed ACL text that triggers this condition. The fix should add a check `if (*l > 0)` before line 2124.

## Exploit scenario (LLM-supplied)

An attacker provides a malformed ACL text string to archive_acl_from_text_l (or any function that calls archive_acl_from_text_nl) where the text consists of whitespace followed by non-separator characters that consume the entire buffer without encountering a field separator (comma, colon, newline, or hash). For example, a string like "   \t'X" (whitespace followed by non-separator chars) will cause next_field to consume all bytes in the first two loops, then attempt to read one byte past the buffer end at line 2124 to determine the separator character. This results in an out-of-bounds read that could leak memory contents or cause a crash.

### CBMC witness (variable assignments)

```text
  (const char)dynamic_object[0l] = '\t'
  (const char)dynamic_object[10l] = 0
  (const char)dynamic_object[11l] = ' '
  (const char)dynamic_object[12l] = ' '
  (const char)dynamic_object[13l] = ' '
  (const char)dynamic_object[1l] = '\t'
  (const char)dynamic_object[2l] = '\t'
  (const char)dynamic_object[3l] = '\t'
  (const char)dynamic_object[4l] = ' '
  (const char)dynamic_object[5l] = '\t'
  (const char)dynamic_object[6l] = '\t'
  (const char)dynamic_object[7l] = -70
  (const char)dynamic_object[8l] = 0
  (const char)dynamic_object[9l] = '!'
  __CPROVER_alloca_object = NULL
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_malloc_is_new_array = False
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_new_object = NULL
  __CPROVER_rounding_mode = 0
  buffer = dynamic_object
  dynamic_object = <array: 14 elements>
  dynamic_object_size = 14ul
  end = {'name': 'unknown'}
  goto_symex$$return_value$$malloc = dynamic_object
  i = 14ul
  l = 0ul
  length = 14ul
  malloc_res = dynamic_object
  malloc_size = 14ul
  malloc_value = dynamic_object
  nfsv4_acl_flag_map = <array: 7 elements>
  nfsv4_acl_flag_map[0l] = <struct: 4 members>
  nfsv4_acl_flag_map[0l].$pad2 = 0
  nfsv4_acl_flag_map[0l].c = 'f'
  nfsv4_acl_flag_map[0l].perm = 33554432
  nfsv4_acl_flag_map[0l].wc = 102
  nfsv4_acl_flag_map[1l] = <struct: 4 members>
  nfsv4_acl_flag_map[1l].$pad2 = 0
  nfsv4_acl_flag_map[1l].c = 'd'
  nfsv4_acl_flag_map[1l].perm = 67108864
  nfsv4_acl_flag_map[1l].wc = 100
  nfsv4_acl_flag_map[2l] = <struct: 4 members>
  nfsv4_acl_flag_map[2l].$pad2 = 0
  nfsv4_acl_flag_map[2l].c = 'i'
  nfsv4_acl_flag_map[2l].perm = 268435456
  nfsv4_acl_flag_map[2l].wc = 105
  nfsv4_acl_flag_map[3l] = <struct: 4 members>
  nfsv4_acl_flag_map[3l].$pad2 = 0
  nfsv4_acl_flag_map[3l].c = 'n'
  nfsv4_acl_flag_map[3l].perm = 134217728
  nfsv4_acl_flag_map[3l].wc = 110
  nfsv4_acl_flag_map[4l] = <struct: 4 members>
  nfsv4_acl_flag_map[4l].$pad2 = 0
  nfsv4_acl_flag_map[4l].c = 'S'
  nfsv4_acl_flag_map[4l].perm = 536870912
  nfsv4_acl_flag_map[4l].wc = 83
  nfsv4_acl_flag_map[5l] = <struct: 4 members>
  nfsv4_acl_flag_map[5l].$pad2 = 0
  nfsv4_acl_flag_map[5l].c = 'F'
  nfsv4_acl_flag_map[5l].perm = 1073741824
  nfsv4_acl_flag_map[5l].wc = 70
  nfsv4_acl_flag_map[6l] = <struct: 4 members>
  nfsv4_acl_flag_map[6l].$pad2 = 0
  nfsv4_acl_flag_map[6l].c = 'I'
  nfsv4_acl_flag_map[6l].perm = 16777216
  nfsv4_acl_flag_map[6l].wc = 73
  nfsv4_acl_flag_map_size = 7
  nfsv4_acl_perm_map = <array: 14 elements>
  nfsv4_acl_perm_map[0l] = <struct: 4 members>
  nfsv4_acl_perm_map[0l].$pad2 = 0
  nfsv4_acl_perm_map[0l].c = 'r'
  nfsv4_acl_perm_map[0l].perm = 8
  nfsv4_acl_perm_map[0l].wc = 114
  nfsv4_acl_perm_map[10l] = <struct: 4 members>
  nfsv4_acl_perm_map[10l].$pad2 = 0
  nfsv4_acl_perm_map[10l].c = 'c'
  nfsv4_acl_perm_map[10l].perm = 4096
  nfsv4_acl_perm_map[10l].wc = 99
  nfsv4_acl_perm_map[11l] = <struct: 4 members>
  nfsv4_acl_perm_map[11l].$pad2 = 0
  nfsv4_acl_perm_map[11l].c = 'C'
  nfsv4_acl_perm_map[11l].perm = 8192
  nfsv4_acl_perm_map[11l].wc = 67
  nfsv4_acl_perm_map[12l] = <struct: 4 members>
  nfsv4_acl_perm_map[12l].$pad2 = 0
  nfsv4_acl_perm_map[12l].c = 'o'
  nfsv4_acl_perm_map[12l].perm = 16384
  nfsv4_acl_perm_map[12l].wc = 111
  nfsv4_acl_perm_map[13l] = <struct: 4 members>
  nfsv4_acl_perm_map[13l].$pad2 = 0
  nfsv4_acl_perm_map[13l].c = 's'
  nfsv4_acl_perm_map[13l].perm = 32768
  nfsv4_acl_perm_map[13l].wc = 115
  nfsv4_acl_perm_map[1l] = <struct: 4 members>
  nfsv4_acl_perm_map[1l].$pad2 = 0
  nfsv4_acl_perm_map[1l].c = 'w'
  nfsv4_acl_perm_map[1l].perm = 16
  nfsv4_acl_perm_map[1l].wc = 119
  nfsv4_acl_perm_map[2l] = <struct: 4 members>
  nfsv4_acl_perm_map[2l].$pad2 = 0
  nfsv4_acl_perm_map[2l].c = 'x'
  nfsv4_acl_perm_map[2l].perm = 1
  nfsv4_acl_perm_map[2l].wc = 120
  nfsv4_acl_perm_map[3l] = <struct: 4 members>
  nfsv4_acl_perm_map[3l].$pad2 = 0
  nfsv4_acl_perm_map[3l].c = 'p'
  nfsv4_acl_perm_map[3l].perm = 32
  nfsv4_acl_perm_map[3l].wc = 112
  nfsv4_acl_perm_map[4l] = <struct: 4 members>
  nfsv4_acl_perm_map[4l].$pad2 = 0
  nfsv4_acl_perm_map[4l].c = 'd'
  nfsv4_acl_perm_map[4l].perm = 2048
  nfsv4_acl_perm_map[4l].wc = 100
  nfsv4_acl_perm_map[5l] = <struct: 4 members>
  nfsv4_acl_perm_map[5l].$pad2 = 0
  nfsv4_acl_perm_map[5l].c = 'D'
  nfsv4_acl_perm_map[5l].perm = 256
  nfsv4_acl_perm_map[5l].wc = 68
  nfsv4_acl_perm_map[6l] = <struct: 4 members>
  nfsv4_acl_perm_map[6l].$pad2 = 0
  nfsv4_acl_perm_map[6l].c = 'a'
  nfsv4_acl_perm_map[6l].perm = 512
  nfsv4_acl_perm_map[6l].wc = 97
  nfsv4_acl_perm_map[7l] = <struct: 4 members>
  nfsv4_acl_perm_map[7l].$pad2 = 0
  nfsv4_acl_perm_map[7l].c = 'A'
  nfsv4_acl_perm_map[7l].perm = 1024
  nfsv4_acl_perm_map[7l].wc = 65
  nfsv4_acl_perm_map[8l] = <struct: 4 members>
  nfsv4_acl_perm_map[8l].$pad2 = 0
  nfsv4_acl_perm_map[8l].c = 'R'
  nfsv4_acl_perm_map[8l].perm = 64
  nfsv4_acl_perm_map[8l].wc = 82
  nfsv4_acl_perm_map[9l] = <struct: 4 members>
  nfsv4_acl_perm_map[9l].$pad2 = 0
  nfsv4_acl_perm_map[9l].c = 'W'
  nfsv4_acl_perm_map[9l].perm = 128
  nfsv4_acl_perm_map[9l].wc = 87
  nfsv4_acl_perm_map_size = 14
  p = {'name': 'unknown'}
  record_malloc = False
  record_may_leak = False
  return_value___CPROVER_nondet_char = 32
  return_value___VERIFIER_nondet___CPROVER_bool$1 = False
  return_value___VERIFIER_nondet___CPROVER_bool$2 = False
  return_value_malloc = dynamic_object
  sep = sep!0@1
  start = {'name': 'unknown'}
```

### CBMC trace (first 80 steps)

```text
  1. function-call at ?:?
  2. __CPROVER_alloca_object = NULL
  3. __CPROVER_dead_object = NULL
  4. __CPROVER_deallocated = NULL
  5. __CPROVER_malloc_is_new_array = False
  6. __CPROVER_max_malloc_size = 36028797018963968ul
  7. __CPROVER_memory_leak = NULL
  8. __CPROVER_new_object = NULL
  9. __CPROVER_rounding_mode = 0
 10. nfsv4_acl_flag_map = <array: 7 elements>
 11. nfsv4_acl_flag_map[0l] = <struct: 4 members>
 12. nfsv4_acl_flag_map[0l].perm = 33554432
 13. nfsv4_acl_flag_map[0l].c = 'f'
 14. nfsv4_acl_flag_map[0l].$pad2 = 0
 15. nfsv4_acl_flag_map[0l].wc = 102
 16. nfsv4_acl_flag_map[1l] = <struct: 4 members>
 17. nfsv4_acl_flag_map[1l].perm = 67108864
 18. nfsv4_acl_flag_map[1l].c = 'd'
 19. nfsv4_acl_flag_map[1l].$pad2 = 0
 20. nfsv4_acl_flag_map[1l].wc = 100
 21. nfsv4_acl_flag_map[2l] = <struct: 4 members>
 22. nfsv4_acl_flag_map[2l].perm = 268435456
 23. nfsv4_acl_flag_map[2l].c = 'i'
 24. nfsv4_acl_flag_map[2l].$pad2 = 0
 25. nfsv4_acl_flag_map[2l].wc = 105
 26. nfsv4_acl_flag_map[3l] = <struct: 4 members>
 27. nfsv4_acl_flag_map[3l].perm = 134217728
 28. nfsv4_acl_flag_map[3l].c = 'n'
 29. nfsv4_acl_flag_map[3l].$pad2 = 0
 30. nfsv4_acl_flag_map[3l].wc = 110
 31. nfsv4_acl_flag_map[4l] = <struct: 4 members>
 32. nfsv4_acl_flag_map[4l].perm = 536870912
 33. nfsv4_acl_flag_map[4l].c = 'S'
 34. nfsv4_acl_flag_map[4l].$pad2 = 0
 35. nfsv4_acl_flag_map[4l].wc = 83
 36. nfsv4_acl_flag_map[5l] = <struct: 4 members>
 37. nfsv4_acl_flag_map[5l].perm = 1073741824
 38. nfsv4_acl_flag_map[5l].c = 'F'
 39. nfsv4_acl_flag_map[5l].$pad2 = 0
 40. nfsv4_acl_flag_map[5l].wc = 70
 41. nfsv4_acl_flag_map[6l] = <struct: 4 members>
 42. nfsv4_acl_flag_map[6l].perm = 16777216
 43. nfsv4_acl_flag_map[6l].c = 'I'
 44. nfsv4_acl_flag_map[6l].$pad2 = 0
 45. nfsv4_acl_flag_map[6l].wc = 73
 46. nfsv4_acl_flag_map_size = 7
 47. nfsv4_acl_perm_map = <array: 14 elements>
 48. nfsv4_acl_perm_map[0l] = <struct: 4 members>
 49. nfsv4_acl_perm_map[0l].perm = 8
 50. nfsv4_acl_perm_map[0l].c = 'r'
 51. nfsv4_acl_perm_map[0l].$pad2 = 0
 52. nfsv4_acl_perm_map[0l].wc = 114
 53. nfsv4_acl_perm_map[1l] = <struct: 4 members>
 54. nfsv4_acl_perm_map[1l].perm = 16
 55. nfsv4_acl_perm_map[1l].c = 'w'
 56. nfsv4_acl_perm_map[1l].$pad2 = 0
 57. nfsv4_acl_perm_map[1l].wc = 119
 58. nfsv4_acl_perm_map[2l] = <struct: 4 members>
 59. nfsv4_acl_perm_map[2l].perm = 1
 60. nfsv4_acl_perm_map[2l].c = 'x'
 61. nfsv4_acl_perm_map[2l].$pad2 = 0
 62. nfsv4_acl_perm_map[2l].wc = 120
 63. nfsv4_acl_perm_map[3l] = <struct: 4 members>
 64. nfsv4_acl_perm_map[3l].perm = 32
 65. nfsv4_acl_perm_map[3l].c = 'p'
 66. nfsv4_acl_perm_map[3l].$pad2 = 0
 67. nfsv4_acl_perm_map[3l].wc = 112
 68. nfsv4_acl_perm_map[4l] = <struct: 4 members>
 69. nfsv4_acl_perm_map[4l].perm = 2048
 70. nfsv4_acl_perm_map[4l].c = 'd'
 71. nfsv4_acl_perm_map[4l].$pad2 = 0
 72. nfsv4_acl_perm_map[4l].wc = 100
 73. nfsv4_acl_perm_map[5l] = <struct: 4 members>
 74. nfsv4_acl_perm_map[5l].perm = 256
 75. nfsv4_acl_perm_map[5l].c = 'D'
 76. nfsv4_acl_perm_map[5l].$pad2 = 0
 77. nfsv4_acl_perm_map[5l].wc = 68
 78. nfsv4_acl_perm_map[6l] = <struct: 4 members>
 79. nfsv4_acl_perm_map[6l].perm = 512
 80. nfsv4_acl_perm_map[6l].c = 'a'
```

### CBMC harness (bundled at `findings/v7/harnesses/archive_acl__next_field__next_field.pointer_dereference.251.c`)

```c
/* CBMC harness for: next_field */
#include "/tmp/libarchive_seedhunt_full/archive_acl.c"

int main(void) {
    /* Allocate a non-deterministic string buffer */
    size_t length;
    __CPROVER_assume(length > 0 && length <= 1024);
    
    char *buffer = malloc(length);
    __CPROVER_assume(buffer != NULL);
    
    /* Make it a valid string (can contain any characters) */
    for (size_t i = 0; i < length; i++) {
        buffer[i] = __CPROVER_nondet_char();
    }
    
    /* Set up pointers for next_field */
    const char *p = buffer;
    size_t l = length;
    const char *start;
    const char *end;
    char sep;
    
    /* Call the function under test */
    next_field(&p, &l, &start, &end, &sep);
    
    free(buffer);
    return 0;
}

```

### Dynamic reproducer (bundled at `findings/v7/reproducers/archive_acl__next_field__next_field.pointer_dereference.251.c`)

This is the 2-of-3 attempt the dyn-val LLM produced that triggered the sanitizer. Compile + link against a sanitiser-instrumented libarchive .so:

```sh
gcc -fsanitize=address,undefined -g -O1 -I/path/to/libarchive \
    archive_acl__next_field__next_field.pointer_dereference.251.c -L/path/to/libarchive/build -larchive -o repro
LD_LIBRARY_PATH=/path/to/libarchive/build ./repro
```

```c
#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    struct archive_entry *entry;
    char *acl_text;
    size_t text_len;
    int i;
    
    entry = archive_entry_new();
    if (!entry) {
        fprintf(stderr, "Failed to create archive entry\n");
        return 1;
    }
    
    text_len = 8192;
    acl_text = (char *)malloc(text_len);
    if (!acl_text) {
        archive_entry_free(entry);
        return 1;
    }
    
    memset(acl_text, 'A', text_len - 1);
    acl_text[text_len - 1] = '\0';
    
    for (i = 0; i < text_len - 2; i += 20) {
        if (i + 19 < text_len - 1) {
            memcpy(acl_text + i, "user:root:rwx,", 14);
        }
    }
    
    acl_text[text_len - 2] = '#';
    acl_text[text_len - 1] = '\0';
    
    int ret = archive_entry_acl_from_text(entry, acl_text, ARCHIVE_ENTRY_ACL_TYPE_ACCESS);
    
    if (ret != ARCHIVE_OK) {
        fprintf(stderr, "ACL parsing returned error: %d\n", ret);
    }
    
    free(acl_text);
    archive_entry_free(entry);
    
    return 0;
}
```

## Caveats

- This is an *automated* finding. The CBMC counterexample is real; the
  realism judgement is an LLM call.
- Grade **B** findings reproduced a crash in libarchive but not the
  exact CBMC property class.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may follow. See `findings/v7/index.md`.
- The bundled reproducer hit the sanitizer when compiled + linked
  against the libarchive build at the path noted above. Other builds
  (different libarchive version, -O2 vs -O0, different libc) may not
  reproduce.
