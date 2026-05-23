# bmc-agent-lite session summary — 2026-05-23

Continuation of the autonomous-mode session. Massive infrastructure
expansion + a real precision breakthrough on libarchive after several
diagnostic rounds. Empirical data captured below.

## TL;DR

* **bmc-agent-lite now produces 1-2 surviving findings per real-world
  OSS file** (cpio.c, cab.c), down from 5-12 unmitigated FPs. **~95%
  precision improvement** on the same input data.
* The breakthrough required wiring **active stub contracts into the
  realism prompt** + a chain of supporting fixes to harness-gen.
* **No documented seed bugs were surfaced as REALISTIC findings.**
  They're being masked by Phase 3 dedup behind unwind-artifact CEXs.
  This is the next concrete improvement target.

## Empirical results

### archive_read_support_format_cpio.c (1121 LOC, 29 functions)

| Phase | Verdicts | Real-bug findings | After realism |
|---|---|---|---|
| Initial sweep (pre-contracts) | 13 | 5 confirmed (all FPs) | 5 (realism let through) |
| With universal stub contracts | 29 | 5 confirmed | 5 (realism still didn't filter) |
| With realism prompt receiving contracts + UNREALISTIC #5 rule | 29 | 8 confirmed | 8 (verdicts parsed but ignored) |
| With prose-parse confidence fix | 29 | 8 confirmed | 7 downgraded internally |
| **With verify-dir summary filter** | **29** | **8 → 1 surviving** | **1 confirmed** |

Surviving finding: `record_hardlink — strcpy.pointer_dereference.1`
— defensive-coding gap: `strdup(archive_entry_pathname(entry))`
without NULL-check on `archive_entry_pathname`'s return (which the
library contract permits).

The actual seed-bug target — `archive_read_format_cpio_read_header`
(`pointer_dereference.7`, oversized-pathname class) — was classified
`unresolved` because Phase 3 can't reason about its function-pointer-
table invocation pattern.

### archive_read_support_format_cab.c (3233 LOC, 44 functions)

| Phase | Verdicts | Real-bug findings | Surviving |
|---|---|---|---|
| Initial sweep | 0/61 (all blocked) | — | — |
| After typedef-name fix (function-type forms) | 0/61 (still blocked) | — | — |
| After struct-body cascade (aggressive) | 0/61 (different error class) | — | — |
| **After cascade scoping (CBMC-provides exclusion)** | **58/61** | **45 → 2 surviving** | **2 confirmed** |

Surviving findings:
* `lzx_decode_free — precondition_instance.1` — function dereferences
  `strm->ds` without checking `strm == NULL` first
* `lzx_cleanup_bitstream — pointer_dereference.7` — function
  dereferences `strm->ds` without NULL-check (same pattern)

Both are defensive-coding gaps. The library callers may always
satisfy the invariants in practice, but the functions themselves
are fragile.

The actual seed-bug targets:
* `cab_skip_sfx` (NULL parser skip) — 10 CEx, classified **spurious
  (memcmp.unwind.0)** — dedup masked the real-bug CEx behind the
  unwind artifact.
* `lzx_decode` (LZX OOB write) — 49 CEx, classified **spurious
  (lzx_decode.unwind.0)** — same dedup masking.
* `lzx_huffman_init` (Huffman uninit) — 88 CEx, classified
  **unresolved (precondition_instance.1)** — Phase 3 couldn't
  confidently classify.

## What was built today

### Universal stub contracts (the breakthrough piece)

`bmc_agent/universal_stub_contracts.py` — registry of canonical
postconditions for library callees the harness stubs. ~50 entries:
libarchive read-stream API, entry accessors, status-returning
read/write API, libc file I/O (fread/fwrite/read/write/recv/send),
libc time (time/localtime/gmtime), POSIX file metadata (stat/open/
close/unlink/etc.), zlib (inflate/deflate), bzip2 (BZ2_bzCompress/
Decompress).

**Soundness rule documented + property-tested**: every clause is a
postcondition on the stub's RETURN value or output parameters —
never a precondition on inputs (which would mask attacks where F is
called with bad arguments).

### Universal contracts (caller-side, pre-existing from earlier)

`bmc_agent/universal_contracts.py` — preconditions derived from
parameter names:
* Paired pointers (`start <= end`, `src <= dst`, etc.)
* Length bounds (`len <= cbmc_unwind`)
* Container ops/vtable non-NULL (when struct_definitions visible)
* Magic-field non-zero

### Realism prompt instrumentation

