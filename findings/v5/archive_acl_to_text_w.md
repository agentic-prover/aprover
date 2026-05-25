# bmc-agent-sec confirmed finding: `archive_acl_to_text_w`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T05:56:49.178620+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_to_text_w` (lines 675-778)
- **Violated property**: `archive_acl_to_text_w.pointer_dereference.77` (CBMC-reported)
- **Call chain established**: `archive_acl_to_text_w`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 3655 where `*wp++ = L'\0';` writes beyond the allocated buffer. Looking at the buffer allocation at line 3605: `wp = ws = malloc(length * sizeof(*wp));` where `length` is computed by `archive_acl_text_len()` at line 3598. The key issue is that `archive_acl_text_len()` counts the number of wchar_t characters needed, but the actual writing loop can write MORE characters than counted due to several factors:

1. Line 3616 writes a separator after each POSIX ACL entry (user/group/other base permissions)
2. Lines 3641-3649 iterate through `acl->acl_head` entries, writing a separator before each (line 3641: `if (count > 0) *wp++ = separator;`)
3. Line 3655 writes the null terminator

The counting in `archive_acl_text_len()` (lines 3473-3579) attempts to account for separators at line 3568 (`length ++;`), but the logic is complex and error-prone. The counterexample shows `length = 2ul` but the code writes at least 3 wchar_t values:
- Line 3616: separator after first base permission
- Line 3621: separator after second base permission  
- Line 3655: null terminator

This is a classic off-by-one buffer overflow. The `length` calculation doesn't properly account for all the separators and the null terminator that get written. An attacker can craft ACL entries (via `archive_acl_add_entry_w_len()` or by parsing ACL text) that cause the length calculation to undercount, leading to heap buffer overflow when `archive_acl_to_text_w()` is called.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive file (tar, zip, etc.) containing crafted ACL metadata. When libarchive parses this archive and calls `archive_acl_to_text_w()` to convert the ACL to text format (e.g., for display or validation), the function allocates a buffer that is too small based on the flawed length calculation. The subsequent writes overflow the heap buffer, potentially corrupting adjacent heap structures. This could lead to arbitrary code execution through heap metadata corruption or information disclosure by overwriting sensitive data.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_alloca_object = NULL
  __CPROVER_dead_object = len!0@1
  __CPROVER_deallocated = NULL
  __CPROVER_errno = 0
  __CPROVER_malloc_is_new_array = False
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_new_object = NULL
  __CPROVER_rounding_mode = 0
  __a_obj_archive_format_name_buf = <array: 5 elements>
  __a_obj_archive_format_name_buf[0l] = 0
  __a_obj_archive_format_name_buf[1l] = 0
  __a_obj_archive_format_name_buf[2l] = 0
  __a_obj_archive_format_name_buf[3l] = 0
  __a_obj_archive_format_name_buf[4l] = 0
  __a_obj_archive_format_name_len = 2u
  __a_obj_current_code_buf = <array: 5 elements>
  __a_obj_current_code_buf[0l] = 0
  __a_obj_current_code_buf[1l] = 0
  __a_obj_current_code_buf[2l] = 0
  __a_obj_current_code_buf[3l] = 0
  __a_obj_current_code_buf[4l] = 0
  __a_obj_current_code_len = 0u
  __a_obj_error_buf = <array: 5 elements>
  __a_obj_error_buf[0l] = 0
  __a_obj_error_buf[1l] = 0
  __a_obj_error_buf[2l] = 0
  __a_obj_error_buf[3l] = 0
  __a_obj_error_buf[4l] = 0
  __a_obj_error_len = 0u
  __a_obj_read_data_block_buf = <array: 5 elements>
  __a_obj_read_data_block_buf[0l] = 0
  __a_obj_read_data_block_buf[1l] = 0
  __a_obj_read_data_block_buf[2l] = 0
  __a_obj_read_data_block_buf[3l] = 0
  __a_obj_read_data_block_buf[4l] = 0
  __a_obj_read_data_block_len = 0u
  __acl_obj_acl_head_obj = <struct: 6 members>
  __acl_obj_acl_head_obj.next = ((struct archive_acl_entry *)NULL)
  __acl_obj_acl_p_obj = <struct: 6 members>
  __acl_obj_acl_p_obj.next = ((struct archive_acl_entry *)NULL)
  __acl_obj_acl_text_buf = <array: 5 elements>
  __acl_obj_acl_text_buf[0l] = 0
  __acl_obj_acl_text_buf[1l] = 0
  __acl_obj_acl_text_buf[2l] = 0
  __acl_obj_acl_text_buf[3l] = 0
  __acl_obj_acl_text_buf[4l] = 0
  __acl_obj_acl_text_len = 0u
  _a_obj = <struct: 21 members>
  _a_obj.archive_format_name = __a_obj_archive_format_name_buf!0@1
  _a_obj.current_code = __a_obj_current_code_buf!0@1
  _a_obj.error = __a_obj_error_buf!0@1
  _a_obj.read_data_block = __a_obj_read_data_block_buf!0@1
  _acl_obj = <struct: 10 members>
  _acl_obj.acl_head = __acl_obj_acl_head_obj!0@1
  _acl_obj.acl_p = __acl_obj_acl_p_obj!0@1
  _acl_obj.acl_text = __acl_obj_acl_text_buf!0@1
  _text_len_val = 0l
  a = _a_obj!0@1
  acl = _acl_obj!0@1
  ap = __acl_obj_acl_head_obj!0@1
  count = 3
  dynamic_object = <array: 2 elements>
  dynamic_object[0l] = 10
  dynamic_object[1l] = 10
  dynamic_object_size = 2ul
  flags = 775
  goto_symex$$return_value$$archive_acl_text_len = 2ul
  goto_symex$$return_value$$archive_acl_text_want_type = 768
  goto_symex$$return_value$$malloc = dynamic_object
  id = -1
  idlen = 1
  len = 0ul
  length = 2ul
  malloc_res = dynamic_object
  malloc_size = 8ul
  malloc_value = dynamic_object
  name = ((char *)NULL)
  perm = 0
  prefix = ((signed int *)NULL)
  r = 0
  record_malloc = False
  record_may_leak = False
  result = ((signed int *)NULL)
  return_value___VERIFIER_nondet___CPROVER_bool$1 = False
  return_value___VERIFIER_nondet___CPROVER_bool$2 = False
  return_value_archive_acl_to_text_w = ((signed int *)NULL)
  ... (truncated)
