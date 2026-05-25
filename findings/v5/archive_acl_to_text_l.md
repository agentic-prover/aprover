# bmc-agent-sec confirmed finding: `archive_acl_to_text_l`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:03:11.213240+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_to_text_l` (lines 912-1017)
- **Violated property**: `archive_acl_text_len.overflow.12` (CBMC-reported)
- **Call chain established**: `archive_acl_to_text_l`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 2094 in archive_acl_text_len when computing 'length + (unsigned long int)1'. The counterexample shows length=18446744073709551615ul (SIZE_MAX), causing overflow when adding 1. Tracing the call chain: archive_acl_to_text_l (line 3801) calls archive_acl_text_len (line 3473) with attacker-controlled acl structure. The acl structure can be populated via archive_acl_add_entry (line 3193) or archive_acl_add_entry_w_len (line 3210), both of which are public APIs that accept arbitrary type, permset, tag, and name parameters. An attacker can craft an ACL with numerous entries that cause archive_acl_text_len to accumulate length until it approaches SIZE_MAX. Specifically, at line 3519 in archive_acl_text_len, each ACL entry contributes to length based on name length, tag type, and permission string representations. With a large number of entries (e.g., 10001 entries via acl_head->next chain as shown in counterexample), or entries with long names via archive_mstring functions, the accumulated length can reach SIZE_MAX. The function then adds 1 at line 3568 ('length ++') for the null terminator, causing unsigned overflow. The overflow is then used at line 3808 to malloc(length * sizeof(*p)), which with wrapped-around small value would allocate insufficient memory, leading to buffer overrun when archive_acl_to_text_l writes the ACL string starting at line 3816. The malloc contract (line 901) guarantees valid pointer OR NULL, but the subsequent strlen check at line 3861 would pass with a too-small buffer, and the earlier writes (lines 3816-3858) would have already overflowed. The CBMC witness shows this is reachable with acl_head containing one entry, but the length calculation logic allows arbitrary accumulation through repeated API calls.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive file containing an entry with a crafted ACL that, when parsed by libarchive's archive_acl_from_text_l or similar functions, results in thousands of ACL entries being added via archive_acl_add_entry. Each entry contributes to the accumulated length in archive_acl_text_len. By carefully choosing the number and properties of entries (e.g., NFSv4 ACLs with long permission strings and flags as computed in lines 3550-3568), the attacker causes length to reach SIZE_MAX. When archive_acl_to_text_l is called (e.g., during archive_entry_acl_to_text at line 2986), the overflow occurs, malloc allocates a tiny buffer, and subsequent string operations write far beyond the allocated region, corrupting heap metadata and potentially achieving arbitrary code execution.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_alloca_object = NULL
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_errno = 0
  __CPROVER_malloc_is_new_array = False
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_new_object = NULL
  __CPROVER_rounding_mode = 0
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
  __acl_obj_acl_text_len = 1u
  _acl_obj = <struct: 10 members>
  _acl_obj.acl_head = __acl_obj_acl_head_obj!0@1
  _acl_obj.acl_p = __acl_obj_acl_p_obj!0@1
  _acl_obj.acl_text = __acl_obj_acl_text_buf!0@1
  _text_len_val = 0l
  a = ((struct archive *)NULL)
  acl = _acl_obj!0@1
  ap = __acl_obj_acl_head_obj!0@1
  count = 1
  flags = 774
  goto_symex$$return_value$$archive_acl_text_want_type = 15360
  id = 0
  idlen = 0
  len = 18446744073709551609ul
  length = 18446744073709551615ul
  name = {'name': 'unknown'}
  p = ((char *)NULL)
  prefix = ((char *)NULL)
  r = 0
  result = ((char *)NULL)
  return_value_archive_acl_to_text_l = ((char *)NULL)
  s = ((char *)NULL)
  sc = ((struct archive_string_conv *)NULL)
  separator = 0
  text_len = _text_len_val!0@1
  tmp = 0
  want_type = 15360
  wide = 0
  wname = ((signed int *)NULL)
```

## Function source (from the snapshot)

```c
char *
archive_acl_to_text_l(struct archive_acl *acl, ssize_t *text_len, int flags,
    struct archive_string_conv *sc)
{
	int count;
	size_t length;
	size_t len;
	const char *name;
	const char *prefix;
	char separator;
	struct archive_acl_entry *ap;
	int id, r, want_type;
	char *p, *s;

	want_type = archive_acl_text_want_type(acl, flags);

	/* Both NFSv4 and POSIX.1 types found */
	if (want_type == 0)
		return (NULL);

	if (want_type == ARCHIVE_ENTRY_ACL_TYPE_POSIX1E)
		flags |= ARCHIVE_ENTRY_ACL_STYLE_MARK_DEFAULT;

	length = archive_acl_text_len(acl, want_type, flags, 0, NULL, sc);

	if (length == 0)
		return (NULL);

	if (flags & ARCHIVE_ENTRY_ACL_STYLE_SEPARATOR_COMMA)
		separator = ',';
	else
		separator = '\n';

	/* Now, allocate the string and actually populate it. */
	p = s = malloc(length * sizeof(*p));
	if (p == NULL) {
		if (errno == ENOMEM)
			__archive_errx(1, "No memory");
		return (NULL);
	}
	count = 0;

	if ((want_type & ARCHIVE_ENTRY_ACL_TYPE_ACCESS) != 0) {
		append_entry(&p, NULL, ARCHIVE_ENTRY_ACL_TYPE_ACCESS,
		    ARCHIVE_ENTRY_ACL_USER_OBJ, flags, NULL,
		    acl->mode & 0700, -1);
		*p++ = separator;
		append_entry(&p, NULL, ARCHIVE_ENTRY_ACL_TYPE_ACCESS,
		    ARCHIVE_ENTRY_ACL_GROUP_OBJ, flags, NULL,
		    acl->mode & 0070, -1);
		*p++ = separator;
		append_entry(&p, NULL, ARCHIVE_ENTRY_ACL_TYPE_ACCESS,
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
			prefix = "default:";
		else
			prefix = NULL;
		r = archive_mstring_get_mbs_l(
		    NULL, &ap->name, &name, &len, sc);
		if (r != 0) {
			free(s);
			return (NULL);
		}
		if (count > 0)
			*p++ = separator;
		if (name == NULL ||
		    (flags & ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID)) {
			id = ap->id;
		} else {
			id = -1;
		}
		append_entry(&p, prefix, ap->type, ap->tag, flags, name,
		    ap->permset, id);
		count++;
	}

	/* Add terminating character */
	*p++ = '\0';

	len = strlen(s);

	if (len > length - 1)
		__archive_errx(1, "Buffer overrun");

	if (text_len != NULL)
		*text_len = len;

	return (s);
}
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/archive_acl_text_len.overflow.10.json`
- `bug_reports/archive_acl_text_len.overflow.12.json`
- `bug_reports/strlen.pointer_arithmetic.5.json`
- `bug_reports/strlen.pointer_dereference.5.json`
- `bug_reports/unnamed_1779685656305.json`

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
    archive_acl_to_text_l/harness.c
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
