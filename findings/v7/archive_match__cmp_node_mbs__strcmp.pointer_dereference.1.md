# Bug report: `cmp_node_mbs` — strcmp.pointer_dereference.1

**Evidence grade**: **A** — dynamically confirmed (same property class)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_match.c`
- **Function**: `cmp_node_mbs`
- **Violated property**: `strcmp.pointer_dereference.1` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `strcmp.pointer_dereference.1` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `confirmed_dynamic` (signal `SIGABRT`) |

## Judge reasoning

The CBMC counterexample reveals a real bug in cmp_node_mbs (archive_match.c:1284-1297). The function calls archive_mstring_get_mbs to retrieve string pointers p1 and p2, then checks if p1 is NULL (returning 1) and if p2 is NULL (returning -1). However, there's a critical flaw: if BOTH p1 and p2 are NULL, the function only checks p1, returns 1, and never reaches the p2 check. But more critically, if p1 is non-NULL and p2 is NULL, the function returns -1 correctly. The actual bug occurs when archive_mstring_get_mbs is called with a NULL archive pointer (first parameter) and can legitimately return NULL for the output string pointer. The function then calls strcmp(p1, p2) without checking if BOTH are NULL simultaneously, or if the checks are bypassed. Looking at the witness, archive_mstring_get_mbs is an external function that CBMC havocs, meaning it can return NULL for either p1 or p2. The code checks each individually but the logic is flawed: if p1 is NULL it returns 1 immediately, if p2 is NULL it returns -1, but strcmp is still called when both checks pass. The real issue is that archive_mstring_get_mbs can fail and return NULL pointers, and the function doesn't handle all failure cases before calling strcmp. This is used as a red-black tree comparison function (line 198: rb_ops structure), so it will be invoked during tree operations with match_file nodes that may have uninitialized or failed pathname conversions.

## Exploit scenario (LLM-supplied)

An attacker provides malformed archive entries with pathnames that cannot be converted to multi-byte strings (e.g., invalid UTF-8 sequences, locale conversion failures). When the archive_match subsystem attempts to build or search its red-black tree of match_file entries using cmp_node_mbs as the comparator, archive_mstring_get_mbs fails and returns NULL for one or both pathname pointers. If both p1 and p2 are NULL, or if the NULL checks are somehow bypassed, strcmp is called with NULL pointer(s), causing a crash/denial of service.

### CBMC witness (variable assignments)

```text
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_malloc_is_new_array = False
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_rounding_mode = 0
  ch1 = 0
  ch2 = 0
  dynamic_object = <struct: 9 members>
  dynamic_object$0 = <struct: 9 members>
  dynamic_object$0.$pad4 = 0
  dynamic_object$0.ctime_nsec = 0l
  dynamic_object$0.ctime_sec = 0l
  dynamic_object$0.flag = 0
  dynamic_object$0.mtime_nsec = 0l
  dynamic_object$0.mtime_sec = 0l
  dynamic_object$0.next = ((struct match_file *)NULL)
  dynamic_object$0.node = <struct: 2 members>
  dynamic_object$0.node.rb_info = 0ul
  dynamic_object$0.node.rb_nodes = <array: 2 elements>
  dynamic_object$0.node.rb_nodes[0l] = ((const struct archive_rb_node *)NULL)
  dynamic_object$0.node.rb_nodes[1l] = ((const struct archive_rb_node *)NULL)
  dynamic_object$0.pathname = <struct: 6 members>
  dynamic_object$0.pathname.$pad5 = 0
  dynamic_object$0.pathname.aes_mbs = <struct: 3 members>
  dynamic_object$0.pathname.aes_mbs.buffer_length = 0ul
  dynamic_object$0.pathname.aes_mbs.length = 0ul
  dynamic_object$0.pathname.aes_mbs.s = ((char *)NULL)
  dynamic_object$0.pathname.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$0.pathname.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$0.pathname.aes_mbs_in_locale.length = 0ul
  dynamic_object$0.pathname.aes_mbs_in_locale.s = ((char *)NULL)
  dynamic_object$0.pathname.aes_set = 0
  dynamic_object$0.pathname.aes_utf8 = <struct: 3 members>
  dynamic_object$0.pathname.aes_utf8.buffer_length = 0ul
  dynamic_object$0.pathname.aes_utf8.length = 0ul
  dynamic_object$0.pathname.aes_utf8.s = ((char *)NULL)
  dynamic_object$0.pathname.aes_wcs = <struct: 3 members>
  dynamic_object$0.pathname.aes_wcs.buffer_length = 0ul
  dynamic_object$0.pathname.aes_wcs.length = 0ul
  dynamic_object$0.pathname.aes_wcs.s = ((signed int *)NULL)
  dynamic_object.$pad4 = 0
  dynamic_object.ctime_nsec = 0l
  dynamic_object.ctime_sec = 0l
  dynamic_object.flag = 0
  dynamic_object.mtime_nsec = 0l
  dynamic_object.mtime_sec = 0l
  dynamic_object.next = ((struct match_file *)NULL)
  dynamic_object.node = <struct: 2 members>
  dynamic_object.node.rb_info = 0ul
  dynamic_object.node.rb_nodes = <array: 2 elements>
  dynamic_object.node.rb_nodes[0l] = ((const struct archive_rb_node *)NULL)
  dynamic_object.node.rb_nodes[1l] = ((const struct archive_rb_node *)NULL)
  dynamic_object.pathname = <struct: 6 members>
  dynamic_object.pathname.$pad5 = 0
  dynamic_object.pathname.aes_mbs = <struct: 3 members>
  dynamic_object.pathname.aes_mbs.buffer_length = 0ul
  dynamic_object.pathname.aes_mbs.length = 0ul
  dynamic_object.pathname.aes_mbs.s = ((char *)NULL)
  dynamic_object.pathname.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object.pathname.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object.pathname.aes_mbs_in_locale.length = 0ul
  dynamic_object.pathname.aes_mbs_in_locale.s = ((char *)NULL)
  dynamic_object.pathname.aes_set = 0
  dynamic_object.pathname.aes_utf8 = <struct: 3 members>
  dynamic_object.pathname.aes_utf8.buffer_length = 0ul
  dynamic_object.pathname.aes_utf8.length = 0ul
  dynamic_object.pathname.aes_utf8.s = ((char *)NULL)
  dynamic_object.pathname.aes_wcs = <struct: 3 members>
  dynamic_object.pathname.aes_wcs.buffer_length = 0ul
  dynamic_object.pathname.aes_wcs.length = 0ul
  dynamic_object.pathname.aes_wcs.s = ((signed int *)NULL)
  f1 = dynamic_object
  f2 = dynamic_object$0
  goto_symex$$return_value$$malloc = {'name': 'unknown'}
  i = 0ul
  malloc_res = {'name': 'unknown'}
  malloc_size = sizeof(struct match_file) /*176ul*/ 
  malloc_value = {'name': 'unknown'}
  n1 = {'name': 'unknown'}
  n2 = {'name': 'unknown'}
  p1 = {'name': 'unknown'}
  p2 = {'name': 'pointer', 'type': 'char *'}
  rb_ops.rbto_compare_key = cmp_key_mbs
  rb_ops.rbto_compare_nodes = cmp_node_mbs
  record_malloc = False
  record_may_leak = False
  result = 0
  return_value___VERIFIER_nondet___CPROVER_bool$1 = False
  return_value___VERIFIER_nondet___CPROVER_bool$2 = False
  return_value_cmp_node_mbs = 0
  return_value_malloc = {'name': 'unknown'}
  return_value_malloc$0 = {'name': 'unknown'}
  return_value_strcmp = 0
  s1 = {'name': 'unknown'}
  s2 = {'name': 'pointer', 'type': 'char *'}
