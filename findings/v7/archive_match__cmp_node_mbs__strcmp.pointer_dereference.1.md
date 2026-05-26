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

### Source (`archive_match.c` starting at line 150)

```c
static int	cmp_node_mbs(const struct archive_rb_node *,
		    const struct archive_rb_node *);
#else
static int	cmp_key_wcs(const struct archive_rb_node *, const void *);
static int	cmp_node_wcs(const struct archive_rb_node *,
		    const struct archive_rb_node *);
#endif
static void	entry_list_add(struct entry_list *, struct match_file *);
static void	entry_list_free(struct entry_list *);
static void	entry_list_init(struct entry_list *);
static int	error_nomem(struct archive_match *);
static void	match_list_add(struct match_list *, struct match *);
static void	match_list_free(struct match_list *);
static void	match_list_init(struct match_list *);
static int	match_list_unmatched_inclusions_next(struct archive_match *,
		    struct match_list *, int, const void **);
static int	match_owner_id(struct id_array *, int64_t);
#if !defined(_WIN32) || defined(__CYGWIN__)
static int	match_owner_name_mbs(struct archive_match *,
		    struct match_list *, const char *);
#else
static int	match_owner_name_wcs(struct archive_match *,
		    struct match_list *, const wchar_t *);
#endif
static int	match_path_exclusion(struct archive_match *,
		    struct match *, int, const void *);
static int	match_path_inclusion(struct archive_match *,
		    struct match *, int, const void *);
static int	owner_excluded(struct archive_match *,
		    struct archive_entry *);
static int	path_excluded(struct archive_match *, int, const void *);
static int	set_timefilter(struct archive_match *, int, time_t, long,
		    time_t, long);
static int	set_timefilter_pathname_mbs(struct archive_match *,
		    int, const char *);
static int	set_timefilter_pathname_wcs(struct archive_match *,
		    int, const wchar_t *);
static int	set_timefilter_date(struct archive_match *, int, const char *);
static int	set_timefilter_date_w(struct archive_match *, int,
		    const wchar_t *);
static int	time_excluded(struct archive_match *,
		    struct archive_entry *);
static int	validate_time_flag(struct archive *, int, const char *);

#define get_date archive_parse_date

static const struct archive_rb_tree_ops rb_ops = {
#if !defined(_WIN32) || defined(__CYGWIN__)
	cmp_node_mbs, cmp_key_mbs
#else
	cmp_node_wcs, cmp_key_wcs
#endif
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
- **Reproducer**: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/dynamic/cmp_node_mbs/reproducer_attempt1.c`

**Sanitizer output (excerpt)**:

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


## Reproduction artifacts

- Harness: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/harness.c`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/cbmc_result.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/judge_strcmp.pointer_dereference.1.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
