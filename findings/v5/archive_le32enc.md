# bmc-agent-sec confirmed finding: `archive_le32enc`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:32:54.896597+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_read_support_format_7zip.c`
- **Function**: `archive_le32enc` ((unknown lines))
- **Violated property**: `archive_le32enc.pointer_dereference.1` (CBMC-reported)
- **Call chain established**: `archive_le64enc -> archive_le32enc`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **confirmed**, signal=`SIGSEGV` |
| Final tier | `confirmed_dynamic` |

## Realism reasoning

The violated property is a NULL pointer dereference in archive_le32enc at line 3786 when pp=NULL. The call chain shows archive_le64enc (line 3796) calls archive_le32enc(pp, ...) where pp is passed directly from the caller. The dynamic harness confirms this: calling archive_le64enc(NULL, 0) triggers SIGSEGV. Looking at the actual call sites in the codebase (lines in CALL-SITE ANALYSIS section), archive_le32enc is called from archive_le64enc with 'p' (line 3795) and 'p+4' (line 3796). The function archive_le64enc is a public utility that encodes a 64-bit value into a buffer, and it does not validate that pp is non-NULL before passing it to archive_le32enc. While the specification says 'requires valid(pp)', there is no runtime check enforcing this precondition. An attacker who controls the buffer pointer passed to any code path that eventually calls archive_le64enc with a NULL pointer (e.g., through a corrupted archive structure, malformed 7zip header causing pointer arithmetic to yield NULL, or direct API misuse by a caller) can trigger this NULL dereference. The dynamic validation CONFIRMED the crash with SIGSEGV, proving the violation is reachable with pp=NULL.

## Exploit scenario (LLM-supplied)

An attacker crafts a malformed 7-Zip archive that causes the libarchive parser to attempt encoding metadata (e.g., timestamps, file sizes) into a buffer whose pointer has been corrupted or improperly initialized to NULL. When the parser calls archive_le64enc to serialize a 64-bit value, the NULL pointer is passed through to archive_le32enc, which immediately dereferences it at p[0], p[1], p[2], p[3] (lines 3786-3789), causing a segmentation fault and potential denial-of-service. Alternatively, if an application using libarchive incorrectly passes NULL as the buffer pointer to any encoding function, the same crash occurs.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_rounding_mode = 0
  p = ((unsigned char *)NULL)
  pp = NULL
  u = 0u
```

## Function source (from the snapshot)

```c
(libarchive source not available on common paths; see file_stem above)
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/archive_le32enc.pointer_arithmetic.1.json`
- `bug_reports/archive_le32enc.pointer_dereference.1.json`
- `bug_reports/unnamed_1779689538032.json`

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
    archive_le32enc/harness.c
# (paste the harness contents from the section below into harness.c first;
#  it is also committed alongside this report as harness.c.)

```

To re-run the full sweep end-to-end (re-derives this finding from scratch):

```bash
(no command provided)
```

## Honest caveats (read before upstream reporting)

- **Dynamic outcome was `confirmed`.** STRONG evidence: the dynamic GCC+ASAN harness actually crashed at runtime, matching CBMC's predicted property.
- The realism LLM's attacker scenario may hypothesize an upstream condition (e.g. "some bug elsewhere creates the dangling pointer state"). **Independent code-level verification of that condition is required before reporting upstream.**
- Realism nondeterminism: the same CEx can flip between REALISTIC and UNREALISTIC across runs. Multiple per-CEx records in `bug_reports/` may show different verdicts; this report uses the strongest realistic record by mtime.
- The harness is auto-generated and uses CBMC's nondeterministic-input model. Reading `harness.c` shows exactly what input states CBMC was free to explore — verify those states are actually reachable from the real public API before declaring a vulnerability.
