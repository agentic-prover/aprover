# bmc-agent-sec confirmed finding: `archive_be32enc`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:32:54.888535+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_read_support_format_7zip.c`
- **Function**: `archive_be32enc` ((unknown lines))
- **Violated property**: `archive_be32enc.pointer_dereference.1` (CBMC-reported)
- **Call chain established**: `archive_be64enc -> archive_be32enc`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `medium` |
| Dynamic harness (GCC + signal handlers) | **inconclusive**, signal=`None` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation is a NULL pointer dereference in `archive_be32enc` at line 3762, called from `archive_be64enc` at line 3772. The call chain shows `archive_be64enc(NULL, u)` is invoked with a NULL pointer.

Looking at the caller context, `archive_be64enc` casts `pp` to `unsigned char *p` and then calls `archive_be32enc(p, ...)` twice (lines 3772-3773). If `pp` is NULL, then `p` is NULL, and the first call `archive_be32enc(p, ...)` will dereference NULL at line 3762 when attempting `p[0] = ...`.

The function signature shows `void archive_be32enc(void* pp, uint32_t u)` with no precondition requiring `pp` to be non-NULL. Similarly, `archive_be64enc(void *pp, uint64_t u)` has no NULL check.

These are utility functions for encoding integers in big-endian format into a buffer. They are part of the archive library's internal encoding helpers (lines 3751-3797). Looking at the full source context, these functions are static inline helpers defined in what appears to be `archive_endian.h` or similar.

The key question: can an attacker cause these functions to be called with NULL?

Searching the provided source for actual call sites of `archive_be64enc` or `archive_be32enc` shows they are used throughout the 7-zip format reader. For example, line 3772 shows `archive_be32enc(p, (uint32_t)(u >> 32));` inside `archive_be64enc`.

The attacker scenario: The 7-zip format reader allocates buffers for reading/writing archive metadata. If memory allocation fails (OOM), or if a buffer pointer is not properly initialized, a NULL pointer could be passed to these encoding functions. In a security context, an attacker could:

1. Craft a malformed 7-zip archive that triggers unusual code paths
2. Cause memory allocation to fail at a critical point (e.g., via resource exhaustion)
3. Trigger a code path where a buffer pointer remains NULL but is still passed to encoding functions

Given that this is a bounded-model-checker finding, CBMC likely explored a path where `pp=NULL` reaches `archive_be64enc`. While the SPECIFIC witness (u=0, exact symbolic values) may be a CBMC artifact, the NULL pointer dereference TYPE is reachable if any code path fails to validate the buffer pointer before encoding.

The lack of NULL checks in these utility functions means ANY caller that doesn't validate its pointer will crash. This is a classic missing input validation bug.

## Exploit scenario (LLM-supplied)

An attacker crafts a malformed 7-zip archive with manipulated metadata that causes the archive reader to attempt encoding data into an uninitialized or failed-allocation buffer pointer. When the reader calls archive_be64enc (or archive_be32enc) with this NULL pointer, the program crashes with a NULL dereference. Alternatively, the attacker could trigger this via resource exhaustion: by opening many archives simultaneously or providing archives with extreme size values, they cause memory allocation for encoding buffers to fail, leaving NULL pointers that are later dereferenced. The vulnerability requires only that some code path in the 7-zip reader calls these encoding functions without first checking for NULL, which is plausible given the lack of precondition guards in the functions themselves.

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

- `bug_reports/archive_be32enc.pointer_dereference.1.json`
- `bug_reports/unnamed_1779689539957.json`

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
    archive_be32enc/harness.c
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
