# bmc-agent-sec confirmed finding: `archive_acl_text_len`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:32:54.855648+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_text_len` (lines 544-669)
- **Violated property**: `archive_acl_text_len.overflow.12` (CBMC-reported)
- **Call chain established**: `archive_acl_to_text_l -> archive_acl_text_len`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The overflow occurs at line 3568 in archive_acl_text_len when computing 'length + (unsigned long int)1' (the final 'length ++' statement). The function iterates over ACL entries and accumulates various length increments without any overflow checking. An attacker can control the ACL structure by calling archive_acl_add_entry or archive_acl_from_text_l to populate acl->acl_head with many entries. Each iteration adds multiple increments to 'length' (lines 3495-3568), including fixed constants (lines 3496-3518), name lengths from archive_mstring_get_mbs_l (lines 3532-3539), and additional calculations (lines 3551-3566). The CBMC witness shows length = SIZE_MAX-1 (18446744073709551615ul) before the final increment, which would overflow. This is achievable by: (1) creating a very long linked list of ACL entries via repeated archive_acl_add_entry calls, or (2) providing ACL entries with extremely long name strings via archive_mstring_copy_mbs_len, or (3) a combination causing the cumulative length to approach SIZE_MAX. The call chain archive_acl_to_text_l → archive_acl_text_len (line 3801) shows this is reachable from public API. The counterexample shows len=18446744073709551612ul from archive_mstring_get_mbs_l (line 3539), meaning a maliciously crafted mstring could contribute massively to the overflow. No overflow guards exist in the loop (lines 3484-3569).

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive file (e.g., tar, cpio, zip) containing ACL entries with extremely long username/group name strings or an enormous number of ACL entries. When libarchive parses this file and calls archive_acl_to_text_l (for example, to display ACL text or convert to a specific format), the archive_acl_text_len function accumulates lengths without checking for overflow. By carefully sizing the input—either through thousands of ACL entries or through name strings approaching SIZE_MAX in aggregate—the attacker causes 'length' to reach SIZE_MAX-1. The final 'length++' at line 3568 then wraps to 0, causing malloc(0) at line 3808, resulting in a tiny or NULL allocation. Subsequent string operations write to this under-allocated buffer, leading to heap corruption and potential code execution.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_errno = 0
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_rounding_mode = 0
  __a_obj_archive_format_name_buf = <array: 5 elements>
  __a_obj_archive_format_name_buf[0l] = 0
  __a_obj_archive_format_name_buf[1l] = 0
  __a_obj_archive_format_name_buf[2l] = 0
  __a_obj_archive_format_name_buf[3l] = 0
  __a_obj_archive_format_name_buf[4l] = 0
  __a_obj_archive_format_name_len = 0u
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
  __a_obj_error_len = 1u
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
  a = _a_obj!0@1
  acl = _acl_obj!0@1
  ap = __acl_obj_acl_head_obj!0@1
  count = 1
  flags = 5
  idlen = 0
  len = 18446744073709551612ul
  length = 18446744073709551615ul
  name = {'name': 'unknown'}
  r = 0
  result = 0ul
  return_value_archive_acl_text_len = 0ul
  return_value_wcslen = -7
  sc = {'name': 'unknown'}
  tmp = 0
  want_type = 15360
  wide = 33554432
  wname = {'name': 'unknown'}
