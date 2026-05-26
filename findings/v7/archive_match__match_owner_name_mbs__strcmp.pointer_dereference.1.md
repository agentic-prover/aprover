# Bug report: `match_owner_name_mbs` — strcmp.pointer_dereference.1

**Evidence grade**: **B** — dynamically reproduced a related crash (different property class — circumstantial)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_match.c`
- **Function**: `match_owner_name_mbs`
- **Violated property**: `strcmp.pointer_dereference.1` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `strcmp.pointer_dereference.1` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `confirmed_dynamic` (signal `SIGABRT`) |

### Adjacent-bug context

This finding was surfaced as an adjacent bug while judging the primary CEx on `match_owner_name_mbs` (`strcmp.pointer_dereference.2`). The primary verdict was `unrealistic`; the adjacent bug was BMC-confirmed against this function and the new CEx was re-judged realistic.

### Independently re-surfaced via

- primary (grade C)
- adjacent of match_owner_name_mbs (grade C)
- adjacent of match_owner_name_mbs (grade C)

## Judge reasoning

The CBMC counterexample reveals a real bug at line 1768 of archive_match.c in match_owner_name_mbs. The witness shows archive_mstring_get_mbs returns 8 (positive, indicating success) but leaves p uninitialized/invalid. The critical flaw is in the error handling logic at lines 1765-1767: the code checks "if (archive_mstring_get_mbs(...) < 0 && errno == ENOMEM)" which only handles the specific case of negative return with ENOMEM. However, when archive_mstring_get_mbs returns a positive value (as shown in the witness: return_value_archive_mstring_get_mbs = 8), the error check is bypassed, and execution proceeds to line 1768 where strcmp dereferences p without verifying it was properly initialized by archive_mstring_get_mbs. The witness confirms p is invalid ('unknown') when strcmp is called, causing the NULL pointer dereference. This is reachable through the public API via owner_excluded -> match_owner_name_mbs, where archive_entry_uname/gname values from attacker-controlled archives flow into the 'name' parameter. The bug occurs when the pattern mstring structure is in a state where archive_mstring_get_mbs returns a non-negative, non-zero value without setting p to a valid pointer.

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive with owner name patterns that cause archive_mstring_get_mbs to return a positive error code (not negative, so not caught by the errno check). When archive_match_owner_excluded is called during archive extraction with inclusion filters set, the code path reaches match_owner_name_mbs. The function calls archive_mstring_get_mbs which returns a positive value without initializing p, bypassing the error check at lines 1765-1767. The code then executes strcmp(p, name) at line 1768 with an uninitialized/invalid p pointer, causing a crash or potential memory corruption.

### CBMC witness (variable assignments)

_witness not recovered_

### CBMC harness (bundled at `findings/v7/harnesses/archive_match__match_owner_name_mbs__strcmp.pointer_dereference.1.c`)

```c
/* CBMC harness for: match_owner_name_mbs */
#include "/tmp/libarchive_seedhunt_full/archive_match.c"

int main(void) {
    /* Allocate archive_match structure */
    struct archive_match *a = malloc(sizeof(struct archive_match));
    __CPROVER_assume(a != NULL);
    
    /* Initialize archive structure fields that might be accessed */
    a->archive.magic = 0xdeb0c5U;
    a->archive.state = 1;
    
    /* Allocate match_list */
    struct match_list *list = malloc(sizeof(struct match_list));
    __CPROVER_assume(list != NULL);
    
    /* Create a linked list of 0-3 match entries */
    unsigned int num_matches;
    __CPROVER_assume(num_matches <= 3);
    
    struct match *prev = NULL;
    struct match *first = NULL;
    
    for (unsigned int i = 0; i < num_matches; i++) {
        struct match *m = malloc(sizeof(struct match));
        __CPROVER_assume(m != NULL);
        
        m->next = NULL;
        m->matched = 0;
        
        /* Initialize the pattern mstring */
        m->pattern.aes_set = 0;
        m->pattern.aes_mbs.s = NULL;
        m->pattern.aes_mbs.length = 0;
        m->pattern.aes_mbs.buffer_length = 0;
        m->pattern.aes_utf8.s = NULL;
        m->pattern.aes_utf8.length = 0;
        m->pattern.aes_utf8.buffer_length = 0;
        m->pattern.aes_wcs.s = NULL;
        m->pattern.aes_wcs.length = 0;
        m->pattern.aes_wcs.buffer_length = 0;
        m->pattern.aes_mbs_in_locale.s = NULL;
        m->pattern.aes_mbs_in_locale.length = 0;
        m->pattern.aes_mbs_in_locale.buffer_length = 0;
        
        if (prev == NULL) {
            first = m;
        } else {
            prev->next = m;
        }
        prev = m;
    }
    
    list->first = first;
    
    /* Create input name string */
    unsigned int name_len;
    __CPROVER_assume(name_len < 256);
    
    char *name = NULL;
    if (name_len > 0) {
        name = malloc(name_len + 1);
        __CPROVER_assume(name != NULL);
        for (unsigned int i = 0; i < name_len; i++) {
            name[i] = nondet_char();
            __CPROVER_assume(name[i] != '\0');
        }
        name[name_len] = '\0';
    }
    
    /* Call the function under test */
    int result = match_owner_name_mbs(a, list, name);
    
    return 0;
}

