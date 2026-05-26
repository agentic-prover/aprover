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

### Source (`archive_match.c` starting at line 180)

```c
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

- **Outcome**: `not_triggered`
- **Signal**: `-`
- **Sanitizer hit**: `False`
- **Attempts**: 3 (final attempt = 1)
- **Reproducer**: `/tmp/libarchive_judge_v7/judge_v7/archive_match/archive_match_excluded/dynamic/path_excluded/reproducer_attempt1.c`

**Sanitizer output (excerpt)**:

```text
No crash occurred, bug not reproduced

```


## Reproduction artifacts

- Harness: `(adjacent re-run; harness generated on the fly)`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_match/archive_match_excluded/judge_path_excluded.unwind.2.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_match/archive_match_excluded/judge_path_excluded.unwind.2.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
