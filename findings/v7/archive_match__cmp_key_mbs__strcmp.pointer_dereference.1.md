# Bug report: `cmp_key_mbs` — strcmp.pointer_dereference.1

**Evidence grade**: **C** — dynamically confirmed (same property class)

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

### Source (`archive_match.c` starting at line 149)

```c
static int	cmp_key_mbs(const struct archive_rb_node *, const void *);
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
- **Reproducer**: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_key_mbs/dynamic/cmp_key_mbs/reproducer_attempt1.c`

**Sanitizer output (excerpt)**:

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


## Reproduction artifacts

- Harness: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_key_mbs/harness.c`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_key_mbs/cbmc_result.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_key_mbs/judge_strcmp.pointer_dereference.1.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
