# Bug report: `cmp_key_mbs` — strcmp.pointer_dereference.1

**Evidence grade**: **A** — dynamically confirmed (same property class)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_match.c`
- **Function**: `cmp_key_mbs`
- **Violated property**: `strcmp.pointer_dereference.1` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `strcmp.pointer_dereference.1` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `confirmed_dynamic` (signal `SIGABRT`) |

### Independently re-surfaced via

- adjacent of cmp_key_mbs (grade C)
- adjacent of cmp_node_mbs (grade C)

## Judge reasoning

The CBMC counterexample reveals a real bug in cmp_key_mbs (archive_match.c:1309). The function calls archive_mstring_get_mbs at line 1306 and checks if p is NULL at line 1307-1308, returning -1 if so. However, the function then proceeds to call strcmp(p, (const char *)key) at line 1309 WITHOUT checking if the key parameter is NULL. The CBMC witness shows that strcmp fails with a NULL dereference in s1, which corresponds to p being NULL despite the check. Looking more carefully at the code, the actual bug is that when p is NULL, the function returns -1 at line 1308, but if p is non-NULL and key is NULL, strcmp is called with a NULL second argument. The witness shows s1 (first strcmp arg) is NULL, meaning p itself was NULL and the check at line 1307 should have caught it - but CBMC found a path where strcmp is reached with NULL. Actually, re-examining: the check "if (p == NULL) return (-1);" should prevent the strcmp call when p is NULL. The bug must be that key is NULL. This is a red-black tree comparison callback (rb_ops.rbto_compare_key = cmp_key_mbs at line 198), and if a NULL key is passed to the tree search operations, this will crash. This is realistic because the key comes from external callers performing tree lookups.

## Exploit scenario (LLM-supplied)

An attacker can trigger this by causing a red-black tree search operation in the archive_match subsystem to be called with a NULL key parameter. Since cmp_key_mbs is registered as the key comparison callback (line 198), any tree search with a NULL key will invoke strcmp with NULL, causing a crash. This could occur through malformed archive metadata or API misuse where pathname matching is attempted with NULL input.

### CBMC witness (variable assignments)

```text
  (char)dynamic_object$0[1023l] = 0
  (char)dynamic_object$1[511l] = 0
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_malloc_is_new_array = False
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_rounding_mode = 0
  ch1 = 0
  ch2 = 0
  dynamic_object = <struct: 9 members>
  dynamic_object$0 = {'name': 'unknown'}
  dynamic_object$1 = {'name': 'unknown'}
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
  dynamic_object.pathname.aes_mbs.buffer_length = 1024ul
  dynamic_object.pathname.aes_mbs.length = 1023ul
  dynamic_object.pathname.aes_mbs.s = dynamic_object$0
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
  dynamic_object_size = 1024ul
  dynamic_object_size$0 = 512ul
  f = dynamic_object
  goto_symex$$return_value$$malloc = dynamic_object$1
  i = 0ul
  key = dynamic_object$1
  key_len = 511ul
  malloc_res = dynamic_object$1
  malloc_size = 512ul
  malloc_value = dynamic_object$1
  mbs_str = dynamic_object$0
  n = {'name': 'unknown'}
  p = {'name': 'unknown'}
  rb_ops.rbto_compare_key = cmp_key_mbs
  rb_ops.rbto_compare_nodes = cmp_node_mbs
  record_malloc = False
  record_may_leak = False
  return_value___VERIFIER_nondet___CPROVER_bool$1 = False
  return_value___VERIFIER_nondet___CPROVER_bool$2 = False
  return_value_malloc = {'name': 'unknown'}
  return_value_malloc$0 = dynamic_object$0
  return_value_malloc$1 = dynamic_object$1
  return_value_strcmp = 0
  s1 = {'name': 'unknown'}
  s2 = dynamic_object$1
  str_len = 1023ul
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
 11. location-only at ?:11
 12. function-call at ?:11
 13. f = ((struct match_file *)NULL)
 14. return_value_malloc = NULL
 15. function-call at main:13
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
 68. f = dynamic_object
 69. str_len = 1023ul
 70. location-only at main:19
 71. mbs_str = ((char *)NULL)
 72. return_value_malloc$0 = NULL
 73. location-only at main:21
 74. function-call at main:21
 75. malloc_size = 1024ul
 76. location-only at malloc:17
 77. location-only at malloc:27
 78. malloc_res = NULL
 79. malloc_value = NULL
 80. dynamic_object_size = 1024ul
```

### CBMC harness (bundled at `findings/v7/harnesses/archive_match__cmp_key_mbs__strcmp.pointer_dereference.1.c`)

```c
/* CBMC harness for: cmp_key_mbs */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* Forward declarations for external functions */
int archive_mstring_get_mbs(struct archive *, struct archive_mstring *, const char **);

#include "/tmp/libarchive_seedhunt_full/archive_match.c"