```

## Function source (from the snapshot)

```c
static size_t
archive_acl_text_len(struct archive_acl *acl, int want_type, int flags,
    int wide, struct archive *a, struct archive_string_conv *sc) {
	struct archive_acl_entry *ap;
	const char *name;
	const wchar_t *wname;
	int count, idlen, tmp, r;
	size_t length;
	size_t len;

	count = 0;
	length = 0;
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
		count++;
		if ((want_type & ARCHIVE_ENTRY_ACL_TYPE_DEFAULT) != 0
		    && (ap->type & ARCHIVE_ENTRY_ACL_TYPE_DEFAULT) != 0)
			length += 8; /* "default:" */
		switch (ap->tag) {
		case ARCHIVE_ENTRY_ACL_USER_OBJ:
			if (want_type == ARCHIVE_ENTRY_ACL_TYPE_NFS4) {
				length += 6; /* "owner@" */
				break;
			}
			/* FALLTHROUGH */
		case ARCHIVE_ENTRY_ACL_USER:
		case ARCHIVE_ENTRY_ACL_MASK:
			length += 4; /* "user", "mask" */
			break;
		case ARCHIVE_ENTRY_ACL_GROUP_OBJ:
			if (want_type == ARCHIVE_ENTRY_ACL_TYPE_NFS4) {
				length += 6; /* "group@" */
				break;
			}
			/* FALLTHROUGH */
		case ARCHIVE_ENTRY_ACL_GROUP:
		case ARCHIVE_ENTRY_ACL_OTHER:
			length += 5; /* "group", "other" */
			break;
		case ARCHIVE_ENTRY_ACL_EVERYONE:
			length += 9; /* "everyone@" */
			break;
		}
		length += 1; /* colon after tag */
		if (ap->tag == ARCHIVE_ENTRY_ACL_USER ||
		    ap->tag == ARCHIVE_ENTRY_ACL_GROUP) {
			if (wide) {
				r = archive_mstring_get_wcs(a, &ap->name,
				    &wname);
				if (r == 0 && wname != NULL)
					length += wcslen(wname);
				else if (r < 0 && errno == ENOMEM)
					return (0);
				else
					length += sizeof(uid_t) * 3 + 1;
			} else {
				r = archive_mstring_get_mbs_l(a, &ap->name, &name,
				    &len, sc);
				if (r != 0)
					return (0);
				if (len > 0 && name != NULL)
					length += len;
				else
					length += sizeof(uid_t) * 3 + 1;
			}
			length += 1; /* colon after user or group name */
		} else if (want_type != ARCHIVE_ENTRY_ACL_TYPE_NFS4)
			length += 1; /* 2nd colon empty user,group or other */

		if (((flags & ARCHIVE_ENTRY_ACL_STYLE_SOLARIS) != 0)
		    && ((want_type & ARCHIVE_ENTRY_ACL_TYPE_POSIX1E) != 0)
		    && (ap->tag == ARCHIVE_ENTRY_ACL_OTHER
		    || ap->tag == ARCHIVE_ENTRY_ACL_MASK)) {
			/* Solaris has no colon after other: and mask: */
			length = length - 1;
		}

		if (want_type == ARCHIVE_ENTRY_ACL_TYPE_NFS4) {
			/* rwxpdDaARWcCos:fdinSFI:deny */
			length += 27;
			if ((ap->type & ARCHIVE_ENTRY_ACL_TYPE_DENY) == 0)
				length += 1; /* allow, alarm, audit */
		} else
			length += 3; /* rwx */

		if ((ap->tag == ARCHIVE_ENTRY_ACL_USER ||
		    ap->tag == ARCHIVE_ENTRY_ACL_GROUP) &&
		    (flags & ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID) != 0) {
			length += 1; /* colon */
			/* ID digit count */
			idlen = 1;
			tmp = ap->id;
			while (tmp > 9) {
				tmp = tmp / 10;
				idlen++;
			}
			length += idlen;
		}
		length ++; /* entry separator */
	}

	/* Add filemode-mapping access entries to the length */
	if ((want_type & ARCHIVE_ENTRY_ACL_TYPE_ACCESS) != 0) {
		if ((flags & ARCHIVE_ENTRY_ACL_STYLE_SOLARIS) != 0) {
			/* "user::rwx\ngroup::rwx\nother:rwx\n" */
			length += 31;
		} else {
			/* "user::rwx\ngroup::rwx\nother::rwx\n" */
			length += 32;
		}
	} else if (count == 0)
		return (0);

	/* The terminating character is included in count */
	return (length);
}
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/archive_acl_text_len.overflow.10.json`
- `bug_reports/archive_acl_text_len.overflow.12.json`
- `bug_reports/archive_acl_text_len.overflow.8.json`
- `bug_reports/unnamed_1779685657494.json`

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
    archive_acl_text_len/harness.c
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