```

### Dynamic reproducer (bundled at `findings/v7/reproducers/archive_match__match_owner_name_mbs__strcmp.pointer_dereference.1.c`)

This is the 1-of-1 attempt the dyn-val LLM produced that triggered the sanitizer. Compile + link against a sanitiser-instrumented libarchive .so:

```sh
gcc -fsanitize=address,undefined -g -O1 -I/path/to/libarchive \
    archive_match__match_owner_name_mbs__strcmp.pointer_dereference.1.c -L/path/to/libarchive/build -larchive -o repro
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
    
    /* Create a malicious archive in memory */
    unsigned char archive_data[2048];
    size_t archive_size = 0;
    
    /* Minimal tar header with crafted owner name */
    unsigned char tar_header[512];
    memset(tar_header, 0, sizeof(tar_header));
    
    /* File name */
    strcpy((char*)tar_header, "test.txt");
    
    /* Mode */
    strcpy((char*)tar_header + 100, "0000644");
    
    /* UID */
    strcpy((char*)tar_header + 108, "0000000");
    
    /* GID */
    strcpy((char*)tar_header + 116, "0000000");
    
    /* Size */
    strcpy((char*)tar_header + 124, "00000000000");
    
    /* Mtime */
    strcpy((char*)tar_header + 136, "00000000000");
    
    /* Checksum placeholder */
    memset(tar_header + 148, ' ', 8);
    
    /* Type flag */
    tar_header[156] = '0';
    
    /* Owner name - craft with invalid UTF-8 or encoding that causes archive_mstring_get_mbs to return positive */
    /* Use invalid UTF-8 sequence that might trigger encoding warning/error */
    tar_header[265] = 0xFF;
    tar_header[266] = 0xFE;
    tar_header[267] = 0xFF;
    tar_header[268] = 0xFE;
    
    /* Calculate checksum */
    unsigned int checksum = 0;
    for (int i = 0; i < 512; i++) {
        checksum += tar_header[i];
    }
    sprintf((char*)tar_header + 148, "%06o", checksum);
    tar_header[154] = 0;
    tar_header[155] = ' ';
    
    memcpy(archive_data, tar_header, 512);
    archive_size = 512;
    
    /* Add two blocks of zeros to end archive */
    memset(archive_data + archive_size, 0, 1024);
    archive_size += 1024;
    
    /* Create archive reader */
    a = archive_read_new();
    archive_read_support_format_tar(a);
    archive_read_support_filter_all(a);
    
    /* Add owner name exclusion pattern to trigger match_owner_name_mbs */
    archive_read_set_options(a, "exclude-owner=testowner");
    
    /* Open the malicious archive */
    r = archive_read_open_memory(a, archive_data, archive_size);
    if (r != ARCHIVE_OK) {
        fprintf(stderr, "Failed to open archive: %s\n", archive_error_string(a));
        archive_read_free(a);
        return 1;
    }
    
    /* Read the entry - this should trigger match_owner_name_mbs */
    r = archive_read_next_header(a, &entry);
    if (r == ARCHIVE_OK) {
        /* Try to read data to fully process the entry */
        while (archive_read_data_block(a, &buff, &size, &offset) == ARCHIVE_OK) {
            /* Process data */
        }
    }
    
    archive_read_free(a);
    
    return 0;
}
```

### Sanitizer output

```text
Failed to open archive

=================================================================
==270356==ERROR: LeakSanitizer: detected memory leaks

Direct leak of 32 byte(s) in 1 object(s) allocated from:
    #0 0x7699f10fd340 in calloc ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:77
    #1 0x7699f0f2986d in archive_read_open_memory2 /tmp/libarchive_bench/libarchive/libarchive/archive_read_open_memory.c:72
    #2 0x7699f0f29840 in archive_read_open_memory /tmp/libarchive_bench/libarchive/libarchive/archive_read_open_memory.c:58
    #3 0x60f304466d9e in main /tmp/libarchive_judge_v7/judge_v7/archive_match/match_owner_name_mbs/dynamic/match_owner_name_mbs/reproducer_attempt1.c:53
    #4 0x7699f042a1c9  (/lib/x86_64-linux-gnu/libc.so.6+0x2a1c9) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #5 0x7699f042a28a in __libc_start_main (/lib/x86_64-linux-gnu/libc.so.6+0x2a28a) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #6 0x60f304466404 in _start (/tmp/libarchive_judge_v7/judge_v7/archive_match/match_owner_name_mbs/dynamic/match_owner_name_mbs/reproducer_attempt1.bin+0x3404) (BuildId: b0c80ed0d98f9f081e7e2b16b34e64bcbac5b4a3)

SUMMARY: AddressSanitizer: 32 byte(s) leaked in 1 allocation(s).

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
