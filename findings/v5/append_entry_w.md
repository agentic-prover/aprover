# bmc-agent-sec confirmed finding: `append_entry_w`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T05:56:49.162659+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `append_entry_w` (lines 717-730)
- **Violated property**: `append_entry_w.pointer_dereference.71` (CBMC-reported)
- **Call chain established**: `archive_acl_to_text_w -> append_entry_w`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 3721 in append_entry_w where *(*wp)++ dereferences wp after potentially writing beyond the allocated buffer. The function archive_acl_to_text_w (lines 3580-3662) calculates the required buffer length via archive_acl_text_len (lines 3473-3579), then allocates 'length * sizeof(*wp)' bytes (line 3605). However, the length calculation has a critical flaw: at line 3551 it adds 27 characters for NFSv4 ACL entries, but the actual append_entry_w can write more. Specifically, lines 3745-3757 write up to 14 permission characters (nfsv4_acl_perm_map_size=14), then 7 flag characters (nfsv4_acl_flag_map_size=7), plus colons and type strings. When flags & 0x00000010 is zero, each character position is filled, leading to more characters than the 27 budgeted. An attacker controlling ACL data (via archive_entry_acl_add_entry_w or similar APIs) with NFSv4 ACL types can trigger this. The counterexample shows type=0x900 (DENY|ALLOW bits mixed, though unusual, type validation at lines 3279-3308 allows multiple type bits), tag=268445459 (passes validation since it's checked at lines 3309-3328 but unusual values may pass through), flags=4, and perm=15. With these values, append_entry_w writes more than allocated, causing wp to advance beyond the buffer end, triggering the pointer-outside-object-bounds violation.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive containing NFSv4 ACL entries with specific combinations of permission bits (setting all 14 permission flags) and flag bits (setting all 7 inheritance flags) with flags parameter set to exclude ARCHIVE_ENTRY_ACL_STYLE_COMPACT (0x00000010). When archive_acl_to_text_w is called (e.g., via archive_entry_acl_to_text_w from user code processing the archive), the length calculation underestimates the required buffer size. As append_entry_w writes the full permission and flag strings (potentially 14+7=21 characters plus separators and type string, exceeding the budgeted 27), the write pointer advances beyond the allocated buffer, causing a buffer overflow that could lead to memory corruption or information disclosure.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_rounding_mode = 0
  _prefix_val = 0
  _wname_val = 0
  _wp_backing = <array: 5 elements>
  _wp_cursor = {'name': 'unknown'}
  flags = 4
  i = 0
  id = 0
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
  nfsv4_acl_perm_map[11l].perm = 8192
  nfsv4_acl_perm_map[11l].wc = 67
  nfsv4_acl_perm_map[12l] = <struct: 4 members>
  nfsv4_acl_perm_map[12l].$pad2 = 0
  nfsv4_acl_perm_map[12l].c = 'o'
  nfsv4_acl_perm_map[12l].perm = 16384
  nfsv4_acl_perm_map[12l].wc = 111
  nfsv4_acl_perm_map[13l] = <struct: 4 members>
  nfsv4_acl_perm_map[13l].$pad2 = 0
  nfsv4_acl_perm_map[13l].c = 's'
  nfsv4_acl_perm_map[13l].perm = 32768
  nfsv4_acl_perm_map[13l].wc = 115
  nfsv4_acl_perm_map[1l] = <struct: 4 members>
  nfsv4_acl_perm_map[1l].$pad2 = 0
  nfsv4_acl_perm_map[1l].c = 'w'
  nfsv4_acl_perm_map[1l].perm = 16
  nfsv4_acl_perm_map[1l].wc = 119
  nfsv4_acl_perm_map[2l] = <struct: 4 members>
  nfsv4_acl_perm_map[2l].$pad2 = 0
  nfsv4_acl_perm_map[2l].c = 'x'
  nfsv4_acl_perm_map[2l].perm = 1
  nfsv4_acl_perm_map[2l].wc = 120
  nfsv4_acl_perm_map[3l] = <struct: 4 members>
  ... (truncated)
```

## Function source (from the snapshot)

```c
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
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/append_entry_w.pointer_dereference.71.json`
- `bug_reports/unnamed_1779685657406.json`

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
    append_entry_w/harness.c
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
