# Bug report: `archive_acl_text_len` — archive_acl_text_len.overflow.3

**Evidence grade**: **A** — dynamically confirmed (same property class)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_text_len`
- **Violated property**: `archive_acl_text_len.overflow.3` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `archive_acl_text_len.overflow.3` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `confirmed_dynamic` (signal `SIGABRT`) |

## Judge reasoning

The unsigned integer overflow in archive_acl_text_len at the line "length += 6" is a realistic exploitable bug. The function accumulates the length of ACL text representation by iterating through ACL entries and adding lengths of user/group names obtained via wcslen(). If an ACL entry contains an extremely long name (or a name string without proper null termination), wcslen() can return a very large value causing length to overflow. The witness shows length reaching UINT64_MAX-2 before adding 6, which overflows to 3. Both callers (archive_acl_to_text_l and archive_acl_to_text_w) use the returned length to allocate a buffer via malloc(length * sizeof(*p)). An overflowed small length value causes a tiny buffer allocation, but the subsequent writing loop will write the actual (much larger) amount of data, causing a heap buffer overflow. An attacker can trigger this by crafting an archive with ACL entries containing extremely long user/group names.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive file (tar, cpio, etc.) with ACL metadata containing entries with extremely long user or group names (or multiple entries whose combined name lengths exceed SIZE_MAX). When libarchive parses this archive and calls archive_acl_to_text_w() or archive_acl_to_text_l() to convert ACL data to text format, archive_acl_text_len() overflows during length calculation and returns a small value (e.g., 3). The caller allocates a tiny buffer but then writes gigabytes of ACL text data into it, causing a heap buffer overflow that can lead to code execution or denial of service.

### Source (`archive_acl.c` starting at line 59)

```c
static size_t	archive_acl_text_len(struct archive_acl *acl, int want_type,
		    int flags, int wide, struct archive *a,
		    struct archive_string_conv *sc);
static int	isint_w(const wchar_t *start, const wchar_t *end, int *result);
static int	ismode_w(const wchar_t *start, const wchar_t *end, int *result);
static int	is_nfs4_flags_w(const wchar_t *start, const wchar_t *end,
		    int *result);
static int	is_nfs4_perms_w(const wchar_t *start, const wchar_t *end,
		    int *result);
static void	next_field_w(const wchar_t **wp, const wchar_t **start,
		    const wchar_t **end, wchar_t *sep);
static void	append_entry_w(wchar_t **wp, const wchar_t *prefix, int type,
		    int tag, int flags, const wchar_t *wname, int perm, int id);
static void	append_id_w(wchar_t **wp, int id);
static int	isint(const char *start, const char *end, int *result);
static int	ismode(const char *start, const char *end, int *result);
static int	is_nfs4_flags(const char *start, const char *end,
		    int *result);
static int	is_nfs4_perms(const char *start, const char *end,
		    int *result);
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
- **Attempts**: 1 (final attempt = 1)
- **Reproducer**: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/dynamic/archive_acl_text_len/reproducer_attempt1.c`

**Sanitizer output (excerpt)**:

```text
=================================================================
==187120==ERROR: AddressSanitizer: requested allocation size 0x4000000000000000 (0x4000000000001000 after adjustments for alignment, red zones etc.) exceeds maximum supported size of 0x10000000000 (thread T0)
    #0 0x7b05412fd9c7 in malloc ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69
    #1 0x600360a6b4f6 in main /tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/dynamic/archive_acl_text_len/reproducer_attempt1.c:22
    #2 0x7b054062a1c9  (/lib/x86_64-linux-gnu/libc.so.6+0x2a1c9) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #3 0x7b054062a28a in __libc_start_main (/lib/x86_64-linux-gnu/libc.so.6+0x2a28a) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #4 0x600360a6b304 in _start (/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/dynamic/archive_acl_text_len/reproducer_attempt1.bin+0x2304) (BuildId: 16ca064e933d0204346260433366e4ecc8aa659a)

==187120==HINT: if you don't care about these errors you may set allocator_may_return_null=1
SUMMARY: AddressSanitizer: allocation-size-too-big ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69 in malloc
==187120==ABORTING

```


## Reproduction artifacts

- Harness: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/harness.c`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/cbmc_result.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/judge_archive_acl_text_len.overflow.3.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
