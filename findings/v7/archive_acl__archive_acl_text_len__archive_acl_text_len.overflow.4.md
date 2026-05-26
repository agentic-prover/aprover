# Bug report: `archive_acl_text_len` — archive_acl_text_len.overflow.4

**Evidence grade**: **C** — judge-only (timeout)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_text_len`
- **Violated property**: `archive_acl_text_len.overflow.4` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `archive_acl_text_len.overflow.4` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `timeout` (signal `-`) |

## Judge reasoning

The CBMC counterexample reveals a real integer overflow vulnerability in archive_acl_text_len at line 581 (length += 4). The witness shows length reaching UINT64_MAX-1, causing overflow when adding 4. This is realistic because:

1. The function accumulates length by calling wcslen() on attacker-controlled wide-character strings at line 604: "length += wcslen(wname)". An archive with maliciously crafted ACL entries containing extremely long user/group names can cause wcslen to return huge values.

2. With 10 ACL entries (the harness limit), if each has a wname causing wcslen to return ~1.8 exabytes, the cumulative length approaches UINT64_MAX.

3. The callers (archive_acl_to_text_l and archive_acl_to_text_w) use the returned length to allocate memory via malloc(length * sizeof(*p)). If overflow causes length to wrap to a small value (or 0), malloc succeeds with insufficient buffer, leading to heap buffer overflow when the actual ACL text is written.

4. An attacker can trigger this through the public API by crafting an archive file with ACL entries containing extremely long UTF-8/wide-character user or group names, which libarchive will parse and store in the ACL structure.

The overflow occurs at archive_acl.c:581 in the function archive_acl_text_len when processing ACL entries with ARCHIVE_ENTRY_ACL_MASK tags.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive (tar, zip, etc.) containing file entries with ACL metadata. The ACL entries specify extremely long user or group names (multi-megabyte UTF-8 strings that expand to huge wide-character lengths). When a victim application calls archive_entry_acl_to_text() or similar functions to serialize the ACLs, archive_acl_text_len overflows, returns a small value, causing undersized malloc, followed by heap buffer overflow during text generation.

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

- **Outcome**: `timeout`
- **Signal**: `-`
- **Sanitizer hit**: `None`
- **Attempts**: 3 (final attempt = 2)
- **Reproducer**: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/dynamic/archive_acl_text_len/reproducer_attempt2.c`

**Sanitizer output (excerpt)**:

```text

```


## Reproduction artifacts

- Harness: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/harness.c`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/cbmc_result.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/judge_archive_acl_text_len.overflow.4.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