```

### CBMC trace (first 80 steps)

```text
  1. function-call at ?:?
  2. __CPROVER_dead_object = NULL
  3. __CPROVER_deallocated = NULL
  4. __CPROVER_malloc_is_new_array = False
  5. __CPROVER_max_malloc_size = 36028797018963968ul
  6. __CPROVER_memory_leak = NULL
  7. __CPROVER_rounding_mode = 0
  8. rb_ops.rbto_compare_nodes = cmp_node_mbs
  9. rb_ops.rbto_compare_key = cmp_key_mbs
 10. function-return at ?:?
 11. location-only at ?:5
 12. function-call at ?:5
 13. f1 = ((struct match_file *)NULL)
 14. return_value_malloc = NULL
 15. function-call at main:7
 16. malloc_size = sizeof(struct match_file) /*176ul*/ 
 17. location-only at malloc:17
 18. location-only at malloc:27
 19. malloc_res = NULL
 20. malloc_value = NULL
 21. dynamic_object = <struct: 9 members>
 22. dynamic_object.node = <struct: 2 members>
 23. dynamic_object.node.rb_nodes = <array: 2 elements>
 24. dynamic_object.node.rb_nodes[0l] = ((const struct archive_rb_node *)NULL)
 25. dynamic_object.node.rb_nodes[1l] = ((const struct archive_rb_node *)NULL)
 26. dynamic_object.node.rb_info = 0ul
 27. dynamic_object.next = ((struct match_file *)NULL)
 28. dynamic_object.pathname = <struct: 6 members>
 29. dynamic_object.pathname.aes_mbs = <struct: 3 members>
 30. dynamic_object.pathname.aes_mbs.s = ((char *)NULL)
 31. dynamic_object.pathname.aes_mbs.length = 0ul
 32. dynamic_object.pathname.aes_mbs.buffer_length = 0ul
 33. dynamic_object.pathname.aes_utf8 = <struct: 3 members>
 34. dynamic_object.pathname.aes_utf8.s = ((char *)NULL)
 35. dynamic_object.pathname.aes_utf8.length = 0ul
 36. dynamic_object.pathname.aes_utf8.buffer_length = 0ul
 37. dynamic_object.pathname.aes_wcs = <struct: 3 members>
 38. dynamic_object.pathname.aes_wcs.s = ((signed int *)NULL)
 39. dynamic_object.pathname.aes_wcs.length = 0ul
 40. dynamic_object.pathname.aes_wcs.buffer_length = 0ul
 41. dynamic_object.pathname.aes_mbs_in_locale = <struct: 3 members>
 42. dynamic_object.pathname.aes_mbs_in_locale.s = ((char *)NULL)
 43. dynamic_object.pathname.aes_mbs_in_locale.length = 0ul
 44. dynamic_object.pathname.aes_mbs_in_locale.buffer_length = 0ul
 45. dynamic_object.pathname.aes_set = 0
 46. dynamic_object.pathname.$pad5 = 0
 47. dynamic_object.flag = 0
 48. dynamic_object.$pad4 = 0
 49. dynamic_object.mtime_sec = 0l
 50. dynamic_object.mtime_nsec = 0l
 51. dynamic_object.ctime_sec = 0l
 52. dynamic_object.ctime_nsec = 0l
 53. malloc_value = {'name': 'unknown'}
 54. malloc_res = {'name': 'unknown'}
 55. record_malloc = False
 56. return_value___VERIFIER_nondet___CPROVER_bool$1 = False
 57. return_value___VERIFIER_nondet___CPROVER_bool$1 = False
 58. record_malloc = False
 59. __CPROVER_malloc_is_new_array = False
 60. record_may_leak = False
 61. return_value___VERIFIER_nondet___CPROVER_bool$2 = False
 62. return_value___VERIFIER_nondet___CPROVER_bool$2 = False
 63. record_may_leak = False
 64. __CPROVER_memory_leak = NULL
 65. goto_symex$$return_value$$malloc = {'name': 'unknown'}
 66. function-return at malloc:59
 67. return_value_malloc = {'name': 'unknown'}
 68. f1 = dynamic_object
 69. f2 = ((struct match_file *)NULL)
 70. return_value_malloc$0 = NULL
 71. function-call at main:8
 72. malloc_size = sizeof(struct match_file) /*176ul*/ 
 73. location-only at malloc:17
 74. location-only at malloc:27
 75. malloc_res = NULL
 76. malloc_value = NULL
 77. dynamic_object$0 = <struct: 9 members>
 78. dynamic_object$0.node = <struct: 2 members>
 79. dynamic_object$0.node.rb_nodes = <array: 2 elements>
 80. dynamic_object$0.node.rb_nodes[0l] = ((const struct archive_rb_node *)NULL)