```

## Function source (from the snapshot)

```c
wchar_t *
archive_acl_to_text_w(struct archive_acl *acl, ssize_t *text_len, int flags,
    struct archive *a)
{
	int count;
	size_t length;
	size_t len;
	const wchar_t *wname;
	const wchar_t *prefix;
	wchar_t separator;
	struct archive_acl_entry *ap;
	int id, r, want_type;
	wchar_t *wp, *ws;

	want_type = archive_acl_text_want_type(acl, flags);

	/* Both NFSv4 and POSIX.1 types found */
	if (want_type == 0)
		return (NULL);

	if (want_type == ARCHIVE_ENTRY_ACL_TYPE_POSIX1E)
		flags |= ARCHIVE_ENTRY_ACL_STYLE_MARK_DEFAULT;

	length = archive_acl_text_len(acl, want_type, flags, 1, a, NULL);

	if (length == 0)
		return (NULL);

	if (flags & ARCHIVE_ENTRY_ACL_STYLE_SEPARATOR_COMMA)
		separator = L',';
	else
		separator = L'\n';

	/* Now, allocate the string and actually populate it. */
	wp = ws = malloc(length * sizeof(*wp));
	if (wp == NULL) {
		if (errno == ENOMEM)
			__archive_errx(1, "No memory");
		return (NULL);
	}
	count = 0;

	if ((want_type & ARCHIVE_ENTRY_ACL_TYPE_ACCESS) != 0) {
		append_entry_w(&wp, NULL, ARCHIVE_ENTRY_ACL_TYPE_ACCESS,
		    ARCHIVE_ENTRY_ACL_USER_OBJ, flags, NULL,
		    acl->mode & 0700, -1);
		*wp++ = separator;
		append_entry_w(&wp, NULL, ARCHIVE_ENTRY_ACL_TYPE_ACCESS,
		    ARCHIVE_ENTRY_ACL_GROUP_OBJ, flags, NULL,
		    acl->mode & 0070, -1);
		*wp++ = separator;
		append_entry_w(&wp, NULL, ARCHIVE_ENTRY_ACL_TYPE_ACCESS,
		    ARCHIVE_ENTRY_ACL_OTHER, flags, NULL,
		    acl->mode & 0007, -1);
		count += 3;
	}

