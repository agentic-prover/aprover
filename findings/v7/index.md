# bmc-agent judge_v7 findings — index

**Sweep**: `/tmp/libarchive_judge_v7/judge_v7`  
**Corpus**: `/tmp/libarchive_seedhunt_full` (7 .c files, libarchive snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)  
**Config**: `--agentic-harness --refine-rounds 1 --enable-flag-selection`  
**Note**: sweep was still in progress at index-generation time; more findings may follow.

## 10 unique realistic finding(s)

| Grade | File | Function | Property | Dyn-val | Also via | Report |
|---|---|---|---|---|---|---|
| **A** | `archive_acl.c` | `archive_acl_text_len` | `archive_acl_text_len.overflow.3` | confirmed_dynamic | - | [archive_acl__archive_acl_text_len__archive_acl_text_len.overflow.3.md](archive_acl__archive_acl_text_len__archive_acl_text_len.overflow.3.md) (primary) |
| **A** | `archive_match.c` | `cmp_key_mbs` | `strcmp.pointer_dereference.1` | confirmed_dynamic | +2 re-surface(s) | [archive_match__cmp_key_mbs__strcmp.pointer_dereference.1.md](archive_match__cmp_key_mbs__strcmp.pointer_dereference.1.md) (primary) |
| **A** | `archive_match.c` | `cmp_node_mbs` | `strcmp.pointer_dereference.1` | confirmed_dynamic | - | [archive_match__cmp_node_mbs__strcmp.pointer_dereference.1.md](archive_match__cmp_node_mbs__strcmp.pointer_dereference.1.md) (primary) |
| **B** | `archive_acl.c` | `next_field` | `next_field.pointer_dereference.317` | confirmed_dynamic | - | [archive_acl__next_field__next_field.pointer_dereference.317.md](archive_acl__next_field__next_field.pointer_dereference.317.md) (primary) |
| **B** | `archive_match.c` | `match_owner_name_mbs` | `strcmp.pointer_dereference.1` | confirmed_dynamic | +3 re-surface(s) | [archive_match__match_owner_name_mbs__strcmp.pointer_dereference.1.md](archive_match__match_owner_name_mbs__strcmp.pointer_dereference.1.md) (adjacent) |
| **C** | `archive_acl.c` | `archive_acl_text_len` | `archive_acl_text_len.overflow.4` | timeout | - | [archive_acl__archive_acl_text_len__archive_acl_text_len.overflow.4.md](archive_acl__archive_acl_text_len__archive_acl_text_len.overflow.4.md) (primary) |
| **C** | `archive_acl.c` | `next_field` | `next_field.pointer_dereference.251` | not_triggered | - | [archive_acl__next_field__next_field.pointer_dereference.251.md](archive_acl__next_field__next_field.pointer_dereference.251.md) (primary) |
| **C** | `archive_match.c` | `archive_match_exclude_entry` | `add_entry.pointer_dereference.32` | llm_no_reproducer | - | [archive_match__archive_match_exclude_entry__add_entry.pointer_dereference.32.md](archive_match__archive_match_exclude_entry__add_entry.pointer_dereference.32.md) (primary) |
| **C** | `archive_match.c` | `cmp_node_mbs` | `strcmp.pointer_dereference.2` | llm_no_reproducer | - | [archive_match__cmp_node_mbs__strcmp.pointer_dereference.2.md](archive_match__cmp_node_mbs__strcmp.pointer_dereference.2.md) (primary) |
| **C** | `archive_match.c` | `path_excluded` | `path_excluded.pointer_dereference.7` | not_triggered | - | [archive_match__path_excluded__path_excluded.pointer_dereference.7.md](archive_match__path_excluded__path_excluded.pointer_dereference.7.md) (adjacent) |

## Evidence grades

- **A** — judge realistic AND dyn-val sanitizer hit reproduces the same property class. Strongest evidence.
- **B** — judge realistic AND dyn-val sanitizer hit on a related path, different property class. The reproducer triggered a crash in libarchive's code via the same code path, but not the same bug class CBMC identified. Circumstantial — needs human review to decide whether the ASan signal corresponds to the CBMC finding or is an unrelated side-bug.
- **C** — judge realistic, dyn-val did not reproduce. Judge-only.

## Caveats

- These are automated findings from a research prototype. The CBMC   counterexample is real; the realism judgement is an LLM call.
- The agentic harness writes a harness it believes matches the real   caller chain. When it gets that wrong, the finding may be a   harness artifact even when CBMC says 'verification failed'.
- Grade **B** findings should be treated as 'crash reproduced in   libarchive but the exact CBMC trace was not exhibited'.
- This is not coordinated disclosure. None of these has been   filed with libarchive upstream.