# bmc-agent-sec confirmed finding: `append_entry`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:32:54.846980+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `append_entry` (lines 954-967)
- **Violated property**: `strcpy.pointer_dereference.11` (CBMC-reported)
- **Call chain established**: `archive_acl_to_text_l -> append_entry`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs in strcpy at line 3891 in append_entry when copying 'owner@' string. Looking at the call chain: archive_acl_to_text_l (line 3783-3866) → append_entry (line 3877-3987). The counterexample shows prefix='\x80\x10\x00\x08' (a 4-byte buffer), tag=10005 (ARCHIVE_ENTRY_ACL_MASK), type=256, perm=42. At line 3882-3884, if prefix is non-NULL, strcpy(*p, prefix) is called and *p is advanced by strlen(*p). The prefix buffer in the witness is only 4 bytes with _prefix_len=4, but contains non-null-terminated data (ends with value 8, not 0). When strlen is called on this at line 3884, it will read beyond the 4-byte buffer looking for a null terminator, then strcpy will write that many+1 bytes (including the final null) into *p. The _p_backing buffer is only 5 bytes, so after writing prefix, there's minimal space left. At line 3886-3925, the switch statement processes tag=10005 (ARCHIVE_ENTRY_ACL_MASK), which at line 3907 does strcpy(*p, 'mask'), requiring 5 bytes including null. Combined with earlier writes, this exceeds the 5-byte _p_backing buffer. An attacker controlling ACL text input via archive_acl_from_text_l can supply a malformed prefix or construct ACL entries that cause the buffer to be undersized relative to the formatted output, triggering the OOB write in strcpy.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive file with crafted POSIX.1e ACL entries. By controlling the ACL text format parsed by archive_acl_from_text_l (line 4370-4587), they can inject entries with a mask tag and specific prefix patterns. When archive_acl_to_text_l is called to serialize these ACLs back to text (common during archive extraction or listing), the function allocates a buffer based on archive_acl_text_len but the calculation can be incorrect if prefix contains non-printable characters or the name field has unexpected encoding. The strcpy at line 3908 (or 3891) writes 'mask' (or 'owner@') into the undersized buffer *p, overwriting adjacent heap metadata or other sensitive structures, leading to memory corruption exploitable for code execution or information disclosure.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_rounding_mode = 0
  _name_buf = <array: 5 elements>
  _name_buf[0l] = 0
  _name_buf[1l] = -128
  _name_buf[2l] = ' '
  _name_buf[3l] = 8
  _name_buf[4l] = 0
  _name_len = 0u
  _p_backing = <array: 5 elements>
  _p_backing[(signed long int)__CPROVER_POINTER_OFFSET(_p_backing!0@1 + 2l) + 1l] = {'name': 'unknown'}
  _p_backing[(signed long int)__CPROVER_POINTER_OFFSET(_p_backing!0@1 + 2l) + 2l] = {'name': 'unknown'}
  _p_backing[(signed long int)__CPROVER_POINTER_OFFSET(_p_backing!0@1 + 2l)] = {'name': 'unknown'}
  _p_backing[0l] = -128
  _p_backing[1l] = 16
  _p_backing[2l] = 'm'
  _p_backing[3l] = 'a'
  _p_backing[4l] = 's'
  _p_cursor = {'name': 'unknown'}
  _p_nul_at = 0u
  _prefix_buf = <array: 5 elements>
  _prefix_buf[0l] = -128
  _prefix_buf[1l] = 16
  _prefix_buf[2l] = 0
  _prefix_buf[3l] = 8
  _prefix_buf[4l] = 0
  _prefix_len = 4u
  ch = 'k'
  dst = {'name': 'unknown'}
  flags = 20
  goto_symex$$return_value$$strlen = 2ul
  i = 3ul
  id = -2049
  len = 2ul
  name = _name_buf!0@1
  nfsv4_acl_flag_map = <array: 7 elements>
  nfsv4_acl_flag_map[0l] = <struct: 4 members>
  nfsv4_acl_flag_map[0l].$pad2 = 0
  nfsv4_acl_flag_map[0l].c = 'f'
  nfsv4_acl_flag_map[0l].perm = 33554432
  nfsv4_acl_flag_map[0l].wc = 102
  nfsv4_acl_flag_map[1l] = <struct: 4 members>
  nfsv4_acl_flag_map[1l].$pad2 = 0
  nfsv4_acl_flag_map[1l].c = 'd'
  nfsv4_acl_flag_map[1l].perm = 67108864
  nfsv4_acl_flag_map[1l].wc = 100
  nfsv4_acl_flag_map[2l] = <struct: 4 members>
  nfsv4_acl_flag_map[2l].$pad2 = 0
  nfsv4_acl_flag_map[2l].c = 'i'
  nfsv4_acl_flag_map[2l].perm = 268435456
  nfsv4_acl_flag_map[2l].wc = 105
  nfsv4_acl_flag_map[3l] = <struct: 4 members>
  nfsv4_acl_flag_map[3l].$pad2 = 0
  nfsv4_acl_flag_map[3l].c = 'n'
  nfsv4_acl_flag_map[3l].perm = 134217728
  nfsv4_acl_flag_map[3l].wc = 110
  nfsv4_acl_flag_map[4l] = <struct: 4 members>
  nfsv4_acl_flag_map[4l].$pad2 = 0
  nfsv4_acl_flag_map[4l].c = 'S'
  nfsv4_acl_flag_map[4l].perm = 536870912
  nfsv4_acl_flag_map[4l].wc = 83
  nfsv4_acl_flag_map[5l] = <struct: 4 members>
  nfsv4_acl_flag_map[5l].$pad2 = 0
  nfsv4_acl_flag_map[5l].c = 'F'
  nfsv4_acl_flag_map[5l].perm = 1073741824
  nfsv4_acl_flag_map[5l].wc = 70
  nfsv4_acl_flag_map[6l] = <struct: 4 members>
  nfsv4_acl_flag_map[6l].$pad2 = 0
  nfsv4_acl_flag_map[6l].c = 'I'
  nfsv4_acl_flag_map[6l].perm = 16777216
  nfsv4_acl_flag_map[6l].wc = 73
  nfsv4_acl_flag_map_size = 7
  nfsv4_acl_perm_map = <array: 14 elements>
  nfsv4_acl_perm_map[0l] = <struct: 4 members>
  nfsv4_acl_perm_map[0l].$pad2 = 0
  nfsv4_acl_perm_map[0l].c = 'r'
  nfsv4_acl_perm_map[0l].perm = 8
  nfsv4_acl_perm_map[0l].wc = 114
  nfsv4_acl_perm_map[10l] = <struct: 4 members>
  nfsv4_acl_perm_map[10l].$pad2 = 0
  nfsv4_acl_perm_map[10l].c = 'c'
  nfsv4_acl_perm_map[10l].perm = 4096
  nfsv4_acl_perm_map[10l].wc = 99
  nfsv4_acl_perm_map[11l] = <struct: 4 members>
  nfsv4_acl_perm_map[11l].$pad2 = 0
  nfsv4_acl_perm_map[11l].c = 'C'
  ... (truncated)
```

## Function source (from the snapshot)

```c
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
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/strcpy.pointer_dereference.11.json`
- `bug_reports/unnamed_1779685657679.json`

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
    append_entry/harness.c
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