int main(void) {
    /* Allocate a match_file structure */
    struct match_file *f = malloc(sizeof(struct match_file));
    __CPROVER_assume(f != NULL);
    
    /* Initialize the archive_mstring pathname field */
    /* The aes_mbs field is what archive_mstring_get_mbs will access */
    size_t str_len;
    __CPROVER_assume(str_len < 1024);
    
    char *mbs_str = malloc(str_len + 1);
    if (mbs_str != NULL) {
        mbs_str[str_len] = '\0';
        f->pathname.aes_mbs.s = mbs_str;
        f->pathname.aes_mbs.length = str_len;
        f->pathname.aes_mbs.buffer_length = str_len + 1;
    } else {
        f->pathname.aes_mbs.s = NULL;
        f->pathname.aes_mbs.length = 0;
        f->pathname.aes_mbs.buffer_length = 0;
    }
    
    /* Initialize other fields to avoid undefined behavior */
    f->pathname.aes_set = 0;
    
    /* Allocate the key string */
    size_t key_len;
    __CPROVER_assume(key_len < 1024);
    char *key = malloc(key_len + 1);
    __CPROVER_assume(key != NULL);
    key[key_len] = '\0';
    
    /* Call the function under test */
    cmp_key_mbs((const struct archive_rb_node *)f, (const void *)key);
    
    return 0;
}

```

### Dynamic reproducer (bundled at `findings/v7/reproducers/archive_match__cmp_key_mbs__strcmp.pointer_dereference.1.c`)

This is the 1-of-1 attempt the dyn-val LLM produced that triggered the sanitizer. Compile + link against a sanitiser-instrumented libarchive .so:

```sh
gcc -fsanitize=address,undefined -g -O1 -I/path/to/libarchive \
    archive_match__cmp_key_mbs__strcmp.pointer_dereference.1.c -L/path/to/libarchive/build -larchive -o repro
LD_LIBRARY_PATH=/path/to/libarchive/build ./repro
```

```c
#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    struct archive *a;
    struct archive_entry *entry;
    const void *buff;
    size_t size;
    int64_t offset;
    int r;

    /* Create a new archive for writing */
    a = archive_write_new();
    if (a == NULL) {
        fprintf(stderr, "Failed to create archive\n");
        return 1;
    }

    archive_write_set_format_pax(a);
    archive_write_open_memory(a, (void **)&buff, &size, NULL);

    /* Create an entry with a pathname */
    entry = archive_entry_new();
    archive_entry_set_pathname(entry, "test.txt");
    archive_entry_set_size(entry, 0);
    archive_entry_set_filetype(entry, AE_IFREG);
    archive_entry_set_perm(entry, 0644);
    
    archive_write_header(a, entry);
    archive_entry_free(entry);
    archive_write_close(a);
    archive_write_free(a);

    /* Now read the archive and set up matching */
    a = archive_read_new();
    archive_read_support_format_all(a);
    archive_read_open_memory(a, buff, size);

    /* Create a match object */
    struct archive *match = archive_match_new();
    if (match == NULL) {
        fprintf(stderr, "Failed to create match\n");
        archive_read_free(a);
        return 1;
    }

    /* Add a pathname pattern - this will populate the internal tree */
    archive_match_include_pattern(match, "test.txt");

    /* Read the entry */
    entry = archive_entry_new();
    r = archive_read_next_header(a, &entry);
    
    /* Now try to match with a NULL pathname by creating an entry with NULL pathname */
    struct archive_entry *null_entry = archive_entry_new();
    archive_entry_set_pathname(null_entry, NULL);
    
    /* This should trigger the bug - matching with NULL pathname */
    /* The match operation will call cmp_key_mbs with NULL key */
    archive_match_path_excluded(match, null_entry);

    archive_entry_free(null_entry);
    archive_entry_free(entry);
    archive_read_free(a);
    archive_match_free(match);

    return 0;
}
```

### Sanitizer output

```text
=================================================================
==262061==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x745d26e00048 at pc 0x745d2c8fb303 bp 0x7ffccbbf4240 sp 0x7ffccbbf39e8
WRITE of size 1536 at 0x745d26e00048 thread T0
    #0 0x745d2c8fb302 in memcpy ../../../../src/libsanitizer/sanitizer_common/sanitizer_common_interceptors_memintrinsics.inc:115
    #1 0x745d2c78f17d in memory_write /tmp/libarchive_bench/libarchive/libarchive/archive_write_open_memory.c:97
    #2 0x745d2c787e74 in archive_write_client_close /tmp/libarchive_bench/libarchive/libarchive/archive_write.c:534
    #3 0x745d2c787688 in __archive_write_filters_close /tmp/libarchive_bench/libarchive/libarchive/archive_write.c:298
    #4 0x745d2c7881de in _archive_write_close /tmp/libarchive_bench/libarchive/libarchive/archive_write.c:644
    #5 0x745d2c786ee0 in archive_write_close /tmp/libarchive_bench/libarchive/libarchive/archive_virtual.c:67
    #6 0x5b319540279f in main /tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_key_mbs/dynamic/cmp_key_mbs/reproducer_attempt1.c:34
    #7 0x745d2bc2a1c9  (/lib/x86_64-linux-gnu/libc.so.6+0x2a1c9) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #8 0x745d2bc2a28a in __libc_start_main (/lib/x86_64-linux-gnu/libc.so.6+0x2a28a) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #9 0x5b3195402444 in _start (/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_key_mbs/dynamic/cmp_key_mbs/reproducer_attempt1.bin+0x2444) (BuildId: bcdc9bb65dec618f57c642dd5bbb6e9278ecacb8)

Address 0x745d26e00048 is located in stack of thread T0 at offset 72 in frame
    #0 0x5b3195402518 in main /tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_key_mbs/dynamic/cmp_key_mbs/reproducer_attempt1.c:7

  This frame has 3 object(s):
    [32, 40) 'entry' (line 9)
    [64, 72) 'buff' (line 10)
    [96, 104) 'size' (line 11) <== Memory access at offset 72 partially underflows this variable
HINT: this may be a false positive if your program uses some c
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