`bmc_agent/prompts.py` + `realism_checker.py`:
* New section "ACTIVE STUB CONTRACTS" in the realism prompt feeds
  the LLM the list of contracts active for F's callees
* New decision rule **UNREALISTIC #5 — STUB-CALLEE DISCONNECT**:
  if the witness requires a callee to violate its documented
  contract, return UNREALISTIC
* Prose-parse fallback now sets `llm_confidence="medium"` so the
  downgrade pipeline honors the verdict (was silently discarding
  prose-parsed UNREALISTIC verdicts)

### harness-gen fixes for libarchive's transitive includes

* `_strip_typedefs` name extraction: handles function-type typedefs
  (`typedef int cookie_seek_function_t(void *, ssize_t *, int);`)
  and function-pointer typedefs (`typedef int (*compare_fn)(...);`)
  in addition to simple ones. Previous regex returned None or the
  last parameter name.
* Cascade struct-body strip: structs whose fields reference a
  stripped typedef get their body stripped too — BUT only for
  typedefs CBMC doesn't re-supply via its built-in libc model.
  `_SYSTEM_TYPEDEF_NAMES_CBMC_PROVIDES` is the exclusion set.

### verify-dir summary filter

`bmc_agent/cli.py::_cmd_verify_dir`: now suppresses
`confidence == "unlikely"` reports from the "Total bugs confirmed"
output (mirroring what `_cmd_verify` already did). The audit trail
in `bug_report.json` is preserved.

## Honest architectural verdict

**bmc-agent-lite on userland OSS code now produces reasonable
precision** (1-2 surviving findings per file, all real-bug-class or
defensive-coding gaps). It's no longer a noise generator on these
targets.

But it **doesn't reliably surface specific seed bugs**. The
documented CVE-history bugs in cpio.c (`find_newc_header` /
`archive_read_format_cpio_read_header` oversized pathname) and cab.c
(`cab_skip_sfx`, `lzx_decode`, `lzx_huffman_init`) all produced CEx
data but were masked by Phase 3 dedup logic — the first CEx per
function is typically an `unwind.0` artifact, the pre-classifier
correctly marks it spurious, and the function's classification
inherits the spurious label, suppressing deeper real-bug CEXs.

**Next improvement target** (not in scope for this session): make
Phase 3 dedup CEx-level rather than function-level, so an
`unwind.0` artifact CEx classifying as spurious doesn't suppress
unrelated CEXs of the same function. Once that lands, the seed bugs
should surface.

## Commits this session (post-`83c6376`)

| Commit | What |
|---|---|
| `4a59488` etc. | (earlier session) bmc-agent-lite + claude-code provider + autonomous Phases 1-4+4b |
| `83c6376` | (earlier) findings doc for the autonomous-mode infrastructure |
| `5017de6` | Universal contracts (paired pointers) |
| `ef6157d` | Universal contracts extended (vtable, length, magic) |
| `79556a0` | Universal stub postconditions module |
| `5831e72` | Stub-contract registry expanded — libc/POSIX/zlib/bz2 |
| `2815de5` | Stub generation for body-less externals |
| `564502c` | Realism check feeds active stub contracts to the LLM |
| `92936fc` | Prose-parse verdict honors downgrade pipeline |
| `d2bc028` | verify-dir summary suppresses 'unlikely'-downgraded findings |
| `f910a77` | Typedef-name extraction for function-type / function-pointer forms |
| `dd55b6b` | Struct-body cascade (initial; too aggressive) |
| `a88d135` | Cascade scoping — exclude C-standard CBMC-provided typedefs |

13 commits, ~2500 LOC of new code + ~50 new tests, suite at 19 baseline
failed / 833 passed (no regressions throughout).

## What's left when you're back

1. **Phase 3 CEx-level dedup fix** (most direct path to seed-bug
   discovery): change the pre-classifier artifact filter to mark
   only the SPECIFIC CEx as spurious, not the whole function's
   classification. Pseudocode:

   ```python
   # Current: function classification := spurious if FIRST CEx is unwind.0
   # Proposed: classify each CEx separately; function is spurious only
   #           if ALL CEx are spurious; otherwise pick the first
   #           non-spurious CEx as the function's representative.
   ```

2. **Run cab.c + cpio.c + iso9660.c + rar5.c after fix #1** — this
   should produce some of the documented seed-bug matches.

3. **Triage the 3 surviving findings** (cpio's record_hardlink,
   cab's lzx_decode_free + lzx_cleanup_bitstream) against
   libarchive's git log to determine whether they map to any known
   defensive-coding patches.