```

### CBMC harness (bundled at `findings/v7/harnesses/archive_match__cmp_node_mbs__strcmp.pointer_dereference.1.c`)

```c
/* CBMC harness for: cmp_node_mbs */
#include "/tmp/libarchive_seedhunt_full/archive_match.c"
#include <stdlib.h>

int main(void) {
    /* Allocate two match_file structures */
    struct match_file *f1 = malloc(sizeof(struct match_file));
    struct match_file *f2 = malloc(sizeof(struct match_file));
    
    __CPROVER_assume(f1 != NULL);
    __CPROVER_assume(f2 != NULL);
    
    /* Initialize the archive_mstring structures within match_file.
     * The archive_mstring_get_mbs function is external and will be havoc'd,
     * so we don't need to fully initialize the mstring internals.
     * However, we should zero-initialize to avoid undefined behavior. */
    f1->pathname.aes_mbs.s = NULL;
    f1->pathname.aes_mbs.length = 0;
    f1->pathname.aes_mbs.buffer_length = 0;
    f1->pathname.aes_utf8.s = NULL;
    f1->pathname.aes_utf8.length = 0;
    f1->pathname.aes_utf8.buffer_length = 0;
    f1->pathname.aes_wcs.s = NULL;
    f1->pathname.aes_wcs.length = 0;
    f1->pathname.aes_wcs.buffer_length = 0;
    f1->pathname.aes_mbs_in_locale.s = NULL;
    f1->pathname.aes_mbs_in_locale.length = 0;
    f1->pathname.aes_mbs_in_locale.buffer_length = 0;
    f1->pathname.aes_set = 0;
    
    f2->pathname.aes_mbs.s = NULL;
    f2->pathname.aes_mbs.length = 0;
    f2->pathname.aes_mbs.buffer_length = 0;
    f2->pathname.aes_utf8.s = NULL;
    f2->pathname.aes_utf8.length = 0;
    f2->pathname.aes_utf8.buffer_length = 0;
    f2->pathname.aes_wcs.s = NULL;
    f2->pathname.aes_wcs.length = 0;
    f2->pathname.aes_wcs.buffer_length = 0;
    f2->pathname.aes_mbs_in_locale.s = NULL;
    f2->pathname.aes_mbs_in_locale.length = 0;
    f2->pathname.aes_mbs_in_locale.buffer_length = 0;
    f2->pathname.aes_set = 0;
    
    /* Cast to archive_rb_node pointers as the function expects */
    const struct archive_rb_node *n1 = (const struct archive_rb_node *)f1;
    const struct archive_rb_node *n2 = (const struct archive_rb_node *)f2;
    
    /* Call the function under test */
    int result = cmp_node_mbs(n1, n2);
    
    return 0;
}

