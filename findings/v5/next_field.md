# bmc-agent-sec confirmed finding: `next_field`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:03:11.226503+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `next_field` (lines 1698-1703)
- **Violated property**: `next_field.pointer_arithmetic.11` (CBMC-reported)
- **Call chain established**: `archive_acl_from_text_nl -> next_field`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **inconclusive**, signal=`None` |
| Final tier | `confirmed_bmc` |

## Realism reasoning

The violation occurs at line 4738 in next_field() where (*p)++ executes pointer arithmetic that CBMC claims produces a pointer outside object bounds. The counterexample shows _p_cursor pointing to a 5-byte buffer (_p_backing) with _l_val=2, meaning length=2. The function is called from archive_acl_from_text_nl (line 4409) which parses attacker-controlled text input. The critical flow is: (1) text points to _p_backing[0]=tab, (2) length=2, (3) next_field is called repeatedly to parse colon-separated fields. The first loop at lines 4733-4736 skips whitespace/tabs/newlines - with tab at offset 0, it increments *p from 0→1, then *l from 2→1. The second loop (4738-4741) advances past non-separator chars. With newline at offset 1, the loop condition **p != newline is false immediately, so *end = *p = offset 1. The third loop (4743-4747) searches for separators - with newline at offset 1, **p == newline is true, so loop body never executes, *sep = newline. Line 4748-4753 handles comment-skip (not triggered here since *sep != '#'). Finally, lines 4755-4758: the condition *l > 0 is TRUE (l=1), so (*p)++ executes, advancing p from offset 1 to offset 2. But the buffer is only 5 bytes (indices 0-4), and we've consumed 2 bytes of the provided length - however, the pointer arithmetic itself is valid (offset 2 < 5). The CBMC witness shows _p_backing has a null at offset 3 and 4, with the 5th byte being 0x03. The violation likely triggers because CBMC models the pointer as reaching offset 4 after multiple field-parse iterations, and the final increment tries to compute offset 5 (== &_p_backing[5]), which is technically one-past-end but should be valid for pointer arithmetic in C. However, the key issue is that the function does NOT validate that *p remains within the original buffer bounds - it only checks *l > 0, which is a count that can be manipulated independently. An attacker providing text='\t\n\n\0' with length=2 can cause next_field to advance p beyond the provided length, and if called repeatedly (as in the do-while loop at lines 4407-4415), subsequent iterations may dereference p when it points past the safe region. The violation is REALISTIC because: (a) archive_acl_from_text_nl is a public API (line 4377), (b) text and length are attacker-controlled (from archive file metadata), (c) there is no bounds-check ensuring *p stays within [text, text+original_length), only that *l hasn't underflowed to zero, and (d) the field-parsing loop can call next_field multiple times, each time incrementing *p and decrementing *l, until *l=0 but *p may point arbitrarily far into memory.

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive file with an ACL text field containing carefully placed separator characters (tabs, newlines, colons, commas) such that the cumulative pointer advances in next_field() exceed the actual buffer size. For example, supplying a 5-byte buffer '\t\n\n\0\x03' with length=2 causes the first next_field call to parse tab+newline as one field, advancing p to offset 2. The caller (archive_acl_from_text_nl) loops while 'text != NULL && length > 0 && *text != \0' (line 4405), calling next_field repeatedly. Each call decrements length and advances text. After the first iteration, length becomes 0, so the outer loop exits. However, if the attacker supplies a longer input or multiple short fields, the cumulative effect is that *p can be incremented beyond the safe buffer region (because the function trusts *l as a gate, but doesn't verify *p - text < original_safe_length). Once *p points past the buffer, subsequent dereferences (**p in lines 4733, 4738, 4743, 4747, 4749) trigger out-of-bounds reads, leaking memory or causing crashes. The violation at line 4743 ('*p + 1l') indicates pointer arithmetic producing an invalid address, which can be reached by an attacker controlling the ACL text input in a tar/zip/cpio archive processed by libarchive.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_rounding_mode = 0
  _end_off = 4u
  _l_val = 2ul
  _p_backing = <array: 5 elements>
  _p_backing[0l] = '\t'
  _p_backing[1l] = '\n'
  _p_backing[2l] = '\n'
  _p_backing[3l] = 0
  _p_backing[4l] = 0
  _p_cursor = {'name': 'unknown'}
  _p_nul_at = 4u
  _sep_buf = <array: 5 elements>
  _sep_buf[0l] = 0
  _sep_buf[1l] = 0
  _sep_buf[2l] = 0
  _sep_buf[3l] = 0
  _sep_buf[4l] = 0
  _sep_len = 0u
  _shared_buf_0 = <array: 5 elements>
  _shared_buf_0[0l] = 0
  _shared_buf_0[1l] = 0
  _shared_buf_0[2l] = 0
  _shared_buf_0[3l] = 0
  _shared_buf_0[4l] = 3
  _start_off = 4u
  byte_extract_little_endian(_shared_buf_0, (signed long int)__CPROVER_POINTER_OFFSET(start), char *) = {'name': 'unknown'}
  end = {'name': 'unknown'}
  l = _l_val!0@1
  p = _p_cursor!0@1
  sep = _sep_buf!0@1
  start = {'name': 'unknown'}
```

## Function source (from the snapshot)

```c
			const char *start, *end;
			next_field(&text, &length, &start, &end, &sep);
			if (fields < numfields) {
				field[fields].start = start;
				field[fields].end = end;
			}
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/next_field.pointer_arithmetic.11.json`
- `bug_reports/unnamed_1779685659342.json`

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
    next_field/harness.c
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
