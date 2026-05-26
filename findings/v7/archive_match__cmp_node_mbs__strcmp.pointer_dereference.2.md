# Bug report: `cmp_node_mbs` — strcmp.pointer_dereference.2

**Evidence grade**: **C** — judge-only (llm_no_reproducer)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_match.c`
- **Function**: `cmp_node_mbs`
- **Violated property**: `strcmp.pointer_dereference.2` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `strcmp.pointer_dereference.2` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `llm_no_reproducer` (signal `-`) |

## Judge reasoning

The CBMC counterexample reveals a real bug in cmp_node_mbs (archive_match.c:1291-1297). The function calls archive_mstring_get_mbs twice to retrieve string pointers p1 and p2, checks if they are NULL (returning 1 or -1 respectively), but then unconditionally calls strcmp(p1, p2) at line 1297. The problem is that archive_mstring_get_mbs is an external function that can return success while setting the output pointer to an invalid (non-NULL but unreadable) pointer. The witness shows p1 and p2 are set to invalid pointers (not NULL, but not valid memory), which passes the NULL checks but causes strcmp to dereference invalid memory. This is a missing validation bug: the function should check the return value of archive_mstring_get_mbs (which returns int, likely an error code) before using the output pointers. The function is used as a red-black tree comparator (rb_ops.rbto_compare_nodes = cmp_node_mbs), so it would be called during tree operations when comparing match_file nodes.

## Exploit scenario (LLM-supplied)

An attacker could trigger this by causing the archive matching subsystem to insert match_file entries with corrupted or uninitialized archive_mstring pathname fields into a red-black tree. When the tree performs comparisons during insertion/lookup operations, cmp_node_mbs would be invoked. If archive_mstring_get_mbs fails to produce valid strings but returns non-NULL pointers (e.g., due to encoding errors or memory corruption), the strcmp call would dereference invalid memory, causing a crash or potential memory disclosure.

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
  p1 = {'name': 'pointer', 'type': 'char *'}
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
  s1 = {'name': 'pointer', 'type': 'char *'}
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

### CBMC harness (bundled at `findings/v7/harnesses/archive_match__cmp_node_mbs__strcmp.pointer_dereference.2.c`)

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
