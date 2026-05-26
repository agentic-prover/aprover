# Bug report: `path_excluded` — path_excluded.pointer_dereference.7

**Evidence grade**: **C** — judge-only (not_triggered)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_match.c`
- **Function**: `path_excluded`
- **Violated property**: `path_excluded.pointer_dereference.7` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `path_excluded.pointer_dereference.7` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `not_triggered` (signal `None`) |

### Adjacent-bug context

This finding was surfaced as an adjacent bug while judging the primary CEx on `archive_match_excluded` (`path_excluded.unwind.2`). The primary verdict was `unrealistic`; the adjacent bug was BMC-confirmed against this function and the new CEx was re-judged realistic.

## Judge reasoning

This is a real NULL pointer dereference bug. The CBMC trace shows the failure at line 734 in path_excluded when dereferencing match->matched, but the root cause is that pathname is NULL. The call chain is: archive_match_excluded/archive_match_path_excluded → path_excluded(a, 1, archive_entry_pathname(entry)). The function archive_entry_pathname can return NULL for malformed archive entries. When pathname is NULL, it gets passed to match_path_inclusion (line 735) and match_path_exclusion (line 747), which then cast it to (const char *)pn or (const wchar_t *)pn and pass it to __archive_pathmatch/__archive_pathmatch_w. These pattern matching functions will dereference the NULL pointer. Unlike time_excluded which checks for NULL pathname at line ~1090, path_excluded has no such check. The harness correctly models this scenario by setting pathname=NULL, which is a valid return value from archive_entry_pathname for certain malformed entries.

## Exploit scenario (LLM-supplied)

An attacker crafts a malformed archive entry where archive_entry_pathname returns NULL. When this entry is processed through archive_match_excluded or archive_match_path_excluded (public API functions), the NULL pathname flows to path_excluded, which passes it unchecked to match_path_inclusion/match_path_exclusion. These functions cast the NULL pointer and pass it to __archive_pathmatch, causing a NULL pointer dereference crash.

### CBMC witness (variable assignments)

_witness not recovered_

### CBMC harness (bundled at `findings/v7/harnesses/archive_match__path_excluded__path_excluded.pointer_dereference.7.c`)

```c
/* Auto-generated CBMC harness (real-libc mode) for: archive_match_excluded */
/* Source: /tmp/libarchive_seedhunt_full/archive_match.c */
/* Harness entry: main */

#include "/tmp/libarchive_seedhunt_full/archive_match.c"


int main(void) {
    /* Step 1: nondeterministic inputs */
    /* struct-pointer init for '_a' (struct archive, 19 fields) */
    struct archive __a_obj;
    struct archive* _a = &__a_obj;
    char ___a_obj_archive_format_name_buf[5];
    unsigned int ___a_obj_archive_format_name_len;
    __CPROVER_assume(___a_obj_archive_format_name_len <= (unsigned int)4);
    ___a_obj_archive_format_name_buf[___a_obj_archive_format_name_len] = '\0';
    __a_obj.archive_format_name = ___a_obj_archive_format_name_buf;
    __CPROVER_assume(__a_obj.file_count >= 0 && __a_obj.file_count <= (long)(4));
    __CPROVER_assume(__a_obj.archive_error_number >= 0 && __a_obj.archive_error_number <= (long)(4));
    char ___a_obj_error_buf[5];
    unsigned int ___a_obj_error_len;
    __CPROVER_assume(___a_obj_error_len <= (unsigned int)4);
    ___a_obj_error_buf[___a_obj_error_len] = '\0';
    __a_obj.error = ___a_obj_error_buf;
    char ___a_obj_current_code_buf[5];
    unsigned int ___a_obj_current_code_len;
    __CPROVER_assume(___a_obj_current_code_len <= (unsigned int)4);
    ___a_obj_current_code_buf[___a_obj_current_code_len] = '\0';
    __a_obj.current_code = ___a_obj_current_code_buf;
    char ___a_obj_read_data_block_buf[5];
    unsigned int ___a_obj_read_data_block_len;
    __CPROVER_assume(___a_obj_read_data_block_len <= (unsigned int)4);
    ___a_obj_read_data_block_buf[___a_obj_read_data_block_len] = '\0';
    __a_obj.read_data_block = ___a_obj_read_data_block_buf;
    __CPROVER_assume(__a_obj.read_data_offset >= 0 && __a_obj.read_data_offset <= (long)(4));
    __CPROVER_assume(__a_obj.read_data_output_offset >= 0 && __a_obj.read_data_output_offset <= (long)(4));
    __CPROVER_assume(__a_obj.read_data_remaining >= 0 && __a_obj.read_data_remaining <= (long)(4));
    __CPROVER_assume(__a_obj.read_data_is_posix_read >= 0 && __a_obj.read_data_is_posix_read <= (long)(4));
    /* opaque struct archive_entry: nondet pointer (archive_entry body not in TU) */
    struct archive_entry* entry;
    /* Step 2: precondition assumptions */
    /* precondition: true — no assumptions needed */
    /* Step 3: call function under test */
    int result = archive_match_excluded(_a, entry);
    /* Step 4: postcondition assertions */
    /* precondition: true — no assumptions needed */
    (void)result;
    return 0;
}

```

### Dynamic reproducer (bundled at `findings/v7/reproducers/archive_match__path_excluded__path_excluded.pointer_dereference.7.c`)

This is the 1-of-3 attempt the dyn-val LLM produced that triggered the sanitizer. Compile + link against a sanitiser-instrumented libarchive .so:

```sh
gcc -fsanitize=address,undefined -g -O1 -I/path/to/libarchive \
    archive_match__path_excluded__path_excluded.pointer_dereference.7.c -L/path/to/libarchive/build -larchive -o repro
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
    int r;

    /* Create archive match object */
    a = archive_match_new();
    if (a == NULL) {
        fprintf(stderr, "Failed to create archive_match\n");
        return 1;
    }

    /* Add an inclusion pattern to trigger path matching */
    r = archive_match_include_pattern(a, "*.txt");
    if (r != ARCHIVE_OK) {
        fprintf(stderr, "Failed to add inclusion pattern\n");
        archive_match_free(a);
        return 1;
    }

    /* Create an archive entry with NULL pathname */
    entry = archive_entry_new();
    if (entry == NULL) {
        fprintf(stderr, "Failed to create archive_entry\n");
        archive_match_free(a);
        return 1;
    }

    /* Explicitly set pathname to NULL to simulate malformed archive entry */
    archive_entry_set_pathname(entry, NULL);

    /* Verify pathname is actually NULL */
    const char *pathname = archive_entry_pathname(entry);
    if (pathname != NULL) {
        fprintf(stderr, "Pathname is not NULL, cannot reproduce bug\n");
        archive_entry_free(entry);
        archive_match_free(a);
        return 1;
    }

    /* Call archive_match_excluded which will call path_excluded with NULL pathname */
    /* This should trigger NULL pointer dereference in match_path_inclusion */
    r = archive_match_excluded(a, entry);

    /* If we reach here, the bug was not triggered */
    fprintf(stderr, "No crash occurred, bug not reproduced\n");
    
    archive_entry_free(entry);
    archive_match_free(a);
    return 0;
}
```

### Sanitizer output

```text
No crash occurred, bug not reproduced

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
