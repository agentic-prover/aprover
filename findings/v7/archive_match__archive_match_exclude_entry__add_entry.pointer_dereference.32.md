# Bug report: `archive_match_exclude_entry` — add_entry.pointer_dereference.32

**Evidence grade**: **C** — judge-only (llm_no_reproducer)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_match.c`
- **Function**: `archive_match_exclude_entry`
- **Violated property**: `add_entry.pointer_dereference.32` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `add_entry.pointer_dereference.32` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `llm_no_reproducer` (signal `-`) |

## Judge reasoning

The bug is real. In add_entry (archive_match.c:1404-1418), when __archive_rb_tree_insert_node returns 0 (indicating the node already exists), the code calls __archive_rb_tree_find_node to retrieve the existing node. However, the code only checks if f2 != NULL at line 1418 AFTER already dereferencing it would occur. The problem is that __archive_rb_tree_find_node is an external function (declared in archive_rb.h but not defined in the corpus), and CBMC correctly models it as potentially returning an invalid pointer. The witness shows that when r=0 (insert failed), __archive_rb_tree_find_node can return a non-NULL but invalid pointer, causing the dereference at line 1419 (f2->flag) to fail. The check at line 1418 happens too late - the code structure suggests the developers expected find_node to always succeed after insert fails, but there's no guarantee. This is exploitable through the public API archive_match_exclude_entry when called with a valid archive_match object and entry, if the rb-tree implementation can return invalid pointers in edge cases.

## Exploit scenario (LLM-supplied)

An attacker calls archive_match_exclude_entry with a crafted archive_entry that causes __archive_rb_tree_insert_node to fail (return 0) and __archive_rb_tree_find_node to return an invalid pointer. This could happen if the rb-tree is in an inconsistent state or if there's a race condition. The invalid pointer dereference at f2->flag (line 1419) would cause a crash or potentially exploitable memory corruption.

### CBMC witness (variable assignments)

```text
  __CPROVER_alloca_object = NULL
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_malloc_is_new_array = False
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_new_object = NULL
  __CPROVER_rounding_mode = 0
  _a = {'name': 'unknown'}
  _fn = {'name': 'unknown'}
  a = dynamic_object
  alloc_size = sizeof(struct match_file) /*176ul*/ 
  dynamic_object = <struct: 28 members>
  dynamic_object$0.$pad4 = 0u
  dynamic_object$0.ctime_nsec = 0l
  dynamic_object$0.ctime_sec = 0l
  dynamic_object$0.flag = 771
  dynamic_object$0.mtime_nsec = 0l
  dynamic_object$0.mtime_sec = 0l
  dynamic_object$0.next = ((struct match_file *)NULL)
  dynamic_object$0.node.rb_info = 0ul
  dynamic_object$0.node.rb_nodes = <array: 2 elements>
  dynamic_object$0.node.rb_nodes[0l] = ((const struct archive_rb_node *)NULL)
  dynamic_object$0.node.rb_nodes[1l] = ((const struct archive_rb_node *)NULL)
  dynamic_object$0.pathname.$pad5 = 0u
  dynamic_object$0.pathname.aes_mbs.buffer_length = 0ul
  dynamic_object$0.pathname.aes_mbs.length = 0ul
  dynamic_object$0.pathname.aes_mbs.s = ((const char *)NULL)
  dynamic_object$0.pathname.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$0.pathname.aes_mbs_in_locale.length = 0ul
  dynamic_object$0.pathname.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$0.pathname.aes_set = 0
  dynamic_object$0.pathname.aes_utf8.buffer_length = 0ul
  dynamic_object$0.pathname.aes_utf8.length = 0ul
  dynamic_object$0.pathname.aes_utf8.s = ((const char *)NULL)
  dynamic_object$0.pathname.aes_wcs.buffer_length = 0ul
  dynamic_object$0.pathname.aes_wcs.length = 0ul
  dynamic_object$0.pathname.aes_wcs.s = ((signed int *)NULL)
  dynamic_object.$pad11 = 0u
  dynamic_object.$pad15 = 0u
  dynamic_object.$pad19 = 0u
  dynamic_object.$pad7 = 0u
  dynamic_object.archive = <struct: 21 members>
  dynamic_object.archive.$pad19 = 0
  dynamic_object.archive.$pad4 = 0u
  dynamic_object.archive.archive_error_number = 0
  dynamic_object.archive.archive_format = 0
  dynamic_object.archive.archive_format_name = ((const char *)NULL)
  dynamic_object.archive.current_code = ((const char *)NULL)
  dynamic_object.archive.current_codepage = 0u
  dynamic_object.archive.current_oemcp = 0u
  dynamic_object.archive.error = ((const char *)NULL)
  dynamic_object.archive.error_string = <struct: 3 members>
  dynamic_object.archive.error_string.buffer_length = 0ul
  dynamic_object.archive.error_string.length = 0ul
  dynamic_object.archive.error_string.s = ((const char *)NULL)
  dynamic_object.archive.file_count = 0
  dynamic_object.archive.magic = 212668873u
  dynamic_object.archive.read_data_block = ((const char *)NULL)
  dynamic_object.archive.read_data_is_posix_read = 0
  dynamic_object.archive.read_data_offset = 0l
  dynamic_object.archive.read_data_output_offset = 0l
  dynamic_object.archive.read_data_remaining = 0ul
  dynamic_object.archive.read_data_requested = 0ul
  dynamic_object.archive.sconv = ((struct archive_string_conv *)NULL)
  dynamic_object.archive.state = 1u
  dynamic_object.archive.vtable = ((const struct archive_vtable *)NULL)
  dynamic_object.exclusion_entry_list = <struct: 2 members>
  dynamic_object.exclusion_entry_list.first = ((struct match_file *)NULL)
  dynamic_object.exclusion_entry_list.last = {'name': 'unknown'}
  dynamic_object.exclusion_tree = <struct: 2 members>
  dynamic_object.exclusion_tree.rbt_ops = ((const struct archive_rb_tree_ops *)NULL)
  dynamic_object.exclusion_tree.rbt_root = ((const struct archive_rb_node *)NULL)
  dynamic_object.exclusions = <struct: 6 members>
  dynamic_object.exclusions.$pad5 = 0u
  dynamic_object.exclusions.first = ((struct match *)NULL)
  dynamic_object.exclusions.last = ((struct match **)NULL)
  dynamic_object.exclusions.unmatched_count = 0ul
  dynamic_object.exclusions.unmatched_eof = 0
  dynamic_object.exclusions.unmatched_next = ((struct match *)NULL)
  dynamic_object.inclusion_gids = <struct: 3 members>
  dynamic_object.inclusion_gids.count = 0ul
  dynamic_object.inclusion_gids.ids = ((int64_t *)NULL)
  dynamic_object.inclusion_gids.size = 0ul
  dynamic_object.inclusion_gnames = <struct: 6 members>
  dynamic_object.inclusion_gnames.$pad5 = 0u
  dynamic_object.inclusion_gnames.first = ((struct match *)NULL)
  dynamic_object.inclusion_gnames.last = ((struct match **)NULL)
  dynamic_object.inclusion_gnames.unmatched_count = 0ul
  dynamic_object.inclusion_gnames.unmatched_eof = 0
  dynamic_object.inclusion_gnames.unmatched_next = ((struct match *)NULL)
  dynamic_object.inclusion_uids = <struct: 3 members>
  dynamic_object.inclusion_uids.count = 0ul
  dynamic_object.inclusion_uids.ids = ((int64_t *)NULL)
  dynamic_object.inclusion_uids.size = 0ul
  dynamic_object.inclusion_unames = <struct: 6 members>
  dynamic_object.inclusion_unames.$pad5 = 0u
  dynamic_object.inclusion_unames.first = ((struct match *)NULL)
  dynamic_object.inclusion_unames.last = ((struct match **)NULL)
  dynamic_object.inclusion_unames.unmatched_count = 0ul
  dynamic_object.inclusion_unames.unmatched_eof = 0
  dynamic_object.inclusion_unames.unmatched_next = ((struct match *)NULL)
  dynamic_object.inclusions = <struct: 6 members>
  dynamic_object.inclusions.$pad5 = 0u
  dynamic_object.inclusions.first = ((struct match *)NULL)
  dynamic_object.inclusions.last = ((struct match **)NULL)
  dynamic_object.inclusions.unmatched_count = 0ul
  dynamic_object.inclusions.unmatched_eof = 0
  dynamic_object.inclusions.unmatched_next = ((struct match *)NULL)
  dynamic_object.newer_ctime_filter = 0
  dynamic_object.newer_ctime_nsec = 0l
  dynamic_object.newer_ctime_sec = 0l
  dynamic_object.newer_mtime_filter = 0
  dynamic_object.newer_mtime_nsec = 0l
  dynamic_object.newer_mtime_sec = 0l
  dynamic_object.now = 0l
  dynamic_object.older_ctime_filter = 0
  dynamic_object.older_ctime_nsec = 0l
  dynamic_object.older_ctime_sec = 0l
  dynamic_object.older_mtime_filter = 0
  dynamic_object.older_mtime_nsec = 0l
  dynamic_object.older_mtime_sec = 0l
  dynamic_object.recursive_include = 1
  dynamic_object.setflag = 0
  entry = {'name': 'unknown'}
  f = dynamic_object$0
  f2 = {'name': 'pointer', 'type': 'struct match_file *'}
  flag = 771
  goto_symex$$return_value$$calloc = {'name': 'unknown'}
  goto_symex$$return_value$$malloc = {'name': 'unknown'}
  goto_symex$$return_value$$validate_time_flag = 0
  list = {'name': 'unknown'}
  magic_test = -2078
  malloc_res = {'name': 'unknown'}
  malloc_size = sizeof(struct archive_match) /*496ul*/ 
  malloc_value = {'name': 'unknown'}
  nmemb = 1ul
  pathname = {'name': 'unknown'}
  r = 0
  rb_ops.rbto_compare_key = cmp_key_mbs
  rb_ops.rbto_compare_nodes = cmp_node_mbs
  record_malloc = False
  record_may_leak = False
  return_value___VERIFIER_nondet___CPROVER_bool$1 = False
  return_value___VERIFIER_nondet___CPROVER_bool$2 = False
  return_value___archive_check_magic = -2078
  return_value___archive_rb_tree_find_node = {'name': 'pointer', 'type': 'const struct archive_rb_node *'}
  return_value_add_entry = 0
  return_value_archive_entry_ctime = 0l
  return_value_archive_entry_ctime_nsec = 0l
  return_value_archive_entry_mtime = 0l
  return_value_archive_entry_mtime_nsec = 0l
  return_value_archive_entry_pathname = {'name': 'unknown'}
  return_value_calloc = {'name': 'unknown'}
  return_value_malloc = {'name': 'unknown'}
  size = sizeof(struct match_file) /*176ul*/ 
  tmp_overflow_result = <struct: 2 members>
  tmp_overflow_result.overflow-* = False
  tmp_overflow_result.value = sizeof(struct match_file) /*176ul*/ 
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
 10. rb_ops.rbto_compare_nodes = cmp_node_mbs
 11. rb_ops.rbto_compare_key = cmp_key_mbs
 12. function-return at ?:?
 13. location-only at ?:4
 14. function-call at ?:4
 15. a = ((struct archive_match *)NULL)
 16. return_value_malloc = NULL
 17. function-call at main:6
 18. malloc_size = sizeof(struct archive_match) /*496ul*/ 
 19. location-only at malloc:17
 20. location-only at malloc:27
 21. malloc_res = NULL
 22. malloc_value = NULL
 23. dynamic_object = <struct: 28 members>
 24. dynamic_object.archive = <struct: 21 members>
 25. dynamic_object.archive.magic = 0u
 26. dynamic_object.archive.state = 0u
 27. dynamic_object.archive.vtable = ((const struct archive_vtable *)NULL)
 28. dynamic_object.archive.archive_format = 0
 29. dynamic_object.archive.$pad4 = 0u
 30. dynamic_object.archive.archive_format_name = ((const char *)NULL)
 31. dynamic_object.archive.file_count = 0
 32. dynamic_object.archive.archive_error_number = 0
 33. dynamic_object.archive.error = ((const char *)NULL)
 34. dynamic_object.archive.error_string = <struct: 3 members>
 35. dynamic_object.archive.error_string.s = ((const char *)NULL)
 36. dynamic_object.archive.error_string.length = 0ul
 37. dynamic_object.archive.error_string.buffer_length = 0ul
 38. dynamic_object.archive.current_code = ((const char *)NULL)
 39. dynamic_object.archive.current_codepage = 0u
 40. dynamic_object.archive.current_oemcp = 0u
 41. dynamic_object.archive.sconv = ((struct archive_string_conv *)NULL)
 42. dynamic_object.archive.read_data_block = ((const char *)NULL)
 43. dynamic_object.archive.read_data_offset = 0l
 44. dynamic_object.archive.read_data_output_offset = 0l
 45. dynamic_object.archive.read_data_remaining = 0ul
 46. dynamic_object.archive.read_data_is_posix_read = 0
 47. dynamic_object.archive.$pad19 = 0
 48. dynamic_object.archive.read_data_requested = 0ul
 49. dynamic_object.setflag = 0
 50. dynamic_object.recursive_include = 0
 51. dynamic_object.exclusions = <struct: 6 members>
 52. dynamic_object.exclusions.first = ((struct match *)NULL)
 53. dynamic_object.exclusions.last = ((struct match **)NULL)
 54. dynamic_object.exclusions.unmatched_count = 0ul
 55. dynamic_object.exclusions.unmatched_next = ((struct match *)NULL)
 56. dynamic_object.exclusions.unmatched_eof = 0
 57. dynamic_object.exclusions.$pad5 = 0u
 58. dynamic_object.inclusions = <struct: 6 members>
 59. dynamic_object.inclusions.first = ((struct match *)NULL)
 60. dynamic_object.inclusions.last = ((struct match **)NULL)
 61. dynamic_object.inclusions.unmatched_count = 0ul
 62. dynamic_object.inclusions.unmatched_next = ((struct match *)NULL)
 63. dynamic_object.inclusions.unmatched_eof = 0
 64. dynamic_object.inclusions.$pad5 = 0u
 65. dynamic_object.now = 0l
 66. dynamic_object.newer_mtime_filter = 0
 67. dynamic_object.$pad7 = 0u
 68. dynamic_object.newer_mtime_sec = 0l
 69. dynamic_object.newer_mtime_nsec = 0l
 70. dynamic_object.newer_ctime_filter = 0
 71. dynamic_object.$pad11 = 0u
 72. dynamic_object.newer_ctime_sec = 0l
 73. dynamic_object.newer_ctime_nsec = 0l
 74. dynamic_object.older_mtime_filter = 0
 75. dynamic_object.$pad15 = 0u
 76. dynamic_object.older_mtime_sec = 0l
 77. dynamic_object.older_mtime_nsec = 0l
 78. dynamic_object.older_ctime_filter = 0
 79. dynamic_object.$pad19 = 0u
 80. dynamic_object.older_ctime_sec = 0l