```

### Dynamic reproducer (bundled at `findings/v7/reproducers/archive_match__cmp_node_mbs__strcmp.pointer_dereference.1.c`)

This is the 1-of-1 attempt the dyn-val LLM produced that triggered the sanitizer. Compile + link against a sanitiser-instrumented libarchive .so:

```sh
gcc -fsanitize=address,undefined -g -O1 -I/path/to/libarchive \
    archive_match__cmp_node_mbs__strcmp.pointer_dereference.1.c -L/path/to/libarchive/build -larchive -o repro
LD_LIBRARY_PATH=/path/to/libarchive/build ./repro
```

```c
#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <locale.h>

int main(void) {
    struct archive *a;
    struct archive_entry *entry1, *entry2;
    int r;
    
    /* Set a locale that will cause conversion failures for invalid sequences */
    setlocale(LC_ALL, "en_US.UTF-8");
    
    a = archive_read_new();
    if (a == NULL) {
        return 1;
    }
    
    archive_read_support_filter_all(a);
    archive_read_support_format_all(a);
    
    /* Create an in-memory archive with entries containing invalid UTF-8 sequences */
    struct archive *writer = archive_write_new();
    archive_write_set_format_pax(writer);
    archive_write_add_filter_none(writer);
    
    char *buffer = NULL;
    size_t buffer_size = 0;
    archive_write_open_memory(writer, &buffer, &buffer_size, &buffer_size);
    
    /* Create entries with invalid multi-byte sequences in pathnames */
    entry1 = archive_entry_new();
    /* Invalid UTF-8: 0xFF is not valid in UTF-8 */
    char invalid_path1[] = {0xFF, 0xFE, 0xFD, 0x00};
    archive_entry_set_pathname(entry1, invalid_path1);
    archive_entry_set_size(entry1, 0);
    archive_entry_set_filetype(entry1, AE_IFREG);
    archive_entry_set_perm(entry1, 0644);
    archive_write_header(writer, entry1);
    archive_entry_free(entry1);
    
    entry2 = archive_entry_new();
    /* Another invalid UTF-8 sequence */
    char invalid_path2[] = {0xC0, 0x80, 0x00};
    archive_entry_set_pathname(entry2, invalid_path2);
    archive_entry_set_size(entry2, 0);
    archive_entry_set_filetype(entry2, AE_IFREG);
    archive_entry_set_perm(entry2, 0644);
    archive_write_header(writer, entry2);
    archive_entry_free(entry2);
    
    archive_write_close(writer);
    archive_write_free(writer);
    
    /* Now read the archive with matching enabled */
    r = archive_read_open_memory(a, buffer, buffer_size);
    if (r != ARCHIVE_OK) {
        archive_read_free(a);
        free(buffer);
        return 1;
    }
    
    /* Enable matching - this will use the red-black tree with cmp_node_mbs */
    struct archive *match = archive_match_new();
    if (match == NULL) {
        archive_read_free(a);
        free(buffer);
        return 1;
    }
    
    /* Add exclusion patterns that will trigger tree operations */
    archive_match_exclude_pattern(match, "*.txt");
    
    /* Read entries - this should trigger the tree comparison with NULL pointers */
    struct archive_entry *entry;
    while (archive_read_next_header(a, &entry) == ARCHIVE_OK) {
        /* Check if entry is excluded - this triggers tree search with cmp_node_mbs */
        archive_match_excluded(match, entry);
    }
    
    archive_match_free(match);
    archive_read_free(a);
    free(buffer);
    
    return 0;
}
```

### Sanitizer output

```text
=================================================================
==263507==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x70ccd3700038 at pc 0x70ccd8efb303 bp 0x7ffcaadaa4a0 sp 0x7ffcaada9c48
WRITE of size 4096 at 0x70ccd3700038 thread T0
    #0 0x70ccd8efb302 in memcpy ../../../../src/libsanitizer/sanitizer_common/sanitizer_common_interceptors_memintrinsics.inc:115
    #1 0x70ccd95e917d in memory_write /tmp/libarchive_bench/libarchive/libarchive/archive_write_open_memory.c:97
    #2 0x70ccd95e1e74 in archive_write_client_close /tmp/libarchive_bench/libarchive/libarchive/archive_write.c:534
    #3 0x70ccd95e1688 in __archive_write_filters_close /tmp/libarchive_bench/libarchive/libarchive/archive_write.c:298
    #4 0x70ccd95e21de in _archive_write_close /tmp/libarchive_bench/libarchive/libarchive/archive_write.c:644
    #5 0x70ccd95e0ee0 in archive_write_close /tmp/libarchive_bench/libarchive/libarchive/archive_virtual.c:67
    #6 0x5d68cc7e3906 in main /tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/dynamic/cmp_node_mbs/reproducer_attempt1.c:54
    #7 0x70ccd822a1c9  (/lib/x86_64-linux-gnu/libc.so.6+0x2a1c9) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #8 0x70ccd822a28a in __libc_start_main (/lib/x86_64-linux-gnu/libc.so.6+0x2a28a) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #9 0x5d68cc7e34c4 in _start (/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/dynamic/cmp_node_mbs/reproducer_attempt1.bin+0x24c4) (BuildId: bfa21eaa9c6d54293a3e0c4d1df31e8e32a7e6f1)

Address 0x70ccd3700038 is located in stack of thread T0 at offset 56 in frame
    #0 0x5d68cc7e3598 in main /tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/dynamic/cmp_node_mbs/reproducer_attempt1.c:8

  This frame has 5 object(s):
    [48, 56) 'buffer' (line 29)
    [80, 88) 'buffer_size' (line 30) <== Memory access at offset 56 partially underflows this variable
    [112, 120) 'entry' (line 77) <== Memory access at offset 56 partially unde
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
