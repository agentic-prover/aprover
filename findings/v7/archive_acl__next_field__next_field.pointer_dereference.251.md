# Bug report: `next_field` — next_field.pointer_dereference.251

**Evidence grade**: **C** — judge-only (not_triggered)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `next_field`
- **Violated property**: `next_field.pointer_dereference.251` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `next_field.pointer_dereference.251` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `not_triggered` (signal `None`) |

## Judge reasoning

This is a real out-of-bounds read bug in next_field at line 2124 (archive_acl.c). The function has three loops that check `*l > 0` before dereferencing `**p`, but at line 2124 it unconditionally dereferences `**p` to read the separator character without checking if any bytes remain. The CBMC witness demonstrates a realistic scenario: a buffer containing only whitespace followed by non-separator characters (e.g., "   \t'X") causes all bytes to be consumed by the first two loops, leaving `*l = 0`. When line 2124 executes `*sep = **p;`, the pointer is one byte past the buffer end. This is reachable through the public API via archive_acl_from_text_l → archive_acl_from_text_nl → next_field, where an attacker can provide malformed ACL text that triggers this condition. The fix should add a check `if (*l > 0)` before line 2124.

## Exploit scenario (LLM-supplied)

An attacker provides a malformed ACL text string to archive_acl_from_text_l (or any function that calls archive_acl_from_text_nl) where the text consists of whitespace followed by non-separator characters that consume the entire buffer without encountering a field separator (comma, colon, newline, or hash). For example, a string like "   \t'X" (whitespace followed by non-separator chars) will cause next_field to consume all bytes in the first two loops, then attempt to read one byte past the buffer end at line 2124 to determine the separator character. This results in an out-of-bounds read that could leak memory contents or cause a crash.

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

- **Outcome**: `not_triggered`
- **Signal**: `-`
- **Sanitizer hit**: `False`
- **Attempts**: 3 (final attempt = 2)
- **Reproducer**: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/dynamic/next_field/reproducer_attempt2.c`

**Sanitizer output (excerpt)**:

```text

```


## Reproduction artifacts

- Harness: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/harness.c`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/cbmc_result.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_acl/next_field/judge_next_field.pointer_dereference.251.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
