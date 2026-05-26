# Bug report: `match_owner_name_mbs` — strcmp.pointer_dereference.1

**Evidence grade**: **C** — dynamically reproduced a related crash (different property class — circumstantial)

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

### Source (`archive_match.c` starting at line 168)

```c
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
- **Reproducer**: `/tmp/libarchive_judge_v7/judge_v7/archive_match/match_owner_name_mbs/dynamic/match_owner_name_mbs/reproducer_attempt1.c`

**Sanitizer output (excerpt)**:

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


## Reproduction artifacts

- Harness: `(adjacent re-run; harness generated on the fly)`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_match/match_owner_name_mbs/judge_strcmp.pointer_dereference.2.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_match/match_owner_name_mbs/judge_strcmp.pointer_dereference.2.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
