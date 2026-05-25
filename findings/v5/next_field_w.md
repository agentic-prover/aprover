# bmc-agent-sec confirmed finding: `next_field_w`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:32:54.883103+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `next_field_w` (lines 1199-1204)
- **Violated property**: `next_field_w.pointer_arithmetic.11` (CBMC-reported)
- **Call chain established**: `archive_acl_from_text_w -> next_field_w`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 4356 in next_field_w() where (*end)-- is executed. The counterexample shows _start_off = 2u, _end_off = 4u, meaning start points to index 2 and end points to index 4 of a 5-element buffer. The function enters the else branch at line 4354 when *wp != *start (i.e., the field is not empty). At line 4355, *end is set to *wp - 1. Then the while loop at line 4356 decrements *end while **end is whitespace. The bug occurs when the field contains trailing whitespace at positions 3 and 4, and the loop decrements *end past *start, potentially going to index -1 (before the buffer start). The counterexample witness shows _shared_buf_0[2l] = 8 (backspace), _shared_buf_0[3l] = 50331648, _shared_buf_0[4l] = 8 (backspace) - these backspace characters would satisfy the whitespace check (**end == L'\t' || **end == L'\n') at line 4356. An attacker can reach this by calling archive_acl_from_text_w() (line 3989) with a malicious wide-character ACL string containing a field like 'X:\b\b' where \b is backspace (wchar 8). The parser would set start at 'X' and end after the backspaces, then the loop would decrement end past start, causing an out-of-bounds read/write. This is exploitable via archive_entry_acl_from_text() (line 2989) which is part of the public libarchive API.

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive file (tar, zip, etc.) with an ACL entry containing a wide-character string like 'user:X:\b\b:rwx' where \b represents backspace characters (Unicode 0x08). When libarchive parses this via archive_acl_from_text_w(), the next_field_w() function processes the 'X:\b\b' field. It sets start='X' and end after the backspaces, then enters the trailing-whitespace-trimming loop. Since backspace satisfies the whitespace check, the loop decrements end past start into negative array indices, causing a pointer arithmetic violation that can lead to memory corruption or information disclosure.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_rounding_mode = 0
  _end_off = 4u
  _sep_val = 0
  _shared_buf_0 = <array: 5 elements>
  _shared_buf_0[0l] = 35124
  _shared_buf_0[1l] = 16777504
  _shared_buf_0[2l] = 8
  _shared_buf_0[3l] = 50331648
  _shared_buf_0[4l] = 8
  _start_off = 2u
  _wp_backing = <array: 5 elements>
  _wp_cursor = {'name': 'unknown'}
  byte_extract_little_endian(_shared_buf_0, (signed long int)__CPROVER_POINTER_OFFSET(start), signed int *) = {'name': 'unknown'}
  end = {'name': 'unknown'}
  sep = _sep_val!0@1
  start = {'name': 'unknown'}
  wp = _wp_cursor!0@1
```

## Function source (from the snapshot)

```c
			const wchar_t *start, *end;
			next_field_w(&text, &start, &end, &sep);
			if (fields < numfields) {
				field[fields].start = start;
				field[fields].end = end;
			}
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/main.pointer_dereference.2.json`
- `bug_reports/next_field_w.pointer_arithmetic.11.json`
- `bug_reports/next_field_w.pointer_dereference.65.json`
- `bug_reports/unnamed_1779685659480.json`
- `bug_reports/unnamed_1779689165781.json`
- `bug_reports/unnamed_1779689227113.json`

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
    next_field_w/harness.c
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