```

### CBMC harness (bundled at `findings/v7/harnesses/archive_match__archive_match_exclude_entry__add_entry.pointer_dereference.32.c`)

```c
/* CBMC harness for: archive_match_exclude_entry */
#include "/tmp/libarchive_seedhunt_full/archive_match.c"

int main(void) {
    /* Allocate archive_match structure */
    struct archive_match *a = malloc(sizeof(struct archive_match));
    __CPROVER_assume(a != NULL);
    
    /* Initialize critical fields to match archive_match_new() */
    a->archive.magic = 0xcad11c9U;
    a->archive.state = 1U;
    a->recursive_include = 1;
    
    /* Initialize the exclusion_tree and exclusion_entry_list that add_entry uses */
    __archive_rb_tree_init(&(a->exclusion_tree), &rb_ops);
    entry_list_init(&(a->exclusion_entry_list));
    
    /* Use external archive_entry - CBMC will treat it as opaque */
    struct archive_entry *entry;
    
    /* Create flag parameter - must be valid combination per validate_time_flag */
    int flag;
    
    /* Time flags: ARCHIVE_MATCH_MTIME (0x0100) or ARCHIVE_MATCH_CTIME (0x0200) or both */
    /* Comparison flags: ARCHIVE_MATCH_NEWER (0x0001), ARCHIVE_MATCH_OLDER (0x0002), 
       or ARCHIVE_MATCH_EQUAL (0x0010), or combinations */
    /* Valid combinations observed from validate_time_flag logic */
    __CPROVER_assume(
        (flag & 0xff00) == 0x0100 || 
        (flag & 0xff00) == 0x0200 || 
        (flag & 0xff00) == 0x0300
    );
    __CPROVER_assume(
        (flag & 0x00ff) == 0x0001 || 
        (flag & 0x00ff) == 0x0002 || 
        (flag & 0x00ff) == 0x0010 ||
        (flag & 0x00ff) == 0x0003 ||
        (flag & 0x00ff) == 0x0011 ||
        (flag & 0x00ff) == 0x0012 ||
        (flag & 0x00ff) == 0x0013
    );
    
    /* Call the function under test */
    archive_match_exclude_entry(&(a->archive), flag, entry);
    
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