	for (ap = acl->acl_head; ap != NULL; ap = ap->next) {
		if ((ap->type & want_type) == 0)
			continue;
		/*
		 * Filemode-mapping ACL entries are stored exclusively in
		 * ap->mode so they should not be in the list
		 */
		if ((ap->type == ARCHIVE_ENTRY_ACL_TYPE_ACCESS)
		    && (ap->tag == ARCHIVE_ENTRY_ACL_USER_OBJ
		    || ap->tag == ARCHIVE_ENTRY_ACL_GROUP_OBJ
		    || ap->tag == ARCHIVE_ENTRY_ACL_OTHER))
			continue;
		if (ap->type == ARCHIVE_ENTRY_ACL_TYPE_DEFAULT &&
		    (flags & ARCHIVE_ENTRY_ACL_STYLE_MARK_DEFAULT) != 0)
			prefix = L"default:";
		else
			prefix = NULL;
		r = archive_mstring_get_wcs(a, &ap->name, &wname);
		if (r == 0) {
			if (count > 0)
				*wp++ = separator;
			if (flags & ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID)
				id = ap->id;
			else
				id = -1;
			append_entry_w(&wp, prefix, ap->type, ap->tag, flags,
			    wname, ap->permset, id);
			count++;
		} else if (r < 0 && errno == ENOMEM) {
			free(ws);
			return (NULL);
		}
	}

	/* Add terminating character */
	*wp++ = L'\0';

	len = wcslen(ws);

	if (len > length - 1)
		__archive_errx(1, "Buffer overrun");

	if (text_len != NULL)
		*text_len = len;

	return (ws);
}
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/archive_acl_text_len.overflow.8.json`
- `bug_reports/archive_acl_to_text_w.overflow.1.json`
- `bug_reports/archive_acl_to_text_w.pointer_dereference.77.json`
- `bug_reports/unnamed_1779685656497.json`
- `bug_reports/unnamed_1779686380873.json`
- `bug_reports/unnamed_1779686463538.json`
- `bug_reports/unnamed_1779686558380.json`
- `bug_reports/unnamed_1779686796062.json`

## Reproduction

The harness CBMC verified is committed alongside this report as `harness.c`. To re-verify just this finding:

```bash
# 1. clone libarchive at the snapshot the sweep used
cd /tmp && git clone https://github.com/libarchive/libarchive
cd libarchive && git checkout 67830f7b9c27080c0170bcd71d94fb42316c47dd

# 2. apply CBMC bounds + pointer + signed-overflow checks
cbmc \
    --bounds-check --pointer-check --div-by-zero-check \
    --signed-overflow-check --unsigned-overflow-check --pointer-overflow-check \
    --unwind 4 --timeout 60 \
    -I /tmp/libarchive/libarchive -I /tmp/libarchive/libarchive/build \
    -DHAVE_CONFIG_H \
    --function main \
    archive_acl_to_text_w/harness.c
# (paste the harness contents from the section below into harness.c first;
#  it is also committed alongside this report as harness.c.)

```

To re-run the full sweep end-to-end (re-derives this finding from scratch):

```bash
(no command provided)
```

## Honest caveats (read before upstream reporting)

- **Dynamic outcome was `no_record`.** WEAK evidence: the dynamic harness did NOT reproduce the crash with the concrete CBMC witness. The realism LLM's vote is the only evidence.
- The realism LLM's attacker scenario may hypothesize an upstream condition (e.g. "some bug elsewhere creates the dangling pointer state"). **Independent code-level verification of that condition is required before reporting upstream.**
- Realism nondeterminism: the same CEx can flip between REALISTIC and UNREALISTIC across runs. Multiple per-CEx records in `bug_reports/` may show different verdicts; this report uses the strongest realistic record by mtime.
- The harness is auto-generated and uses CBMC's nondeterministic-input model. Reading `harness.c` shows exactly what input states CBMC was free to explore — verify those states are actually reachable from the real public API before declaring a vulnerability.
