# bmc-agent-sec confirmed finding: `next_field_w`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:08:30.449801+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `next_field_w` (lines 1199-1204)
- **Violated property**: `next_field_w.pointer_dereference.65` (CBMC-reported)
- **Call chain established**: `archive_acl_from_text_w -> next_field_w`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **inconclusive**, signal=`None` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 4357 in next_field_w when dereferencing **end after a decrement loop (lines 4356-4358). The counterexample shows _start_off = 4u and _end_off = 4u, meaning start and end point to the same position initially. The function is called from archive_acl_from_text_w (line 4021) which parses attacker-controlled wide-character ACL text. At line 4352, when *wp == *start (both at offset 4), the else branch at line 4354 is NOT taken, so the code falls through to line 4356 and sets *end = *wp - 1. Since *wp points to offset 4 in a 5-element buffer, *wp - 1 points to offset 3. However, the while loop condition at line 4356 (**end == L' ' || **end == L'\t' || **end == L'\n') dereferences *end before checking bounds. If the attacker crafts input where offset 3 contains whitespace and offset 2 also contains whitespace, the loop continues decrementing. Eventually (*end)-- at line 4357 can move *end before the buffer start (offset -1), causing an out-of-bounds read at the next loop iteration when **end is dereferenced again at line 4356. The CBMC witness shows the buffer contains values like 35124, 16777504, etc., which when cast to wchar_t could produce various characters including whitespace. The key attack vector is: supply a wchar_t string to archive_acl_from_text_w where the first field consists entirely of whitespace at the beginning of the buffer, causing next_field_w to be called with wp pointing near the buffer start, triggering the underflow in the trim-trailing-whitespace loop.

## Exploit scenario (LLM-supplied)

An attacker provides a maliciously crafted ACL text string (via archive_acl_from_text_w) containing a field that starts at the beginning of the input buffer and consists of multiple leading whitespace characters followed by a separator. When next_field_w processes this field, it sets *start = *wp at the buffer start, then advances *wp past the whitespace. If the field is empty or very short, the else branch at line 4354 sets *end = *wp - 1 and attempts to trim trailing whitespace by decrementing *end in a loop. If the memory immediately before the buffer also contains whitespace patterns (or the attacker can influence adjacent memory through heap layout), the decrement loop underflows *end past the buffer's beginning, causing an out-of-bounds read when **end is dereferenced at line 4356. This violates memory safety and could leak adjacent memory contents or crash the process.

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
  _shared_buf_0[2l] = 0
  _shared_buf_0[3l] = 50331648
  _shared_buf_0[4l] = 8
  _start_off = 4u
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

- **Dynamic outcome was `inconclusive`.** WEAK evidence: the dynamic harness did NOT reproduce the crash with the concrete CBMC witness. The realism LLM's vote is the only evidence.
- The realism LLM's attacker scenario may hypothesize an upstream condition (e.g. "some bug elsewhere creates the dangling pointer state"). **Independent code-level verification of that condition is required before reporting upstream.**
- Realism nondeterminism: the same CEx can flip between REALISTIC and UNREALISTIC across runs. Multiple per-CEx records in `bug_reports/` may show different verdicts; this report uses the strongest realistic record by mtime.
- The harness is auto-generated and uses CBMC's nondeterministic-input model. Reading `harness.c` shows exactly what input states CBMC was free to explore — verify those states are actually reachable from the real public API before declaring a vulnerability.
