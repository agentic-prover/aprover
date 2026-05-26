# Bug report: `next_field` — next_field.pointer_dereference.317

**Evidence grade**: **B** — dynamically reproduced a related crash (different property class — circumstantial)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `next_field`
- **Violated property**: `next_field.pointer_dereference.317` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `next_field.pointer_dereference.317` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `confirmed_dynamic` (signal `SIGABRT`) |

## Judge reasoning

This is a real out-of-bounds read bug in next_field at line 2132 of archive_acl.c. The bug occurs when processing ACL text that ends with a '#' character. The function has a comment-handling block (lines 2127-2133) that checks if the separator is '#', then loops to skip to the next ',' or '\n'. However, after this loop exhausts the buffer (l becomes 0), line 2132 unconditionally dereferences **p without checking if l > 0. The CBMC witness demonstrates this with a 6-byte buffer " \t3#" where after consuming all bytes, l=0 but **p is dereferenced. The function is called by archive_acl_from_text_nl, which is part of the public API chain (archive_entry_acl_from_text → archive_acl_from_text_l → archive_acl_from_text_nl → next_field). An attacker can trigger this by providing malformed ACL text ending with '#' to any archive entry ACL parsing function.

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive file with ACL metadata that contains text ending with a '#' character (e.g., "user:root:rwx#"). When libarchive parses this archive via archive_entry_acl_from_text or related functions, the ACL text is passed to next_field. The function processes the '#' as a comment separator, exhausts the buffer in the comment-handling loop, then performs an out-of-bounds read at line 2132, potentially leaking memory contents or causing a crash.

### Source (`archive_acl.c` starting at line 79)

```c
static void	next_field(const char **p, size_t *l, const char **start,
		    const char **end, char *sep);
static void	append_entry(char **p, const char *prefix, int type,
		    int tag, int flags, const char *name, int perm, int id);
static void	append_id(char **p, int id);

static const struct {
	const int perm;
	const char c;
	const wchar_t wc;
} nfsv4_acl_perm_map[] = {
	{ ARCHIVE_ENTRY_ACL_READ_DATA | ARCHIVE_ENTRY_ACL_LIST_DIRECTORY, 'r',
	    L'r' },
	{ ARCHIVE_ENTRY_ACL_WRITE_DATA | ARCHIVE_ENTRY_ACL_ADD_FILE, 'w',
	    L'w' },
	{ ARCHIVE_ENTRY_ACL_EXECUTE, 'x', L'x' },
	{ ARCHIVE_ENTRY_ACL_APPEND_DATA | ARCHIVE_ENTRY_ACL_ADD_SUBDIRECTORY,
	    'p', L'p' },
	{ ARCHIVE_ENTRY_ACL_DELETE, 'd', L'd' },
	{ ARCHIVE_ENTRY_ACL_DELETE_CHILD, 'D', L'D' },
	{ ARCHIVE_ENTRY_ACL_READ_ATTRIBUTES, 'a', L'a' },
	{ ARCHIVE_ENTRY_ACL_WRITE_ATTRIBUTES, 'A', L'A' },
	{ ARCHIVE_ENTRY_ACL_READ_NAMED_ATTRS, 'R', L'R' },
	{ ARCHIVE_ENTRY_ACL_WRITE_NAMED_ATTRS, 'W', L'W' },
	{ ARCHIVE_ENTRY_ACL_READ_ACL, 'c', L'c' },
	{ ARCHIVE_ENTRY_ACL_WRITE_ACL, 'C', L'C' },
	{ ARCHIVE_ENTRY_ACL_WRITE_OWNER, 'o', L'o' },
	{ ARCHIVE_ENTRY_ACL_SYNCHRONIZE, 's', L's' }
};
```
### CBMC witness (variable assignments)

```text
(no witness assignments captured)
```

### Dynamic validation

- **Outcome**: `confirmed_dynamic`
- **Signal**: `SIGABRT`
- **Sanitizer hit**: `True`
- **Attempts**: 3 (final attempt = 3)
- **Reproducer**: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/dynamic/next_field/reproducer_attempt3.c`

**Sanitizer output (excerpt)**:

```text
ACL text length: 8439

=================================================================
==194398==ERROR: LeakSanitizer: detected memory leaks

Direct leak of 8440 byte(s) in 1 object(s) allocated from:
    #0 0x7782704fd9c7 in malloc ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69
    #1 0x778270b135aa in archive_acl_to_text_l /tmp/libarchive_bench/libarchive/libarchive/archive_acl.c:946
    #2 0x778270b1b1b8 in archive_entry_acl_to_text /tmp/libarchive_bench/libarchive/libarchive/archive_entry.c:1743
    #3 0x60bc3bb018ae in main /tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/dynamic/next_field/reproducer_attempt3.c:57
    #4 0x77826f82a1c9  (/lib/x86_64-linux-gnu/libc.so.6+0x2a1c9) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #5 0x77826f82a28a in __libc_start_main (/lib/x86_64-linux-gnu/libc.so.6+0x2a28a) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #6 0x60bc3bb01304 in _start (/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/dynamic/next_field/reproducer_attempt3.bin+0x2304) (BuildId: 0300e24f57aeebc109f75a1a669a6771b76aabf6)

SUMMARY: AddressSanitizer: 8440 byte(s) leaked in 1 allocation(s).

```


## Reproduction artifacts

- Harness: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/harness.c`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/cbmc_result.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/judge_next_field.pointer_dereference.317.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
