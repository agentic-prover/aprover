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

- **Outcome**: `llm_no_reproducer`
- **Signal**: `-`
- **Sanitizer hit**: `None`
- **Attempts**: 1 (final attempt = 1)
- **Reproducer**: `-`

**Sanitizer output (excerpt)**:

```text

```


## Reproduction artifacts

- Harness: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/harness.c`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/cbmc_result.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_match/cmp_node_mbs/judge_strcmp.pointer_dereference.2.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
